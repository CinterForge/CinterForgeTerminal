"""CinterForgeXcodeBuild — development Build & Run for the three native products.

PURPOSE: Make the existing Xcode Debug action honor *all* changed authored
C/C++ and Python/JavaScript/HTML/CSS resources in macOS, Linux and Windows.
The Mac Host itself is compiled and restaged by assemble.sh before this owner.

METHODS:
    _native_inputs: Find native source files referenced by the maintained CMake tree.
    _dirty_native_targets: Compare those sources with existing target binaries.
    refresh: Use existing cross Build only if native code changed, then use the
             existing unpacked Build receipts and copy-only Stage for foreign apps.
    main: CLI entry point used by the Xcode build phase.

BOUNDARY: This module belongs to host/lifecycle. It must never import application
runtime or start an installed application. Setup alone provisions dependencies;
this owner does not package ZIPs, notarize, publish, download, or run tests.
The existing Steam destinations and t profile are deliberately unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile

from host import HOST_RELEASE


_NATIVE_SUFFIXES = frozenset(('.c', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.mm', '.m'))
_NATIVE_EXECUTABLES = {
    'linux': 'Intermediates/CinterForgeTerminal/LinuxNative/x64/cmake/CinterForgeTerminal',
    'win': 'Intermediates/CinterForgeTerminal/WindowsCross/x64/cmake/CinterForgeTerminal.exe',
}


def _native_inputs(repo: Path) -> list[Path]:
    """Inputs used by the Linux and Windows CMake application-shell targets."""
    sources = [repo / 'CMakeLists.txt', repo / 'app/build/resources/AppIcon.png']
    for folder in ('app/src', 'app/include', 'app/build/terminal_bootstrap'):
        root = repo / folder
        sources.extend(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in _NATIVE_SUFFIXES)
    return sorted(set(sources))


def _native_identity(repo: Path) -> str:
    """Hash source bytes, not timestamps (ZIP updates can preserve old mtimes)."""
    digest = hashlib.sha256()
    for source in _native_inputs(repo):
        digest.update(source.relative_to(repo).as_posix().encode('utf-8') + b'\0')
        with source.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b''):
                digest.update(chunk)
    return digest.hexdigest()


def _native_receipt(builds: Path) -> Path:
    return builds / 'Intermediates/CinterForgeTerminal/Xcode/native-build-inputs.json'


def _binary_identity(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _dirty_native_targets(repo: Path, builds: Path) -> tuple[str, ...]:
    """Only reuse foreign binaries proven to match the exact native inputs.

    An initial Xcode run without this Build receipt compiles both platforms
    once. Subsequent JS/Python edits retain the proven native binaries; C/C++
    edits force a rebuild even if source ZIP extraction preserved file mtimes.
    """
    try:
        saved = json.loads(_native_receipt(builds).read_text('utf-8'))
    except (OSError, ValueError, TypeError):
        saved = {}
    if saved.get('schema') != 'cinterforge.xcode.native-inputs.v1' or saved.get('source_sha256') != _native_identity(repo):
        return ('win', 'linux')
    previous = saved.get('binaries') or {}
    return tuple(target for target, rel in _NATIVE_EXECUTABLES.items()
                 if _binary_identity(builds / rel) != previous.get(target))


def _save_native_receipt(repo: Path, builds: Path) -> None:
    """Record completed cross-build inputs only after both targets are valid."""
    binaries = {target: _binary_identity(builds / path)
                for target, path in _NATIVE_EXECUTABLES.items()}
    if not all(binaries.values()):
        raise RuntimeError('Native Linux/Windows Build outputs missing after compilation')
    receipt = _native_receipt(builds)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    record = {'schema': 'cinterforge.xcode.native-inputs.v1',
              'source_sha256': _native_identity(repo), 'binaries': binaries}
    fd, temp = tempfile.mkstemp(prefix='NativeInputs-', dir=receipt.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(record, output, sort_keys=True, indent=2)
            output.write('\n')
        Path(temp).replace(receipt)
    finally:
        Path(temp).unlink(missing_ok=True)


def refresh(builds: Path) -> None:
    """Refresh both foreign products from current sources before Xcode runs.

    Native C/C++ changes compile their own x64 binaries first. A Python, JS,
    HTML, CSS, data, or Code-OSS edit leaves those binaries alone and refreshes
    just Build-owned resource snapshots. Stage then copies changed files to
    the exact Steam paths consumed by the Linux/Windows VNC launchers.
    """
    if platform.system() != 'Darwin':
        raise RuntimeError('Xcode foreign-platform Build requires macOS')
    builds = builds.resolve(strict=True)
    repo = Path(__file__).resolve().parents[2]
    if builds != (repo.parent / 'Builds').resolve(strict=True):
        raise RuntimeError('Xcode Build must use its canonical sibling Builds')
    dirty = _dirty_native_targets(repo, builds)
    if dirty:
        print('[Xcode] Native input changed: rebuilding ' + ', '.join(dirty) + ' in the existing t builder', flush=True)
        from host.lifecycle.amd64_backend import managed_amd64_backend
        with managed_amd64_backend(builds, setup=False) as docker:
            if 'win' in dirty:
                from host.lifecycle.cross_build import build
                build(builds)
            if 'linux' in dirty:
                from host.linux.linux_runner import build_terminal
                build_terminal(builds, docker=docker,
                               logfile=builds / 'Logs/CinterForgeTerminal/linux-native-build.log')
        _save_native_receipt(repo, builds)
    else:
        print('[Xcode] Native Linux/Windows binaries current; refreshing authored resource inputs', flush=True)
    from host.lifecycle.unpacked_release import build_four, stage_four
    build_four(builds, release=HOST_RELEASE, targets=('linux', 'win'))
    # This is the existing independent, copy-only Stage implementation, not
    # another Build responsibility smuggled into runtime.
    stage_four(builds, release=HOST_RELEASE, targets=('linux', 'win'))
    print('[Xcode] Linux and Windows installed launcher resources match current Build', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='Refresh existing Linux/Windows Xcode development products')
    parser.add_argument('--builds', type=Path, required=True)
    args = parser.parse_args()
    refresh(args.builds)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
