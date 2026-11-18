"""CraftMaster Git source and native build integration for Terminal.

Terminal Setup acquires Git source and provisions its toolchain. Terminal Build
invokes the *standalone* CraftMaster Cinter lifecycle for mac/linux/win; the
packager consumes its content-verified stages. No published binary fallback,
no Build-time downloads, and no import of CraftMaster implementation sources.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

TARGETS = ('mac', 'linux', 'win')
SOURCES = {
    'cinterforge-craftmaster': ('Cinter', 'CMakeLists.txt', 'lifecycle/driver.py', 'data/MCTemplate.mcworld'),
    'cinterforge-leveldb': ('CMakeLists.txt', 'include/leveldb/db.h'),
}


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def ensure_craftmaster_sources(builds: Path) -> tuple[Path, Path]:
    """Setup-only: clone missing maintained repos, never reset existing work."""
    repo = Path(__file__).resolve().parents[2]
    if builds.resolve() != (repo.parent / 'Builds').resolve():
        raise RuntimeError('CraftMaster requires the same sibling Builds as Terminal')
    resolved = {}
    for name, required in SOURCES.items():
        target = repo.parent / name
        if target.is_symlink():
            raise RuntimeError('Refusing symlinked CraftMaster source: ' + str(target))
        if not target.exists():
            git = shutil.which('git')
            if not git:
                raise RuntimeError('Terminal Setup needs Git to clone ' + name)
            from host.lifecycle.setup_progress import progress_step
            with progress_step('clone ' + name, prefix='[CRAFTMASTER SOURCE]'):
                subprocess.run([git, '-c', 'http.version=HTTP/1.1', 'clone', '--progress',
                                '--depth', '1', '--single-branch', '--no-tags',
                                'https://github.com/CinterForge/' + name + '.git', str(target)],
                               check=True)
        if not target.is_dir() or any(not (target / relative).is_file() for relative in required):
            raise RuntimeError('CraftMaster source checkout is incomplete: ' + str(target))
        resolved[name] = target.resolve(strict=True)
        print('[CRAFTMASTER SOURCE] PRESENT ' + name + ': ' + str(target), flush=True)
    return resolved['cinterforge-craftmaster'], resolved['cinterforge-leveldb']


def setup_receipts_present(builds: Path, targets: tuple[str, ...]) -> bool:
    """Dirty-gate check for Setup's own receipts, never Build/Stage outputs."""
    for target in targets:
        if target not in TARGETS:
            return False
        receipt = builds / 'CinterForgeCraftMaster/Setup' / (target + '.json')
        try:
            doc = json.loads(receipt.read_text(encoding='utf-8'))
            if (doc.get('schema') != 'cinterforge.craftmaster.setup.v1'
                    or doc.get('target') != target):
                return False
        except (OSError, ValueError, TypeError):
            return False
    return True


def _source_roots(builds: Path) -> tuple[Path, Path]:
    repo = Path(__file__).resolve().parents[2]
    roots = (repo.parent / 'cinterforge-craftmaster', repo.parent / 'cinterforge-leveldb')
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError('CraftMaster source missing; rerun Terminal Setup: ' + str(root))
    for source, required in zip(roots, SOURCES.values()):
        if any(not (source / name).is_file() for name in required):
            raise RuntimeError('CraftMaster source incomplete; rerun Setup: ' + str(source))
    return roots


def _mac_lifecycle(builds: Path, action: str) -> None:
    source, leveldb = _source_roots(builds)
    if platform.system() != 'Darwin':
        raise RuntimeError('Mac CraftMaster native target requires Xcode/macOS')
    env = dict(os.environ, CINTER_BUILDS_ROOT=str(builds),
               CINTER_LEVELDB_SOURCE=str(leveldb))
    from host.lifecycle.setup_progress import progress_step
    with progress_step('CraftMaster mac ' + action, prefix='[CRAFTMASTER]'):
        subprocess.run([sys.executable, '-B', str(source / 'Cinter'), action, 'mac'],
                       cwd=source, env=env, check=True)


def _prepare_guest_cmake_generator(builds: Path, target: str) -> None:
    """Keep generated CraftMaster CMake trees compatible with the t guest.

    The AMD64 image provisions Ninja, not GNU Make. A previous failed
    configure may have persisted the default 'Unix Makefiles' generator;
    simply setting CMAKE_GENERATOR=Ninja cannot reuse that cache. Only the
    target's disposable CMake intermediates are invalidated, never sources,
    build receipts, staged assets or any other platform's generated files.
    """
    root = builds / 'CinterForgeCraftMaster' / 'Intermediates' / target
    if root.is_symlink():
        raise RuntimeError('Refusing symlinked CraftMaster CMake tree: ' + str(root))
    if not root.is_dir():
        return
    cache = root / 'CMakeCache.txt'
    if cache.is_symlink():
        raise RuntimeError('Refusing symlinked CraftMaster CMake cache: ' + str(cache))
    if cache.is_file():
        cached = next((line.partition('=')[2].strip()
                       for line in cache.read_text(encoding='utf-8', errors='replace').splitlines()
                       if line.startswith('CMAKE_GENERATOR:INTERNAL=')), '')
    else:
        cached = ''
    if cached == 'Ninja':
        return
    if cache.is_file() or (root / 'CMakeFiles').exists():
        print('[CRAFTMASTER BUILD] Resetting only ' + target +
              ' CMake intermediates (cached generator=' + (cached or 'unknown') +
              '; t guest requires Ninja)', flush=True)
        shutil.rmtree(root)


