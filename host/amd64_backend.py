"""Setup/Build ownership of Terminal's ONE macOS x86-64 Debian Docker backend.

The ARM64 LinuxKit producer (c) is separate and must be stopped after Bedrock
acceptance. Wine, MinGW, Linux WebKitGTK and both VNC sessions run on t, never c.
No unrelated Colima profile, user Docker engine, or Windows VM is selected.
"""
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
    # The managed runtime picks the shortest real Builds ancestor so Lima
    # stays under macOS's Unix socket length limit.
    roots = [parent / 'CFTVM/T' for parent in reversed((builds, *builds.parents))
             if parent.name == 'Builds' and parent.is_dir() and not parent.is_symlink()]
    root = Path(os.environ.get('COLIMA_HOME', '/__unmanaged__')).parent
    if root not in roots or root.is_symlink():
        raise RuntimeError('Setup/Build Docker COLIMA_HOME is not the managed t VM state')
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


@contextmanager
def managed_amd64_backend(builds: Path, *, setup: bool = True):
    """Start/reuse the single t guest after Bedrock and stop it on every exit.

    Build may start an already provisioned VM but must never create/repair its
    configuration or install a missing Buildx tool. Setup owns those actions.
    """
    if platform.system() != 'Darwin':
        yield None
        return
    from host.mac_bootstrap import _ensure_buildx_plugin
    from webapp import CinterForgeTerminal as runtime

    builds = builds.resolve(strict=True)
    sdk = builds / 'CinterForgeTerminal/Toolchains'
    previous = {key: os.environ.get(key) for key in (
        'CINTER_BUILDS_ROOT', 'CINTER_TERMINAL_RUNTIME_MODE',
        'DOCKER_CONTEXT', 'DOCKER_HOST', 'DOCKER_TLS_VERIFY',
        'DOCKER_CERT_PATH', 'HOME', 'COLIMA_HOME', 'LIMA_HOME',
        'COLIMA_CACHE_HOME', 'DOCKER_CONFIG', 'XDG_CONFIG_HOME',
        'XDG_CACHE_HOME', 'TMPDIR', 'PATH')}
    owns_vm = False
    try:
        os.environ['CINTER_BUILDS_ROOT'] = str(builds)
        os.environ['CINTER_TERMINAL_RUNTIME_MODE'] = 'developer'
        env = runtime._env(sdk)
        # Never silently take an actively running developer app's Docker engine.
        with runtime._managed_vm_lock(env):
            owner = runtime._runtime_watchdog_owner(env)
            if owner.is_file():
                import json
                record = json.loads(owner.read_text(encoding='utf-8'))
                pid = record.get('native_pid')
                if isinstance(pid, int) and runtime._live_runtime_watchdog(pid, env):
                    raise RuntimeError('Native CinterForgeTerminal owns the t guest; '
                                       'refusing to stop or repurpose its containers')
            if setup:
                _ensure_buildx_plugin(builds, env)
            else:
                # Build may start a provisioned VM, not repair/recreate its disk.
                profile_file = Path(env['COLIMA_HOME']) / runtime.PROFILE / 'colima.yaml'
                plugin = Path(env['DOCKER_CONFIG']) / 'cli-plugins/docker-buildx'
                if not profile_file.is_file() or not plugin.is_file():
                    raise RuntimeError('Setup-provisioned x86-64 Terminal guest/Buildx missing; '
                                       'run ./Cinter setup all')
                if (runtime._configure_colima_runtime_emulation(env, dry_run=True) or
                        runtime._configure_colima_host_mounts(env, runtime._mac_host_mounts(),
                                                              dry_run=True)):
                    raise RuntimeError('Setup-provisioned Terminal guest config changed; '
                                       'Build cannot repair it')
            # Set ownership before start: even a partially booted VM is ours
            # to stop if an exception or timeout interrupts the start.
            owns_vm = True
            docker = runtime._ensure_docker_locked(sdk, env, builds.parent)
            arch = subprocess.run([docker, 'info', '--format', '{{.Architecture}}'],
                                  env=env, capture_output=True, text=True,
                                  timeout=45, check=False)
            actual = arch.stdout.strip().lower()
            if arch.returncode or actual not in ('x86_64', 'amd64'):
                raise RuntimeError('Terminal guest Docker daemon is not native x86-64 '
                                   f'(actual={actual!r}): {arch.stderr[-500:]}')
            print('[TERMINAL AMD64] PASS: one QEMU x86-64 t guest, '
                  'Docker daemon architecture=' + actual, flush=True)
        # Replace every Docker selector as a unit, including a previous c
        # context. dict.update() alone leaves stale keys in os.environ.
        for key in ('DOCKER_CONTEXT', 'DOCKER_HOST', 'DOCKER_TLS_VERIFY',
                    'DOCKER_CERT_PATH'):
            os.environ.pop(key, None)
        # All children (including Windows cross build and Linux samples) use t.
        os.environ.update(env)
        require_managed_t_docker(builds)
        yield docker
    finally:
        try:
            if owns_vm:
                print('[TERMINAL AMD64] Releasing Setup/Build-owned t guest', flush=True)
                runtime.stop_runtime_vm(setup_sdk=sdk)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
