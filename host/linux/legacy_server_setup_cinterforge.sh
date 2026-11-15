#!/usr/bin/env bash
set -euo pipefail

# Setup is a consumer/download path. Never allow setup or tester machines to publish local artifacts.
export CINTERFORGE_SETUP_DISABLE_UPLOADS=1
unset CINTERFORGE_CLOUDFLARE_UPLOADS_ALLOWED || true

# Maintainer setup must be unattended once invoked through Cinter. Homebrew's
# installer honors NONINTERACTIVE, while brew itself honors the cleanup/env
# controls below. Password prompts required by macOS itself (for example sudo)
# cannot be suppressed safely and remain the only allowed interactive prompt.
export NONINTERACTIVE=1
export CI=1
export HOMEBREW_NO_INSTALL_CLEANUP=1
export HOMEBREW_NO_ENV_HINTS=1

# CinterForge developer bootstrap (macOS/Linux).
#
# This script lives under:
#   CinterForgeServer/cf/build_cinterforge/setup_cinterforge.sh
#
# It bootstraps the standalone CinterForge repo only.  Product-only work such as
# registry publishing, USD/USDZ Cloudflare sync, and internal content staging is
# intentionally owned by the parent Cinter wrapper.
#
# The virtual environment is stored outside source at Builds/cinterenv.
# Positional arguments are not interpreted as alternate venv names.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../../.." && pwd -P)"
SERVER_DIR="${REPO_ROOT}/CinterForgeServer"
# Public Steam Pack staging; requires no Steam credentials for local preparation.
PACK_STAGER="${SCRIPT_DIR}/orchestration/stage_steam_packs.py"
# Generated outputs live beside Cinter, never inside the CinterForge source tree.
BUILDS_ROOT="$(cd -- "${REPO_ROOT}/.." && pwd -P)/Builds"
VENV_NAME="cinterenv"
# Virtual environments are ABI/platform-specific.  The repository is shared
# between the native host and the canonical Linux guest, so Linux must never
# mistake the host's Builds/cinterenv directory for a usable Linux venv.
if [[ "$(uname -s)" == "Linux" ]]; then
  VENV_DIR="${CINTERFORGE_VENV_DIR:-${BUILDS_ROOT}/Linux/${VENV_NAME}}"
else
  VENV_DIR="${CINTERFORGE_VENV_DIR:-${BUILDS_ROOT}/${VENV_NAME}}"
fi
LEGACY_VENV_DIR="${SERVER_DIR}/${VENV_NAME}"
PYTHON_VERSION="${CINTERFORGE_PYTHON_VERSION:-3.11.0}"

SKIP_MODELS=0
REPAIR_ONLY=0
PREFLIGHT_ONLY=0
LINUX_CUDA=0
RUNTIME_ONLY=0
USE_SYSTEM_PYTHON=0
SKIP_HUGO=0
GENERATE_BUILD_ASSETS=0
GENERATE_DLCS=0
LINUX_UPDATE_DATA=0
UPDATE_DATA=0
UPDATE_REGISTRY=0
UPDATE_RAGS=0
UPDATE_CINTER=0
UPDATE_ASSETS=0
REPACK_ALL_ASSETS=0
AUTO_ASSET_REFRESH=0
ASSET_DIRTY_CHECK=0
FORCE_ASSET_REFRESH=0
ASSET_INPUT_STATE_FILE="${BUILDS_ROOT}/Intermediates/Dirty/Assets/current_inputs.json"
ASSET_APPLIED_STATE_FILE="${BUILDS_ROOT}/Intermediates/Dirty/Assets/applied_inputs.json"
RAG_DIRTY_SELECTION_FILE=""
ASSET_DIRTY_SELECTION_FILE=""
UPDATE_IMAGE_SIGNATURES=0
UPDATE_CARD_IMAGES=0
BLOCKBENCH_OVERRIDE=0
CODEOSS_OVERRIDE=0
ZMACHINE_BACKEND="javascript"
EXPECT_ZMACHINE_BACKEND=0
BUILD_RPG=0
UPLOAD_PACKS=0
SKIP_PACK_SYNC=0
PUSH_ASSETS=0
PULL_ASSETS=0
TEMPLATE_ASSETS_ADDED=0
TEMPLATE_ASSETS_UPDATED=0
TEMPLATE_ASSETS_SKIPPED=0
BUILDS_REPORT_PATH="${BUILDS_ROOT}/BUILDS_REPORT.txt"

repair_builds_preflight() {
  # Builds contains independent product/tool trees owned by different producers.
  # Setup must never infer that one existing tree is a legacy alias of another
  # and move/delete generated or maintainer content automatically.
  mkdir -p "${BUILDS_ROOT}"
  echo "[repair] Builds layout: automatic relocation disabled; preserving legacy and canonical producer-owned trees in place."
}

generate_builds_report() {
  python3 "${SCRIPT_DIR}/orchestration/builds_inventory.py" --repo-root "${REPO_ROOT}"
}

# Every setup/update mode begins by repairing the existing generated layout.
# Builds inventory is intentionally NOT an EXIT trap: routine setup/update modes
# must not recursively scan the entire Builds tree. Preflight requests it explicitly.
repair_builds_preflight

# Beta 1 owner cutover. The former global producer flags must not
# generate partial side effects through direct internal setup invocation.
for legacy_arg in "$@"; do
  case "$legacy_arg" in
    --update-data|--update_data|--update-assets|--update_assets|--update-rags|--update_rags|--update-registry|--update_registry|--update-cinter|--update_cinter)
      echo "[setup] RETIRED: $legacy_arg (use owning lifecycle instead)" >&2
      exit 64 ;;
  esac
done

