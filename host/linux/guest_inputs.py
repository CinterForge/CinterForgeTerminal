#!/usr/bin/env python3
# CINTERFORGE SOURCE HEADER — host/linux/guest_inputs.py
# PURPOSE: Implements the Linux guest inputs owner for guest VM tooling, Bedrock runtime and cross-platform launcher integration.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   _run: Run run for this module.
#   _require_elf: Handle require elf through this component's established interfaces.
#   prepare_official_bedrock_archive: Setup-only acquisition of official Bedrock acceptance bytes.
#   provision_guest_runtime: Build the LinuxKit guest loader/probe without downloading Bedrock.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
"""Production LinuxKit input provisioning for Setup, never acceptance tests.

Only Setup calls ``provision_guest_runtime`` and ``prepare_official_bedrock_archive``.
Build, Stage and Test consume these pinned, checksum-verified outputs; no
acceptance code or test package is needed to produce the build inputs.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROBE_SOURCE = HERE / 'probes/cinter_nethernet_probe.c'
GLIBC_IMAGE = 'ubuntu:24.04'
PROBE_IMAGE = 'alpine:3.20'


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print('[CFOS LinuxKit Setup] ' + ' '.join(map(str, command)), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def _require_elf(path: Path, machine: int) -> None:
    with path.open('rb') as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b'\x7fELF' or header[4] != 2 or header[5] != 1:
        raise RuntimeError(f'Not a 64-bit little-endian ELF executable: {path}')
    actual = struct.unpack_from('<H', header, 18)[0]
    if actual != machine:
        raise RuntimeError(f'Unexpected ELF architecture {actual} (expected {machine}): {path}')


def official_bedrock_archive(repo: Path, target: str = 'linux') -> Path:
    """Read-only lookup through the CraftMaster-owned archive cache."""
    from host.lifecycle.craftmaster_dependency import craftmaster_bedrock_module
    builds = repo.resolve().parent / 'Builds'
    return craftmaster_bedrock_module(builds, 'CinterForgeLifecycleBedrock').official_bedrock_archive(builds, target)


def prepare_official_bedrock_archive(repo: Path, target: str = 'linux') -> Path:
    """Delegate Setup-only acquisition, verification and legacy-cache reuse."""
    import platform
    from host.lifecycle.craftmaster_dependency import craftmaster_bedrock_module
    builds = repo.resolve().parent / 'Builds'
    system = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}[platform.system()]
    return craftmaster_bedrock_module(builds, 'CinterForgeLifecycleBedrock').prepare_official_bedrock_archive(
        builds, target, system=system, app='CinterForgeTerminal')


def provision_guest_runtime(destination: Path, docker: str, docker_env: dict[str, str]) -> None:
    """Build the LinuxKit guest loader/probe without downloading Bedrock.

    Setup supplies the *guest infrastructure*. The official Bedrock binary and
    EULA receipt are exclusively installed by the end user into steamapps.
    """
    destination = destination.resolve()
    loader = destination / 'glibc-x86_64/ld-linux-x86-64.so.2'
    probe = destination / 'cinter-nethernet-probe'
    if loader.is_file() and probe.is_file():
        _require_elf(loader, 62)
        _require_elf(probe, 183)
        return
    destination.mkdir(parents=True, exist_ok=True)
    if not PROBE_SOURCE.is_file():
        raise FileNotFoundError('Missing original NetherNet probe source: '+str(PROBE_SOURCE))
    _run([docker, 'run', '--rm', '--platform', 'linux/arm64',
          '--mount', f'type=bind,src={PROBE_SOURCE.parent.resolve()},dst=/source,readonly',
          '--mount', f'type=bind,src={destination},dst=/out',
          PROBE_IMAGE, 'sh', '-ec',
          'apk add --no-cache build-base >/dev/null && '
          'cc -O2 -static -o /out/cinter-nethernet-probe /source/cinter_nethernet_probe.c && '
          'chmod 755 /out/cinter-nethernet-probe'], env=docker_env)
    _require_elf(probe,183)
    root = destination / 'glibc-x86_64'
    root.mkdir(exist_ok=True)
    container = _run([docker, 'create', '--platform', 'linux/amd64', GLIBC_IMAGE,
                      '/bin/true'], capture_output=True,text=True,env=docker_env).stdout.strip()
    if not container:
        raise RuntimeError('Docker failed to acquire Ubuntu x86-64 guest loader')
    try:
        _run([docker,'cp','-L',f'{container}:/lib/x86_64-linux-gnu/.',str(root)],env=docker_env)
        if not loader.is_file():
            _run([docker,'cp','-L',f'{container}:/lib64/ld-linux-x86-64.so.2',str(loader)],env=docker_env)
        _require_elf(loader,62)
        loader.chmod(loader.stat().st_mode|0o111)
    finally:
        _run([docker,'rm','-f',container],stdout=subprocess.DEVNULL,env=docker_env)
    print('[LinuxKit] Standalone guest runtime provisioned (no Bedrock game)',flush=True)


