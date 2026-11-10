"""Cache the exact WebView 0.12.0 source needed for the native Shell build.

Acquisition is allowed during Setup, not during an offline packaged rebuild.
The initial source is fetched from the upstream versioned archive; subsequent
builds verify the entire staged source tree and never invoke FetchContent or
reach into another repository. Native SDKs remain OS-specific prerequisites.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile

TAG = '0.12.0'
URL = f'https://github.com/webview/webview/archive/refs/tags/{TAG}.tar.gz'
SCHEMA = 'cinterforge.terminal.webview-source.v1'


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _files(root: Path) -> dict[str, str]:
    result = {}
    for p in root.rglob('*'):
        if p.is_symlink():
            raise RuntimeError(f'WebView source contains nonportable link: {p}')
        if p.is_file() and p.name != 'manifest.json':
            result[p.relative_to(root).as_posix()] = _sha(p)
    if 'CMakeLists.txt' not in result:
        raise RuntimeError('WebView source lacks CMakeLists.txt')
    return dict(sorted(result.items()))


def _extract(archive: Path, target: Path) -> None:
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        if len(members) > 20000 or sum(max(m.size, 0) for m in members) > 300 * 1024 * 1024:
            raise RuntimeError('WebView source archive exceeds expected bounds')
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.name or
                    not (member.isfile() or member.isdir())):
                raise RuntimeError(f'Unsafe WebView source archive entry: {member.name}')
            if not path.parts or not path.parts[0].startswith('webview-'):
                raise RuntimeError(f'Unexpected WebView archive root: {member.name}')
            if any(part.startswith('.') for part in path.parts[1:]):
                # Source trees in Builds are authored inputs, not Git metadata.
                continue
            if member.isfile():
                relative = Path(*path.parts[1:])
                if relative == Path('.'):
                    continue
                out = target / relative
                out.parent.mkdir(parents=True, exist_ok=True)
                stream = tar.extractfile(member)
                if stream is None:
                    raise RuntimeError(f'Unable to extract WebView member: {member.name}')
                with stream, out.open('wb') as dest:
                    shutil.copyfileobj(stream, dest)
    _files(target)


def provision(builds: Path) -> Path:
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise ValueError('WebView must be staged within existing sibling Builds')
    output = builds / 'Toolchains/WebView' / TAG / 'source'
    manifest = output / 'manifest.json'
    if output.exists():
        if not manifest.is_file():
            raise RuntimeError(f'Incomplete WebView source stage: {output}')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if data.get('schema') != SCHEMA or data.get('tag') != TAG or data.get('files') != _files(output):
            raise RuntimeError(f'WebView source stage has been modified: {output}')
        return output
    cache = builds / 'Intermediates/CinterForgeTerminal/WebView' / f'webview-{TAG}.tar.gz'
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.is_file():
        with tempfile.NamedTemporaryFile(prefix='webview-', suffix='.part', dir=cache.parent, delete=False) as out:
            temporary = Path(out.name)
        try:
            request = urllib.request.Request(URL, headers={'User-Agent': 'CinterForgeTerminal/1.0'})
            with urllib.request.urlopen(request, timeout=90) as response, temporary.open('wb') as dest:
                if response.status != 200:
                    raise RuntimeError(f'WebView source download returned HTTP {response.status}')
                shutil.copyfileobj(response, dest)
            temporary.replace(cache)
        finally:
            temporary.unlink(missing_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='WebView-', dir=output.parent) as scratch:
        candidate = Path(scratch) / 'source'
        candidate.mkdir()
        _extract(cache, candidate)
        receipt = {'schema': SCHEMA, 'tag': TAG, 'source': URL, 'archive_sha256': _sha(cache),
                   'files': _files(candidate)}
        (candidate / 'manifest.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        candidate.rename(output)
    return output


# Upstream webview/webview's Windows CMake backend needs the matching WebView2
# SDK *headers*. It can fetch them itself, but that would make offline builds
# nondeterministic. Fetch and seal the exact upstream pin at Setup time instead.
WEBVIEW2_VERSION = '1.0.1150.38'  # webview 0.12.0 cmake/webview.cmake
WEBVIEW2_URL = f'https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/{WEBVIEW2_VERSION}'
WEBVIEW2_SCHEMA = 'cinterforge.terminal.webview2-headers.v1'


def _webview2_files(root: Path) -> dict[str, str]:
    included = root / 'build/native/include'
    if not (included / 'WebView2.h').is_file():
        raise RuntimeError('Upstream WebView2 SDK header WebView2.h missing')
    files = {}
    for p in root.rglob('*'):
        if p.is_symlink():
            raise RuntimeError(f'WebView2 headers include a symlink: {p}')
        if p.is_file() and p.name != 'manifest.json':
            files[p.relative_to(root).as_posix()] = _sha(p)
    return dict(sorted(files.items()))


def provision_webview2(builds: Path) -> Path:
    """Stage upstream WebView2 headers once, then verify every byte offline.

    The upstream CMake FindMSWebView2 module consumes MSWebView2_ROOT pointing
    at this folder. WebView2Loader.dll is not required by webview's default
    built-in loader; the installed Windows WebView2 runtime remains required
    to execute the native window.
    """
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise ValueError('WebView2 headers must be staged within sibling Builds')
    output = builds / 'Toolchains/WebView2' / WEBVIEW2_VERSION / 'source'
    manifest = output / 'manifest.json'
    if output.exists():
        if not manifest.is_file():
            raise RuntimeError(f'Incomplete WebView2 SDK stage: {output}')
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if (data.get('schema') != WEBVIEW2_SCHEMA or
                data.get('version') != WEBVIEW2_VERSION or
                data.get('files') != _webview2_files(output)):
            raise RuntimeError(f'WebView2 SDK stage changed: {output}')
        return output
    cache = builds / 'Intermediates/CinterForgeTerminal/WebView2' / f'Microsoft.Web.WebView2.{WEBVIEW2_VERSION}.nupkg'
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.is_file():
        with tempfile.NamedTemporaryFile(prefix='webview2-', suffix='.part', dir=cache.parent, delete=False) as out:
            temporary = Path(out.name)
        try:
            request = urllib.request.Request(WEBVIEW2_URL, headers={'User-Agent': 'CinterForgeTerminal/1.0'})
            with urllib.request.urlopen(request, timeout=90) as response, temporary.open('wb') as dest:
                if response.status != 200:
                    raise RuntimeError(f'WebView2 NuGet request returned HTTP {response.status}')
                shutil.copyfileobj(response, dest)
            temporary.replace(cache)
        finally:
            temporary.unlink(missing_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='WebView2-', dir=output.parent) as scratch:
        candidate = Path(scratch) / 'source'
        candidate.mkdir()
        with zipfile.ZipFile(cache) as archive:
            members = [m for m in archive.infolist() if m.filename.startswith('build/native/include/')]
            if len(members) > 500 or sum(m.file_size for m in members) > 80 * 1024 * 1024:
                raise RuntimeError('WebView2 SDK header archive exceeds expected size')
            for member in members:
                parts = PurePosixPath(member.filename)
                if (parts.is_absolute() or '..' in parts.parts or '\\' in member.filename or
                        not parts.parts[:3] == ('build', 'native', 'include')):
                    raise RuntimeError(f'Unsafe WebView2 SDK path: {member.filename}')
                if member.is_dir():
                    continue
                # NuGet header files must be regular entries, not symlinks.
                mode = (member.external_attr >> 16) & 0o170000
                if mode not in (0, 0o100000):
                    raise RuntimeError(f'Nonregular WebView2 SDK entry: {member.filename}')
                dest = candidate.joinpath(*parts.parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, dest.open('wb') as target:
                    shutil.copyfileobj(source, target)
        receipt = {'schema': WEBVIEW2_SCHEMA, 'version': WEBVIEW2_VERSION,
                   'source': WEBVIEW2_URL, 'archive_sha256': _sha(cache),
                   'files': _webview2_files(candidate)}
        (candidate / 'manifest.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        candidate.rename(output)
    return output


def upstream_windows_toolchain(builds: Path, webview_source: Path) -> Path:
    """Use webview/webview's pinned MinGW toolchain, with a macOS sysroot shim.

    Its upstream file hardcodes /usr/x86_64-w64-mingw32 (the Linux location).
    On macOS, include that unchanged file then clear only the Linux-specific
    sysroot so the installed compiler uses its own built-in search paths.
    No second copy of WebView or independent linker recipe is introduced.
    """
    official = webview_source / 'cmake/toolchains/x86_64-w64-mingw32.cmake'
    if not official.is_file() or not webview_source.resolve().is_relative_to(builds.resolve()):
        raise RuntimeError('Pinned upstream webview MinGW toolchain missing from Builds')
    if __import__('platform').system() != 'Darwin':
        return official
    generated = builds / 'Intermediates/CinterForgeTerminal/WindowsCross/x64/webview-mingw-toolchain.cmake'
    generated.parent.mkdir(parents=True, exist_ok=True)
    contents = ('# Exact upstream webview/webview MinGW toolchain, with macOS sysroot correction.\n'
                f'include("{official.as_posix()}")\n'
                'set(CMAKE_SYSROOT "")\n')
    if not generated.is_file() or generated.read_text(encoding='utf-8') != contents:
        generated.write_text(contents, encoding='utf-8')
    return generated
