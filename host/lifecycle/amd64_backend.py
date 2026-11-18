"""Setup/Build ownership of Terminal's ONE macOS x86-64 Debian Docker backend.

The ARM64 LinuxKit producer (c) is separate and must be stopped after Bedrock
acceptance. Wine, MinGW, Linux WebKitGTK and both VNC sessions run on t, never c.
No unrelated Colima profile, user Docker engine, or Windows VM is selected.
"""
# CINTERFORGE SOURCE HEADER — host/lifecycle/amd64_backend.py
# PURPOSE: Owns the amd64 backend stage of the existing CinterForgeTerminal lifecycle; generated files remain in sibling Builds.
# OWNERSHIP: This source belongs strictly to the lifecycle layer and uses CinterForgeLifecycleVM.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   require_managed_t_docker: Reject foreign/legacy Docker endpoints before Windows/Linux Setup work.
#   docker_bind_source: Docker bind mounts use paths on the *daemon*, not on the Mac client.
#   managed_amd64_backend: Start/reuse the single t guest after Bedrock and stop it on every exit.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import platform
import subprocess


def require_managed_t_docker(builds: Path) -> str:
    """Reject foreign/legacy Docker endpoints before Windows/Linux Setup work.

    This checks *which* guest owns DOCKER_HOST; callers additionally query the
    real daemon architecture.  A Docker context name is not proof of ownership:
    it may be missing, stale, or point at LinuxKit c instead of AMD64 t.
    """
    if platform.system() != 'Darwin':
        return os.environ.get('DOCKER_HOST', '')
    builds = builds.resolve(strict=True)
    configured = os.environ.get('CINTER_BUILDS_ROOT', '')
    if (builds.name != 'Builds' or not configured or
            Path(configured).expanduser().resolve() != builds or
            os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') != 'developer'):
        raise RuntimeError('Setup/Build must own the developer Builds t backend')
    # Exactly one VM state root, never an ancestor, OS cache or Steam copy.
    root = builds / 'CFTVM/T'
    if Path(os.environ.get('COLIMA_HOME', '/__unmanaged__')).parent != root or root.is_symlink():
        raise RuntimeError('Setup/Build Docker COLIMA_HOME is not the one Builds/CFTVM/T state')
    expected = {
        'COLIMA_HOME': str(root / 'c'),
        'LIMA_HOME': str(root / 'l'),
        'DOCKER_CONFIG': str(root / 'd'),
        'DOCKER_HOST': 'unix://' + str(root / 'c/t/docker.sock'),
    }
    for key, correct in expected.items():
        if os.environ.get(key) != correct:
            raise RuntimeError(f'Setup/Build Docker {key} does not identify owned t: '
                               f'expected {correct!r}, got {os.environ.get(key)!r}')
    for key in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        if os.environ.get(key):
            raise RuntimeError(f'Setup/Build Docker {key} is a stale/foreign selector; '
                               'the owned t backend uses DOCKER_HOST only')
    return expected['DOCKER_HOST']


def docker_bind_source(path: Path) -> str:
    """Docker bind mounts use paths on the *daemon*, not on the Mac client.

    The t guest exposes the Mac filesystem at /host/mac, whereas c used
    separately mounted source paths. Do not apply this mapping to c or Linux.
    """
    resolved = str(path.resolve())
    if platform.system() == 'Darwin':
        builds = os.environ.get('CINTER_BUILDS_ROOT', '')
        if not builds:
            raise RuntimeError('Docker bind requires explicit managed developer Builds root')
        require_managed_t_docker(Path(builds))
        return '/host/mac' + resolved
    return resolved


def _setup_guest_rsync(builds: Path, vm, sdk: Path, environment: dict[str, str]) -> None:
    """Install and verify Lima's required rsync *inside* the real x86-64 t VM.

    The Mac host's rsync and the Docker container's rsync are not substitutes:
    Lima's 9p/host-volume bootstrap runs in the Ubuntu VM itself. This is an
    explicit Setup-only operation, never a Build, Stage, Test or app-start repair.
    """
    colima = str(sdk / 'mac/env/bin/colima')
    ssh = [colima, 'ssh', '--profile', vm.profile, '--']
    probe = [*ssh, 'sh', '-c',
             'command -v rsync >/dev/null 2>&1 && '
             'rsync --version >/dev/null 2>&1 || exit 42']

    def check_guest_rsync() -> int:
        result = subprocess.run(probe, env=environment, capture_output=True,
                                text=True, check=False, timeout=45)
        if result.returncode not in (0, 42):
            raise RuntimeError(
                'Setup cannot check the managed t guest over SSH; '
                'repair guest SSH before installing any packages. Detail: ' +
                (result.stderr or result.stdout).strip()[-600:])
        return result.returncode

    if check_guest_rsync() == 42:
        vm.progress('Setup: rsync missing in managed x86-64 t guest; installing it there')
        vm.run_checked(
            [*ssh, 'sudo', 'sh', '-c',
             'export DEBIAN_FRONTEND=noninteractive; '
             'apt-get update && '
             'apt-get install -y --no-install-recommends rsync'],
            environment,
            title='Setup provisioning required guest rsync (Ubuntu VM)',
            timeout=600,
        )
        if check_guest_rsync() != 0:
            raise RuntimeError(
                'Setup installed rsync, but the managed t guest cannot execute it; '
                'refusing to seal an incomplete DevTools VM')
    vm.progress('PASS Setup guest rsync: executable verified inside t Ubuntu')

    # The Setup cache may skip VM boot only if the same instance was verified.
    # Disk contents are mutable, so the receipt identifies the profile's own
    # directories, not timestamps of its large writable Docker/Ubuntu disk.
    from host.lifecycle.setup_dirty import record_verified_t_guest
    record_verified_t_guest(builds)


@contextmanager
def managed_amd64_backend(builds: Path, *, setup: bool = True):
    """Start/reuse the single t guest after Bedrock and stop it on every exit.

    Build may start an already provisioned VM but must never create/repair its
    configuration or install a missing Buildx tool. Setup owns those actions.
    """
    if platform.system() != 'Darwin':
        yield None
        return
    from host.mac.mac_bootstrap import _ensure_buildx_plugin
    from host.lifecycle.CinterForgeLifecycleVM import CinterForgeLifecycleVM

    builds = builds.resolve(strict=True)
    vm = CinterForgeLifecycleVM(builds)
    sdk = builds / 'Toolchains'
    previous = {key: os.environ.get(key) for key in (
        'CINTER_BUILDS_ROOT', 'CINTER_TERMINAL_RUNTIME_MODE',
        'DOCKER_CONTEXT', 'DOCKER_HOST', 'DOCKER_TLS_VERIFY',
        'DOCKER_CERT_PATH', 'HOME', 'COLIMA_HOME', 'LIMA_HOME',
        'COLIMA_CACHE_HOME', 'DOCKER_CONFIG', 'XDG_CONFIG_HOME',
        'XDG_CACHE_HOME', 'TMPDIR', 'PATH')}
    owns_vm = False
    completed = False
    try:
        os.environ['CINTER_BUILDS_ROOT'] = str(builds)
        os.environ['CINTER_TERMINAL_RUNTIME_MODE'] = 'developer'
        env = vm.environment(sdk)
        # Never silently take an actively running developer app's Docker engine.
        with vm.managed_vm_lock(env):
            if vm.native_owner_active(env):
                raise RuntimeError('Native CinterForgeTerminal owns the t guest; '
                                   'refusing to stop or repurpose its containers')
            if setup:
                _ensure_buildx_plugin(builds, env)
            else:
                # Build may start a provisioned VM, not repair/recreate its disk.
                profile_file = Path(env['COLIMA_HOME']) / vm.profile / 'colima.yaml'
                plugin = Path(env['DOCKER_CONFIG']) / 'cli-plugins/docker-buildx'
                if not profile_file.is_file() or not plugin.is_file():
                    raise RuntimeError('Setup-provisioned x86-64 Terminal guest/Buildx missing; '
                                       'run ./Cinter setup all')
                differences = vm.colima_configuration_differences(
                    env, vm.mac_host_mounts())
                if differences:
                    raise RuntimeError('Setup-provisioned Terminal guest config changed; '
                                       'Build cannot repair it. Differences: ' +
                                       '; '.join(differences) +
                                       '. Run ./Cinter setup all to repair the owned profile')
            # Set ownership before start: even a partially booted VM is ours
            # to stop if an exception or timeout interrupts the start.
            owns_vm = True
            # Setup owns capacity provisioning.  The original 32-GiB t disk is
            # too small for the pinned Windows cross SDK plus BuildKit/Wine.
            # Build/Test may start t but must never resize or repair it.
            docker = vm.ensure_docker_locked(
                sdk, env, builds.parent, min_disk_gib=96 if setup else None)
            arch = subprocess.run([docker, 'info', '--format', '{{.Architecture}}'],
                                  env=env, capture_output=True, text=True,
                                  timeout=45, check=False)
            actual = arch.stdout.strip().lower()
            if arch.returncode or actual not in ('x86_64', 'amd64'):
                raise RuntimeError('Terminal guest Docker daemon is not native x86-64 '
                                   f'(actual={actual!r}): {arch.stderr[-500:]}')
            print('[TERMINAL AMD64] PASS: one QEMU x86-64 t guest, '
                  'Docker daemon architecture=' + actual, flush=True)
            if setup:
                _setup_guest_rsync(builds, vm, sdk, env)
        # Replace every Docker selector as a unit, including a previous c
        # context. dict.update() alone leaves stale keys in os.environ.
        for key in ('DOCKER_CONTEXT', 'DOCKER_HOST', 'DOCKER_TLS_VERIFY',
                    'DOCKER_CERT_PATH'):
            os.environ.pop(key, None)
        # All children (including Windows cross build and Linux samples) use t.
        os.environ.update(env)
        require_managed_t_docker(builds)
        yield docker
        completed = True
    finally:
        try:
            if owns_vm:
                print('[TERMINAL AMD64] Releasing Setup/Build-owned t guest', flush=True)
                vm.stop(sdk)
                if setup and completed:
                    # Colima --save-config can rewrite its YAML *during boot*.
                    # Seal the final persisted topology only after the owned
                    # guest has shut down. Build must never do this repair.
                    with vm.managed_vm_lock(env):
                        if vm.native_owner_active(env) or vm.owned_qemu_pids(env, sdk):
                            raise RuntimeError('Setup cannot seal t configuration while '
                                               'a native owner or QEMU remains active')
                        emulation_changed = vm.configure_colima_runtime_emulation(env)
                        mounts_changed = vm.configure_colima_host_mounts(
                            env, vm.mac_host_mounts())
                        differences = vm.colima_configuration_differences(
                            env, vm.mac_host_mounts())
                        if differences:
                            raise RuntimeError('Setup could not seal t VM configuration: '
                                               + '; '.join(differences))
                        print('[TERMINAL AMD64] PASS Setup sealed stopped t VM '
                              'configuration (emulation=' +
                              ('normalized' if emulation_changed else 'current') +
                              ', mounts=' +
                              ('normalized' if mounts_changed else 'current') + ')',
                              flush=True)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
