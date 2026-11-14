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


def docker_bind_source(path: Path) -> str:
    """Docker bind mounts use paths on the *daemon*, not on the Mac client.

    The t guest exposes the Mac filesystem at /host/mac, whereas c used
    separately mounted source paths. Do not apply this mapping to c or Linux.
    """
    resolved = str(path.resolve())
    if platform.system() == 'Darwin' and os.environ.get('DOCKER_CONTEXT') == 'colima-t':
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
        # Docker's DOCKER_HOST takes precedence over DOCKER_CONTEXT. A prior
        # LinuxKit c environment may have set it; remove it before switching.
        for key in ('DOCKER_HOST', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
            os.environ.pop(key, None)
        # All children (including Windows cross build and Linux samples) use t.
        os.environ.update(env)
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
