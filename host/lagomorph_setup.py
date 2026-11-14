"""Stage the owner's Macintosh Lagomorph 1 data for a Quadra 650 emulator.

Never downloads game/OS assets. Only reads a user-owned source within sibling
Builds, preserves classic resource forks using macOS ditto, and creates a
separate HFS/ISO content disk for Infinite Mac. No Apple OS disk is repackaged.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

SCHEMA = 'cinterforge.lagomorph.stage.v1'
RELATIVE_STAGE = Path('Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph')
IMAGE_NAME = 'Lagomorph-Quadra650.iso'
APPLICATION_FLOPPY = 'Lagomorph-Application-1440K.img'
BATTLES_FLOPPY = 'Lagomorph-Battles-1440K.img'
FLOPPY_BYTES = 1440 * 1024
MANIFEST_NAME = 'manifest.json'


def _macos_xattr_get(path: Path, name: str) -> bytes:
    """Read macOS extended attributes even when the selected Python omits os.getxattr.

    Darwin's libc has a six-argument getxattr, unlike Linux's four-argument
    ABI. Use its native API, not a huge hex string passed through xattr(1).
    """
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    getter = libc.getxattr
    getter.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                       ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int)
    getter.restype = ctypes.c_ssize_t
    path_bytes, name_bytes = os.fsencode(path), name.encode('utf-8')
    size = getter(path_bytes, name_bytes, None, 0, 0, 0)
    if size < 0:
        raise OSError(ctypes.get_errno(), f'getxattr {name}: {path}')
    if size == 0:
        return b''
    buffer = ctypes.create_string_buffer(size)
    count = getter(path_bytes, name_bytes, buffer, size, 0, 0)
    if count < 0:
        raise OSError(ctypes.get_errno(), f'getxattr {name}: {path}')
    return buffer.raw[:count]


def _macos_xattr_set(path: Path, name: str, value: bytes) -> None:
    """Write resource forks through Darwin libc without Python xattr support."""
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    setter = libc.setxattr
    setter.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                       ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int)
    setter.restype = ctypes.c_int
    payload = ctypes.create_string_buffer(value)
    result = setter(os.fsencode(path), name.encode('utf-8'), payload,
                    len(value), 0, 0)
    if result != 0:
        raise OSError(ctypes.get_errno(), f'setxattr {name}: {path}')


def _getxattr(path: Path, name: str) -> bytes:
    getter = getattr(os, 'getxattr', None)
    if callable(getter):
        return getter(path, name)
    if platform.system() == 'Darwin':
        return _macos_xattr_get(path, name)
    raise RuntimeError('Extended attributes unavailable on this platform')


def _setxattr(path: Path, name: str, value: bytes) -> None:
    setter = getattr(os, 'setxattr', None)
    if callable(setter):
        setter(path, name, value)
    elif platform.system() == 'Darwin':
        _macos_xattr_set(path, name, value)
    else:
        raise RuntimeError('Extended attributes unavailable on this platform')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _source(builds: Path, configured: str | None) -> Path:
    if configured:
        source = Path(configured).expanduser().resolve()
        if not source.exists() or not within(source, builds):
            raise ValueError(f'Lagomorph source must exist within sibling Builds: {source}')
        return source
    # The user-owned canonical tree is already staged in sibling Builds;
    # neither an app-local Input folder nor a second source copy is required.
    # Do not require a second copy under CinterForgeTerminal/Input.
    roots = [builds / 'Lagomorph/Lagomorph 1.0/Lagomorph',
             builds / 'Lagomorph/Lagomorph 1.0',
             builds / 'InteractiveFiction/Lagomorph',
             builds / 'CreativeSuite/Lagomorph',
             builds / 'InteractiveFiction/Projects/Lagomorph']
    for candidate in roots:
        if candidate.is_dir():
            return candidate
        if candidate.is_file() and candidate.suffix.lower() == '.zip':
            return candidate
        archive = Path(str(candidate) + '.zip')
        if archive.is_file():
            return archive
    # Fallback only to pre-existing canonical asset areas; never ask users to
    # put a duplicate original ZIP into the Terminal Input tree.
    for directory in (builds / 'InteractiveFiction', builds / 'CreativeSuite/Artwork'):
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if child.name.lower() in ('lagomorph.zip', 'lagomorph 1.zip', 'lagomorph'):
                return child
    raise FileNotFoundError('Lagomorph source not found under Builds/Lagomorph/Lagomorph 1.0/Lagomorph/; '
                            'pass --source <path-inside-Builds> only for a deliberate override. '
                            'Include the game, Battles 1–6, icon and Lagomorph Manual.pdf.')


def _validate_zip(path: Path) -> None:
    # Reject traversal, symlinks and nested directories masquerading as archives.
    with zipfile.ZipFile(path) as archive:
        names = archive.infolist()
        if not names:
            raise RuntimeError(f'Empty Lagomorph source: {path}')
        for info in names:
            member = Path(info.filename.replace('\\', '/'))
            if (member.is_absolute() or '..' in member.parts or '\x00' in info.filename
                    or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise RuntimeError(f'Unsafe archive entry: {info.filename}')


def _appledouble_metadata(payload: bytes, origin: str) -> tuple[bytes, bytes]:
    """Recover classic Finder info and resource fork from macOS ZIP sidecars."""
    if len(payload) < 26:
        raise RuntimeError(f'Truncated AppleDouble metadata in {origin}')
    magic, version = struct.unpack_from('>II', payload)
    if (magic, version) != (0x00051607, 0x00020000):
        raise RuntimeError(f'Invalid AppleDouble metadata in {origin}')
    count = struct.unpack_from('>H', payload, 24)[0]
    if len(payload) < 26 + 12 * count:
        raise RuntimeError(f'Truncated AppleDouble entries in {origin}')
    entries = {}
    for index in range(count):
        entry_id, offset, size = struct.unpack_from('>III', payload, 26 + 12 * index)
        if offset < 26 + 12 * count or offset + size > len(payload) or entry_id in entries:
            raise RuntimeError(f'Invalid AppleDouble entry {entry_id} in {origin}')
        entries[entry_id] = payload[offset:offset + size]
    finder = entries.get(9, b'')[:32]
    fork = entries.get(2, b'')
    if finder and len(finder) != 32:
        raise RuntimeError(f'Incomplete Finder metadata in {origin}')
    return finder, fork


def _recover_zip_forks(archive_path: Path, extracted: Path) -> None:
    """Restore AppleDouble forks explicitly; modern ZIP tools can lose them."""
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.namelist():
            if not item.startswith('__MACOSX/') or '/._' not in item:
                continue
            relative = item[len('__MACOSX/'):]
            parts = relative.split('/')
            if parts[-1].startswith('._'):
                parts[-1] = parts[-1][2:]
            else:
                continue
            target = extracted.joinpath(*parts)
            if not target.is_file() or not within(target, extracted):
                raise RuntimeError(f'Original Mac file missing for resource fork {item}')
            finder, fork = _appledouble_metadata(archive.read(item), item)
            if finder:
                _setxattr(target, 'com.apple.FinderInfo', finder)
            if fork:
                _setxattr(target, 'com.apple.ResourceFork', fork)


def _classic_application_metadata(path: Path) -> tuple[bytes, bytes]:
    """Verify a real resource-fork classic executable, not an empty document."""
    try:
        finder = _getxattr(path, 'com.apple.FinderInfo')
        fork = _getxattr(path, 'com.apple.ResourceFork')
    except OSError as exc:
        raise RuntimeError(f'Lagomorph application lost FinderInfo/resource fork: {path}') from exc
    if len(finder) < 8 or finder[:4] != b'APPL' or not fork:
        raise RuntimeError(f'Lagomorph must be a classic APPL with a resource fork: {path}; '
                           f'type={finder[:4]!r}, resource_bytes={len(fork)}. '
                           'Use the original Lagomorph.zip containing __MACOSX/._Lagomorph.')
    return finder, fork


def _unpack(source: Path, output: Path) -> None:
    if source.is_file():
        if source.suffix.lower() != '.zip':
            raise RuntimeError(f'Expected a source directory or .zip: {source}')
        _validate_zip(source)
        if platform.system() == 'Darwin':
            subprocess.run(['ditto', '-x', '-k', str(source), str(output)], check=True)
            _recover_zip_forks(source, output)
        else:
            # Inspection/test-only fallback; only macOS can stage a real HFS disk.
            with zipfile.ZipFile(source) as archive:
                archive.extractall(output)
    else:
        if source.is_symlink():
            raise ValueError(f'Refusing symlinked Lagomorph source: {source}')
        if platform.system() == 'Darwin':
            subprocess.run(['ditto', '--rsrc', str(source), str(output)], check=True)
        else:
            shutil.copytree(source, output, dirs_exist_ok=True)


def _files(root: Path):
    return sorted(p for p in root.rglob('*') if p.is_file()
                  and '__MACOSX' not in p.relative_to(root).parts and not p.name.startswith('._'))


def _choose_inputs(root: Path):
    paths = _files(root)
    manuals = [p for p in paths if p.name.casefold() in
               ('lagomorph manual.pdf', 'lagomorph manual text.pdf', 'lagomorph_manual.pdf')]
    if not manuals:
        manuals = [p for p in paths if p.suffix.lower() == '.pdf' and 'lagomorph' in p.name.lower()]
    if not manuals:
        raise FileNotFoundError('Source lacks Lagomorph Manual.pdf; source extraction has not been staged')
    manual = sorted(manuals, key=lambda x: len(x.parts))[0]
    dirs = [p for p in root.rglob('*') if p.is_dir() and p.name.casefold() == 'battles']
    battles = sorted(dirs, key=lambda x: len(x.parts))[0] if dirs else None
    if battles is None:
        battle_files = [p for p in paths if p.name.lower().startswith('battle')
                        and p != manual and p.suffix.lower() not in ('.pdf', '.zip')]
        numbered = [p for p in battle_files if any(str(n) in p.stem for n in range(1, 7))]
        if len(numbered) < 6:
            raise FileNotFoundError('Source lacks Battles directory or all Battle 1–6 files')
        battles = sorted(numbered, key=lambda x: x.name)
    game_candidates = [p for p in paths if p.name.casefold() in ('lagomorph', 'lagomorph 1', 'lagomorph i')
                       and p != manual and not p.name.startswith('._')]
    game_dirs = [p for p in root.rglob('*') if p.is_dir() and p.name.casefold() in ('lagomorph 1.app', 'lagomorph.app')]
    if game_dirs:
        game = sorted(game_dirs, key=lambda x: len(x.parts))[0]
    elif game_candidates:
        game = sorted(game_candidates, key=lambda x: len(x.parts))[0]
    else:
        raise FileNotFoundError('Source lacks a Lagomorph 1 application (resource forks must be preserved)')
    # The icon is normally contained in the app's resource fork; do not create a
    # fake icon if the source supplies only a data fork. Preserve related Icon\r.
    icons = [p for p in paths if (('lagomorph' in p.name.casefold() and 'icon' in p.name.casefold())
             or p.name.casefold().startswith('icon')) and p != game]
    return game, battles, manual, icons


def _copy_classic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == 'Darwin':
        subprocess.run(['ditto', '--rsrc', str(source), str(destination)], check=True)
    elif source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)



def _classic_hfs_signature(image: Path) -> None:
    """Require an HFS Standard MDB, not an HFS+ volume or an ISO9660 view.

    In a partitionless raw HFS volume the MDB signature is at byte 1024.
    This is intentionally a hard failure: publishing an unreadable image would
    replace the working public object with broken media.
    """
    with image.open('rb') as stream:
        stream.seek(1024)
        signature = stream.read(2)
    if signature != b'BD':
        raise RuntimeError('Lagomorph image is not classic HFS (expected HFS Standard MDB '
                           'signature BD at byte 1024, found '
                           f'{signature.hex() or "EOF"}). Mac OS 7.6 cannot read HFS+; '
                           'previous staged ISO was preserved.')


# Debian Bookworm provides hfsutils 3.2.6 for AMD64. Trixie (our pinned
# Windows/Linux image) omits the package, so load its 79 KB, checksum-verified
# Bookworm package into the SAME t container rather than changing the image.
# The checksum is Debian's published package SHA-256.
HFS_DEB_NAME = 'hfsutils_3.2.6-15_amd64.deb'
HFS_DEB_URL = 'https://deb.debian.org/debian/pool/main/h/hfsutils/' + HFS_DEB_NAME
HFS_DEB_SHA256 = '068f90fac9397de360e4673e117d0e3ee7e8c576649e6f2dc4752ba308f7d0c8'


def _hfs_package(builds: Path) -> Path:
    """Setup-owned download only; no Homebrew or global host installation."""
    folder = builds / 'CinterForgeTerminal/Toolchains/Linux/hfsutils/3.2.6-15'
    folder.mkdir(parents=True, exist_ok=True)
    package = folder / HFS_DEB_NAME
    if package.is_file() and sha256(package) == HFS_DEB_SHA256:
        return package
    temporary = package.with_suffix('.candidate')
    print('[LAGOMORPH] Downloading pinned Linux hfsutils 3.2.6 (78 KB)', flush=True)
    try:
        with urllib.request.urlopen(HFS_DEB_URL, timeout=45) as response, temporary.open('wb') as output:
            shutil.copyfileobj(response, output)
        if temporary.stat().st_size != 79532 or sha256(temporary) != HFS_DEB_SHA256:
            raise RuntimeError('Debian hfsutils package SHA-256/size verification failed')
        temporary.replace(package)
    finally:
        temporary.unlink(missing_ok=True)
    return package


def _stage_hfs_linux(builds: Path, temp: Path,
                     jobs: list[tuple[Path, Path, str, int | None, str | None]]) -> None:
    """Mac encodes resource forks; the existing Debian AMD64 t guest writes HFS.

    One Docker call prepares all three volumes, avoids rebuilding Wine/MSYS2,
    and keeps the published media fully offline and unchanged until verified.
    """
    if os.environ.get('DOCKER_CONTEXT') != 'colima-t':
        raise RuntimeError('Lagomorph HFS Setup requires the existing AMD64 t guest')
    package = _hfs_package(builds)
    from host.amd64_backend import docker_bind_source
    from host.three_samples import _docker_image
    from host.windows_environment import _run
    docker = builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/docker'
    if not docker.is_file():
        raise RuntimeError('Setup-owned t Docker CLI is missing')
    log = builds / 'Logs/CinterForgeTerminal/lagomorph-linux-hfs.log'
    image = _docker_image(builds, str(docker), log)
    def rel(path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_relative_to(builds):
            raise RuntimeError('Lagomorph input must reside under Builds: ' + str(path))
        return resolved.relative_to(builds).as_posix()
    specs = []
    for index, (content, result, label, capacity, application_path) in enumerate(jobs):
        files = _files(content)
        total = sum(p.stat().st_size + (len(_getxattr(p, 'com.apple.ResourceFork'))
                    if _has_resource_fork(p) else 0) for p in files)
        size = capacity if capacity is not None else max(32, (total * 2 + 16 * 1048576 + 1048575) // 1048576) * 1048576
        if size == FLOPPY_BYTES and total > FLOPPY_BYTES - 40 * 1024:
            raise RuntimeError(f'{label} exceeds safe 1.44 MB HFS capacity ({total} bytes)')
        encoded = temp / ('MacBinary-%d' % index)
        encoded.mkdir()
        input_files = []
        for pos, source in enumerate(files):
            binary = encoded / ('%04d.bin' % pos)
            _macbinary(source, binary)
            input_files.append({'target': source.relative_to(content).as_posix(),
                                'macbinary': rel(binary), 'sha256': sha256(binary)})
        dirs = [p.relative_to(content).as_posix() for p in sorted(
            (p for p in content.rglob('*') if p.is_dir()),
            key=lambda d: (len(d.relative_to(content).parts), str(d)))]
        app = None
        if application_path:
            source_app = content / application_path.lstrip(':').replace(':', '/')
            finder, fork = _classic_application_metadata(source_app)
            app = {'target': source_app.relative_to(content).as_posix(),
                   'finder8': finder[:8].hex(), 'fork_bytes': len(fork),
                   'fork_sha256': hashlib.sha256(fork).hexdigest()}
        specs.append({'image': rel(result), 'bytes': size, 'volume_name': label,
                      'directories': dirs, 'files': input_files, 'application': app})
    job_file = temp / 'hfs-linux-job.json'
    job_file.write_text(json.dumps({'schema': 'cinterforge.lagomorph.hfs-linux.v1',
                                    'package': rel(package), 'package_sha256': HFS_DEB_SHA256,
                                    'working': rel(temp), 'images': specs},
                                   indent=2, sort_keys=True) + '\n', encoding='utf-8')
    cmd = [str(docker), 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
           '--mount', f'type=bind,src={docker_bind_source(builds)},dst=/builds',
           '--mount', f'type=bind,src={docker_bind_source(Path(__file__).resolve().parents[1])},dst=/source,readonly',
           '--workdir', '/source', image, 'python3', '-B', '-m', 'host.lagomorph_hfs_linux',
           '--builds', '/builds', '--job', '/builds/' + rel(job_file)]
    print('[LAGOMORPH] Building three HFS volumes inside existing Linux t container', flush=True)
    completed = _run(cmd, timeout=1800, output_log=log)
    if completed.returncode:
        raise RuntimeError('Linux HFS assembly failed; see ' + str(log))
    for _, result, _, _, _ in jobs:
        _classic_hfs_signature(result)


def _macbinary(source: Path, target: Path) -> None:
    """Encode both Macintosh forks plus Finder type/creator for hcopy -m.

    hfsutils recognizes MacBinary II and reconstructs the forks and metadata
    inside the HFS volume. Never write a resource-bearing executable raw.
    """
    name = source.name.encode('mac_roman', errors='strict')
    if not 1 <= len(name) <= 63 or ':' in source.name:
        raise RuntimeError('Classic HFS requires a 1-63 byte MacRoman filename: ' + str(source))
    finder = b''
    fork = b''
    if platform.system() == 'Darwin':
        for key, var in (('com.apple.FinderInfo', 'finder'), ('com.apple.ResourceFork', 'fork')):
            try:
                value = _getxattr(source, key)
            except (OSError, AttributeError):
                value = b''
            if var == 'finder':
                finder = value
            else:
                fork = value
    data_len = source.stat().st_size
    if data_len > 0xffffffff or len(fork) > 0xffffffff:
        raise RuntimeError('MacBinary fork exceeds 4 GiB: ' + str(source))
    header = bytearray(128)
    header[1] = len(name)
    header[2:2+len(name)] = name
    header[65:69] = finder[:4].ljust(4, b' ') if finder else b'????'
    header[69:73] = finder[4:8].ljust(4, b' ') if len(finder) >= 8 else b'????'
    header[73] = finder[8] if len(finder) > 8 else 0
    header[74] = 0
    header[75:77] = finder[10:12] if len(finder) >= 12 else b'\0\0'
    header[101] = finder[9] if len(finder) > 9 else 0
    header[83:87] = data_len.to_bytes(4, 'big')
    header[87:91] = len(fork).to_bytes(4, 'big')
    header[122] = 129
    header[123] = 129
    crc = 0
    for byte in header[:124]:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xffff
    header[124:126] = crc.to_bytes(2, 'big')
    with target.open('wb') as out:
        out.write(header)
        with source.open('rb') as original:
            shutil.copyfileobj(original, out, length=1024*1024)
        out.write(b'\0' * ((-data_len) % 128))
        out.write(fork)
        out.write(b'\0' * ((-len(fork)) % 128))


def _verify_hfs_application(tools: dict[str, Path], temp: Path, env: dict, source: Path,
                            hfs_path: str = ':Desktop Folder:Lagomorph 1') -> None:
    """Round-trip the application through HFS and check forks and Finder type."""
    expected_finder, expected_fork = _classic_application_metadata(source)
    destination = temp / 'hfs-verified-app.bin'
    subprocess.run([str(tools['hcopy']), '-m', hfs_path, str(destination)],
                   check=True, env=env)
    result = destination.read_bytes()
    if len(result) < 128:
        raise RuntimeError('HFS app verification produced a truncated MacBinary file')
    data_length = int.from_bytes(result[83:87], 'big')
    fork_length = int.from_bytes(result[87:91], 'big')
    fork_offset = 128 + ((data_length + 127) // 128) * 128
    recovered = result[fork_offset:fork_offset + fork_length]
    if (result[65:69] != b'APPL' or result[69:73] != expected_finder[4:8]
            or len(recovered) != len(expected_fork) or recovered != expected_fork):
        raise RuntimeError('HFS app verification failed: Lagomorph APPL type, creator, or '
                           'resource fork changed during hcopy -m')
    print(f'[LAGOMORPH] Verified classic APPL {len(recovered)}-byte resource fork '
          'after HFS round-trip', flush=True)


def _make_classic_hfs_image(content: Path, image: Path, temp: Path,
                            tools: dict[str, Path], *, volume_name: str = 'Lagomorph 1',
                            image_bytes: int | None = None,
                            application_path: str | None = ':Desktop Folder:Lagomorph 1') -> None:
    """Create native HFS Standard bytes without modern macOS HFS+ fallback."""
    files = _files(content)
    total = sum(p.stat().st_size + len(_getxattr(p, 'com.apple.ResourceFork'))
                if platform.system() == 'Darwin' and _has_resource_fork(p)
                else p.stat().st_size for p in files)
    if image_bytes is None:
        image_bytes = max(32, (total * 2 + 16 * 1048576 + 1048575) // 1048576) * 1048576
    if image_bytes == FLOPPY_BYTES and total > FLOPPY_BYTES - 40 * 1024:
        raise RuntimeError(f'{volume_name} exceeds safe 1.44 MB HFS capacity ({total} bytes)')
    with image.open('wb') as output:
        output.truncate(image_bytes)
    subprocess.run([str(tools['hformat']), '-l', volume_name, str(image)], check=True)
    _classic_hfs_signature(image)
    # hfsutils holds the current volume in ~/.hcwd by default. Explicitly
    # redirect its state to Builds, never to the user's hidden home directory.
    env = dict(os.environ)
    env['HOME'] = str(temp)
    subprocess.run([str(tools['hmount']), str(image)], check=True, env=env)
    try:
        for directory in sorted((d for d in content.rglob('*') if d.is_dir()),
                                key=lambda d: (len(d.relative_to(content).parts), str(d))):
            hpath = ':' + ':'.join(directory.relative_to(content).parts)
            subprocess.run([str(tools['hmkdir']), hpath], check=True, env=env)
        for index, source in enumerate(files):
            relative = source.relative_to(content)
            hpath = ':' + ':'.join(relative.parts)
            encoded = temp / f'macbinary-{index}.bin'
            _macbinary(source, encoded)
            subprocess.run([str(tools['hcopy']), '-m', str(encoded), hpath],
                           check=True, env=env)
            encoded.unlink()
        if application_path:
            source_app = content / application_path.lstrip(':').replace(':', '/')
            _verify_hfs_application(tools, temp, env, source_app, application_path)
        listing = subprocess.run([str(tools['hls'])], capture_output=True, text=True,
                                 check=True, env=env)
        if not listing.stdout.strip():
            raise RuntimeError('Classic HFS verification failed: empty volume listing')
    finally:
        subprocess.run([str(tools['humount'])], check=True, env=env)
    _classic_hfs_signature(image)


def _has_resource_fork(source: Path) -> bool:
    try:
        return bool(_getxattr(source, 'com.apple.ResourceFork'))
    except (OSError, AttributeError):
        return False


def _original_game_override(source_root: Path, temp: Path) -> Path | None:
    """Use the original resource-fork app ZIP in the *same staged source*.

    It is important that we do not silently replace the resource-bearing app
    with an empty data fork found during a broad directory scan.
    """
    archive = source_root / 'Lagomorph.zip' if source_root.is_dir() else source_root
    if not archive.is_file() or archive.suffix.lower() != '.zip':
        return None
    _validate_zip(archive)
    extracted = temp / 'Original Game Archive'
    extracted.mkdir()
    _unpack(archive, extracted)
    candidates = [p for p in _files(extracted) if p.name.casefold() == 'lagomorph']
    if len(candidates) != 1:
        raise RuntimeError(f'Original archive {archive} must contain exactly one Lagomorph application')
    _classic_application_metadata(candidates[0])
    print(f'[LAGOMORPH] Original APPL/resource fork: {archive}', flush=True)
    return candidates[0]


def _finder_metadata_for_inspection(path: Path) -> dict:
    """Inspect classic Finder type/creator and forks without modifying originals."""
    result = {'path': str(path), 'name': path.name,
              'data_bytes': path.stat().st_size}
    for attribute, output_key in (('com.apple.FinderInfo', 'finder_info'),
                                  ('com.apple.ResourceFork', 'resource_fork')):
        try:
            raw = _getxattr(path, attribute)
        except (OSError, AttributeError, RuntimeError):
            raw = b''
        if output_key == 'finder_info':
            result['type'] = raw[:4].decode('mac_roman', errors='replace') if len(raw) >= 4 else None
            result['creator'] = raw[4:8].decode('mac_roman', errors='replace') if len(raw) >= 8 else None
            result['finder_info_bytes'] = len(raw)
        else:
            result['resource_bytes'] = len(raw)
    return result


def _game_path_hints(game: Path) -> dict:
    """Find candidate names in original bytes; strings do not prove an open path."""
    data = game.read_bytes()
    try:
        fork = _getxattr(game, 'com.apple.ResourceFork')
    except (OSError, AttributeError, RuntimeError):
        fork = b''
    patterns = (b'battles', b'battle', b'desktop folder',
                b'\\', b'folder')
    found = []
    for fork_name, payload in (('data', data), ('resource', fork)):
        for pattern in patterns:
            matches = list(re.finditer(re.escape(pattern), payload, flags=re.IGNORECASE))
            for match in matches[:24]:
                near = payload[max(0, match.start()-48):min(len(payload),match.end()+64)]
                printable = ''.join(chr(c) if 32 <= c < 127 else ' ' for c in near)
                found.append({'fork': fork_name, 'offset': match.start(),
                              'pattern': pattern.decode('ascii'),
                              'context': ' '.join(printable.split())[:120]})
    return {'searched_data_bytes': len(data), 'searched_resource_bytes': len(fork),
            'candidates': found[:120],
            'limitation': 'Static byte patterns are clues, not evidence of actual Macintosh File Manager calls.'}


def inspect_original_battles(builds: Path, source: Path | None = None) -> dict:
    """Report source paths/forks and probable file names, without changing game media."""
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise ValueError('Inspection requires the sibling Builds directory')
    raw = _source(builds, str(source) if source else os.environ.get('CINTER_LAGOMORPH_SOURCE'))
    temp_root = builds / 'Intermediates/CinterForgeTerminal/Lagomorph'
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='inspection-', dir=temp_root) as directory:
        tmp = Path(directory)
        unpacked = tmp / 'Originals'
        unpacked.mkdir()
        _unpack(raw, unpacked)
        game, battles, manual, _icons = _choose_inputs(unpacked)
        game = _original_game_override(raw, tmp) or game
        _classic_application_metadata(game)
        battle_files = battles if isinstance(battles, list) else _files(battles)
        metadata = [_finder_metadata_for_inspection(b) for b in battle_files]
        for item, path in zip(metadata, battle_files):
            item['original_relative_path'] = str(path.relative_to(unpacked)) if within(path, unpacked) else path.name
            item.pop('path', None)
        report = {
            'schema': 'cinterforge.lagomorph.battles-inspection.v1',
            'source': str(raw),
            'game': {**_finder_metadata_for_inspection(game), 'path': game.name},
            'original_battles_path': (str(battles.relative_to(unpacked)) if isinstance(battles, Path) and within(battles, unpacked) else 'individual Battle files'),
            'battles': metadata,
            'manual': str(manual.relative_to(unpacked)),
            'current_hfs_layout': 'Game and Battles at root of a writable combined HFS image; separate 1.44 MB HFS floppy tests',
            'application_string_hints': _game_path_hints(game),
            'conclusion': ('This is a non-destructive static inspection. It does not establish the actual '
                           'directory requested at runtime. That requires guest file-open tracing or an '
                           'unambiguous original file-access resource.'),
        }
    dest = builds / 'CinterForgeTerminal/Inspection/Lagomorph'
    dest.mkdir(parents=True, exist_ok=True)
    output = dest / 'battles-report.json'
    candidate = dest / 'battles-report.json.candidate'
    candidate.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    candidate.replace(output)
    print('[LAGOMORPH] Battles inspection: ' + str(output), flush=True)
    for item in metadata:
        print('[LAGOMORPH] Battle: ' + item['name'] + ' type=' + str(item['type']) +
              ' creator=' + str(item['creator']) + ' resource_bytes=' + str(item['resource_bytes']), flush=True)
    print('[LAGOMORPH] Runtime lookup location remains unverified; original game and saves unchanged.', flush=True)
    return report


def stage(builds: Path, source: Path | None = None) -> dict:
    builds = builds.resolve()
    if builds.name != 'Builds' or not builds.is_dir():
        raise ValueError(f'Source output must be the sibling Builds root: {builds}')
    if platform.system() != 'Darwin':
        raise RuntimeError('Mac host required to preserve original Finder metadata and resource forks')
    if not shutil.which('ditto'):
        raise RuntimeError('macOS ditto is required to preserve resource forks')
    raw = _source(builds, str(source) if source is not None else os.environ.get('CINTER_LAGOMORPH_SOURCE'))
    print(f'[LAGOMORPH] Using staged original source: {raw}', flush=True)
    intermediates = builds / 'Intermediates/CinterForgeTerminal/Lagomorph'
    intermediates.mkdir(parents=True, exist_ok=True)
    final = builds / RELATIVE_STAGE
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=intermediates, prefix='candidate-') as temp:
        temp = Path(temp)
        unpacked = temp / 'Originals'
        unpacked.mkdir()
        _unpack(raw, unpacked)
        game, battles, manual, icons = _choose_inputs(unpacked)
        game = _original_game_override(raw, temp) or game
        _classic_application_metadata(game)
        # Keep the original application and Battles together at the root of
        # the compatibility HFS volume. The owner's source uses this layout.
        # The manual opens in the host PDF viewer and must not be on game media.
        content = temp / 'HFS Content'
        content.mkdir()
        _copy_classic(game, content / 'Lagomorph 1')
        _classic_application_metadata(content / 'Lagomorph 1')
        battle_files = battles if isinstance(battles, list) else _files(battles)
        for battle in battle_files:
            _copy_classic(battle, content / battle.name)
        # The 1.37 MB application plus 0.43 MB Battles cannot fit together on
        # a 1.44 MB floppy. Build TWO real HFS floppy volumes for testing.
        floppy_app = temp / 'Floppy Application'
        floppy_app.mkdir()
        _copy_classic(game, floppy_app / 'Lagomorph 1')
        floppy_battles = temp / 'Floppy Battles'
        floppy_battles.mkdir()
        for battle in battle_files:
            _copy_classic(battle, floppy_battles / battle.name)
        app_image = temp / APPLICATION_FLOPPY
        battle_image = temp / BATTLES_FLOPPY
        candidate_image = temp / IMAGE_NAME
        _stage_hfs_linux(builds, temp, [
            (floppy_app, app_image, 'Lagomorph 1', FLOPPY_BYTES, ':Lagomorph 1'),
            (floppy_battles, battle_image, 'Lagomorph Battles', FLOPPY_BYTES, None),
            (content, candidate_image, 'Lagomorph 1', None, ':Lagomorph 1'),
        ])
        for floppy in (app_image, battle_image):
            if floppy.stat().st_size != FLOPPY_BYTES:
                raise RuntimeError(f'Unexpected floppy image size: {floppy}')
            _classic_hfs_signature(floppy)
        if candidate_image.stat().st_size < 32768:
            raise RuntimeError('Classic HFS media builder produced an empty image')
        payload = {p.relative_to(content).as_posix(): sha256(p) for p in _files(content)}
        manifest = {'schema': SCHEMA, 'machine': 'Quadra 650', 'system_disk': 'Mac OS 7.6',
                    'emulator': 'BasiliskII', 'image': IMAGE_NAME,
                    'image_sha256': sha256(candidate_image), 'image_size': candidate_image.stat().st_size,
                    'assets': payload, 'floppies': {
                        APPLICATION_FLOPPY: {'bytes': app_image.stat().st_size, 'sha256': sha256(app_image)},
                        BATTLES_FLOPPY: {'bytes': battle_image.stat().st_size, 'sha256': sha256(battle_image)},
                    }, 'source_sha256': sha256(raw) if raw.is_file() else None,
                    'private': True, 'publish_automatically': False,
                    'url_path': '/lagomorph/' + IMAGE_NAME,
                    'notes': 'Classic HFS root-level game + Battles; two 1.44 MB floppy test images; manual only in host',
                    'filesystem': 'HFS Standard', 'image_format': 'raw-HFS-volume'}
        prepared = temp / 'stage'
        prepared.mkdir()
        _copy_classic(candidate_image, prepared / IMAGE_NAME)
        for floppy in (app_image, battle_image):
            _copy_classic(floppy, prepared / floppy.name)
        _copy_classic(manual, prepared / 'Lagomorph Manual.pdf')
        (prepared / MANIFEST_NAME).write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        for item in (prepared / IMAGE_NAME, prepared / APPLICATION_FLOPPY,
                     prepared / BATTLES_FLOPPY, prepared / 'Lagomorph Manual.pdf'):
            if not item.is_file() or item.stat().st_size == 0:
                raise RuntimeError(f'Incomplete staged Lagomorph asset: {item}')
        # Stage a verified candidate, then switch the asset directory. Source is
        # never changed and failed image creation cannot replace the last image.
        backup = final.with_name(final.name + '.previous')
        if backup.exists() or backup.is_symlink():
            raise RuntimeError(f'Lagomorph stage has an unresolved prior transaction: {backup}')
        if final.exists():
            final.rename(backup)
        try:
            prepared.rename(final)
        except BaseException:
            if backup.exists():
                backup.rename(final)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        print(f'[CinterForgeTerminal Lagomorph] Classic HFS content staged: {final / IMAGE_NAME}', flush=True)
        print(f'[LAGOMORPH] 1.44 MB HFS floppies: {final / APPLICATION_FLOPPY}, {final / BATTLES_FLOPPY}', flush=True)
        return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Create Lagomorph 1 HFS content media from private Builds inputs')
    parser.add_argument('--builds', type=Path, default=Path(__file__).resolve().parents[2] / 'Builds')
    parser.add_argument('--source', type=Path, help='Private user game source directory/zip inside Builds')
    parser.add_argument('--inspect-only', action='store_true', help='Inspect original Battles metadata and game strings without rebuilding images')
    opts = parser.parse_args(argv)
    try:
        if opts.inspect_only:
            inspect_original_battles(opts.builds, opts.source)
        else:
            stage(opts.builds, opts.source)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print(f'[CinterForgeTerminal Lagomorph] {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