for arg in "$@"; do
  if [[ "$EXPECT_ZMACHINE_BACKEND" == "1" ]]; then
    case "$arg" in
      bocfel|javascript|both) ZMACHINE_BACKEND="$arg" ;;
      *)
        echo "[setup] ERROR: --zmachine-backend must be one of: bocfel, javascript, both (got: $arg)" >&2
        exit 2
        ;;
    esac
    EXPECT_ZMACHINE_BACKEND=0
    continue
  fi
  case "$arg" in
    repair|--repair|--repair-only)
      REPAIR_ONLY=1
      ;;
    preflight|--preflight)
      PREFLIGHT_ONLY=1
      ;;
    --linux-cuda|--cuda)
      LINUX_CUDA=1
      ;;
    --runtime-only)
      RUNTIME_ONLY=1
      ;;
    --skip-model-sync|--skip-models)
      SKIP_MODELS=1
      ;;
    --skip-hugo|--skip-site)
      SKIP_HUGO=1
      ;;
    --update-assets|--update_assets)
      UPDATE_ASSETS=1
      GENERATE_BUILD_ASSETS=1
      ASSET_DIRTY_CHECK=1
      ;;
    --repack-all-assets)
      REPACK_ALL_ASSETS=1
      UPDATE_ASSETS=1
      GENERATE_BUILD_ASSETS=1
      ASSET_DIRTY_CHECK=1
      ;;
    --generate-assets|--generate-build-assets)
      GENERATE_BUILD_ASSETS=1
      ;;
    --force-assets)
      GENERATE_BUILD_ASSETS=1
      FORCE_ASSET_REFRESH=1
      ;;
    --generate-dlcs|--generate-dlc|--update-dlcs|--update-dlc|--update_dlcs|--update_dlc)
      GENERATE_DLCS=1
      ;;
    --linux)
      LINUX_UPDATE_DATA=1
      ;;
    --update-data|--update_data)
      # Composite maintainer command: refresh Registry + DLC/model inputs, then
      # rebuild only assets whose watched inputs changed.
      UPDATE_DATA=1
      UPDATE_REGISTRY=1
      UPDATE_RAGS=1
      GENERATE_BUILD_ASSETS=1
      ASSET_DIRTY_CHECK=1
      ;;
    --update-registry|--update_registry|registry|--registry)
      UPDATE_REGISTRY=1
      ;;
    --update-rags|--update_rags|--update-rag|--update_rag)
      UPDATE_RAGS=1
      ;;
    --update-cinter|--update_cinter)
      UPDATE_CINTER=1
      UPDATE_RAGS=1
      GENERATE_BUILD_ASSETS=1
      ;;
    --update-image-signatures|--update_image_signatures)
      UPDATE_IMAGE_SIGNATURES=1
      ;;
    --update-card-images|--update_card_images)
      UPDATE_CARD_IMAGES=1
      UPDATE_IMAGE_SIGNATURES=1
      ;;
    --blockbench-override|--skip-blockbench)
      BLOCKBENCH_OVERRIDE=1
      ;;
    --codeoss-override|--skip-codeoss)
      CODEOSS_OVERRIDE=1
      ;;
    --zmachine-backend)
      EXPECT_ZMACHINE_BACKEND=1
      ;;
    --zmachine-backend=*)
      ZMACHINE_BACKEND="${arg#*=}"
      case "$ZMACHINE_BACKEND" in
        bocfel|javascript|both) ;;
        *) echo "[setup] ERROR: --zmachine-backend must be one of: bocfel, javascript, both (got: $ZMACHINE_BACKEND)" >&2; exit 2 ;;
      esac
      ;;
    --build-rpg|--update-rpg)
      BUILD_RPG=1
      ;;
    --upload-packs|--publish-packs)
      UPLOAD_PACKS=1
      ;;
    --skip-pack-sync|--skip-packs-download)
      SKIP_PACK_SYNC=1
      ;;
    --push-assets)
      PUSH_ASSETS=1
      ;;
    --pull-assets)
      PULL_ASSETS=1
      ;;
    no-publish|--no-publish|--force-registry|--skip-registry)
      # Cinter-only product flags.  They may arrive here from older wrappers or
      # manual commands, but they must never change the venv name.
      ;;
    setup)
      # Harmless when this script is invoked through ./Cinter setup.
      ;;
    *)
      echo "[setup] ERROR: unsupported setup argument: $arg" >&2
      echo "[setup] Refusing to continue because an unknown flag could accidentally run plain setup." >&2
      exit 2
      ;;
  esac
done
if [[ "$EXPECT_ZMACHINE_BACKEND" == "1" ]]; then
  echo "[setup] ERROR: --zmachine-backend requires one of: bocfel, javascript, both" >&2
  exit 2
fi

if [[ "$PUSH_ASSETS" == "1" && "$PULL_ASSETS" == "1" ]]; then
  echo "[setup] ERROR: --push-assets and --pull-assets are mutually exclusive" >&2
  exit 2
fi

if [[ "$PUSH_ASSETS" == "1" || "$PULL_ASSETS" == "1" ]]; then
  CONFIG_PATH="${CINTERFORGE_CONFIG_JSON:-}"
  if [[ -z "$CONFIG_PATH" ]]; then
    if [[ -f "${HOME}/dev/config.json" ]]; then
      CONFIG_PATH="${HOME}/dev/config.json"
    elif [[ -f "${HOME}/cfdev/config.json" ]]; then
      CONFIG_PATH="${HOME}/cfdev/config.json"
    fi
  fi
  if [[ -z "$CONFIG_PATH" || ! -f "$CONFIG_PATH" ]]; then
    echo "[setup] ERROR: product config not found; set CINTERFORGE_CONFIG_JSON or install ~/dev/config.json / ~/cfdev/config.json" >&2
    exit 2
  fi

  if [[ "$PUSH_ASSETS" == "1" ]]; then
    # Push is the one explicit setup-family operation allowed to publish. The
    # local Assets tree is a working set/cache: publish new/changed files but
    # never infer a remote deletion from a locally absent asset.
    unset CINTERFORGE_SETUP_DISABLE_UPLOADS || true
    export CINTERFORGE_CLOUDFLARE_UPLOADS_ALLOWED=1
    echo "[setup] Push Assets: local working set -> Cloudflare (add/update only; remote-only assets preserved)."
    exec python3 "${SCRIPT_DIR}/orchestration/product_asset_sync.py" publish-assets --repo-root "${REPO_ROOT}" --config "$CONFIG_PATH" --allow-upload
  fi

  # Pull is the inverse explicit operation: fully stage the Cloudflare snapshot
  # first, then replace the local Assets tree. It never uploads.
  echo "[setup] Pull Assets: Cloudflare -> local Builds/Assets (Cloudflare authoritative)."
  exec python3 "${SCRIPT_DIR}/assets/shared_asset_sync.py" pull --repo-root "${REPO_ROOT}" --config "$CONFIG_PATH"
fi

if [[ "$PREFLIGHT_ONLY" == "1" ]]; then
  generate_builds_report
  trap - EXIT
  python3 "${SCRIPT_DIR}/orchestration/preflight_handoff.py" --repo-root "${REPO_ROOT}" --report-already-generated
  echo "[setup] Preflight complete. Upload ${BUILDS_ROOT}/CinterForgePreflight.zip for sandbox testing."
  exit 0
fi

if [[ "$REPAIR_ONLY" == "1" ]]; then
  echo "[repair] Repair-only mode complete."
  exit 0
fi


if [[ "${CINTERFORGE_GENERATE_BUILD_ASSETS:-}" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]]; then
  GENERATE_BUILD_ASSETS=1
fi
if [[ "${CINTERFORGE_GENERATE_DLCS:-}" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]]; then
  GENERATE_DLCS=1
fi

# Maintenance stages are intentionally separate:
#   --update-registry refreshes Registry data and stops.
#   --update-dlcs refreshes DLC/model data and stops.
#   --update-assets consumes local Registry/DLC/RAG/template inputs, rebuilds dirty
#     templates/Packs, and stages the final Packs locally without Cloudflare.
#   --update-data prepares local producer outputs; publishing is separate.
if [[ "$UPDATE_DATA" == "1" ]]; then
  # --update-data produces local inputs only. Cloudflare uploads belong exclusively
  # to an explicit publish operation, never a maintenance/build operation.
  AUTO_ASSET_REFRESH=1
  echo "[setup] Full data maintenance: Registry + DLC inputs + dirty asset rebuild."
elif [[ "$UPDATE_REGISTRY" == "1" && "$GENERATE_BUILD_ASSETS" != "1" ]]; then
  echo "[setup] Registry-only mode will update Registry data and stop."
elif [[ "$GENERATE_DLCS" == "1" && "$GENERATE_BUILD_ASSETS" != "1" ]]; then
  echo "[setup] DLC-only mode will update DLC/model data and stop."
elif [[ "$GENERATE_BUILD_ASSETS" == "1" ]]; then
  echo "[setup] Asset-only mode will rebuild only templates/Packs with changed watched inputs."
fi

echo "[setup] Repo root: ${REPO_ROOT}"
echo "[setup] Server dir: ${SERVER_DIR}"
echo "[setup] Venv name: ${VENV_NAME}"
echo "[setup] Required Python version: ${PYTHON_VERSION}"


