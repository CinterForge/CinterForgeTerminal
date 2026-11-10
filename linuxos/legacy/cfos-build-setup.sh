#!/usr/bin/env bash
# Setup: prepare host toolchains, private Colima profile, and guest sources.
# Sourced by ../CFOS. Nothing in this file runs until cfos_setup is called.
cfos_setup() {
  if [[ "$CFOS_PLATFORM" == linux ]]; then
    # First gate: source inventory. Check before any Homebrew/Colima/Docker
    # installation or mutation; only ./CFOS clone all may acquire sources.
    cfos_run python3 -u "$CFOS_ROOT/scripts/fork.py" clone all --dry-run || return 1
    # The headless AMD64 distro builds on Apple Silicon via managed Colima,
    # or on Linux with an existing Docker/Buildx daemon. Neither needs a
    # CinterForge source checkout, an Xcode project or the Steam client.
    if [[ "$(uname -s)" == Darwin ]]; then
      cfos_require_mac
      local formula
      local linux_formulae=(docker docker-buildx colima)
      if (( ! CFOS_DRY_RUN )); then
        [[ -x /opt/homebrew/bin/brew ]] || { echo 'CFOS Linux Setup requires Apple Silicon Homebrew on macOS.' >&2; return 1; }
        export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
      fi
      for formula in "${linux_formulae[@]}"; do
        if (( CFOS_DRY_RUN )) || ! brew list --versions "$formula" >/dev/null 2>&1; then
          cfos_run brew install "$formula"
        fi
      done
    elif [[ "$(uname -s)" == Linux ]]; then
      if (( CFOS_DRY_RUN )); then
        cfos_run docker info
        cfos_run docker buildx version
      else
        command -v docker >/dev/null || { echo 'CFOS Linux Setup requires Docker.' >&2; return 1; }
        docker info >/dev/null || { echo 'CFOS Linux Setup requires an active Docker daemon.' >&2; return 1; }
        docker buildx version >/dev/null || { echo 'CFOS Linux Setup requires Docker Buildx.' >&2; return 1; }
      fi
    else
      echo "CFOS Linux Setup does not support host OS $(uname -s)" >&2
      return 1
    fi
    cfos_linux setup
    return $?
  fi
  cfos_require_mac
  cfos_run python3 -u "$CFOS_ROOT/scripts/fork.py" clone all --dry-run || return 1
  # Ubuntu AMD64 is the universal precondition for the macOS SteamOS build.
  # Do not build Linux implicitly here: its Build → Stage → Test is independent.
  cfos_linux verify || return 1
  # Never install host dependencies through Intel Homebrew under Rosetta.
  # This also makes Setup and Xcode build scripts resolve the same formulae.
  if (( ! CFOS_DRY_RUN )); then
    [[ -x /opt/homebrew/bin/brew ]] || {
      echo 'CFOS: missing native Apple Silicon Homebrew at /opt/homebrew/bin/brew (do not use /usr/local Intel Homebrew).' >&2
      return 1
    }
    export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
    [[ "$(brew --prefix)" == /opt/homebrew ]] || {
      echo "CFOS: wrong Homebrew prefix: $(brew --prefix); expected /opt/homebrew" >&2
      return 1
    }
  fi
  if (( ! CFOS_DRY_RUN )); then
    command -v brew >/dev/null || {
      echo 'CFOS: install Apple Silicon Homebrew first.' >&2
      return 1
    }
  fi

  # Install MoltenVK repro/bench shader tools here, never inside Xcode build
  # scripts. The formulae below include glslang, spirv-tools and vulkan-loader.
  # Keep the dependency list explicit: reviewers can see what we require.
  local formula
  local formulae=(
    dtc xz lld pkgconf libepoxy meson ninja llvm
    spirv-llvm-translator spirv-tools spirv-headers spirv-cross
    vulkan-loader glslang cmake go rustup colima docker docker-buildx qemu
  )
  for formula in "${formulae[@]}"; do
    if (( CFOS_DRY_RUN )) || ! brew list --versions "$formula" >/dev/null 2>&1; then
      cfos_run brew install "$formula"
    fi
  done

  # Provision pinned Python into the external CFOS build tree, never Homebrew
  # or a user-global Python directory. uv itself is installed locally as well.
  local python_home="$CFOS_BUILD_ROOT/CFOS/work/tools/python"
  local uv_home="$CFOS_BUILD_ROOT/CFOS/work/tools/uv"
  local managed_python="$python_home/bin/python3"
  if (( CFOS_DRY_RUN )); then
    cfos_run mkdir -p "$python_home" "$uv_home"
  else
    mkdir -p "$python_home/bin" "$uv_home/bin"
    if [[ ! -x "$uv_home/bin/uv" ]]; then
      command -v curl >/dev/null || { echo 'CFOS: curl required to bootstrap isolated Python' >&2; return 1; }
      echo "CFOS: installing isolated uv into $uv_home"
      local uv_installer="$uv_home/install.sh"
      curl -fsSL https://astral.sh/uv/install.sh -o "$uv_installer" || return 1
      env UV_INSTALL_DIR="$uv_home/bin" UV_NO_MODIFY_PATH=1 sh "$uv_installer" || return 1
    fi
    [[ -x "$uv_home/bin/uv" ]] || { echo 'CFOS: isolated uv installation failed' >&2; return 1; }
    if [[ ! -x "$managed_python" ]] || ! "$managed_python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 11))' 2>/dev/null; then
      echo "CFOS: installing isolated Python 3.11 into $python_home"
      UV_PYTHON_INSTALL_DIR="$python_home/managed" "$uv_home/bin/uv" python install 3.11 || return 1
      local python_found
      python_found="$(UV_PYTHON_INSTALL_DIR="$python_home/managed" "$uv_home/bin/uv" python find --managed-python 3.11)" || return 1
      [[ -x "$python_found" ]] || { echo "CFOS: Python 3.11 unavailable: $python_found" >&2; return 1; }
      ln -sfn "$python_found" "$managed_python"
    fi
    export CFOS_PYTHON="$managed_python"
    export PATH="$python_home/bin:$PATH"
    echo "CFOS: using isolated Python: $CFOS_PYTHON"
  fi

  # Homebrew packages can be installed but their executables hidden from
  # Xcode's sanitized PATH.  Validate the actual shader tools in Setup.
  if (( ! CFOS_DRY_RUN )); then
    local brew_bin tool
    brew_bin="$(brew --prefix)/bin"
    for tool in meson ninja pkg-config glslangValidator spirv-as spirv-link; do
      if [[ ! -x "$brew_bin/$tool" ]] && ! command -v "$tool" >/dev/null 2>&1; then
        echo "CFOS: Setup missing $tool (Homebrew prefix: $(brew --prefix))" >&2
        return 1
      fi
    done
  fi

  # Homebrew's rustup formula provides the rustup executable directly.
  # Do not assume a rustup-init binary exists in the formula keg.
  # Provision the pinned toolchain in Setup, not during Xcode builds.
  if (( ! CFOS_DRY_RUN )); then
    local rustup_bin pinned_cargo pinned_rustc
    rustup_bin="$(brew --prefix)/bin/rustup"
    [[ -x "$rustup_bin" ]] || {
      echo "CFOS: rustup executable missing: $rustup_bin" >&2
      return 1
    }
    export PATH="$(brew --prefix)/bin:$HOME/.cargo/bin:$PATH"
    "$rustup_bin" toolchain install 1.90.0 --profile minimal || return 1
    "$rustup_bin" default 1.90.0 || return 1
    pinned_cargo="$("$rustup_bin" which --toolchain 1.90.0 cargo)" || return 1
    pinned_rustc="$("$rustup_bin" which --toolchain 1.90.0 rustc)" || return 1
    [[ -x "$pinned_cargo" && -x "$pinned_rustc" ]] || {
      echo 'CFOS: pinned Rust 1.90.0 cargo/rustc binaries unavailable' >&2
      return 1
    }
    export PATH="$(dirname "$pinned_cargo"):$PATH"
  else
    cfos_run rustup toolchain install 1.90.0 --profile minimal
  fi

  # Validate binaries, not just Homebrew package metadata.
  if (( ! CFOS_DRY_RUN )); then
    local tool
    for tool in git make xcodebuild xcrun cargo go python3 docker colima qemu-img; do
      command -v "$tool" >/dev/null || {
        echo "CFOS: missing required tool: $tool" >&2
        return 1
      }
    done
  fi

  # Setup owns the reproducible managed Colima environment. The profile is
  # disposable build infrastructure, not user data. Never migrate it via
  # symlinks or reuse a half-configured instance after a failed startup.
  source "$CFOS_ROOT/scripts/colima-runtime.sh"
  cfos_colima_environment || return 1
  local colima_home="$COLIMA_HOME"
  local colima_profile="$CFOS_COLIMA_PROFILE"
  local docker_socket="$COLIMA_HOME/$colima_profile/docker.sock"
  local mount_root="$CFOS_WORKSPACE"
  local mount_work="$CFOS_BUILD_ROOT/CFOS/work"

  # The one shared Builds directory is a sibling of this checkout.
  # Colima must mount the workspace containing BOTH source repositories and Builds.
  # A running VM with stale mounts cannot be repaired by calling `colima start`.
  cfos_colima_healthy() {
    docker info >/dev/null 2>&1 || return 1
    local probe="CFOS-SetupMountProbe-$$"
    mkdir -p "$mount_work"
    rm -f "$mount_work/$probe"
    if ! docker run --rm \
      --mount "type=bind,source=$CFOS_ROOT,target=/cfos-source,readonly" \
      --mount "type=bind,source=$mount_work,target=/cfos-work" \
      --entrypoint /bin/sh alpine:3.20 -ec \
      "test -f /cfos-source/scripts/build-image.sh && touch /cfos-work/$probe"; then
      echo "CFOS: Docker mount probe failed: source=$CFOS_ROOT builds=$mount_work" >&2
      return 1
    fi
    if [[ ! -f "$mount_work/$probe" ]]; then
      echo "CFOS: Docker did not write to Builds: $mount_work/$probe" >&2
      return 1
    fi
    rm -f "$mount_work/$probe"
  }

  # macOS guest-image builds need more RAM than headless Linux tests. Set it
  # before creating a profile and verify live memory even when already running.
  local build_memory_gib
  build_memory_gib="$(cfos_colima_build_memory_gib)" || return 1
  if (( CFOS_DRY_RUN )); then
    cfos_run mkdir -p "$colima_home" "$mount_work" "$COLIMA_CACHE_HOME" "$DOCKER_CONFIG"
    cfos_run colima start --profile "$colima_profile" --arch aarch64 --vm-type vz --disk 96 --memory "$build_memory_gib" --activate=false --mount "$mount_root:w"
  else
    mkdir -p "$colima_home" "$mount_work" "$COLIMA_CACHE_HOME" "$DOCKER_CONFIG"
    [[ ! -L "$colima_home" && ! -L "$LIMA_HOME" ]] || {
      echo "CFOS: Colima state must not be symlinked: $colima_home" >&2
      return 1
    }
    # First activate the existing profile without modifying its configuration.
    # A failed boot is NOT evidence of a bad disk or a bad mount.
    if ! docker info >/dev/null 2>&1; then
      if [[ -d "$LIMA_HOME/colima-$colima_profile" ]]; then
        cfos_colima_start || return 1
      elif ! cfos_colima_check_host_memory "$build_memory_gib" ||
           ! cfos_colima_start --arch aarch64 --vm-type vz --disk 96 --memory "$build_memory_gib" --mount "$mount_root:w"; then
        echo "CFOS: managed VM failed to start; see $LIMA_HOME/colima-$colima_profile/ha.stderr.log" >&2
        return 1
      fi
    fi
    if ! cfos_colima_healthy; then
      echo "CFOS: managed profile has incorrect mounts; recreating only $colima_profile for workspace $mount_root" >&2
      # Colima owns the profile and its Lima disk lifecycle. Do not manually
      # remove Lima internals or introduce symlinks to a previous workspace.
      colima stop --profile "$colima_profile" || return 1
      colima delete --profile "$colima_profile" --force || return 1
      cfos_colima_check_host_memory "$build_memory_gib" || return 1
      cfos_colima_start --arch aarch64 --vm-type vz --disk 96 --memory "$build_memory_gib" --mount "$mount_root:w" || return 1
      cfos_colima_healthy || {
        echo "CFOS: fresh managed VM cannot mount source=$CFOS_ROOT and Builds=$mount_work" >&2
        return 1
      }
    fi
    cfos_colima_ensure_disk || return 1
    cfos_colima_ensure_memory || return 1
    cfos_colima_healthy || return 1
    echo "CFOS: managed Colima ready: $docker_socket"
  fi

  cfos_run bash "$CFOS_ROOT/guest/fex/build.sh" fetch
  cfos_run bash "$CFOS_ROOT/guest/fex/runtime.sh" fetch
  echo 'CFOS: Setup prepared source prerequisites; the build phase compiles the application.'
}
