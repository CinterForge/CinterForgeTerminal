"""Provision the existing portable PowerShell distribution as a CinterForgeTerminal add-on.

The application defines exact URL and SHA-256 in sibling Builds/CinterForgeTerminal/Input/powershell.json.
No bootstrap script, shell profile, package manager or source checkout is modified.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

SCHEMA = 'cinterforge.terminal.powershell-packages.v1'


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _unpack(archive: Path, destination: Path) -> None:
    with (zipfile.ZipFile(archive) if zipfile.is_zipfile(archive) else tarfile.open(archive, 'r:*')) as package:
        entries = package.infolist() if isinstance(package, zipfile.ZipFile) else package.getmembers()
        if len(entries) > 50000:
            raise RuntimeError('PowerShell archive contains too many entries')
        total = sum(e.file_size if isinstance(e, zipfile.ZipInfo) else e.size for e in entries)
        if total > 2 * 1024 ** 3:
            raise RuntimeError('PowerShell archive uncompressed payload exceeds 2 GiB')
        for e in entries:
            path = e.filename if isinstance(e, zipfile.ZipInfo) else e.name
            name = Path(path.replace('\\', '/'))
            mode = (e.external_attr >> 16) & 0o170000 if isinstance(e, zipfile.ZipInfo) else 0
            if name.is_absolute() or '..' in name.parts or name.parts[:1] == ('',):
                raise RuntimeError(f'Unsafe path in PowerShell archive: {path}')
            if isinstance(e, zipfile.ZipInfo):
                if mode in (0o120000, 0o060000, 0o020000):
                    raise RuntimeError('PowerShell ZIP may not contain symlinks/devices')
            elif not (e.isfile() or e.isdir()):
                raise RuntimeError('PowerShell tarball may not contain links/devices')
        package.extractall(destination)  # All paths/types validated above; no symlinks or devices permitted


def provision(builds: Path, system: str, arch: str) -> Path:
    """Reuse installed portable runtime or install exactly a configured immutable release.

    Configuration is stored under Builds rather than cloning a new builder or
    introducing an independently managed global PowerShell installation.
    """
    if system not in ('mac', 'linux', 'win') or arch not in ('arm64', 'x64'):
        raise ValueError('Unsupported PowerShell target')
    root = builds.resolve()
    if root.name != 'Builds':
        raise ValueError('PowerShell runtime must be staged beneath sibling Builds')
    output = root / 'Toolchains/PowerShell' / system / arch
    exe = output / ('pwsh.exe' if system == 'win' else 'pwsh')
    if exe.is_file():
        return exe
    manifest = root / 'CinterForgeTerminal/Input/powershell.json'
    if not manifest.is_file():
        raise RuntimeError(f'Pinned PowerShell manifest missing: {manifest}; no unpinned download attempted')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('schema') != SCHEMA:
        raise RuntimeError('Invalid PowerShell manifest schema')
    entry = data.get('targets', {}).get(system, {}).get(arch, {})
    url, expected = entry.get('url'), entry.get('sha256')
    if not (isinstance(url, str) and url.startswith('https://github.com/PowerShell/PowerShell/releases/download/')
            and isinstance(expected, str) and len(expected) == 64 and all(c in '0123456789abcdefABCDEF' for c in expected)):
        raise RuntimeError(f'PowerShell manifest lacks a pinned GitHub release and SHA-256 for {system}/{arch}')
    if output.exists():
        raise RuntimeError(f'Incomplete existing PowerShell runtime; reconcile before Setup: {output}')
    archive_root = root / 'Intermediates/CinterForgeTerminal/PowerShell' / system / arch
    archive_root.mkdir(parents=True, exist_ok=True)
    archive = archive_root / ('package-' + expected.lower() + ('.zip' if url.lower().endswith('.zip') else '.tar.gz'))
    if not archive.is_file():
        temp = archive.with_suffix(archive.suffix + '.part')
        temp.unlink(missing_ok=True)
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'CinterForgeTerminal/1.0'})
            with urllib.request.urlopen(request, timeout=60) as response, temp.open('wb') as dest:
                if response.status != 200:
                    raise RuntimeError(f'PowerShell download returned HTTP {response.status}')
                shutil.copyfileobj(response, dest)
            if _sha(temp).lower() != expected.lower():
                raise RuntimeError('PowerShell package checksum mismatch')
            os.replace(temp, archive)
        finally:
            temp.unlink(missing_ok=True)
    if _sha(archive).lower() != expected.lower():
        raise RuntimeError('Existing PowerShell archive checksum mismatch')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='PowerShell-', dir=archive_root) as temporary:
        candidate = Path(temporary) / 'Runtime'
        candidate.mkdir()
        _unpack(archive, candidate)
        binary = candidate / ('pwsh.exe' if system == 'win' else 'pwsh')
        if not binary.is_file():
            raise RuntimeError(f'Portable PowerShell archive has no executable at expected root: {binary}')
        if system != 'win':
            binary.chmod(binary.stat().st_mode | 0o111)
        (candidate / 'cinterterminal-package.json').write_text(json.dumps({
            'schema': SCHEMA, 'platform': system, 'arch': arch, 'url': url,
            'sha256': expected.lower(), 'version': entry.get('version'),
        }, indent=2) + '\n', encoding='utf-8')
        candidate.rename(output)
    return exe