ensure_openrpgtools_subtree() {
  local subtree_prefix="ThirdParty/OpenRPGTools/3D-Action-RPG-JavaScript"
  local subtree_dir="${REPO_ROOT}/${subtree_prefix}"
  local upstream_url="${CINTERFORGE_OPENRPGTOOLS_URL:-https://github.com/hdevx/3D-Action-RPG-JavaScript.git}"
  local upstream_branch="${CINTERFORGE_OPENRPGTOOLS_BRANCH:-main}"
  local remote_name="openrpgtools"

  # A complete vendored subtree is already usable and requires no Git metadata.
  # Git is required only when setup must fetch/materialize a missing subtree.
  if [[ -f "$subtree_dir/index.html" && -d "$subtree_dir/src" ]]; then
    echo "[setup] OpenRPGTools subtree ready: $subtree_prefix"
    return 0
  fi
  if [[ -e "$subtree_dir" ]]; then
    echo "[setup] ERROR: OpenRPGTools target exists but is incomplete: $subtree_dir" >&2
    exit 1
  fi

  command -v git >/dev/null 2>&1 || { echo "[setup] ERROR: Git is required to prepare OpenRPGTools." >&2; exit 1; }
  git -C "$REPO_ROOT" rev-parse --show-toplevel >/dev/null 2>&1 || {
    echo "[setup] ERROR: OpenRPGTools subtree setup requires a Git checkout when the subtree is absent: $REPO_ROOT" >&2
    exit 1
  }

  local existing_url=""
  existing_url="$(git -C "$REPO_ROOT" remote get-url "$remote_name" 2>/dev/null || true)"
  if [[ -z "$existing_url" ]]; then
    git -C "$REPO_ROOT" remote add "$remote_name" "$upstream_url"
    echo "[setup] OpenRPGTools upstream remote added: $remote_name -> $upstream_url"
  elif [[ "$existing_url" != "$upstream_url" ]]; then
    git -C "$REPO_ROOT" remote set-url "$remote_name" "$upstream_url"
    echo "[setup] OpenRPGTools upstream remote updated: $remote_name -> $upstream_url"
  fi

  # Setup must be safe to run from a patched/dirty developer checkout.  A real
  # `git subtree add` creates a commit and refuses dirty tracked files, which made
  # first-run setup unable to materialize the RPG exactly when a developer had
  # just applied the CinterForge patch.  Materialize the upstream tree as normal
  # files here; once committed with CinterForge, git-subtree pull/split works on
  # the prefix without any nested repository or .gitmodules state.
  echo "[setup] Materializing OpenRPGTools source tree (subtree files, no submodule)."
  git -C "$REPO_ROOT" fetch "$remote_name" "$upstream_branch"
  local archive_path="${BUILDS_ROOT}/InteractiveFiction/MaintainerTools/OpenRPGTools/openrpgtools.tar"
  mkdir -p "$(dirname "$archive_path")" "$subtree_dir"
  git -C "$REPO_ROOT" archive --format=tar --output="$archive_path" FETCH_HEAD
  tar -xf "$archive_path" -C "$subtree_dir"
  rm -f "$archive_path"
  [[ -f "$subtree_dir/index.html" && -d "$subtree_dir/src" ]] || {
    echo "[setup] ERROR: OpenRPGTools source materialization did not produce the expected project." >&2
    exit 1
  }
  echo "[setup] OpenRPGTools subtree installed: $subtree_prefix"
}

if [[ "$BUILD_RPG" == "1" ]]; then
  echo "[setup] RPG maintenance explicitly enabled (--build-rpg)."
  ensure_openrpgtools_subtree
else
  echo "[setup] RPG maintenance skipped (default); preserving existing RPG source/assets. Use --build-rpg to enable it."
fi

ensure_fbx2gltf_runtime() {
  local tool_dir="${BUILDS_ROOT}/InteractiveFiction/MaintainerTools/FBX2glTF"
  local tool="${tool_dir}/FBX2glTF"
  local version="v0.9.7"
  local asset=""
  case "$(uname -s)" in
    Darwin) asset="FBX2glTF-darwin-x64" ;;
    Linux) asset="FBX2glTF-linux-x64" ;;
    *) return 0 ;;
  esac
  if [[ -x "$tool" ]] && "$tool" --version >/dev/null 2>&1; then
    echo "[setup] FBX2glTF runtime ready: $tool"
    return 0
  fi
  command -v curl >/dev/null 2>&1 || { echo "[setup] ERROR: curl is required to prepare FBX2glTF." >&2; exit 1; }
  mkdir -p "$tool_dir"
  echo "[setup] Installing FBX2glTF ${version} for Mixamo preview/RPG animation assets."
  curl -fL --retry 3 --retry-delay 2 -o "${tool}.part" \
    "https://github.com/facebookincubator/FBX2glTF/releases/download/${version}/${asset}"
  mv "${tool}.part" "$tool"
  chmod +x "$tool"
  if ! "$tool" --version >/dev/null 2>&1; then
    echo "[setup] ERROR: FBX2glTF was downloaded but cannot execute on this machine: $tool" >&2
    echo "[setup] On Apple Silicon the upstream v0.9.7 binary is x86_64 and requires macOS Rosetta." >&2
    exit 1
  fi
  echo "[setup] FBX2glTF runtime ready: $tool"
}

if [[ "$LINUX_UPDATE_DATA" == "1" ]]; then
  echo "[setup] Linux update-data acceptance slice: skipping FBX2glTF bootstrap."
else
  ensure_fbx2gltf_runtime
fi

# Keep all generated native build products outside the source directories.
mkdir -p "${BUILDS_ROOT}"
LEGACY_DERIVED_DATA="${REPO_ROOT}/../CinterSteam/DerivedData"
TARGET_DERIVED_DATA="${BUILDS_ROOT}/DerivedData"
LEGACY_WIN64="${REPO_ROOT}/../CinterSteam/win64"
TARGET_WIN64="${BUILDS_ROOT}/win64"
if [[ -d "$LEGACY_DERIVED_DATA" && ! -e "$TARGET_DERIVED_DATA" ]]; then
  echo "[setup] Moving legacy Xcode DerivedData: $LEGACY_DERIVED_DATA -> $TARGET_DERIVED_DATA"
  mv "$LEGACY_DERIVED_DATA" "$TARGET_DERIVED_DATA"
fi
if [[ -d "$LEGACY_WIN64" && ! -e "$TARGET_WIN64" ]]; then
  echo "[setup] Moving legacy Windows output: $LEGACY_WIN64 -> $TARGET_WIN64"
  mv "$LEGACY_WIN64" "$TARGET_WIN64"
fi

if [[ ! -d "$VENV_DIR" && -d "$LEGACY_VENV_DIR" ]]; then
  mkdir -p "${BUILDS_ROOT}"
  echo "[setup] Moving legacy venv out of source: ${LEGACY_VENV_DIR} -> ${VENV_DIR}"
  mv "$LEGACY_VENV_DIR" "$VENV_DIR"
fi

command_exists() { command -v "$1" >/dev/null 2>&1; }

