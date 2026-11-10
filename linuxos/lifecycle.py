"""Terminal-owned LinuxKit lifecycle: Setup prepares; Stage only verifies.

Setup provisions the kernel, signed VM launcher with LAN relay, guest glibc and native
NetherNet probe. Stage is read-only; Test starts only the completed runtime for acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parent
BUILDS = WORKSPACE / 'Builds'
HERE = Path(__file__).resolve().parent

# Direct `python linuxos/lifecycle.py` starts with linuxos/ on sys.path, not
# the repository root; make package imports independent of the caller's PATH.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _call(*arguments: str) -> None:
    print('[CinterForgeTerminal LinuxKit] ' + ' '.join(arguments), flush=True)
    subprocess.run(arguments, check=True, cwd=REPO,
                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))


def _linuxkit() -> Path:
    return BUILDS / 'CreativeSuite/CFOS/LinuxKit'  # Existing shared stage, no migration of user VM state.


def managed_setup_docker() -> tuple[str, dict[str, str]]:
    """Provision the dedicated short-lived ``c`` Ubuntu guest-image backend.

    Windows cross compilation uses a separate ``w`` VM. Neither instance is
    a substitute for the other; both use Setup's pinned Colima/Lima tools and
    both are shut down by the outer Setup/Build owner.
    """
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('LinuxKit guest provisioning requires Apple Silicon macOS')
    from linuxos.managed_backend import ensure_backend, source_directory
    return ensure_backend(source_directory(REPO))


def setup_kernel() -> None:
    """Build the LinuxKit kernel/initrd in Setup, never in Build/Stage/Test."""
    _call(sys.executable, '-B', str(HERE / 'build_linux.py'), '--repo-root', str(REPO))


def verify() -> None:
    _call(sys.executable, '-B', str(HERE / 'build_linux.py'), '--repo-root', str(REPO), '--verify-only')


def prepare_guest() -> Path:
    """Produce the Terminal's complete guest closure during Setup only.

    The source LinuxKit image is preserved, while the signed VZ launcher,
    LAN relay, Ubuntu loader and probe are built under Terminal-owned Builds.
    """
    from build_linux import verify_shared_bootstrap
    verify_shared_bootstrap(REPO, require_launcher=False)
    origin = _linuxkit()
    output = BUILDS / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64'
    names = ('cinter-bds-kernel', 'cinter-bds-initrd.img',
             'cinter-bds-cmdline', 'built-source-fingerprint.txt')
    source_hash = {}
    for name in names:
        file = origin / name
        if not file.is_file() or file.is_symlink():
            raise RuntimeError(f'LinuxKit stage component missing or non-regular: {file}')
        source_hash[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            raise RuntimeError(f'Unsafe Terminal-owned generated guest directory: {output}')
        # Setup may refresh its OWN disposable generated inputs when source
        # changed; Build/Stage/Test never repair them. Keep all other paths,
        # particularly installed Bedrock worlds, entirely untouched.
        mismatch = any(
            not (output / name).is_file() or (output / name).is_symlink() or
            hashlib.sha256((output / name).read_bytes()).hexdigest() != digest
            for name, digest in source_hash.items()
        )
        if mismatch:
            shutil.rmtree(output)
    if not output.exists():
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='LinuxKit-', dir=output.parent) as tmp:
            candidate = Path(tmp) / 'arm64'
            candidate.mkdir()
            for name in names:
                shutil.copy2(origin / name, candidate / name)
            candidate.rename(output)
    # Terminal owns the VM launcher, including networking mode and signature.
    # Refresh it without rebuilding or modifying the shared CFOS guest image.
    if platform.system() == 'Darwin':
        from launcher_signing import sign_launcher, has_virtualization_entitlement
        swift = HERE / 'CinterBDSVM.swift'
        digest = hashlib.sha256(swift.read_bytes() + (HERE / 'CinterBDSVM.entitlements').read_bytes()).hexdigest() + '\n'
        fingerprint = output / 'vm-launcher-source-fingerprint.txt'
        target = output / 'CinterBDSVM'
        if (not target.is_file() or not fingerprint.is_file() or
                fingerprint.read_text() != digest or
                not has_virtualization_entitlement(target)):
            with tempfile.TemporaryDirectory(prefix='VMLauncher-', dir=output.parent) as temporary:
                candidate = Path(temporary) / 'CinterBDSVM'
                subprocess.run(['/usr/bin/xcrun', 'swiftc', '-framework', 'Virtualization',
                                '-o', str(candidate), str(swift)], check=True)
                sign_launcher(candidate)
                shutil.copy2(candidate, output / 'CinterBDSVM.new')
                (output / 'CinterBDSVM.new').replace(target)
                fingerprint.write_text(digest)
                print('[CinterForgeTerminal LinuxKit] staged signed VM launcher', flush=True)
    # The original Mac release exposed Bedrock on the LAN with a small
    # user-space relay. Rebuild it in Setup if its source or binary changed.
    # This does not touch the guest kernel or the player's worlds.
    if platform.system() == 'Darwin':
        import json
        from craftmaster.craftmaster_mac_bootstrap import LAN_RELAY_C
        receipt = output / 'relay-source-sha256.json'
        relay_binary = output / 'CinterBDSNetRelay'
        source_digest = hashlib.sha256(LAN_RELAY_C.encode('utf-8')).hexdigest()
        try:
            saved = json.loads(receipt.read_text())
        except (OSError, ValueError):
            saved = {}
        if (not relay_binary.is_file() or relay_binary.is_symlink() or
                saved.get('source_sha256') != source_digest or
                hashlib.sha256(relay_binary.read_bytes()).hexdigest() != saved.get('binary_sha256')):
            sdk = subprocess.run(['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path'],
                                 check=True, capture_output=True, text=True).stdout.strip()
            compiler = subprocess.run(['/usr/bin/xcrun', '--sdk', 'macosx', '-f', 'clang'],
                                      check=True, capture_output=True, text=True).stdout.strip()
            with tempfile.TemporaryDirectory(prefix='BedrockRelay-', dir=output.parent) as scratch:
                source = Path(scratch) / 'cinter_bds_lan_relay.c'
                built = Path(scratch) / 'CinterBDSNetRelay'
                source.write_text(LAN_RELAY_C, encoding='utf-8')
                subprocess.run([compiler, '-arch', 'arm64', '-isysroot', sdk,
                                '-O2', '-pthread', '-o', str(built), str(source)], check=True)
                if not built.is_file() or not built.stat().st_size:
                    raise RuntimeError('Bedrock LAN relay compilation produced no binary')
                built.chmod(0o755)
                digest = hashlib.sha256(built.read_bytes()).hexdigest()
                candidate = output / 'CinterBDSNetRelay.new'
                shutil.copy2(built, candidate)
                candidate.replace(relay_binary)
                receipt.write_text(json.dumps({'source_sha256': source_digest,
                                              'binary_sha256': digest}, sort_keys=True) + '\n')
            print('[CinterForgeTerminal LinuxKit] staged Bedrock LAN relay', flush=True)
    # The VM launcher alone cannot run the installed Bedrock package: retain
    # the existing managed Ubuntu glibc and ARM64 probe producer, independently
    # of the Bedrock server/EULA acquisition performed by the user later.
    if platform.system() == 'Darwin':
        from tests.acceptance_linuxkit import provision_guest_runtime
        docker, env = managed_setup_docker()
        provision_guest_runtime(output / 'GuestRuntime', docker, env)
    # Assemble the existing NAT VM launcher with its owned host LAN relay.
    from craftmaster.craftmaster_mac_bootstrap import RUN_SH
    original = RUN_SH
    runner = original.replace(
        '[ -d "$HERE/glibc-x86_64" ]',
        '[ -d "$HERE/GuestRuntime/glibc-x86_64" ]').replace(
        'cp -R "$HERE/glibc-x86_64" "$BDS/.cinter/glibc-x86_64"',
        'cp -R "$HERE/GuestRuntime/glibc-x86_64" "$BDS/.cinter/glibc-x86_64"').replace(
        'cp "$HERE/cinter-nethernet-probe-linux-arm64" "$BDS/.cinter/cinter-nethernet-probe"',
        'cp "$HERE/GuestRuntime/cinter-nethernet-probe" "$BDS/.cinter/cinter-nethernet-probe"')
    if (runner == original or 'CinterBDSNetRelay' not in runner or
            'PROPS_BACKUP' in runner or 'set_prop' in runner):
        raise RuntimeError('Source LinuxKit LAN relay launcher contract changed')
    launcher = output / 'run-mac-bds.sh'
    if not launcher.is_file() or launcher.read_text() != runner:
        launcher.write_text(runner)
        launcher.chmod(0o755)
    return output


def verify_guest() -> Path:
    """Read-only closure verification; never starts Docker, Colima or a compiler."""
    from build_linux import verify_shared_bootstrap
    verify_shared_bootstrap(REPO, require_launcher=False)
    source = _linuxkit()
    output = BUILDS / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64'
    for name in ('cinter-bds-kernel', 'cinter-bds-initrd.img',
                 'cinter-bds-cmdline', 'built-source-fingerprint.txt'):
        original, prepared = source / name, output / name
        if (not prepared.is_file() or prepared.is_symlink() or
                hashlib.sha256(original.read_bytes()).digest() !=
                hashlib.sha256(prepared.read_bytes()).digest()):
            raise RuntimeError(f'Setup-produced LinuxKit input missing or stale: {prepared}; run ./Cinter setup all')
    if platform.system() == 'Darwin':
        from launcher_signing import has_virtualization_entitlement
        target = output / 'CinterBDSVM'
        fingerprint = output / 'vm-launcher-source-fingerprint.txt'
        expected = hashlib.sha256((HERE / 'CinterBDSVM.swift').read_bytes() +
                                  (HERE / 'CinterBDSVM.entitlements').read_bytes()).hexdigest() + '\n'
        if (not target.is_file() or target.is_symlink() or
                not fingerprint.is_file() or fingerprint.read_text() != expected or
                not has_virtualization_entitlement(target)):
            raise RuntimeError('Signed VM launcher missing/stale; run ./Cinter setup all')
        from tests.acceptance_linuxkit import _require_elf
        runtime = output / 'GuestRuntime'
        loader = runtime / 'glibc-x86_64/ld-linux-x86-64.so.2'
        probe = runtime / 'cinter-nethernet-probe'
        for file, machine in ((loader, 62), (probe, 183)):
            if not file.is_file() or file.is_symlink():
                raise RuntimeError(f'Setup-produced Bedrock guest dependency missing: {file}')
            _require_elf(file, machine)
        runner = output / 'run-mac-bds.sh'
        if (not runner.is_file() or runner.is_symlink() or
                'CinterBDSNetRelay' not in runner.read_text() or
                not (output / 'CinterBDSNetRelay').is_file()):
            raise RuntimeError('Guest LAN relay launcher missing/stale; run ./Cinter setup all')
    return output


def setup() -> None:
    setup_kernel()
    if platform.system() == 'Darwin':
        # The custom VirtioFS kernel producer needs the w Docker image cache.
        # Once that output is fully built and verified, release w before c
        # starts (both reserve substantial VZ memory). This also ensures a
        # fresh empty Builds can reconstruct the kernel before the handoff.
        # The outer lifecycle watchdog still owns crash/error cleanup.
        from host.mac_bootstrap import stop_managed_colima
        print('[CinterForgeTerminal LinuxKit] Kernel prepared; releasing Windows cross-builder w before c', flush=True)
        stop_managed_colima(BUILDS)
    prepare_guest()
    verify_guest()
    # Official Bedrock server bytes are Setup-owned too, not downloaded by Test.
    _licensed()
    from tests.acceptance_linuxkit import prepare_official_bedrock_archive
    prepare_official_bedrock_archive(REPO)
    print('[CinterForgeTerminal LinuxKit] Setup prepared kernel, signed VZ launcher, LAN relay and guest runtime', flush=True)


def stage_guest() -> Path:
    """Read-only check of the Setup-produced closure (no VM, writes or builds)."""
    return verify_guest()


def _licensed() -> None:
    # CLI developers explicitly accept via terminal; packaged apps use WebView.
    # Both routes store the same receipt, which the existing installer verifies.
    from bedrock_consent import require_bedrock_eula
    require_bedrock_eula(BUILDS, 'mac')


def test(*, offline: bool = True) -> None:
    # Test only consumes the official archive already acquired by Setup.
    # Missing bytes are a hard failure, never a reason to download during Test.
    _licensed()
    cmd = [sys.executable, '-B', str(REPO / 'tests' / 'acceptance_linuxkit.py'), '--repo-root', str(REPO), '--offline']
    _call(*cmd)


def launch(server: Path) -> None:
    verify()
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('The copied LinuxKit VM launcher requires Apple Silicon macOS')
    server = server.expanduser().resolve(strict=True)
    if not server.is_dir():
        raise RuntimeError(f'Bedrock directory missing: {server}')
    required = ('bedrock_server', 'server.properties', '.cinter/glibc-x86_64/ld-linux-x86-64.so.2', '.cinter/cinter-nethernet-probe')
    for filename in required:
        if not (server / filename).is_file():
            raise RuntimeError(f'Bedrock runtime component missing: {server / filename}')
    guest = verify_guest()
    launcher = guest / 'CinterBDSVM'
    if not launcher.is_file():
        raise RuntimeError(f'Missing signed Terminal VM launcher: {launcher}')
    print(f'[CinterForgeTerminal LinuxKit] launching official Bedrock runtime from {server}', flush=True)
    _call('/bin/sh', str(guest / 'run-mac-bds.sh'), str(server))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('setup', 'build', 'stage', 'test', 'launch', 'verify'))
    parser.add_argument('--server', type=Path)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args(argv)
    if not (REPO / 'Cinter').is_file():
        raise RuntimeError('LinuxKit lifecycle must execute from CinterForgeTerminal checkout')
    if args.action == 'setup':
        BUILDS.mkdir(parents=True, exist_ok=True)
    elif not BUILDS.is_dir():
        raise RuntimeError('Setup prerequisites missing; run ./Cinter setup all')
    if args.action == 'setup': setup()
    if args.action == 'build': verify_guest()  # Build consumes Setup output; never recreates it.
    if args.action == 'stage': stage_guest()
    if args.action == 'test': test(offline=args.offline)
    if args.action == 'verify': verify_guest()
    if args.action == 'launch':
        if not args.server:
            parser.error('launch requires --server with the already provisioned official Bedrock directory')
        launch(args.server)
    return 0

if __name__ == '__main__':
    try: raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('[CinterForgeTerminal LinuxKit] ' + str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)
