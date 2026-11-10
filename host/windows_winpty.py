"""Setup-only provisioning of the pinned Windows 3.11 pywinpty binary wheel.

Native MSYS2 winpty.exe is NOT importable as Python's ``winpty`` module. The
Windows GUI application runs its own verified CPython 3.11 interpreter, which
must have this compatible wheel embedded before Build. Never pip-install at
runtime, reach into developer machines, or download dependencies during Build.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile

VERSION = '2.0.15'
WHEEL = f'pywinpty-{VERSION}-cp311-cp311-win_amd64.whl'
SHA256 = '9a6bcec2df2707aaa9d08b86071970ee32c5026e10bcc3cc5f6f391d85baf7ca'
SCHEMA = 'cinterforge.python-host.windows-winpty.v1'
RECEIPT = 'cinterforge-winpty.json'


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _target(root: Path) -> Path:
    if not (root / 'python.exe').is_file():
        raise RuntimeError('Windows Python runtime does not contain python.exe: ' + str(root))
    return root / 'Lib/site-packages'


def verify(root: Path) -> None:
    """Read-only verification for Build/Stage; no network or mutation."""
    site = _target(root)
    receipt = root / RECEIPT
    if not receipt.is_file() or receipt.is_symlink():
        raise RuntimeError('Windows runtime missing pinned pywinpty: rerun ./Cinter setup all')
    info = json.loads(receipt.read_text(encoding='utf-8'))
    if (info.get('schema') != SCHEMA or info.get('wheel') != WHEEL or
            info.get('sha256') != SHA256 or not info.get('files')):
        raise RuntimeError('Windows pywinpty receipt is incomplete or mismatched')
    for rel, digest in info['files'].items():
        name = PurePosixPath(rel)
        if name.is_absolute() or '..' in name.parts or '\\' in rel or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise RuntimeError('Unsafe pinned pywinpty receipt member')
        item = site / rel
        if item.is_symlink() or not item.is_file() or _sha(item) != digest:
            raise RuntimeError('Windows pywinpty runtime modified or incomplete: ' + rel)
    if not any(name.startswith('winpty/') for name in info['files']):
        raise RuntimeError('Pinned pywinpty runtime has no importable winpty package')


def _fetch(builds: Path) -> Path:
    path = builds / 'Intermediates/CinterForgeTerminal/PythonPackages' / WHEEL
    if path.is_file():
        if _sha(path) != SHA256:
            raise RuntimeError('Cached pinned pywinpty wheel SHA-256 mismatch')
        return path
    url = 'https://pypi.org/pypi/pywinpty/' + VERSION + '/json'
    request = urllib.request.Request(url, headers={'User-Agent': 'CinterForgeTerminal-Setup/1.0'})
    with urllib.request.urlopen(request, timeout=90) as response:
        metadata = json.load(response)
    choices = [item for item in metadata.get('urls', []) if item.get('filename') == WHEEL]
    if len(choices) != 1 or choices[0].get('digests', {}).get('sha256') != SHA256:
        raise RuntimeError('Pinned official pywinpty wheel/digest unavailable on PyPI')
    download = choices[0]['url']
    parsed = urllib.parse.urlparse(download)
    if parsed.scheme != 'https' or parsed.hostname != 'files.pythonhosted.org' or not parsed.path.endswith('/'+WHEEL):
        raise RuntimeError('PyPI selected an unexpected pywinpty download origin')
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + '.part')
    try:
        request = urllib.request.Request(download, headers={'User-Agent': 'CinterForgeTerminal-Setup/1.0'})
        with urllib.request.urlopen(request, timeout=180) as response, partial.open('wb') as sink:
            shutil.copyfileobj(response, sink, 1024 * 1024)
        if _sha(partial) != SHA256:
            raise RuntimeError('Downloaded official pywinpty wheel SHA-256 mismatch')
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def ensure(builds: Path, root: Path) -> None:
    """Provision wheel into the target Python only during Setup."""
    if (root / RECEIPT).exists():
        verify(root)
        return
    wheel = _fetch(builds)
    site = _target(root)
    site.mkdir(parents=True, exist_ok=True)
    members = {}
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = PurePosixPath(info.filename)
            if (name.is_absolute() or '..' in name.parts or '\\' in info.filename or
                    not (name.parts[0] == 'winpty' or name.parts[0].endswith('.dist-info')) or
                    (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise RuntimeError('Unsafe pinned pywinpty wheel entry: ' + info.filename)
            if info.filename in members or info.file_size > 32 * 1024 * 1024:
                raise RuntimeError('Duplicate/oversized pinned pywinpty wheel entry')
            members[info.filename] = info
        if not members or not any(name.startswith('winpty/') for name in members):
            raise RuntimeError('Pinned pywinpty wheel lacks its native package')
        # All bytes are verified before affecting the existing Python runtime.
        with tempfile.TemporaryDirectory(prefix='winpty-', dir=builds / 'Intermediates/CinterForgeTerminal') as folder:
            stage = Path(folder)
            digests = {}
            for rel, info in members.items():
                target = stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as stream, target.open('wb') as out:
                    shutil.copyfileobj(stream, out, 1024 * 1024)
                digests[rel] = _sha(target)
            for rel in digests:
                dest = site / rel
                if dest.exists() or dest.is_symlink():
                    raise RuntimeError('Windows Python contains untracked pywinpty files; refusing overwrite: ' + rel)
            for rel in digests:
                dest = site / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage / rel, dest)
            data = {'schema': SCHEMA, 'wheel': WHEEL, 'sha256': SHA256, 'files': digests}
            receipt = root / RECEIPT
            draft = root / (RECEIPT + '.part')
            draft.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            os.replace(draft, receipt)
    verify(root)
    print('[WINDOWS PYTHON] staged and verified pinned pywinpty for the app Session backend', flush=True)
