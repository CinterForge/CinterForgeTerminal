"""Provision Microsoft's *actual* pinned WebView2 Fixed Version x64 browser.

This is the redistributable browser runtime, NOT the NuGet headers or the
Evergreen installer. Initial Setup downloads Microsoft's official CAB and
unpacks it inside Builds. Rebuild/launch uses only the verified local files.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request

VERSION = '156.0.4314.58'
CATALOG = 'https://developer.microsoft.com/microsoft-edge/api/webview2'
SCHEMA = 'cinterforge.terminal.webview2-fixed-x64.v1'


def _first_bytes(path: Path, count: int) -> bytes:
    with path.open('rb') as source:
        return source.read(count)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise RuntimeError(f'Fixed WebView2 runtime contains a symlink: {path}')
        if path.is_file() and path.name != 'manifest.json':
            result[path.relative_to(root).as_posix()] = sha(path)
    return result


def verify(builds: Path) -> Path:
    root = builds / 'Toolchains/WebView2/FixedRuntime/x64' / VERSION
    manifest = root / 'manifest.json'
    if not manifest.is_file():
        raise RuntimeError('Required fixed WebView2 browser runtime is not installed: ' + str(root))
    report = json.loads(manifest.read_text(encoding='utf-8'))
    browser = root / report.get('browser', '')
    if (report.get('schema') != SCHEMA or report.get('version') != VERSION or
            not browser.is_file() or _first_bytes(browser, 2) != b'MZ' or
            report.get('files') != _files(root)):
        raise RuntimeError('Fixed WebView2 browser runtime is incomplete or modified: ' + str(root))
    return browser.parent


def _source() -> str:
    request = urllib.request.Request(CATALOG,
                                     headers={'User-Agent': 'CinterForgeTerminal-Setup/1.0'})
    with urllib.request.urlopen(request, timeout=90) as response:
        catalog = json.load(response)
    for version in catalog:
        if version.get('version') != VERSION:
            continue
        for variant in version.get('builds', ()):
            url = variant.get('url', '')
            parsed = urllib.parse.urlparse(url)
            if (variant.get('architecture') == 'x64' and parsed.scheme == 'https' and
                    parsed.hostname == 'msedge.sf.dl.delivery.mp.microsoft.com' and
                    parsed.path.endswith(f'Microsoft.WebView2.FixedVersionRuntime.{VERSION}.x64.cab')):
                return url
    raise RuntimeError(f'Microsoft official WebView2 catalog no longer lists pinned x64 {VERSION}; '
                       'update the pinned dependency intentionally and review licensing')


def ensure(builds: Path, *, executor=None) -> Path:
    builds = builds.resolve()
    if builds.name != 'Builds':
        raise RuntimeError('WebView2 fixed browser must be provisioned into sibling Builds')
    root = builds / 'Toolchains/WebView2/FixedRuntime/x64' / VERSION
    if root.exists():
        return verify(builds)
    cache = builds / 'Intermediates/CinterForgeTerminal/WebView2'
    cache.mkdir(parents=True, exist_ok=True)
    cab = cache / f'Microsoft.WebView2.FixedVersionRuntime.{VERSION}.x64.cab'
    source_receipt = cache / f'fixed-{VERSION}-x64.json'
    if not cab.is_file():
        url = _source()
        partial = cab.with_name(cab.name + '.part')
        try:
            request = urllib.request.Request(url,
                    headers={'User-Agent': 'CinterForgeTerminal-Setup/1.0'})
            with urllib.request.urlopen(request, timeout=240) as response, partial.open('wb') as out:
                shutil.copyfileobj(response, out)
            if partial.stat().st_size < 80_000_000 or _first_bytes(partial, 4) != b'MSCF':
                raise RuntimeError('Microsoft WebView2 fixed CAB is truncated or invalid')
            partial.replace(cab)
            source_receipt.write_text(json.dumps({'url': url, 'sha256': sha(cab),
                 'version': VERSION}, indent=2, sort_keys=True) + '\n')
        finally:
            partial.unlink(missing_ok=True)
    if cab.stat().st_size < 80_000_000 or _first_bytes(cab, 4) != b'MSCF':
        raise RuntimeError('Invalid cached fixed WebView2 CAB')
    if source_receipt.is_file():
        r = json.loads(source_receipt.read_text())
        if r.get('sha256') != sha(cab):
            raise RuntimeError('Downloaded WebView2 CAB changed since first acquisition')
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fixed-webview-', dir=root.parent) as temp:
        candidate = Path(temp) / 'runtime'
        candidate.mkdir()
        if platform.system() == 'Windows':
            command = ['expand', str(cab), '-F:*', str(candidate)]
            result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=1200)
        elif platform.system() == 'Darwin':
            bins = builds / 'CinterForgeTerminal/Toolchains/mac/env/bin'
            seven = next((bins / n for n in ('7zz', '7z') if (bins / n).is_file()), None)
            if not seven:
                raise RuntimeError('Mac Setup must install 7zip into Builds to unpack WebView2 CAB')
            result = subprocess.run([str(seven), 'x', '-y', str(cab), '-o' + str(candidate)],
                                    text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=1200)
        elif executor is not None:
            # Host Linux guest image provisions 7z in Windows-cross.Dockerfile.
            result = executor.execute(['7z', 'x', '-y', executor.path(cab),
                                       '-o' + executor.path(candidate)], cwd=builds, timeout=1200)
        else:
            raise RuntimeError('Linux fixed WebView2 extraction requires the Windows cross engine')
        if result.returncode:
            raise RuntimeError('Fixed WebView2 CAB extraction failed: ' + result.stdout[-900:])
        browsers = list(candidate.rglob('msedgewebview2.exe'))
        if len(browsers) != 1:
            raise RuntimeError('Fixed WebView2 CAB did not yield exactly one browser executable')
        browser = browsers[0]
        if _first_bytes(browser, 2) != b'MZ':
            raise RuntimeError('Fixed WebView2 browser executable is not PE')
        files = _files(candidate)
        if len(files) < 20:
            raise RuntimeError('Fixed WebView2 browser payload is incomplete')
        (candidate / 'manifest.json').write_text(json.dumps({
            'schema': SCHEMA, 'version': VERSION,
            'browser': browser.relative_to(candidate).as_posix(),
            'cab_sha256': sha(cab), 'files': files,
        }, indent=2, sort_keys=True) + '\n')
        candidate.rename(root)
    return verify(builds)