linux_install_packages() {
  # Keep Linux setup as unattended as the distro permits.  CinterForge only
  # asks the native package manager for named runtime/build dependencies; it
  # never adds third-party repositories or downloads arbitrary binaries.
  # Prefer sudo for normal developer accounts, but run the package manager
  # directly when setup itself is already root (CI/container images).
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "[setup] ERROR: linux_install_packages called on non-Linux host." >&2
    return 1
  fi

  local -a elevate=()
  if [[ "$(id -u)" -ne 0 ]]; then
    if ! command_exists sudo; then
      echo "[setup] ERROR: Linux package installation requires root or sudo." >&2
      return 1
    fi
    elevate=(sudo)
  fi

  if command_exists apt-get; then
    echo "[setup] Installing Linux packages with apt: $*"
    "${elevate[@]}" env DEBIAN_FRONTEND=noninteractive apt-get update
    "${elevate[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
    return 0
  fi
  if command_exists dnf; then
    echo "[setup] Installing Linux packages with dnf: $*"
    "${elevate[@]}" dnf install -y "$@"
    return 0
  fi
  if command_exists yum; then
    echo "[setup] Installing Linux packages with yum: $*"
    "${elevate[@]}" yum install -y "$@"
    return 0
  fi
  if command_exists pacman; then
    echo "[setup] Installing Linux packages with pacman: $*"
    "${elevate[@]}" pacman -Sy --noconfirm --needed "$@"
    return 0
  fi
  if command_exists zypper; then
    echo "[setup] Installing Linux packages with zypper: $*"
    "${elevate[@]}" zypper --non-interactive install "$@"
    return 0
  fi

  if command_exists apk; then
    echo "[setup] Installing Linux packages with apk: $*"
    "${elevate[@]}" apk add --no-cache "$@"
    return 0
  fi

  echo "[setup] ERROR: Unsupported Linux package manager; install these packages manually: $*" >&2
  return 1
}

brew_install_unattended() {
  # Homebrew's NONINTERACTIVE/CI mode is the authoritative unattended path.
  # Do not pipe `yes` into brew: modern formula installs do not require it and
  # the pipe floods setup logs with stray `y` lines.
  #
  # A machine can also be in Homebrew's benign-but-blocking state where the
  # exact dependency being poured is already linked from another keg.  Brew
  # reports that as, for example:
  #   Error: Cannot link fontconfig
  #   Another version is already linked: .../Cellar/fontconfig/<version>
  # That should not make Cinter setup fail.  Capture the failed install output,
  # repair only the formula Brew explicitly named, and retry the requested
  # install once.  We deliberately do not use --overwrite: unlink/link keeps
  # Homebrew authoritative over its own symlinks and leaves unrelated formulae
  # untouched.
  local install_log
  install_log="$(mktemp -t cinterforge-brew-install.XXXXXX)"
  if env HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ENV_HINTS=1 NONINTERACTIVE=1 CI=1 brew install "$@" 2>&1 | tee "$install_log"; then
    rm -f "$install_log"
    return 0
  fi

  local link_formula
  link_formula="$(sed -n 's/^Error: Cannot link \([^[:space:]]\+\).*$/\1/p' "$install_log" | tail -n 1)"
  if [[ -n "$link_formula" ]] && grep -Fq "Another version is already linked:" "$install_log"; then
    echo "[setup] Repairing Homebrew linked-keg conflict for ${link_formula} and retrying install..."
    brew unlink "$link_formula" >/dev/null 2>&1 || true
    if ! brew link "$link_formula"; then
      echo "[setup] ERROR: Homebrew could not relink ${link_formula}; refusing to force/overwrite links." >&2
      rm -f "$install_log"
      return 1
    fi
    rm -f "$install_log"
    env HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ENV_HINTS=1 NONINTERACTIVE=1 CI=1 brew install "$@"
    return $?
  fi

  rm -f "$install_log"
  return 1
}

prepare_homebrew_for_unattended_setup() {
  if ! command_exists brew; then
    return 0
  fi

  # Homebrew 6+ requires explicit trust for third-party taps before formulae
  # from them may be loaded. Dart is already used elsewhere in the maintainer
  # toolchain on some developer machines; when that tap is present, trust the
  # specific formula up front so unrelated brew installs/cleanup do not stop
  # Cinter setup midway through with an interactive remediation request.
  if brew tap 2>/dev/null | grep -Fxq "dart-lang/dart"; then
    echo "[setup] Trusting existing Homebrew Dart formula for unattended setup..."
    brew trust --formula dart-lang/dart/dart >/dev/null
  fi
}

if [[ ! -d "$SERVER_DIR" ]]; then
  echo "[setup] ERROR: CinterForgeServer not found: $SERVER_DIR" >&2
  exit 1
fi

# The Linux --update-data profile is a portable maintainer acceptance path for
# IFPlayer's browser tooling. It deliberately avoids unrelated native/product
# bootstraps (pyenv, FBX2glTF, Registry, RAG, DLC, artwork) so CI/sandboxes can
# prove Code - OSS + the browser bridge with only Python, Git, Node acquisition,
# and Chromium. The normal macOS/Windows product update-data path is unchanged.
if [[ "$UPDATE_DATA" == "1" && "$LINUX_UPDATE_DATA" == "1" ]]; then
  command_exists python3 || { echo "[setup] ERROR: Linux update-data acceptance requires python3." >&2; exit 1; }
  export PYTHONPATH="${SERVER_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
  export CINTERFORGE_UPDATE_DATA_PIPELINE=1
  linux_pipeline="${SCRIPT_DIR}/orchestration/update_data_pipeline.py"
  linux_args=(--repo-root "$REPO_ROOT" --skip-acquisition --skip-registry --skip-rags --linux)
  if [[ "$BLOCKBENCH_OVERRIDE" == "1" ]]; then
    linux_args+=(--blockbench-override)
  fi
  if [[ "$CODEOSS_OVERRIDE" == "1" ]]; then
    linux_args+=(--codeoss-override)
  fi
  echo "[setup] Running portable Linux IFPlayer --update-data acceptance."
  python3 -u "$linux_pipeline" "${linux_args[@]}"
  echo "[setup] Linux update-data acceptance completed."
  exit 0
fi

# Install Homebrew only on macOS when it is missing.  Linux contributors are
# expected to install pyenv/python using their distro tooling.
if [[ "$(uname -s)" == "Darwin" ]] && ! command_exists brew; then
  echo "[setup] Homebrew not found. Installing non-interactively..."
  NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

if [[ -x "/opt/homebrew/bin/brew" ]]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x "/usr/local/bin/brew" ]]; then
  eval "$(/usr/local/bin/brew shellenv)"
fi

prepare_homebrew_for_unattended_setup

# Docker CLI plugins are not always discovered automatically when Homebrew
# installs them. Provision buildx only when Docker is present and buildx is
# actually unavailable; never reinstall an already working plugin.
if [[ "$(uname -s)" == "Darwin" ]] && command_exists docker; then
  if ! docker buildx version >/dev/null 2>&1; then
    if ! brew list --versions docker-buildx >/dev/null 2>&1 || \
       [[ -z "$(brew list --versions docker-buildx 2>/dev/null)" ]]; then
      echo "[setup] Installing missing Docker buildx CLI plugin..."
      brew_install_unattended docker-buildx
    else
      echo "[setup] Docker buildx is installed; configuring Docker CLI discovery..."
    fi
    buildx_plugin="$(brew --prefix docker-buildx)/lib/docker/cli-plugins/docker-buildx"
    if [[ ! -x "$buildx_plugin" ]]; then
      echo "[setup] ERROR: Homebrew docker-buildx plugin missing: $buildx_plugin" >&2
      exit 1
    fi
    mkdir -p "$HOME/.docker/cli-plugins"
    plugin_link="$HOME/.docker/cli-plugins/docker-buildx"
    if [[ -e "$plugin_link" && ! -L "$plugin_link" ]]; then
      echo "[setup] ERROR: Existing Docker buildx plugin is not a symlink: $plugin_link" >&2
      exit 1
    fi
    if [[ -L "$plugin_link" ]]; then
      current_plugin="$(readlink "$plugin_link")"
      if [[ "$current_plugin" != "$buildx_plugin" ]]; then
        echo "[setup] ERROR: Existing Docker buildx symlink points elsewhere: $current_plugin" >&2
        exit 1
      fi
    else
      ln -s "$buildx_plugin" "$plugin_link"
    fi
    docker buildx version || { echo "[setup] ERROR: Docker buildx is still unavailable" >&2; exit 1; }
  else
    echo "[setup] Docker buildx already available; skipping Homebrew installation."
  fi
