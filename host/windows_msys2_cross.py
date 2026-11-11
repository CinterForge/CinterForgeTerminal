"""Stage a complete native Windows MSYS2/UCRT64 toolchain from official packages.

MSYS2 (Cygwin-derived) programs cannot run reliably under standard Wine.
Wine is retained for MinGW-generated Windows PE tests, not for MSYS2's Qt
installer, Bash, or pacman.  The official, dated MSYS2 base archive and the
transitive UCRT64 packages are installed in Builds without executing any
Windows installers.  Native Windows acceptance remains a separate gate.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import urllib.request

from host.windows_install import PACKAGES, REQUIRED

SCHEMA = 'cinterforge.terminal.msys2-native-closure.v3'
BASE_URL = 'https://repo.msys2.org/distrib/x86_64/'
UCRT_URL = 'https://repo.msys2.org/mingw/ucrt64/'
MSYS_URL = 'https://repo.msys2.org/msys/x86_64/'


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(url: str, destination: Path, *, sha256: str | None = None) -> Path:
    """Cache by SHA-256; never replace a mismatching existing input."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    receipt = destination.with_name(destination.name + '.json')
    if destination.is_file():
        digest = _sha(destination)
        if sha256 and digest != sha256:
            raise RuntimeError(f'MSYS2 package cache SHA-256 mismatch: {destination}')
        if receipt.is_file():
            saved = json.loads(receipt.read_text(encoding='utf-8'))
            if saved.get('url') != url or saved.get('sha256') != digest:
                raise RuntimeError(f'MSYS2 download receipt mismatch: {receipt}')
            return destination
        # An orphaned file must be independently verified against the signed
        # repository digest, or fetched again if no authoritative hash exists.
        if sha256:
            receipt.write_text(json.dumps({'url': url, 'sha256': digest}, sort_keys=True) + '\n')
            return destination
    if destination.exists() and not destination.is_file():
        raise RuntimeError(f'MSYS2 download destination is not a regular file: {destination}')
    partial = destination.with_name(destination.name + '.part')
    print('[WINDOWS MSYS2] downloading ' + url, flush=True)
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'CinterForgeTerminal-Setup/1.0'})
        with urllib.request.urlopen(request, timeout=90) as response, partial.open('wb') as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        if partial.stat().st_size < 1024:
            raise RuntimeError('MSYS2 download is unexpectedly small: ' + url)
        digest = _sha(partial)
        if sha256 and digest != sha256:
            raise RuntimeError(f'MSYS2 official package SHA-256 mismatch: {url}')
        if destination.is_file():
            if _sha(destination) != digest:
                raise RuntimeError(f'MSYS2 existing input differs from verified redownload: {destination}')
            partial.unlink()
        else:
            partial.replace(destination)
        receipt.write_text(json.dumps({'url': url, 'sha256': digest}, sort_keys=True) + '\n')
        return destination
    finally:
        partial.unlink(missing_ok=True)


def _repo_desc(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    section = ''
    for line in text.splitlines():
        if line.startswith('%') and line.endswith('%'):
            section = line.strip('%')
            result.setdefault(section, [])
        elif line and section:
            result[section].append(line)
    return result


def _repo_packages(database: Path, *, cross=None, builds: Path | None = None) -> dict[str, dict]:
    packages: dict[str, dict] = {}
    unpacked = None
    with database.open('rb') as input_file:
        zstd = input_file.read(4) == b'\x28\xb5\x2f\xfd'
    if zstd:
        if cross is None or builds is None:
            raise RuntimeError('MSYS2 Zstandard repository database requires staged 7z')
        unpacked = database.with_name(database.name + '.tar.part')
        result = cross.execute(
            ['bash', '-o', 'pipefail', '-c',
             '7z x -so "$1" > "$2"', 'bash', cross.path(database), cross.path(unpacked)],
            cwd=builds, timeout=120)
        if result.returncode:
            unpacked.unlink(missing_ok=True)
            raise RuntimeError('Cannot decompress official MSYS2 repository database: '
                               + result.stdout[-900:])
    try:
        return _read_repository_tar(unpacked or database)
    finally:
        if unpacked is not None:
            unpacked.unlink(missing_ok=True)


def _read_repository_tar(database: Path) -> dict[str, dict]:
    packages: dict[str, dict] = {}
    with tarfile.open(database, 'r:*') as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith('/desc'):
                continue
            stream = archive.extractfile(member)
            if stream is None:
                continue
            fields = _repo_desc(stream.read().decode('utf-8'))
            name = fields.get('NAME', [None])[0]
            filename = fields.get('FILENAME', [None])[0]
            digest = fields.get('SHA256SUM', [None])[0]
            if not name or not filename or not digest or not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise RuntimeError(f'MSYS2 repository has incomplete package digest: {member.name}')
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+~@%-]*\.pkg\.tar\.(?:zst|xz|gz)', filename):
                raise RuntimeError(f'MSYS2 repository has unsafe package filename: {filename}')
            packages[name] = {'filename': filename, 'sha256': digest,
                              'version': fields.get('VERSION', [''])[0],
                              'depends': fields.get('DEPENDS', []),
                              'provides': fields.get('PROVIDES', [])}
    if not packages:
        raise RuntimeError('MSYS2 repository database is empty or unreadable')
    return packages


