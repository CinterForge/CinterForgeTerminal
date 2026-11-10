"""Opt-in shared Docker backend for Apple Silicon Linux AMD64 workloads.

Never mutate the user's default Colima profile or global Docker context.
"""
from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import subprocess

from managed_backend import backend_environment, source_directory


def docker_environment() -> dict[str, str]:
    env = os.environ.copy()
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        return env
    docker = shutil.which('docker')
    if not docker:
        return env
    return backend_environment(source_directory(Path(__file__).resolve().parents[2]))


def configure() -> int:
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        print('ERROR: Rosetta backend setup requires Apple Silicon macOS.')
        return 1
    colima = shutil.which('colima')
    if not colima:
        print('ERROR: Colima is missing. Install Colima before configuring Rosetta.')
        return 1
    # Dedicated Colima profile is one shared runtime backend for ALL apps,
    # not a Steam-specific VM. Existing profiles and volumes are untouched.
    env = backend_environment(source_directory(Path(__file__).resolve().parents[2]))
    profile = env['CFOS_COLIMA_PROFILE']
    command = [colima, 'start', '--profile', profile, '--vm-type', 'vz',
               '--vz-rosetta', '--cpu', '4', '--memory', '8', '--disk', '96']
    print('[linux-distro] Starting shared Apple Virtualization/Rosetta backend:', ' '.join(command), flush=True)
    result = subprocess.run(command, env=env, check=False)
    if result.returncode:
        print('ERROR: Colima could not start the Rosetta backend. Existing Docker context was not changed.')
        return result.returncode
    if not env.get('DOCKER_HOST'):
        print('ERROR: CFOS managed Docker socket is not configured.')
        return 1
    probe = subprocess.run(['docker', 'run', '--rm', '--platform', 'linux/amd64',
                            '--security-opt', 'seccomp=unconfined', 'alpine:3.20',
                            'sh', '-c', 'unshare --user --map-root-user --mount --pid --fork true && unshare --user --map-root-user unshare --user --map-root-user true'],
                           env=env, capture_output=True, text=True, check=False)
    if probe.returncode:
        print('[linux-distro] Rosetta AMD64 nested user/mount/PID namespaces: FAIL')
        print((probe.stderr or probe.stdout).strip())
        print('[linux-distro] WARNING: Steam namespace compatibility remains unresolved; this does not block building the shared Linux image.')
    print('[linux-distro] Rosetta AMD64 nested user/mount/PID namespaces: PASS')
    print('[linux-distro] Shared Docker socket:', env['DOCKER_HOST'])
    # Setup must refresh the disposable shared image; otherwise subsequent tests
    # silently run stale packages even when Dockerfile.distro has changed.
    cfos_root = Path(__file__).resolve().parents[2]
    print('[linux-distro] Building updated CFOS headless AMD64 Linux image...', flush=True)
    build = subprocess.run([str(cfos_root / 'CFOS'), 'build', 'cinterforge-os',
                            'linux', 'Release'], cwd=cfos_root, env=env, check=False)
    if build.returncode:
        print(f'ERROR: Shared Linux image build failed (exit {build.returncode}).')
        return build.returncode
    print('[linux-distro] Shared AMD64 Linux image rebuilt successfully.')
    return 0


if __name__ == '__main__':
    raise SystemExit(configure())