fi

if [[ "$RUNTIME_ONLY" == "1" ]]; then
  USE_SYSTEM_PYTHON=1
  if ! command_exists python3; then
    [[ "$(uname -s)" == "Linux" ]] || { echo "[setup] ERROR: runtime-only setup requires python3." >&2; exit 1; }
    linux_install_packages python3 python3-venv
  fi
  echo "[setup] Runtime-only profile: using system Python $(python3 --version 2>&1)."
fi

if [[ "$USE_SYSTEM_PYTHON" != "1" ]] && ! command_exists pyenv; then
  if command_exists brew; then
    echo "[setup] Installing pyenv via brew..."
    brew update >/dev/null
    brew_install_unattended pyenv
  elif [[ "$(uname -s)" == "Linux" ]]; then
    # A clean canonical Linux guest is part of the supported setup path.  It
    # must bootstrap its own exact Python toolchain rather than requiring a
    # human to preinstall pyenv.  Install the normal CPython build prerequisites
    # with the distro package manager, then install upstream pyenv in $HOME.
    echo "[setup] Installing pyenv + CPython build prerequisites on Linux..."
    if command_exists apt-get; then
      linux_install_packages git make build-essential curl ca-certificates \
        libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
        libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
        libffi-dev liblzma-dev
    elif command_exists dnf; then
      linux_install_packages git make gcc patch zlib-devel bzip2 bzip2-devel \
        readline-devel sqlite sqlite-devel openssl-devel tk-devel libffi-devel \
        xz-devel libuuid-devel
    elif command_exists yum; then
      linux_install_packages git make gcc patch zlib-devel bzip2 bzip2-devel \
        readline-devel sqlite sqlite-devel openssl-devel tk-devel libffi-devel \
        xz-devel libuuid-devel
    elif command_exists pacman; then
      linux_install_packages git base-devel openssl zlib xz tk libffi
    elif command_exists zypper; then
      linux_install_packages git make gcc zlib-devel libbz2-devel readline-devel \
        sqlite3-devel libopenssl-devel tk-devel libffi-devel xz-devel
    elif command_exists apk; then
      linux_install_packages git build-base bash openssl-dev zlib-dev bzip2-dev \
        readline-dev sqlite-dev xz-dev tk-dev libffi-dev
    else
      echo "[setup] ERROR: unsupported Linux package manager for pyenv bootstrap." >&2
      exit 1
    fi
    export PYENV_ROOT="${HOME}/.pyenv"
    if [[ ! -x "${PYENV_ROOT}/bin/pyenv" ]]; then
      git clone --depth 1 https://github.com/pyenv/pyenv.git "${PYENV_ROOT}"
    fi
    export PATH="${PYENV_ROOT}/bin:${PATH}"
  else
    echo "[setup] ERROR: pyenv not found on unsupported host: $(uname -s)." >&2
    exit 1
  fi
else
  echo "[setup] pyenv already installed."
fi

if [[ "$USE_SYSTEM_PYTHON" != "1" ]] && ! command_exists pyenv; then
  echo "[setup] ERROR: pyenv bootstrap completed but pyenv is still unavailable." >&2
  exit 1
fi

ensure_pyenv_init() {
  local rc="$1"
  local marker="# >>> pyenv (cinterforge) >>>"

  touch "$rc"
  if grep -Fqs "$marker" "$rc"; then
    return 0
  fi

  cat >> "$rc" <<'EOF'

# >>> pyenv (cinterforge) >>>
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
if command -v pyenv >/dev/null 2>&1; then
  eval "$(pyenv init -)"
fi
# <<< pyenv (cinterforge) <<<
EOF

  echo "[setup] Added pyenv init to: $rc"
}

if [[ "$USE_SYSTEM_PYTHON" != "1" ]]; then
  ensure_pyenv_init "${HOME}/.zshrc"
  ensure_pyenv_init "${HOME}/.bashrc"
  ensure_pyenv_init "${HOME}/.bash_profile"

  export PYENV_ROOT="${HOME}/.pyenv"
  export PATH="${PYENV_ROOT}/bin:${PATH}"
  eval "$(pyenv init -)"

  echo "[setup] Installing Python ${PYTHON_VERSION} (if needed)..."
  pyenv install -s "${PYTHON_VERSION}"

  echo "[setup] Setting pyenv local to ${PYTHON_VERSION} in CinterForge repo + CinterForgeServer..."
  ( cd "${REPO_ROOT}" && pyenv local "${PYTHON_VERSION}" )
  ( cd "${SERVER_DIR}" && pyenv local "${PYTHON_VERSION}" )
fi


# Retired global helper ensure_generative_models_manifest; owner: CinterForgePlugins/pipeline/setup/models/legacy_native_model_functions.sh


# Retired global helper ensure_raw_template_assets; owner: CinterForgePlugins/pipeline/setup/legacy_source_functions.sh


# Retired global helper hydrate_template_output_from_pack_payloads; owner: CinterForgeSteam/tools/legacy_consumer_pack_functions.sh


# Retired global helper cloudflare_sync_packs; owner: CinterForgePlugins/pipeline/stage/legacy_pack_functions.sh



# Retired global helper update_cinterforge_dlcs; owner: CinterForgePlugins/pipeline/build/legacy_asset_functions.sh

# Retired global helper retrieve_published_asset_inputs_for_update; owner: CinterForgePlugins/pipeline/setup/legacy_source_functions.sh

# Retired global helper build_assistant_catalog; owner: CinterForgePlugins/pipeline/setup/legacy_source_functions.sh


# Retired global helper ensure_interactive_fiction_maintainer_compilers; owner: CinterForgeWebApp/tools/web_lifecycle/ifplayer/legacy_if_functions.sh

if_runtime_platform_key() {
  local system="$(uname -s)"
  local machine="$(uname -m)"
  case "$system" in
    Darwin) system="macos" ;;
    Linux) system="linux" ;;
  esac
  case "$machine" in
    aarch64|arm64) machine="arm64" ;;
    amd64|x86_64) machine="x86_64" ;;
  esac
  printf '%s-%s\n' "$system" "$machine"
}

