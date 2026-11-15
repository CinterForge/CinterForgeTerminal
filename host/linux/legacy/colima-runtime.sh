#!/usr/bin/env bash
# Shared CFOS Docker configuration and reboot-safe runtime activation.
# Setup provisions the profile; Build and Xcode activate it on demand.
cfos_colima_environment() {
  local root="${CFOS_ROOT:-${STEAMAC_SOURCE_ROOT:-}}"
  [[ -n "$root" ]] || { echo 'CFOS: source root not set' >&2; return 1; }
  root="$(cd "$root" && pwd -P)" || return 1
  export CFOS_SHARED_BUILDS="$(dirname "$root")/Builds"
  # This Python source belongs to CFOS, not CinterForge. The same resolver is
  # used by the Ubuntu backend, the macOS Xcode build and Setup. Shell-quote
  # values so workspaces containing spaces do not break dispatch.
  local exports
  exports="$("${CFOS_PYTHON:-python3}" "$root/build/linux/managed_backend.py" --shell-env "$root")" || return 1
  eval "$exports"
}

# The SteamOS guest-layer Docker builder is substantially larger than the
# headless Ubuntu smoke environment. Keep its memory policy with CFOS, not in
# Xcode phases or the generic Linux backend. Allow builders with different
# capacities to override this explicitly, but fail before killing a working
# Colima instance when the host cannot support the requested allocation.
cfos_colima_build_memory_gib() {
  local target="${CFOS_MAC_BUILD_MEMORY_GIB:-32}"
  [[ "$target" =~ ^[1-9][0-9]*$ ]] && (( target >= 8 && target <= 256 )) || {
    echo "CFOS: CFOS_MAC_BUILD_MEMORY_GIB must be an integer between 8 and 256 GiB (got '$target')" >&2
    return 1
  }
  printf '%s\n' "$target"
}

cfos_colima_check_host_memory() {
  local target="$1" bytes reserve_gib=4
  [[ "$(uname -s)" == Darwin ]] || return 0
  bytes="$(sysctl -n hw.memsize)" || return 1
  [[ "$bytes" =~ ^[0-9]+$ ]] || { echo "CFOS: unreadable macOS physical RAM: $bytes" >&2; return 1; }
  if (( bytes < (target + reserve_gib) * 1024 * 1024 * 1024 )); then
    echo "CFOS: macOS has $((bytes / 1024 / 1024 / 1024)) GiB physical RAM; a ${target} GiB Colima builder needs at least ${reserve_gib} GiB headroom for macOS/Xcode." >&2
    echo 'CFOS: leaving the existing VM and Docker images untouched. Set CFOS_MAC_BUILD_MEMORY_GIB to a smaller value only if that builder size is known to work.' >&2
    return 1
  fi
}

# Colima can change memory on a *stopped* existing VM without deleting its
# disk. Linux Setup may have started this same profile with only 8 GiB, which
# is insufficient for the SteamOS Debian image builder. Before macOS Build,
# check the live guest and restart only if its allocation is too small.
cfos_colima_ensure_memory() {
  local target memory_kib minimum_kib profile="$CFOS_COLIMA_PROFILE"
  target="$(cfos_colima_build_memory_gib)" || return 1
  memory_kib="$(colima ssh --profile "$profile" -- cat /proc/meminfo 2>/dev/null | awk '$1=="MemTotal:" { print $2; exit }')" || return 1
  [[ "$memory_kib" =~ ^[0-9]+$ ]] || {
    echo "CFOS: cannot read available RAM inside managed Colima profile $profile" >&2
    return 1
  }
  # The guest kernel reserves some of its assigned memory: accept >=95%.
  minimum_kib=$((target * 1024 * 1024 * 95 / 100))
  if (( memory_kib >= minimum_kib )); then
    echo "CFOS: Colima memory ready: $((memory_kib / 1024 / 1024)) GiB (requested ${target} GiB)"
    return 0
  fi
  cfos_colima_check_host_memory "$target" || return 1
  echo "CFOS: Colima has $((memory_kib / 1024 / 1024)) GiB; restarting $profile with ${target} GiB for macOS SteamOS Build (preserving VM disk/images)"
  colima stop --profile "$profile" || return 1
  cfos_colima_start --memory "$target" || return 1
  memory_kib="$(colima ssh --profile "$profile" -- cat /proc/meminfo 2>/dev/null | awk '$1=="MemTotal:" { print $2; exit }')" || return 1
  if [[ ! "$memory_kib" =~ ^[0-9]+$ ]] || (( memory_kib < minimum_kib )); then
    echo "CFOS: managed Colima did not apply ${target} GiB memory (guest reports ${memory_kib:-unknown} KiB); refusing to run SteamOS Build" >&2
    return 1
  fi
  docker info >/dev/null || {
    echo 'CFOS: Docker did not recover after increasing Colima memory' >&2
    return 1
  }
  echo "CFOS: Colima memory ready: $((memory_kib / 1024 / 1024)) GiB (requested ${target} GiB)"
}

