"""Provision and verify Microsoft's offline WebView2 runtime inside real Wine.

The SDK headers only make a Windows PE compilable; they do not provide the
browser engine. Keep the standalone installer and Wine prefix in Builds, never
use Wine's browser substitutes as WebView2 acceptance, and fail closed if the
Microsoft WebView2 registry version is not present after installation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import urllib.request

# Microsoft's official x64 Evergreen *standalone* (offline) installer.
# This is not the network bootstrapper. The downloaded binary is sealed by SHA.
URL = 'https://go.microsoft.com/fwlink/?linkid=2124701'
SCHEMA = 'cinterforge.terminal.webview2-wine-offline-installer.v1'
GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
REGISTRY_KEYS = (
    rf'HKCU\Software\Microsoft\EdgeUpdate\Clients\{GUID}',
    rf'HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{GUID}',
)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _install_archive(builds: Path) -> tuple[Path, str]:
    stage = builds / 'CinterForgeTerminal/Toolchains/WindowsCross/WebView2Runtime'
    stage.mkdir(parents=True, exist_ok=True)
    installer = stage / 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
    manifest = stage / 'manifest.json'
    if installer.is_file() and manifest.is_file():
        receipt = json.loads(manifest.read_text(encoding='utf-8'))
        sha = _hash(installer)
        if (receipt.get('schema') != SCHEMA or receipt.get('source') != URL or
                receipt.get('sha256') != sha or installer.stat().st_size < 10_000_000):
            raise RuntimeError('Staged offline WebView2 installer was modified; refusing to run it')
        return installer, sha
    if installer.exists() or manifest.exists():
        raise RuntimeError(f'Incomplete WebView2 runtime installer stage: {stage}')
    with tempfile.NamedTemporaryFile(prefix='webview2-', suffix='.part', dir=stage,
                                     delete=False) as output:
        temporary = Path(output.name)
    try:
        request = urllib.request.Request(URL, headers={'User-Agent': 'CinterForgeTerminal/1.0'})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as target:
            if response.status != 200:
                raise RuntimeError(f'WebView2 offline runtime HTTP {response.status}')
            shutil.copyfileobj(response, target)
        if temporary.stat().st_size < 10_000_000:
            raise RuntimeError('Microsoft WebView2 standalone installer is too small')
        with temporary.open('rb') as binary:
            signature = binary.read(2)
        if signature != b'MZ':
            raise RuntimeError('Microsoft WebView2 standalone installer is missing or not a PE file')
        sha = _hash(temporary)
        temporary.replace(installer)
        manifest.write_text(json.dumps({'schema': SCHEMA, 'source': URL, 'sha256': sha},
                                       indent=2, sort_keys=True) + '\n', encoding='utf-8')
        return installer, sha
    finally:
        temporary.unlink(missing_ok=True)


def _installed_version(executor, work: Path) -> str | None:
    for key in REGISTRY_KEYS:
        run = executor.execute(['wine', 'reg', 'query', key, '/v', 'pv'],
                               cwd=work, timeout=90)
        if run.returncode == 0:
            match = re.search(r'\bpv\s+REG_SZ\s+(\d+(?:\.\d+){3})\b', run.stdout, re.I)
            if match and any(int(segment) for segment in match.group(1).split('.')):
                return match.group(1)
    return None


def ensure(builds: Path, executor, log) -> dict:
    """Select verified fixed browser under Wine; the JS callback proves launch."""
    from host.windows_fixed_runtime import verify, VERSION
    browser = verify(builds)
    work = builds / 'Run/CinterForgeTerminal/WindowsCross/WebView2Setup'
    work.mkdir(parents=True, exist_ok=True)
    convert = executor.execute(['winepath', '-w', executor.path(browser)],
                               cwd=work, timeout=90)
    if convert.returncode or not convert.stdout.strip():
        raise RuntimeError('Wine failed to resolve the staged WebView2 fixed browser path: '
                           + convert.stdout[-500:])
    windows_path = convert.stdout.strip().splitlines()[-1]
    log.write('Using Microsoft WebView2 Fixed Runtime version=' + VERSION +
              ' browser=' + windows_path + '\n')
    log.flush()
    return {'runtime_version': VERSION, 'execution': 'fixed-browser-staged',
            'browser_path_win': windows_path, 'browser_path_host': str(browser)}