validate_interactive_fiction_runtime_bundle() {
  local root="$1"
  local platform_key="$(if_runtime_platform_key)"
  local manifest="$root/manifest.json"
  local dot="$root/bin/dot"
  local neato="$root/bin/neato"
  [[ -f "$manifest" && -x "$dot" && -x "$neato" ]] || return 1
  grep -Fq '"schema": "cinterforge.if.runtime-tools.v1"' "$manifest" || return 1
  grep -Fq "\"platform\": \"${platform_key}\"" "$manifest" || return 1


  local old_dyld="${DYLD_LIBRARY_PATH:-}"
  local old_gvbindir="${GVBINDIR:-}"
  export DYLD_LIBRARY_PATH="$root/lib${old_dyld:+:$old_dyld}"
  export GVBINDIR="$root/lib/graphviz"
  if ! printf 'digraph G { a -> b }\n' | "$dot" -Tplain >/dev/null 2>&1; then
    export DYLD_LIBRARY_PATH="$old_dyld"; export GVBINDIR="$old_gvbindir"
    return 1
  fi
  if ! printf 'graph G { a -- b }\n' | "$neato" -Tplain >/dev/null 2>&1; then
    export DYLD_LIBRARY_PATH="$old_dyld"; export GVBINDIR="$old_gvbindir"
    return 1
  fi
  export DYLD_LIBRARY_PATH="$old_dyld"
  export GVBINDIR="$old_gvbindir"
  return 0
}

activate_interactive_fiction_runtime_bundle() {
  local root="$1"
  export CINTERFORGE_IF_RUNTIME_ROOT="$root"
  export CINTERFORGE_GRAPHVIZ_DOT="$root/bin/dot"
  export CINTERFORGE_GRAPHVIZ_NEATO="$root/bin/neato"
  cat > "$root/setup_report.json" <<EOF
{
  "schema": "cinterforge.if.runtime-tools.setup-report.v1",
  "status": "ready",
  "platform": "$(if_runtime_platform_key)",
  "runtime_root": "Tools/InteractiveFiction",
  "dot": "Tools/InteractiveFiction/bin/dot",
  "neato": "Tools/InteractiveFiction/bin/neato"
}
EOF
  echo "[setup] Interactive Fiction product runtime ready: Tools/InteractiveFiction"
  echo "[setup] Interactive Fiction tools: bin/dot, bin/neato; Z-machine runtime=Bocfel WASM"
}

install_interactive_fiction_runtime_archive() {
  local archive="$1"
  local runtime_root="$2"
  local tool_parent="$(dirname "$runtime_root")"
  local staging=""
  staging="$(mktemp -d "${TMPDIR:-/tmp}/cinterforge-if-runtime.XXXXXX")"
  trap 'rm -rf "$staging"' RETURN
  tar -xzf "$archive" -C "$staging"
  local extracted="$staging/InteractiveFiction"
  if ! validate_interactive_fiction_runtime_bundle "$extracted"; then
    echo "[setup] ERROR: Interactive Fiction runtime archive failed validation: $archive" >&2
    return 1
  fi
  mkdir -p "$tool_parent"
  rm -rf "$runtime_root"
  mv "$extracted" "$runtime_root"
  rm -rf "$staging"
  trap - RETURN
}


if_distribution_provider() {
  printf '%s' "${CINTERFORGE_IF_PUBLISH_PROVIDER:-cloudflare_r2_usdz}"
}

if_distribution_config() {
  local requested="${CINTERFORGE_CONFIG_JSON:-${CINTERFORGE_CLOUDFLARE_CONFIG:-}}"
  if [[ -n "$requested" && -f "$requested" ]]; then
    printf '%s' "$requested"
    return 0
  fi
  if [[ -f "$HOME/cfdev/config.json" ]]; then
    printf '%s' "$HOME/cfdev/config.json"
    return 0
  fi
  if [[ -f "$HOME/dev/config.json" ]]; then
    printf '%s' "$HOME/dev/config.json"
    return 0
  fi
  return 1
}

if_distribution_public_base_url() {
  # Internal maintainer configs may carry the public custom-domain URL for the
  # same R2 provider used by publish.  Reading that value keeps publish and
  # setup on one bucket without baking credentials into source.
  local config="${1:-}"
  local provider="${2:-$(if_distribution_provider)}"
  [[ -n "$config" && -f "$config" && -x "$VENV_PYTHON" ]] || return 1
  "$VENV_PYTHON" - "$config" "$provider" <<'PY_IF_PUBLIC_URL'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
provider_name = sys.argv[2]
data = json.loads(path.read_text(encoding='utf-8'))
providers = data.get('providers') if isinstance(data, dict) else None
provider = providers.get(provider_name) if isinstance(providers, dict) else None
if not isinstance(provider, dict) and isinstance(data, dict):
    provider = data.get(provider_name)
if not isinstance(provider, dict):
    raise SystemExit(1)
merged = dict(provider)
for key in ('env', 'environment'):
    nested = provider.get(key)
    if isinstance(nested, dict):
        merged.update(nested)
for key in (
    'public_base_url', 'publicBaseUrl',
    'CINTERFORGE_CLOUDFLARE_R2_USDZ_PUBLIC_BASE_URL',
    'CINTERFORGE_CLOUDFLARE_R2_DEV_PUBLIC_BASE_URL',
    'CINTERFORGE_CLOUDFLARE_R2_JSON_PUBLIC_BASE_URL',
    'CINTERFORGE_CLOUDFLARE_R2_PUBLIC_BASE_URL',
):
    value = merged.get(key)
    if value is not None and str(value).strip():
        print(str(value).strip().rstrip('/'))
        raise SystemExit(0)
raise SystemExit(1)
PY_IF_PUBLIC_URL
}

if_download_distribution_archive_from_r2() {
  # Maintainer/developer setup may use authenticated R2 access when the bucket's
  # public custom domain is not configured locally yet.  Consumer machines do
  # not need this path: release packaging carries the validated product bundle.
  local prefix="$1"
  local archive_name="$2"
  local destination="$3"
  local config=""
  config="$(if_distribution_config 2>/dev/null || true)"
  [[ -n "$config" ]] || return 1
  local sync_tool="${SCRIPT_DIR}/publishing/cloudflare_r2_sync.py"
  [[ -f "$sync_tool" && -x "$VENV_PYTHON" ]] || return 1
  local provider="$(if_distribution_provider)"
  local staging=""
  staging="$(mktemp -d "${TMPDIR:-/tmp}/cinterforge-if-r2-download.XXXXXX")"
  if ! "$VENV_PYTHON" "$sync_tool" download-sync \
      --config "$config" \
      --provider "$provider" \
      --prefix "${prefix#/}" \
      --local "$staging" \
      --extension .gz \
      --no-delete; then
    rm -rf "$staging"
    return 1
  fi
  if [[ ! -f "$staging/$archive_name" ]]; then
    rm -rf "$staging"
    return 1
  fi
  mkdir -p "$(dirname "$destination")"
  mv "$staging/$archive_name" "$destination"
  rm -rf "$staging"
  return 0
}

# Retired global helper ensure_interactive_fiction_toolchain; owner: CinterForgeWebApp/tools/web_lifecycle/ifplayer/legacy_if_functions.sh


# Retired global helper ensure_interactive_fiction_story_packs; owner: CinterForgeWebApp/tools/web_lifecycle/ifplayer/legacy_if_functions.sh

# Retired global helper ensure_interactive_fiction_product_content; owner: CinterForgeWebApp/tools/web_lifecycle/ifplayer/legacy_if_functions.sh