def _guest_lifecycle(builds: Path, action: str, target: str, *, docker: str | None = None) -> None:
    """Run standalone Cinter in Terminal's pre-existing AMD64 Debian image.

    Both source trees mount readonly; generated files remain under /builds. The
    image is the already provisioned single t guest, not another VM/server.
    """
    if target not in ('linux', 'win'):
        raise ValueError('Unexpected CraftMaster guest platform: ' + target)
    source, leveldb = _source_roots(builds)
    from host.windows.windows_environment import WindowsCrossEnvironment
    from host.lifecycle.amd64_backend import docker_bind_source
    cross = WindowsCrossEnvironment(builds)
    # Setup alone created the pinned Windows/Linux closure. Build may import its
    # saved image but must never download or alter its package recipe.
    if action != 'setup':
        WindowsCrossEnvironment.validate_staged(builds)
    cross.ensure_portable_image()
    docker = docker or cross.docker
    if not docker or not cross.image:
        raise RuntimeError('Setup-provisioned AMD64 compiler image unavailable')
    mounts = [('--mount', f'type=bind,src={docker_bind_source(source)},dst=/craftmaster,readonly'),
              ('--mount', f'type=bind,src={docker_bind_source(leveldb)},dst=/leveldb,readonly'),
              ('--mount', f'type=bind,src={docker_bind_source(builds)},dst=/builds')]
    cmd = [docker, 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none']
    for item in mounts:
        cmd.extend(item)
    cmd.extend(['--workdir', '/craftmaster', '--env', 'CINTER_BUILDS_ROOT=/builds',
                '--env', 'CINTER_LEVELDB_SOURCE=/leveldb',
                '--env', 'PYTHONUNBUFFERED=1'])
    if action == 'build':
        # Debian t guest contains ninja-build and GCC; it deliberately has no
        # GNU Make. CraftMaster's CMake defaults to Unix Makefiles otherwise.
        _prepare_guest_cmake_generator(builds, target)
        cmd.extend(['--env', 'CMAKE_GENERATOR=Ninja'])
    if target == 'win':
        from host.lifecycle.webview import TAG
        toolchain = ('/builds/Toolchains/WebView/' + TAG +
                     '/source/cmake/toolchains/x86_64-w64-mingw32.cmake')
        cmd.extend(['--env', 'CINTER_CRAFTMASTER_WIN_CMAKE_TOOLCHAIN_FILE=' + toolchain])
    if hasattr(os, 'getuid'):
        cmd.extend(['--user', str(os.getuid()) + ':' + str(os.getgid())])
    cmd.extend([cross.image, 'python3', '-u', '/craftmaster/Cinter', action, target])
    from host.lifecycle.setup_progress import progress_step
    with progress_step('CraftMaster ' + target + ' ' + action, prefix='[CRAFTMASTER]'):
        subprocess.run(cmd, check=True)


def setup_craftmaster(builds: Path, *, targets: tuple[str, ...] = TARGETS,
                      guest: bool = False, docker: str | None = None) -> None:
    """Provision only: no Build/Stage or published-output requirements."""
    for target in targets:
        if target not in TARGETS:
            raise ValueError('Unknown CraftMaster target: ' + target)
        if target == 'mac':
            if guest:
                raise RuntimeError('Mac CraftMaster cannot compile in the Linux guest')
            _mac_lifecycle(builds, 'setup')
        else:
            if not guest:
                raise RuntimeError('Linux/Windows CraftMaster Setup requires the managed AMD64 guest')
            _guest_lifecycle(builds, 'setup', target, docker=docker)


def build_craftmaster(builds: Path, *, targets: tuple[str, ...] = TARGETS,
                      docker: str | None = None) -> None:
    """Build three native CLIs from source, then stage their packaging inputs.

    CraftMaster's internal Stage copies the new build into its verified closure;
    Terminal Stage still only installs the completed Terminal release.
    """
    for target in targets:
        for action in ('build', 'stage'):
            if target == 'mac':
                _mac_lifecycle(builds, action)
            else:
                _guest_lifecycle(builds, action, target, docker=docker)
        staged_craftmaster(builds, target)
        print('[CRAFTMASTER BUILD] PASS new native target ' + target, flush=True)


def staged_craftmaster(builds: Path, target: str) -> Path:
    """Verify the native source-built Stage without downloading/building."""
    if target not in TARGETS:
        raise ValueError('Unsupported CraftMaster target: ' + target)
    candidates = (builds / 'CinterForgeCraftMaster/Stage' / target,)
    problems = []
    for root in candidates:
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            manifest_file = root / 'manifest.json'
            if manifest_file.is_symlink() or not manifest_file.is_file():
                raise RuntimeError('No regular Stage manifest')
            manifest = json.loads(manifest_file.read_text('utf-8'))
            if (manifest.get('schema') != 'cinterforge.craftmaster.stage.v1' or
                    manifest.get('platform') != target):
                raise RuntimeError('Wrong platform or Stage schema')
            files = manifest.get('files')
            if not isinstance(files, dict) or not files:
                raise RuntimeError('Stage has no file inventory')
            expected = {'manifest.json', *files}
            # Re-evaluate the manifest and all file bytes, not merely a receipt
            # or directory existence. Never accept symlinks or extra files.
            observed = set()
            for item in root.rglob('*'):
                if item.is_symlink():
                    raise RuntimeError('Stage contains a symlink: ' + str(item))
                if item.is_file():
                    observed.add(item.relative_to(root).as_posix())
                elif not item.is_dir():
                    raise RuntimeError('Stage contains a nonregular entry')
            if observed not in (expected, expected | {'publication.json'}):
                raise RuntimeError('Stage inventory differs from manifest')
            for name, digest in files.items():
                relative = Path(name)
                if (not isinstance(name, str) or not name or relative.is_absolute()
                        or '..' in relative.parts or '\\' in name or
                        not isinstance(digest, str) or len(digest) != 64):
                    raise RuntimeError('Unsafe Stage file member: ' + str(name))
                file = root / relative
                if not file.is_file() or _digest(file) != digest:
                    raise RuntimeError('Stage file hash mismatch: ' + name)
            binary = root / 'bin' / ('CinterForgeCraftMaster.exe' if target == 'win' else 'CinterForgeCraftMaster')
            if not binary.is_file() or binary.relative_to(root).as_posix() not in files:
                raise RuntimeError('CraftMaster binary missing from Stage receipt')
            magic = binary.open('rb').read(4)
            if target == 'win' and magic[:2] != b'MZ':
                raise RuntimeError('CraftMaster Windows binary is not PE')
            if target == 'linux' and magic != b'\x7fELF':
                raise RuntimeError('CraftMaster Linux binary is not ELF')
            if target == 'mac' and magic not in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
                raise RuntimeError('CraftMaster Mac binary is not Mach-O')
            return root
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            problems.append(str(root) + ': ' + str(error))
    raise RuntimeError('Missing verified native CraftMaster ' + target + ' Build Stage. '
                       'Run ./Cinter build all in Terminal after Setup. '
                       + '; '.join(problems))


def packaged_craftmaster_assets(builds: Path, target: str, prefix: str) -> dict[str, Path]:
    """Return prevalidated regular files for Terminal Build's existing inventory."""
    stage = staged_craftmaster(builds, target)
    return {prefix + '/' + file.relative_to(stage).as_posix(): file
            for file in sorted(stage.rglob('*')) if file.is_file()}


def verify_embedded_craftmaster(app_root: Path, target: str) -> Path:
    """Test the installed Terminal release contains the exact native CLI/data.

    This is read-only. It does not accept a source checkout or download anything.
    """
    if target not in TARGETS:
        raise ValueError('Invalid CraftMaster platform')
    base = (app_root / 'Contents/Resources/Builds' if target == 'mac'
            else app_root / 'Builds')
    folder = base / 'CinterForgeCraftMaster' / target
    if folder.is_symlink() or not folder.is_dir():
        raise RuntimeError('Installed Terminal missing CraftMaster: ' + str(folder))
    manifest_file = folder / 'manifest.json'
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise RuntimeError('Installed CraftMaster has no Stage receipt')
    doc = json.loads(manifest_file.read_text('utf-8'))
    if (doc.get('schema') != 'cinterforge.craftmaster.stage.v1'
            or doc.get('platform') != target or not isinstance(doc.get('files'), dict)):
        raise RuntimeError('Installed CraftMaster receipt mismatch')
    files = doc['files']
    if not files:
        raise RuntimeError('Installed CraftMaster receipt is empty')
    for name, digest in files.items():
        relative = Path(name)
        if (not isinstance(name, str) or relative.is_absolute() or '..' in relative.parts or
                '\\' in name or not isinstance(digest, str) or len(digest) != 64):
            raise RuntimeError('Unsafe installed CraftMaster path')
        item = folder / relative
        if item.is_symlink() or not item.is_file() or _digest(item) != digest:
            raise RuntimeError('Installed CraftMaster file failed SHA-256: ' + name)
    binary_name = 'CinterForgeCraftMaster.exe' if target == 'win' else 'CinterForgeCraftMaster'
    binary = folder / 'bin' / binary_name
    if 'bin/' + binary_name not in files or not binary.is_file():
        raise RuntimeError('Installed CraftMaster native CLI missing')
    if not all('data/' + data in files for data in
               ('Resources.json', 'Contours.json', 'MCTemplate.mcworld',
                'world-template.htm', 'zoomify-viewer.min.js', 'preview.py')):
        raise RuntimeError('Installed CraftMaster runtime data is incomplete')
    return binary