def _dependency_name(spec: str) -> str:
    return re.split(r'[<>=]', spec, maxsplit=1)[0].strip()


def _resolve(packages: dict[str, dict], msys_packages: dict[str, dict] | None = None,
             *, roots: tuple[str, ...] | None = None) -> list[str]:
    """Resolve both UCRT64 and MSYS dependencies, including virtual providers.

    UCRT64 packages (notably Node.js) can depend on native MSYS packages
    such as winpty.  Those are not present in the UCRT64 repository index.
    """
    msys_packages = msys_packages or {}
    providers: dict[str, list[str]] = {}
    for catalog in (packages, msys_packages):
        for name, info in catalog.items():
            for virtual in info['provides']:
                providers.setdefault(_dependency_name(virtual), []).append(name)
    resolved: set[str] = set()
    # The generic resolver serves offline fixtures as well as real Setup.
    # Only Setup's ensure() requests the extra native MSYS pacman closure.
    pending = list(PACKAGES if roots is None else roots)
    while pending:
        needed = _dependency_name(pending.pop())
        if needed in resolved:
            continue
        actual = (needed if needed in packages or needed in msys_packages else
                  next(iter(sorted(providers.get(needed, []))), None))
        if actual is None:
            raise RuntimeError('MSYS2 dependency unavailable in verified repositories: ' + needed)
        if actual in resolved:
            continue
        resolved.add(actual)
        catalog = packages if actual in packages else msys_packages
        pending.extend(catalog[actual]['depends'])
    return sorted(resolved)


def _extract_base(base: Path, root: Path) -> None:
    """Extract official msys64/ tarball safely; recover a stalled Qt install."""
    marker = root / 'base-staged.json'
    if marker.is_file():
        receipt = json.loads(marker.read_text(encoding='utf-8'))
        if (receipt.get('sha256') == _sha(base) and
                all((root / name).is_file() for name in (
                    'usr/bin/pacman.exe', 'usr/bin/bash.exe',
                    'usr/bin/ls.exe', 'usr/bin/tar.exe'))):
            return  # Preserve package extraction progress on retries.
        raise RuntimeError('MSYS2 base installation receipt differs from staged archive')
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='MSYS2-stage-', dir=root.parent) as staging:
        staged = Path(staging)
        with tarfile.open(base, 'r:xz') as archive:
            for member in archive:
                parts = PurePosixPath(member.name).parts
                if not parts or parts[0] != 'msys64' or any(p in ('..', '.') for p in parts):
                    raise RuntimeError('MSYS2 base archive contains a path outside msys64/')
                if len(parts) == 1:
                    continue
                target = staged.joinpath(*parts[1:])
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise RuntimeError(f'MSYS2 base archive member unreadable: {member.name}')
                    with target.open('wb') as output:
                        shutil.copyfileobj(stream, output)
                else:
                    raise RuntimeError(f'MSYS2 base contains unsupported link or file: {member.name}')
        for name in ('usr/bin/pacman.exe', 'usr/bin/bash.exe', 'usr/bin/ls.exe', 'usr/bin/tar.exe'):
            if not (staged / name).is_file():
                raise RuntimeError('Official MSYS2 base archive missing ' + name)
        if root.exists():
            quarantine = root.with_name('MSYS2-incomplete')
            if quarantine.exists():
                raise RuntimeError(f'MSYS2 old incomplete tree already preserved: {quarantine}')
            root.rename(quarantine)  # Keep the timed-out Wine install; never erase it.
        staged.rename(root)
        (root / 'base-staged.json').write_text(
            json.dumps({'sha256': _sha(base)}, sort_keys=True) + '\n', encoding='utf-8')