sync_shared_assets() {
  # Plain setup is the consumer for the complete published product surface.
  # product_asset_sync setup-sync updates only missing/out-of-date content across
  # Registry, USDZ, Examples, source/generated Templates, template RAGs, Pack
  # payload cards, and published product runtime inputs. Collaborative IF/game Assets are app-managed and excluded.
  local sync_script="${SCRIPT_DIR}/orchestration/product_asset_sync.py"
  local config_path="${CINTERFORGE_CONFIG_JSON:-$HOME/dev/config.json}"
  if [[ ! -f "$config_path" && -f "$HOME/cfdev/config.json" ]]; then
    config_path="$HOME/cfdev/config.json"
  fi
  if [[ ! -f "$sync_script" ]]; then
    echo "[setup] ERROR: published product sync tool is missing: $sync_script" >&2
    return 1
  fi
  if [[ ! -f "$config_path" ]]; then
    echo "[setup] WARNING: published product download skipped because product config is unavailable: $config_path" >&2
    return 0
  fi
  echo "[setup] Updating all published product inputs that are missing or out of date; collaborative IF/game Assets remain app-managed."
  PYTHONPATH="${SERVER_DIR}:${REPO_ROOT}:${PYTHONPATH:-}" \
    "$VENV_PYTHON" -u "$sync_script" setup-sync --repo-root "$REPO_ROOT" --config "$config_path"
}


# Retired composite update_cinterforge_data: replaced by individual owner phases.

# Retired global helper evaluate_asset_input_dirty; owner: CinterForgePlugins/pipeline/build/legacy_asset_functions.sh

# Retired global helper record_applied_asset_state; owner: CinterForgePlugins/pipeline/build/legacy_asset_functions.sh


# Retired global helper refresh_internal_cinter_card; owner: CinterForgePlugins/pipeline/build/legacy_asset_functions.sh

# Retired global helper prepare_cinterforge_pack_assets; owner: CinterForgePlugins/pipeline/build/legacy_asset_functions.sh


# Retired global helper stage_cinterforge_plugin_layout; owner: CinterForgeSteam/tools/legacy_consumer_pack_functions.sh

if [[ "$USE_SYSTEM_PYTHON" == "1" ]]; then
  SETUP_PYTHON="$(command -v python3)"
  echo "[setup] Verifying runtime system Python..."
  "$SETUP_PYTHON" --version
else
  echo "[setup] Verifying python version via pyenv..."
  pyenv exec python --version
  SETUP_PYTHON="$(pyenv which python)"
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[setup] Creating venv: ${VENV_DIR}"
  mkdir -p "${BUILDS_ROOT}"
  "$SETUP_PYTHON" -m venv "${VENV_DIR}"
else
  echo "[setup] venv already exists: ${VENV_DIR}"
fi

VENV_PYTHON="${VENV_DIR}/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "[setup] ERROR: venv python not found: $VENV_PYTHON" >&2
  exit 1
fi

"$VENV_PYTHON" --version
echo "[setup] Venv python: ${VENV_PYTHON}"

# Interactive Fiction runtime prerequisites are staged before the large application
# requirements set so a slow/failed optional Python wheel cannot prevent IF maintenance.
# Minecraft/Java/container provisioning is intentionally not part of normal setup.
ensure_interactive_fiction_toolchain
ensure_interactive_fiction_maintainer_compilers
if [[ "$UPDATE_DATA" != "1" ]]; then
  ensure_interactive_fiction_product_content 0
fi
if [[ "$RUNTIME_ONLY" == "1" ]]; then
  ensure_interactive_fiction_product_content
  echo "[setup] Runtime-only setup complete."
  exit 0
fi

REQ="${SERVER_DIR}/requirements.txt"
REQ_INSTALL="$REQ"
REQ_TEMP=""
if [[ "$(uname -s)" == "Linux" && "$LINUX_CUDA" == "1" ]]; then
  case "$(uname -m)" in
    x86_64|amd64) ;;
    *)
      echo "[setup] ERROR: --linux-cuda requires Linux x86-64 (intended for NVIDIA CUDA / WSL2); found $(uname -m)." >&2
      exit 1
      ;;
  esac
  REQ_TEMP="$(mktemp -t cinterforge-requirements.XXXXXX)"
  trap '[[ -n "${REQ_TEMP:-}" ]] && rm -f "$REQ_TEMP"' EXIT
  sed \
    -e 's/torch==2.4.1; platform_system == "Linux"/torch==2.4.1+cu121; platform_system == "Linux"/' \
    -e 's/torchvision==0.19.1; platform_system == "Linux"/torchvision==0.19.1+cu121; platform_system == "Linux"/' \
    -e 's/torchaudio==2.4.1; platform_system == "Linux"/torchaudio==2.4.1+cu121; platform_system == "Linux"/' \
    "$REQ" > "$REQ_TEMP"
  REQ_INSTALL="$REQ_TEMP"
  echo "[setup] Linux accelerator profile: NVIDIA CUDA 12.1"
else
  [[ "$(uname -s)" != "Linux" ]] || echo "[setup] Linux accelerator profile: portable CPU"
fi
DEPENDENCY_MARKER="${VENV_DIR}/.cinterforge_requirements.sha256"
DEPENDENCY_FINGERPRINT=""
if [[ -f "$REQ" ]]; then
  if command_exists shasum; then
    DEPENDENCY_FINGERPRINT="$({ printf 'python=%s\nlinux_cuda=%s\n' "$PYTHON_VERSION" "$LINUX_CUDA"; cat "$REQ_INSTALL"; } | shasum -a 256 | awk '{print $1}')"
  elif command_exists sha256sum; then
    DEPENDENCY_FINGERPRINT="$({ printf 'python=%s\nlinux_cuda=%s\n' "$PYTHON_VERSION" "$LINUX_CUDA"; cat "$REQ_INSTALL"; } | sha256sum | awk '{print $1}')"
  else
    echo "[setup] ERROR: SHA-256 utility not found (need shasum or sha256sum)." >&2
    exit 1
  fi
fi
DEPENDENCY_IMPORTS_CURRENT=1
for required_import in objaverse huggingface_hub cairosvg PIL; do
  if ! "$VENV_PYTHON" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$required_import') is not None else 1)" >/dev/null 2>&1; then
    echo "[setup] Required runtime Python module missing despite dependency marker: ${required_import}; reinstalling requirements."
    DEPENDENCY_IMPORTS_CURRENT=0
    break
  fi
done
if [[ -n "$DEPENDENCY_FINGERPRINT" && -f "$DEPENDENCY_MARKER" && "$(cat "$DEPENDENCY_MARKER")" == "$DEPENDENCY_FINGERPRINT" && "$DEPENDENCY_IMPORTS_CURRENT" == "1" ]]; then
  echo "[setup] Python dependencies are current; skipping pip install."
else
  echo "[setup] Upgrading pip/setuptools in venv..."
  "$VENV_PYTHON" -m pip install --disable-pip-version-check --upgrade pip setuptools --prefer-binary --upgrade-strategy only-if-needed
  if [[ -f "$REQ" ]]; then
    echo "[setup] Installing CinterForgeServer requirements.txt..."
    "$VENV_PYTHON" -m pip install --disable-pip-version-check --progress-bar on -r "$REQ_INSTALL"
    printf '%s\n' "$DEPENDENCY_FINGERPRINT" > "$DEPENDENCY_MARKER"
  else
    echo "[setup] WARNING: requirements.txt not found at $REQ; skipping."
  fi
fi


# Retired global helper ensure_objaverse_index; owner: CinterForgeConverter/build/data/legacy_index_functions.sh

# Retired global helper ensure_pd12m_metadata_checkout; owner: CinterForgeConverter/build/data/legacy_index_functions.sh

# Retired global helper ensure_mixamo_checkout; owner: CinterForgeConverter/build/data/legacy_index_functions.sh

# Retired global helper ensure_ifplayer_asset_indexes; owner: CinterForgeConverter/build/data/legacy_index_functions.sh

