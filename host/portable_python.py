"""Pinned, relocatable Terminal Python runtime for macOS, Linux and Windows.

This is intentionally distinct from Server's and Converter's requirements-driven
venvs. The Terminal bootstrap needs only the CPython standard library; it must
never manufacture a release runtime by copying a developer virtualenv or
linking to ~/.pyenv. Setup may download an exact checked archive; a packaged
application contains the verified result and never downloads it at launch.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

SCHEMA = 'cinterforge.python-host.environment.v1'
VERSION = '3.11.18'
UPSTREAM = '20261026'
ROOT_URL = f'https://github.com/astral-sh/python-build-standalone/releases/download/{UPSTREAM}/'
# SHA-256 values are from the upstream 20261026 release SHA256SUMS.
PACKAGES = {
    ('mac', 'arm64'): ('aarch64-apple-darwin', 'cba3455e18516fbe8668cff2820c9b6a53447c552d612443c2b2fb60d390a908'),
    ('mac', 'x64'):   ('x86_64-apple-darwin', 'a85474bcc78a98ca6b0070d8d615b0494b912b960239e6f18cc12d648f2dc270'),
    ('linux', 'arm64'): ('aarch64-unknown-linux-gnu', 'a344c887b04a309f292cac1ef7c7a85e39e11932ec301d635258c569d830861b'),
    ('linux', 'x64'):   ('x86_64-unknown-linux-gnu', 'a87ee63646c8f8438b7af3954b2135ed2af91d033c631f12a8f20af903046d06'),
    ('win', 'arm64'): ('aarch64-pc-windows-msvc', '9ee1a7cae577c62680659d1d8a45f6bb199c3b2692fe7f05480be20be5df2317'),
    ('win', 'x64'):   ('x86_64-pc-windows-msvc', '75cb501dc11c7b55c6b447c58e4910bce5b1f609f1017328b4f3a858dfaf5428'),
}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as reader:
        for chunk in iter(lambda: reader.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _binary(root: Path, system: str) -> Path:
    if system == 'win':
        return root / 'python.exe'
    return root / 'bin/python'


def _validate_package(package: tarfile.TarFile) -> None:
    members = package.getmembers()
    if len(members) > 120000 or sum(max(0, m.size) for m in members) > 3 * 1024 ** 3:
        raise RuntimeError('Portable Python archive exceeds the permitted size')
    seen = set()
    for m in members:
        path = PurePosixPath(m.name)
        if (path.is_absolute() or '..' in path.parts or path.parts[:1] != ('python',)
                or '\\' in m.name or not (m.isfile() or m.isdir() or m.issym() or m.islnk())):
            raise RuntimeError(f'Unsafe portable Python archive member: {m.name}')
        if m.name in seen:
            raise RuntimeError(f'Duplicate portable Python archive member: {m.name}')
        seen.add(m.name)
        if m.issym() or m.islnk():
            target = PurePosixPath(m.linkname)
            if target.is_absolute() or '\\' in m.linkname:
                raise RuntimeError(f'External portable Python link: {m.name}')
            # A symbolic link is relative to the member directory, while a tar
            # hardlink is relative to the archive's root.
            base = path.parent if m.issym() else PurePosixPath('.')
            stack = []
            for part in (base / target).parts:
                if part == '..':
                    if not stack:
                        raise RuntimeError(f'Escaping portable Python link: {m.name}')
                    stack.pop()
                elif part not in ('.', ''):
                    stack.append(part)
            if not stack or stack[0] != 'python':
                raise RuntimeError(f'Escaping portable Python link: {m.name}')


def _extract_portable(archive: Path, destination: Path) -> None:
    """Flatten internal symlinks; omit only cyclic optional terminfo aliases.

    The upstream install-only runtime can contain circular terminfo aliases
    (e.g. ncr260vt300wpp). `copytree(..., symlinks=False)` follows those and
    raises ELOOP. A consumer runtime must have no symlinks, but the real
    interpreter, standard library and nonoptional resources must remain intact.
    """
    with tempfile.TemporaryDirectory(prefix='python-unpack-', dir=destination.parent) as scratch:
        extract = Path(scratch) / 'source'
        extract.mkdir()
        with tarfile.open(archive, 'r:gz') as pkg:
            _validate_package(pkg)
            members = pkg.getmembers()
            aliases = {m.name for m in members if m.issym() or m.islnk()}
            # TarFile.extractall() follows or applies metadata to links while
            # unpacking. The pinned upstream terminfo archive contains cycles,
            # so it can fail with ELOOP before our flattening pass sees them.
            # Materialize regular bytes first, and create links LAST. Never
            # write a tar member through a directory supplied as a link.
            for member in members:
                parent = PurePosixPath(member.name).parent
                while str(parent) not in ('.', ''):
                    if str(parent) in aliases:
                        raise RuntimeError(
                            f'Portable Python member nested under archive link: {member.name}')
                    parent = parent.parent
            for member in members:
                if not (member.isdir() or member.isfile()):
                    continue
                output = extract / member.name
                output.parent.mkdir(parents=True, exist_ok=True)
                optional_terminfo = PurePosixPath(member.name).parts[:3] == (
                    'python', 'share', 'terminfo')
                try:
                    if member.isdir():
                        output.mkdir(exist_ok=True)
                    else:
                        with pkg.extractfile(member) as source, output.open('xb') as sink:
                            shutil.copyfileobj(source, sink)
                except FileExistsError as exc:
                    # The pinned upstream terminfo tree has aliases whose
                    # normalized names can collide with ordinary entries or
                    # implicit parent directories. Its descriptions are
                    # optional, but never discard a required runtime file.
                    if optional_terminfo and not output.is_symlink():
                        continue
                    raise RuntimeError(
                        f'Conflicting portable Python archive member: {member.name}') from exc
                output.chmod(member.mode & 0o777)
            for member in members:
                if not (member.issym() or member.islnk()):
                    continue
                output = extract / member.name
                output.parent.mkdir(parents=True, exist_ok=True)
                optional_terminfo = PurePosixPath(member.name).parts[:3] == (
                    'python', 'share', 'terminfo')
                # Materializing regular files also creates the directories
                # they need. Some optional upstream terminfo aliases use
                # the very same normalized paths. Prefer existing content;
                # do not let an alias replace it or fail Python packaging.
                if output.exists() or output.is_symlink():
                    if optional_terminfo:
                        continue
                    raise RuntimeError(
                        f'Conflicting portable Python archive link: {member.name}')
                if member.issym():
                    target = member.linkname
                else:
                    # Tar hardlink names are archive-root-relative; filesystem
                    # symlinks (which are flattened below) are parent-relative.
                    target = posixpath.relpath(member.linkname,
                                               posixpath.dirname(member.name))
                output.symlink_to(target)
        root = extract / 'python'
        if not root.is_dir() or root.is_symlink():
            raise RuntimeError('Portable Python archive lacks ordinary python/ root')
        real_root = root.resolve(strict=True)
        count = 0
        copied_bytes = 0

        def flatten(source: Path, output: Path, ancestors: frozenset[Path],
                    relative: Path) -> None:
            nonlocal count, copied_bytes
            count += 1
            if count > 180000:
                raise RuntimeError('Portable Python expanded symlink closure is too large')
            optional_terminfo = relative.parts[:2] == ('share', 'terminfo')
            try:
                resolved = source.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                # Some upstream terminfo alias chains are circular or dangle.
                # They are non-executable optional terminal descriptions, not
                # Python code. Do not ship an unusable link or fail the app.
                if source.is_symlink() and optional_terminfo and (
                    isinstance(exc, FileNotFoundError) or
                    isinstance(exc, RuntimeError) or
                    isinstance(exc, OSError) and exc.errno == errno.ELOOP
                ):
                    return
                raise RuntimeError(f'Unresolvable portable Python member: {relative}') from exc
            if not resolved.is_relative_to(real_root):
                raise RuntimeError(f'Portable Python link leaves runtime: {relative}')
            if resolved.is_dir():
                if resolved in ancestors:
                    if optional_terminfo:
                        return  # circular optional directory alias
                    raise RuntimeError(f'Circular portable Python directory: {relative}')
                output.mkdir(parents=True, exist_ok=True)
                visited = ancestors | {resolved}
                for child in sorted(resolved.iterdir()):
                    flatten(child, output / child.name, visited, relative / child.name)
            elif resolved.is_file():
                copied_bytes += resolved.stat().st_size
                if copied_bytes > 4 * 1024 ** 3:
                    raise RuntimeError('Portable Python expanded symlink closure exceeds 4 GiB')
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(resolved, output)
            else:
                raise RuntimeError(f'Unsupported portable Python member: {relative}')

        flatten(root, destination, frozenset(), Path('.'))
        if any(entry.is_symlink() for entry in destination.rglob('*')):
            raise RuntimeError('Portable Python runtime must be symlink-free')


def _inspect(root: Path, system: str, version: str) -> dict:
    exe = _binary(root, system)
    if not exe.is_file() or exe.is_symlink():
        raise RuntimeError(f'Portable Python executable missing: {exe}')
    code = ('import json,sys,sysconfig,os;print(json.dumps({' 
            '"version":".".join(map(str,sys.version_info[:3])),'
            '"executable":os.path.realpath(sys.executable),'
            '"prefix":os.path.realpath(sys.prefix),'
            '"base_prefix":os.path.realpath(sys.base_prefix),'
            '"stdlib":os.path.realpath(sysconfig.get_path("stdlib"))}))')
    run = subprocess.run([str(exe), '-B', '-I', '-c', code], capture_output=True,
                         text=True, check=False, timeout=45,
                         env=dict(os.environ, PYTHONPATH='', PYTHONNOUSERSITE='1'))
    if run.returncode:
        raise RuntimeError(f'Portable Python failed runtime smoke: {run.stderr[-800:]}')
    report = json.loads(run.stdout.strip())
    if report.get('version') != version:
        raise RuntimeError(f'Portable Python version is {report.get("version")}; expected {version}')
    for name in ('executable', 'prefix', 'base_prefix', 'stdlib'):
        if not Path(report[name]).resolve().is_relative_to(root.resolve()):
            raise RuntimeError(f'Portable Python {name} depends on external path: {report[name]}')
    return report


def _download_verified(cache: Path, target: str, expected: str) -> None:
    if cache.exists():
        if _sha(cache) != expected:
            raise RuntimeError(f'Cached portable Python checksum mismatch: {cache}')
        return
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='download-', suffix='.part',
                                     dir=cache.parent, delete=False) as f:
        temporary = Path(f.name)
    try:
        request = urllib.request.Request(ROOT_URL + target,
                                         headers={'User-Agent': 'CinterForgeTerminal/1.0'})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as out:
            if response.status != 200:
                raise RuntimeError(f'Pinned Python download returned HTTP {response.status}')
            shutil.copyfileobj(response, out)
        if _sha(temporary) != expected:
            raise RuntimeError('Pinned portable Python download SHA-256 mismatch')
        os.replace(temporary, cache)
    finally:
        temporary.unlink(missing_ok=True)


def provision(builds: Path, system: str, arch: str, *, name: str = 'cinterterminalenv') -> Path:
    """Provision and execute the target-native runtime; never accept a foreign stub."""
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise ValueError('Portable Python must be provisioned in existing sibling Builds')
    try:
        triplet, expected = PACKAGES[(system, arch)]
    except KeyError as exc:
        raise RuntimeError(f'Unsupported portable Python target: {system}/{arch}') from exc
    if not (name == 'cinterterminalenv'):
        raise ValueError('This pinned standard-library bootstrap is not a product Python environment')
    root = builds / 'Python/Environments' / system / arch / name
    receipt = root / 'cinterforge-requirements.json'
    if root.exists():
        if not receipt.is_file():
            raise RuntimeError(f'Incomplete existing portable Python, refusing overwrite: {root}')
        data = json.loads(receipt.read_text(encoding='utf-8'))
        if (data.get('schema') != SCHEMA or data.get('platform') != system or
                data.get('arch') != arch or data.get('name') != name or
                data.get('python') != VERSION or data.get('source_sha256') != expected):
            raise RuntimeError(f'Existing portable Python does not match pinned release: {root}')
        _inspect(root, system, VERSION)
        if system == 'win':
            from host.windows_winpty import ensure as ensure_winpty
            ensure_winpty(builds, root)
        return root
    filename = f'cpython-{VERSION}+{UPSTREAM}-{triplet}-install_only.tar.gz'
    cache = builds / 'Intermediates/CinterForgeTerminal/PythonPackages' / filename
    _download_verified(cache, filename, expected)
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cinterterminalenv-', dir=root.parent) as scratch:
        candidate = Path(scratch) / name
        _extract_portable(cache, candidate)
        exe = _binary(candidate, system)
        if system != 'win':
            if not exe.is_file():
                # The upstream `install_only` layout has bin/python3; keep an
                # independent executable copy, never a release symlink.
                original = candidate / 'bin/python3'
                if not original.is_file():
                    raise RuntimeError('Portable Python distribution lacks bin/python3')
                shutil.copy2(original, exe)
            exe.chmod(exe.stat().st_mode | 0o111)
        _inspect(candidate, system, VERSION)
        receipt_data = {'schema': SCHEMA, 'name': name, 'platform': system,
                        'arch': arch, 'python': VERSION, 'source_url': ROOT_URL + filename,
                        'source_sha256': expected, 'distribution': 'python-build-standalone-install-only'}
        receipt_in_candidate = candidate / 'cinterforge-requirements.json'
        receipt_in_candidate.write_text(json.dumps(receipt_data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        candidate.rename(root)
    _inspect(root, system, VERSION)
    if system == 'win':
        from host.windows_winpty import ensure as ensure_winpty
        ensure_winpty(builds, root)
    return root