# Lima records an exclusive owner for an additional disk. After the earlier
# CFOS path migrations, the disk can retain an owner equal to this *stopped*
# instance. This is a metadata ownership failure, not disk corruption.
# Never unlock a disk owned by a running instance or another profile.
cfos_colima_start() {
  local profile="$CFOS_COLIMA_PROFILE"
  local disk="colima-$profile"
  local log="$LIMA_HOME/colima-$profile/ha.stderr.log"
  local start_output log_bytes=0
  [[ ! -f "$log" ]] || log_bytes=$(wc -c < "$log")
  start_output="$(mktemp)" || return 1
  if colima start --profile "$profile" --activate=false "$@" >"$start_output" 2>&1; then
    cat "$start_output"
    rm -f "$start_output"
    return 0
  fi
  cat "$start_output" >&2
  rm -f "$start_output"
  if [[ -f "$log" ]] &&
     tail -c "+$((log_bytes + 1))" "$log" | grep -Fq "failed to attach disk \`$disk\`: in use by instance \`colima-$profile\`" &&
     ! colima status --profile "$profile" >/dev/null 2>&1; then
    echo "CFOS: Lima reports a stale self-attachment for stopped $profile; releasing its disk ownership" >&2
    if ! command -v limactl >/dev/null 2>&1; then
      echo 'CFOS: limactl unavailable; cannot repair disk ownership' >&2
      return 1
    fi
    # Disk data is not removed. The unlock only clears Lima's stale owner.
    limactl disk unlock "$disk" || return 1
    colima start --profile "$profile" --activate=false "$@" || return 1
    return 0
  fi
  echo "CFOS: Colima failed to start; inspect $log" >&2
  return 1
}

# LLVM/Mesa image extraction requires more space than the original 20 GiB VM.
# Colima's --disk flag does not resize an already-created VZ/Lima instance.
# This profile is disposable build infrastructure: recreate only this managed
# profile at the requested size, rather than repeatedly restarting an undersized
# filesystem. Authored source and staged outputs live outside Colima in Builds.
cfos_colima_ensure_disk() {
  local profile="$CFOS_COLIMA_PROFILE" target_gib=96
  local capacity_kib mount_root
  capacity_kib="$(colima ssh --profile "$profile" -- df -Pk /var/lib/containerd 2>/dev/null | awk 'NR==2 {print $2}')" || return 1
  [[ "$capacity_kib" =~ ^[0-9]+$ ]] || {
    echo 'CFOS: unable to measure managed containerd filesystem capacity' >&2
    return 1
  }
  # ext4 reserves metadata, so a 96 GiB disk exposes slightly less than 96 GiB.
  # 80 GiB is ample evidence that the new 96 GiB disk is mounted.
  if (( capacity_kib >= 80 * 1024 * 1024 )); then return 0; fi
  mount_root="$(dirname "${CFOS_ROOT:-${STEAMAC_SOURCE_ROOT:-}}")"
  [[ -d "$mount_root/CinterForgeOS" && -d "$mount_root/Builds" ]] || {
    echo "CFOS: cannot safely recreate managed VM: invalid workspace $mount_root" >&2
    return 1
  }
  echo "CFOS: managed container filesystem is $((capacity_kib / 1024 / 1024)) GiB; replacing undersized disposable Colima profile with ${target_gib} GiB"
  echo 'CFOS: Docker images and container caches in this managed profile will be rebuilt; source and Builds outputs are preserved'
  colima stop --profile "$profile" || return 1
  colima delete --profile "$profile" --force || return 1
  cfos_colima_start --arch aarch64 --vm-type vz --disk "$target_gib" --mount "$mount_root:w" || return 1
  capacity_kib="$(colima ssh --profile "$profile" -- df -Pk /var/lib/containerd 2>/dev/null | awk 'NR==2 {print $2}')" || return 1
  [[ "$capacity_kib" =~ ^[0-9]+$ ]] && (( capacity_kib >= 80 * 1024 * 1024 )) || {
    echo "CFOS: new managed Colima filesystem is still too small (${capacity_kib:-unknown} KiB); refusing to build" >&2
    return 1
  }
  echo "CFOS: managed containerd filesystem ready: $((capacity_kib / 1024 / 1024)) GiB"
}

cfos_colima_activate() {
  cfos_colima_environment || return 1
  command -v docker >/dev/null && command -v colima >/dev/null || {
    echo 'CFOS: docker/colima missing; run ./CFOS setup cinterforge-os mac Release' >&2; return 1;
  }
  [[ -d "$COLIMA_HOME" ]] || {
    echo 'CFOS: managed Colima not configured; run ./CFOS setup cinterforge-os mac Release' >&2; return 1;
  }
  if docker info >/dev/null 2>&1; then
    cfos_colima_ensure_disk || return 1
    cfos_colima_ensure_memory || return 1
    return 0
  fi
  echo 'CFOS: activating managed Colima profile after shutdown/reboot'
  cfos_colima_start || return 1
  local attempt
  for ((attempt=0; attempt<30; attempt++)); do
    if docker info >/dev/null 2>&1; then
      cfos_colima_ensure_disk || return 1
      cfos_colima_ensure_memory || return 1
      return 0
    fi
    sleep 2
  done
  echo "CFOS: Docker unavailable at $DOCKER_HOST" >&2
  return 1
}