if [[ "$SKIP_HUGO" == "1" ]]; then
  echo "[setup] Hugo site build skipped."
elif [[ -f "${SCRIPT_DIR}/build_site.sh" ]]; then
  bash "${SCRIPT_DIR}/build_site.sh" || echo "[setup] WARNING: Hugo site build failed."
else
  echo "[setup] Hugo site build skipped."
fi

if [[ "$UPDATE_DATA" == "1" ]]; then
  echo "[setup] Maintainer update-data: building/reusing finished Objaverse, PD12M, and Mixamo search databases for publication."
  ensure_objaverse_index
  ensure_ifplayer_asset_indexes
  echo "[setup] Media-provider search indexes and Data Sources catalogs are owned by update_data_pipeline.py; Creative Suite setup/build/stage is separate."
elif [[ "$UPDATE_ASSETS" == "1" ]]; then
  echo "[setup] Asset-only mode: Objaverse, PD12M, and Mixamo index maintenance skipped."
else
  echo "[setup] Consumer setup: Objaverse, PD12M, and Mixamo search databases are published product artifacts; local reconstruction skipped."
fi
if [[ "$UPDATE_DATA" == "0" && "$UPDATE_REGISTRY" == "0" && "$UPDATE_RAGS" == "0" && "$UPDATE_CINTER" == "0" && "$GENERATE_BUILD_ASSETS" == "0" && "$GENERATE_DLCS" == "0" ]]; then
  sync_shared_assets
else
  echo "[setup] Shared Builds/Assets consumer download skipped during maintainer update; existing local Assets are preserved."
fi

retrieve_published_asset_inputs_for_update
# The Starter Pack is an output of the maintainer data pipeline.  Do not require
# that output to be complete before --update-data gets a chance to build it.
# The strict product validation/package gate remains immediately after the
# producer so an incomplete maintenance run still fails loudly.
echo "[setup] === UPDATE-DATA STAGE: IF + Registry + RAG producers ==="
update_cinterforge_data
# Validate the newly produced product only. Distribution archives/deltas are
# created by Publish, never by --update-data.
echo "[setup] === UPDATE-DATA STAGE: IF product validation ==="
ensure_interactive_fiction_product_content 1
echo "[setup] === UPDATE-DATA STAGE: Assistant validation/staging ==="
build_assistant_catalog
refresh_internal_cinter_card
echo "[setup] === MODEL CATALOG: prepare Cinter Pleias Q4 only ==="
if [[ "$UPDATE_DATA" == "1" ]]; then
  "$VENV_PYTHON" -u "${REPO_ROOT}/CinterForgeOS/build/creativeSuite/shared/model_pipeline.py" prepare --builds "${BUILDS_ROOT}" || exit 1
fi
echo "[setup] === UPDATE-DATA STAGE: explicit legacy DLC dirty evaluation ==="
update_cinterforge_dlcs
echo "[setup] === UPDATE-DATA STAGE: generated asset dirty evaluation ==="
evaluate_asset_input_dirty

# Data-producing maintenance commands stop here. They deliberately do not
# rebuild templates, Packs, MOD cards, stage runtime caches, or download
# published assets. --update-assets is the only command that consumes these
# outputs and performs the dirty generated-asset rebuild.
if [[ "$UPDATE_REGISTRY" == "1" && "$UPDATE_DATA" != "1" && "$UPDATE_RAGS" != "1" && "$GENERATE_DLCS" != "1" && "$GENERATE_BUILD_ASSETS" != "1" ]]; then
  echo "[setup] Registry-only mode complete; Registry outputs are ready for a later --update-assets run."
  echo "[setup] Done."
  exit 0
elif [[ "$GENERATE_DLCS" == "1" && "$UPDATE_DATA" != "1" && "$GENERATE_BUILD_ASSETS" != "1" ]]; then
  echo "[setup] DLC-only mode complete; DLC/model outputs are ready for a later --update-assets run."
  echo "[setup] Done."
  exit 0
elif [[ "$UPDATE_CINTER" == "1" ]]; then
  echo "[setup] Cinter-only mode complete; forced Cinter RAG and CONV-CINTER card are current."
  echo "[setup] Done."
  exit 0
elif [[ "$UPDATE_RAGS" == "1" && "$UPDATE_DATA" != "1" && "$GENERATE_BUILD_ASSETS" != "1" ]]; then
  echo "[setup] RAG-only mode complete; RAG outputs are ready for a later --update-assets run."
  echo "[setup] Done."
  exit 0
fi

echo "[setup] === UPDATE-DATA STAGE: template/MOD/Pack asset generation ==="
prepare_cinterforge_pack_assets
# An up-to-date asset build still needs to stage its existing final Packs.
# Dirty-input evaluation may have disabled regeneration; it must never disable
# installation into the shared Steam DLC directory.
if [[ "$UPDATE_ASSETS" == "1" && "$GENERATE_BUILD_ASSETS" != "1" ]]; then
  echo "[setup] Assets unchanged; preparing and staging existing final Packs into Steam."
  "$VENV_PYTHON" "${PACK_STAGER}" prepare --builds-root "$BUILDS_ROOT"
  "$VENV_PYTHON" "${SCRIPT_DIR}/assets/verify_update_assets.py" \
    --builds-root "$BUILDS_ROOT" --repo-root "$REPO_ROOT" --staged-only
  "$VENV_PYTHON" "${PACK_STAGER}" install --builds-root "$BUILDS_ROOT"
fi


# Maintainer asset-producing commands end after their requested generation.
# Runtime cache staging and published Pack/MOD downloads belong only to plain
# setup, where the machine is consuming already-published products.
if [[ "$UPDATE_DATA" == "1" || "$ASSET_DIRTY_CHECK" == "1" ]]; then
  if [[ "$UPDATE_DATA" == "1" ]]; then
    echo "[setup] Full data maintenance complete; stopping before runtime staging and consumer downloads."
  else
    echo "[setup] Asset maintenance complete; stopping before runtime staging and consumer downloads."
  fi
  echo "[setup] Done."
  exit 0
fi

stage_cinterforge_plugin_layout

  ensure_generative_models_manifest "setup"

  if [[ "$SKIP_MODELS" == "1" ]]; then
    echo "[setup] Model sync/generation skipped."
  fi

  if [[ "$GENERATE_BUILD_ASSETS" == "1" ]]; then
    echo "[setup] Published Pack/MOD card sync skipped because build asset generation is enabled; using freshly staged local Pack archives and DLC/Models payload cards."
  else
    echo "[setup] Published Pack/MOD card sync enabled because build asset generation is off."
    cloudflare_sync_packs "download-sync"
    "$VENV_PYTHON" "${PACK_STAGER}" install --builds-root "$BUILDS_ROOT"
  fi

  # Plain setup is a consumer path: after Pack payloads are staged/downloaded,
  # materialize their embedded template cards into the build-time template
  # output surface used by automation and developer tooling.
  hydrate_template_output_from_pack_payloads

echo ""
if [[ "$TEMPLATE_ASSETS_ADDED" -gt 0 || "$TEMPLATE_ASSETS_UPDATED" -gt 0 ]]; then
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "[setup] WARNING: setup rebuilt the installable Pack baseline in Builds/SteamPipe."
  echo "[setup] WARNING: launch should unpack Templates from staged Pack payload cards."
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo ""
fi
echo "[setup] Done."
echo "To activate this venv in the current shell:"
echo "  source '${VENV_DIR}/bin/activate'"
echo "Then run:"
echo "  ./Cinter run"
