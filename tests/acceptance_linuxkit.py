#!/usr/bin/env python3
"""Terminal-owned LinuxKit acceptance: run real x86-64 Bedrock with Rosetta.

Setup prepares the kernel, native VM launcher with LAN relay, glibc, probe and official
Bedrock archive; acceptance consumes them offline and verifies console/networking.
All mutable test files live under an isolated Builds/Intermediates directory;
no player's worlds, Steam installation or production server may be modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
LINUXOS = REPO / 'linuxos'
# Entry point works both when invoked directly by Setup and as a package import.
for directory in (str(REPO), str(LINUXOS)):
    if directory not in sys.path:
        sys.path.insert(0, directory)
from build_linux import verify_shared_bootstrap, linuxkit_bootstrap_root
from lifecycle import verify_guest

HERE = LINUXOS
PROBE_SOURCE = HERE / 'probes/cinter_nethernet_probe.c'
GLIBC_IMAGE = 'ubuntu:24.04'  # Pinned source image for the x86-64 guest loader
PROBE_IMAGE = 'alpine:3.20'   # ARM64 native static acceptance probe


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print('[CFOS LinuxKit Test] ' + ' '.join(map(str, command)), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _require_elf(path: Path, machine: int) -> None:
    with path.open('rb') as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b'\x7fELF' or header[4] != 2 or header[5] != 1:
        raise RuntimeError(f'Not a 64-bit little-endian ELF executable: {path}')
    actual = struct.unpack_from('<H', header, 18)[0]
    if actual != machine:
        raise RuntimeError(f'Unexpected ELF architecture {actual} (expected {machine}): {path}')


def prepare_official_bedrock_archive(repo: Path) -> Path:
    """Setup-only acquisition of official Bedrock acceptance bytes.

    The later Test phase extracts only this verified pinned input. Never
    downloads, creates Docker images, or installs packages into steamapps.
    """
    from craftmaster.build_craftmaster_server import _download, discover
    builds = repo.resolve().parent / 'Builds'
    cache = builds / 'Intermediates/CinterForgeTerminal/LinuxKit/Downloads'
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / 'bedrock-server-linux.zip'
    import zipfile
    if archive.is_file():
        with zipfile.ZipFile(archive) as package:
            if package.testzip() is None and 'bedrock_server' in package.namelist():
                return archive
        # Bad cache is generated input, not an installation or user world.
        archive.unlink()
    _download(discover('linux'), archive)
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None or 'bedrock_server' not in package.namelist():
            raise RuntimeError('Official Bedrock archive is missing its Linux server executable')
    return archive


def _install_prepared_runtime(server: Path, guest: Path) -> dict:
    """Populate a disposable Bedrock test fixture using Setup's verified closure.

    No Docker, network, glibc extraction or C compilation occurs here.
    """
    _require_elf(server / 'bedrock_server', 62)
    cinter = server / '.cinter'
    if cinter.exists():
        raise RuntimeError('Isolated acceptance server unexpectedly contains a runtime')
    cinter.mkdir()
    loader_source = guest / 'GuestRuntime/glibc-x86_64/ld-linux-x86-64.so.2'
    probe_source = guest / 'GuestRuntime/cinter-nethernet-probe'
    _require_elf(loader_source, 62)
    _require_elf(probe_source, 183)
    shutil.copytree(loader_source.parent, cinter / 'glibc-x86_64', symlinks=False)
    shutil.copy2(probe_source, cinter / 'cinter-nethernet-probe')
    _require_elf(cinter / 'glibc-x86_64/ld-linux-x86-64.so.2', 62)
    _require_elf(cinter / 'cinter-nethernet-probe', 183)
    manifest = {
        'bedrock_sha256': _sha256(server / 'bedrock_server'),
        'glibc_loader_sha256': _sha256(cinter / 'glibc-x86_64/ld-linux-x86-64.so.2'),
        'probe_sha256': _sha256(cinter / 'cinter-nethernet-probe'),
        'source': 'verified-setup-guest-runtime',
    }
    (cinter / 'acceptance-inputs.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    return manifest


def provision_guest_runtime(destination: Path, docker: str, docker_env: dict[str, str]) -> None:
    """Build the LinuxKit guest loader/probe without downloading Bedrock.

    Setup supplies the *guest infrastructure*. The official Bedrock binary and
    EULA receipt are exclusively installed by the end user into steamapps.
    """
    import shutil
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


def test_linuxkit(repo: Path, *, offline: bool = False) -> None:
    repo = repo.resolve()
    if not (repo / 'Cinter').is_file():
        raise RuntimeError(f'Expected CinterForgeTerminal checkout: {repo}')
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('CFOS LinuxKit Bedrock/Rosetta Test requires Apple Silicon macOS')
    # Direct developer test invocation uses the same explicit CLI consent gate.
    # Packaged applications collect consent in the WebView and share the receipt.
    from bedrock_consent import require_bedrock_eula
    require_bedrock_eula(repo.parent / 'Builds', 'mac')
    server_repo = repo
    verify_shared_bootstrap(repo, require_launcher=False)
    root = repo.parent / 'Builds'
    if not root.is_dir():
        raise RuntimeError(f'Shared Builds directory missing: {root}')
    tmp_parent = root / 'Intermediates/CinterForgeTerminal/LinuxKit'
    tmp_parent.mkdir(parents=True, exist_ok=True)
    build = HERE
    installer = build / 'craftmaster/build_craftmaster_server.py'
    validator = build / 'craftmaster/validate_minecraft_server.py'
    if not installer.is_file() or not validator.is_file():
        raise RuntimeError(f'CinterForge Bedrock installer/validator missing under {build}')
    # Setup has already built and verified the dedicated `c` guest closure.
    # Acceptance uses that closure directly; Test must not start either VM
    # builder or acquire toolchains and images.
    # Keep a durable Test receipt outside the disposable test installation.
    receipt = root / 'Logs/CinterForgeTerminal/linuxkit-bedrock-acceptance.json'
    receipt.parent.mkdir(parents=True, exist_ok=True)
    guest = verify_guest()
    evidence = {'schema': 'cinterforge.cfos.linuxkit-bedrock-test.v1',
                'bootstrap_sha256': {name: _sha256(guest / name) for name in
                   ('cinter-bds-kernel', 'cinter-bds-initrd.img', 'cinter-bds-cmdline', 'CinterBDSVM')},
                'status': 'failed', 'checked_at': datetime.now(timezone.utc).isoformat(),
                'test': 'real-official-bedrock-rosetta-nethernet-console-stop'}
    try:
        _execute_acceptance(repo, server_repo, installer, validator, tmp_parent, evidence, offline,
                            guest)
    except Exception as exc:
        evidence['error'] = str(exc)
        receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
        raise
    evidence['status'] = 'passed'
    receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    print(f'[CFOS LinuxKit Test] Evidence: {receipt}', flush=True)
    print('[CFOS LinuxKit Test] PASS: official Bedrock installed and executed via ARM64 LinuxKit/Rosetta; NetherNet, list, stop, exit verified', flush=True)


def _execute_acceptance(repo: Path, server_repo: Path, installer: Path, validator: Path,
                        tmp_parent: Path, evidence: dict, offline: bool,
                        guest: Path) -> None:
    with tempfile.TemporaryDirectory(prefix='Acceptance-', dir=tmp_parent) as temp:
        server = Path(temp) / 'Server'
        command = [sys.executable, '-u', str(installer), '--repo-root', str(server_repo),
                   '--linuxkit-test-install', str(server)]
        # Setup has already acquired and validated this official archive.
        # Acceptance must never repair missing inputs by downloading them.
        command.append('--offline')
        _run(command)
        evidence['runtime'] = _install_prepared_runtime(server, guest)
        _run([sys.executable, '-u', str(validator), '--repo-root', str(server_repo),
              '--cfos-linuxkit-test-server', str(server), '--cfos-root', str(repo)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, required=True)
    parser.add_argument('--offline', action='store_true',
                        help='Use previously downloaded official Bedrock ZIP')
    args = parser.parse_args()
    try:
        test_linuxkit(args.repo_root, offline=args.offline)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'[CFOS LinuxKit Test] FAIL: {exc}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