def _unpack_ucrt(cross, builds: Path, root: Path, archives: list[Path]) -> None:
    """Use the already-staged Linux/amd64 7z+tar tools, never Windows pacman."""
    # One container invocation for all archives avoids repeated Rosetta startup.
    script = ('set -eu; root="$1"; shift; '
              'for archive do '
              '7z x -so "$archive" | tar --no-same-owner -xf - -C "$root" '
              "--exclude='.PKGINFO' --exclude='.MTREE' --exclude='.BUILDINFO' --exclude='.INSTALL'; "
              'done')
    command = ['bash', '-o', 'pipefail', '-c', script, 'bash', cross.path(root),
               *(cross.path(path) for path in archives)]
    result = cross.execute(command, cwd=builds, timeout=1800)
    if result.returncode:
        raise RuntimeError('MSYS2 UCRT64 native package extraction failed: ' + result.stdout[-1500:])


def ensure(builds: Path, cross, base: Path) -> Path:
    root = builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2'
    manifest = root / 'closure.json'
    required = [root / name for name in (
        *REQUIRED, 'usr/bin/winpty.exe',
        'usr/share/makepkg/util/message.sh',
        'usr/share/makepkg/util/parseopts.sh',
        'usr/share/makepkg/utils_fixed_path.sh')]
    if manifest.is_file() and all(path.is_file() for path in required):
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if data.get('schema') != SCHEMA or data.get('packages') != list(PACKAGES):
            raise RuntimeError('Installed Windows UCRT64 closure does not match required package set')
        if data.get('base_sha256') != _sha(base):
            raise RuntimeError('Staged MSYS2 base does not match installed closure')
        cache = root.parent / 'Downloads/UCRT64'
        for name, expected in (('ucrt64.db.tar.zst', data.get('repository_db_sha256')),
                               ('MSYS/msys.db.tar.zst', data.get('msys_repository_db_sha256'))):
            repository = cache / name
            if not repository.is_file() or _sha(repository) != expected:
                raise RuntimeError('Staged MSYS2 repository index missing or modified: ' + str(repository))
        for filename, expected in data.get('downloaded', {}).items():
            cached = cache / filename
            if not cached.is_file() or _sha(cached) != expected:
                raise RuntimeError('Staged MSYS2 package missing or modified: ' + str(cached))
        return root

    _extract_base(base, root)
    cache = root.parent / 'Downloads/UCRT64'
    database = _fetch(UCRT_URL + 'ucrt64.db.tar.zst', cache / 'ucrt64.db.tar.zst')
    msys_database = _fetch(MSYS_URL + 'msys.db.tar.zst', cache / 'MSYS/msys.db.tar.zst')
    packages = _repo_packages(database, cross=cross, builds=builds)
    msys_packages = _repo_packages(msys_database, cross=cross, builds=builds)
    # Setup owns the complete real Windows dependency closure. Neither Stage
    # nor Test invokes ensure() or downloads the missing makepkg utilities.
    names = _resolve(packages, msys_packages, roots=(*PACKAGES, 'pacman'))
    archives = []
    downloaded = {}
    resolved = {}
    for index, name in enumerate(names, 1):
        repo = 'UCRT64' if name in packages else 'MSYS'
        info = (packages if repo == 'UCRT64' else msys_packages)[name]
        prefix = '' if repo == 'UCRT64' else 'MSYS/'
        path = _fetch((UCRT_URL if repo == 'UCRT64' else MSYS_URL) + info['filename'],
                      cache / prefix / info['filename'], sha256=info['sha256'])
        archives.append(path)
        downloaded[prefix + info['filename']] = info['sha256']
        resolved[name] = {'version': info['version'], 'repository': repo}
        print(f'[WINDOWS MSYS2] verified {repo} package {index}/{len(names)}: {name}', flush=True)
    _unpack_ucrt(cross, builds, root, archives)
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError('Native Windows MSYS2 package closure missing: ' + ', '.join(missing))
    for name in ('usr/bin/bash.exe', 'usr/bin/pacman.exe', 'ucrt64/bin/gcc.exe',
                 'ucrt64/bin/g++.exe', 'ucrt64/bin/cmake.exe', 'ucrt64/bin/python.exe'):
        if (root / name).open('rb').read(2) != b'MZ':
            raise RuntimeError('Installed Windows executable has no PE signature: ' + name)
    manifest.write_text(json.dumps({
        'schema': SCHEMA, 'packages': list(PACKAGES), 'base_sha256': _sha(base),
        'repository_db_sha256': _sha(database),
        'msys_repository_db_sha256': _sha(msys_database),
        'downloaded': downloaded, 'resolved': resolved,
        'required_executables': [*REQUIRED, 'usr/bin/winpty.exe'],
        'native_windows_test': 'not-run',
        'wine_msys2_test': 'unsupported-by-upstream',
    }, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return root
