"""Build and stage the CinterForge Terminal 68K application HFS image.

The genuine 68K APPL is built with Setup-provisioned Retro68 and staged as
CinterForgeTerminal.68k.img under the existing application Distribution tree.
The application publisher uploads the *raw image*, never an Assets pack or ZIP.
Infinite Mac mounts it with a disposable guest overlay on System 7+ machines.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import zlib

IMAGE_NAME = 'CinterForgeTerminal.68k.img'
BUILD_RELATIVE = Path('CinterForgeTerminal/Build/68k')
DISTRIBUTION_RELATIVE = Path('CinterForgeTerminal/Distribution')
RECEIPT_RELATIVE = Path('Intermediates/CinterForgeTerminal/InfiniteMac/launcher-receipt.json')
STAGE_RECEIPT_RELATIVE = Path('Intermediates/CinterForgeTerminal/InfiniteMac/stage-receipt.json')
APPLICATION_NAME = 'CinterForgeTerminal'
FINDER_APPLICATION_NAME = 'CinterForge Terminal'
HFS_VOLUME_NAME = 'CinterForge Terminal'
CAPACITY = 8 * 1024 * 1024

# Classic HFS Master Directory Block (MDB) at offset 1024; the FinderInfo
# open-folder CNID is its third 32-bit field. CNID 2 is the HFS volume root.
# This is *Finder metadata*, not a boot flag or a host macOS DMG option.
HFS_MDB_OFFSET = 1024
HFS_FINDER_OPEN_FOLDER_OFFSET = 0x5C + 2 * 4
HFS_ROOT_CNID = 2
RECEIPT_SCHEMA = 'cinterforge.infinitemac.hfs-build.v4'
STAGE_SCHEMA = 'cinterforge.infinitemac.hfs-stage.v1'
CARD_WIDTH, CARD_HEIGHT = 145, 225
SOURCE = Path(__file__).resolve().parents[2]


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _png_rgb(path: Path) -> tuple[int, int, bytes]:
    """Decode our checked-in RGBA/RGB PNG without optional pip dependencies.

    Only noninterlaced 8-bit PNGs are accepted. Unsupported authored PNGs must
    fail visibly rather than silently substitute unrelated game-card graphics.
    """
    raw = path.read_bytes()
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Not a PNG: ' + str(path))
    off, compressed, width, height, channels = 8, bytearray(), 0, 0, 0
    while off + 12 <= len(raw):
        length = struct.unpack_from('>I', raw, off)[0]
        end = off + 12 + length
        if end > len(raw):
            raise ValueError('Truncated PNG: ' + str(path))
        kind, chunk = raw[off+4:off+8], raw[off+8:off+8+length]
        if zlib.crc32(kind + chunk) != struct.unpack_from('>I', raw, end-4)[0]:
            raise ValueError('Invalid PNG CRC: ' + str(path))
        if kind == b'IHDR':
            width, height, depth, color, compression, filtering, interlaced = struct.unpack('>IIBBBBB', chunk)
            if depth != 8 or color not in (2, 6) or compression or filtering or interlaced:
                raise ValueError('Expected noninterlaced 8-bit RGB/RGBA PNG: ' + str(path))
            channels = 4 if color == 6 else 3
        elif kind == b'IDAT':
            compressed.extend(chunk)
        elif kind == b'IEND':
            break
        off = end
    if not (0 < width <= 4096 and 0 < height <= 4096 and channels):
        raise ValueError('Invalid PNG dimensions: ' + str(path))
    decoded = zlib.decompress(bytes(compressed))
    stride = width * channels
    if len(decoded) != (stride + 1) * height:
        raise ValueError('Unexpected PNG data length: ' + str(path))
    previous = bytearray(stride)
    pixels = bytearray(width * height * 3)
    for y in range(height):
        filter_type = decoded[y * (stride+1)]
        current = bytearray(decoded[y * (stride+1)+1:(y+1) * (stride+1)])
        for x in range(stride):
            a = current[x - channels] if x >= channels else 0
            b = previous[x]
            c = previous[x - channels] if x >= channels else 0
            if filter_type == 1:
                current[x] = (current[x] + a) & 255
            elif filter_type == 2:
                current[x] = (current[x] + b) & 255
            elif filter_type == 3:
                current[x] = (current[x] + ((a+b)//2)) & 255
            elif filter_type == 4:
                p = a + b - c
                choices = (abs(p-a), abs(p-b), abs(p-c))
                predictor = (a, b, c)[choices.index(min(choices))]
                current[x] = (current[x] + predictor) & 255
            elif filter_type != 0:
                raise ValueError('Invalid PNG filter: ' + str(filter_type))
        for x in range(width):
            base = x * channels
            alpha = current[base + 3] if channels == 4 else 255
            dest = (y*width + x)*3
            for ch in range(3):
                pixels[dest + ch] = (current[base+ch]*alpha + 255*(255-alpha)+127)//255
        previous = current
    return width, height, bytes(pixels)


def _resized_rgb(width: int, height: int, rgb: bytes, out_w: int, out_h: int) -> bytes:
    """Fixed-point nearest-neighbor resize, deterministic on every builder."""
    out = bytearray(out_w * out_h * 3)
    for y in range(out_h):
        src_y = min(height-1, y*height//out_h)
        for x in range(out_w):
            source_offset = (src_y*width + min(width-1, x*width//out_w))*3
            destination_offset = (y*out_w+x)*3
            out[destination_offset:destination_offset+3] = rgb[source_offset:source_offset+3]
    return bytes(out)


def _card_rgb(source_png: Path, destination: Path) -> None:
    width, height, rgb = _png_rgb(source_png)
    scaled = _resized_rgb(width, height, rgb, CARD_WIDTH, CARD_HEIGHT)
    with destination.open('wb') as file:
        file.write(struct.pack('>HH', CARD_WIDTH, CARD_HEIGHT))
        for offset in range(0, len(scaled), 3):
            r, g, b = scaled[offset:offset+3]
            file.write(struct.pack('>H', (r >> 3) << 10 | (g >> 3) << 5 | (b >> 3)))



def _rgb555_artwork(source_png: Path, destination: Path, width: int, height: int) -> None:
    """Native classic Mac 16-bit RGB data, using the authored product icon."""
    source_width, source_height, rgb = _png_rgb(source_png)
    scaled = _resized_rgb(source_width, source_height, rgb, width, height)
    with destination.open('wb') as file:
        file.write(struct.pack('>HH', width, height))
        for offset in range(0, len(scaled), 3):
            r, g, b = scaled[offset:offset+3]
            file.write(struct.pack('>H', (r >> 3) << 10 | (g >> 3) << 5 | (b >> 3)))


def _icon_resources(source_png: Path, destination: Path) -> None:
    width, height, rgb = _png_rgb(source_png)
    rgb = _resized_rgb(width, height, rgb, 32, 32)
    # Icon pixels are indices into Apple's fixed classic system palettes,
    # NOT grayscale intensities or arbitrary RGB byte values. A wrong icl8
    # index is displayed as the ROM palette's unrelated magenta/green/cyan.
    palette4 = ((255,255,255), (252,243,5), (255,100,2), (221,8,6),
                (242,8,132), (70,0,165), (0,0,212), (2,171,234),
                (31,183,20), (0,100,17), (86,44,5), (144,113,58),
                (192,192,192), (128,128,128), (64,64,64), (0,0,0))
    levels = (255, 204, 153, 102, 51, 0)
    palette8 = [(r, g, b) for r in levels for g in levels for b in levels]
    ramps = (238, 221, 187, 170, 136, 119, 85, 68, 34, 17)
    palette8.extend((v, 0, 0) for v in ramps)
    palette8.extend((0, v, 0) for v in ramps)
    palette8.extend((0, 0, v) for v in ramps)
    palette8.extend((v, v, v) for v in ramps)
    assert len(palette4) == 16 and len(palette8) == 256
    icon4 = bytearray()
    icon8 = bytearray()
    mono = bytearray(128)
    mask = bytearray(128)
    # Modern AppIcon.png has transparent black background; detect alpha from
    # original RGBA before compositing so the icon outline stays transparent.
    raw = source_png.read_bytes()
    has_alpha = raw[25] == 6
    # Re-decode original transparency by identifying the composited white region;
    # for this icon the transparent surround is pure white after compositing.
    for y in range(32):
        for x in range(32):
            offset = (y*32+x)*3
            pixel = tuple(rgb[offset:offset+3])
            index4 = min(range(16), key=lambda k:
                         sum((pixel[c] - palette4[k][c])**2 for c in range(3)))
            if x % 2 == 0:
                icon4.append(index4 << 4)
            else:
                icon4[-1] |= index4
            index8 = min(range(256), key=lambda k:
                         sum((pixel[c] - palette8[k][c])**2 for c in range(3)))
            icon8.append(index8)
            if pixel != (255,255,255) or not has_alpha:
                byte = y*4+x//8
                mask[byte] |= 1 << (7-x%8)
                if sum(pixel) < 3*150:
                    mono[byte] |= 1 << (7-x%8)
    resources = {
        "ICN#": bytes(mono+mask),
        "icl4": bytes(icon4),
        "icl8": bytes(icon8),
        "BNDL": (b'CFTM' + struct.pack('>HH', 0, 1) + b'FREF' +
                 struct.pack('>Hhh', 0, 0, 128) + b'ICN#' +
                 struct.pack('>Hhh', 0, 0, -16455)),
        "FREF": b'APPL\x00\x00\x00',
        "CFTM": b'',  # Finder signature resource referenced by the BNDL.
    }
    with destination.open('w', encoding='ascii') as output:
        output.write('/* Generated from the canonical app/build/resources/AppIcon.png. */\n')
        for kind, data in resources.items():
            identity = (0 if kind == 'CFTM' else
                        128 if kind in ('BNDL', 'FREF') else -16455)
            output.write(f"data '{kind}' ({identity}) {{\n")
            for offset in range(0, len(data), 32):
                # Rez concatenates adjacent hexadecimal string literals inside a
                # data resource. Only the closing brace takes a semicolon.
                output.write('    $"' + data[offset:offset+32].hex().upper() + '"\n')
            output.write('};\n')


def _fork_resource(fork: bytes, kind: bytes, identity: int) -> bytes | None:
    """Read one classic Mac resource by type/ID, respecting resource-map offsets."""
    if len(fork) < 16:
        raise ValueError('Truncated classic Mac resource fork')
    data_offset, map_offset, data_length, map_length = struct.unpack_from('>IIII', fork)
    data_end, map_end = data_offset + data_length, map_offset + map_length
    if data_end > len(fork) or map_end > len(fork) or map_length < 30:
        raise ValueError('Invalid classic Mac resource fork bounds')
    type_list = map_offset + struct.unpack_from('>H', fork, map_offset + 24)[0]
    if type_list + 2 > map_end:
        raise ValueError('Invalid classic Mac resource type list')
    type_count = struct.unpack_from('>H', fork, type_list)[0] + 1
    if type_list + 2 + type_count * 8 > map_end:
        raise ValueError('Truncated classic Mac resource type entries')
    for type_index in range(type_count):
        entry = type_list + 2 + type_index * 8
        if fork[entry:entry + 4] != kind:
            continue
        ref_count, ref_offset = struct.unpack_from('>HH', fork, entry + 4)
        references = type_list + ref_offset
        if references + (ref_count + 1) * 12 > map_end:
            raise ValueError('Truncated classic Mac resource references')
        for ref_index in range(ref_count + 1):
            reference = references + ref_index * 12
            if struct.unpack_from('>h', fork, reference)[0] != identity:
                continue
            offset = int.from_bytes(fork[reference + 5:reference + 8], 'big')
            resource = data_offset + offset
            if resource + 4 > data_end:
                raise ValueError('Invalid classic Mac resource data offset')
            size = struct.unpack_from('>I', fork, resource)[0]
            if resource + 4 + size > data_end:
                raise ValueError('Truncated classic Mac resource data')
            return fork[resource + 4:resource + 4 + size]
        return None
    return None


def _validated_macbinary(raw: bytes) -> tuple[bytes, bytes, bytes]:
    if len(raw) < 256 or raw[0] != 0 or not 1 <= raw[1] <= 63 or raw[65:69] != b'APPL':
        raise ValueError('Retro68 did not produce a classic APPL MacBinary file')
    data_len = int.from_bytes(raw[83:87], 'big')
    fork_len = int.from_bytes(raw[87:91], 'big')
    start = 128 + ((data_len + 127) // 128) * 128
    end = start + fork_len
    if not fork_len or end > len(raw) or data_len > 20*1024*1024 or fork_len > 20*1024*1024:
        raise ValueError('Classic Mac APPL data/resource forks are incomplete')
    fork = raw[start:end]
    if b'BNDL' not in fork or b'ICN#' not in fork:
        raise ValueError('Retro68 output is missing the Finder icon/BNDL resource')
    # Finder/Process Manager uses SIZE (0) if present, otherwise SIZE (-1).
    # Validate the final combined APPL, not merely the authored Rez source:
    # Retro68 otherwise supplies its own smaller SIZE resource by default.
    for identity in (-1, 0):
        size_resource = _fork_resource(fork, b'SIZE', identity)
        if identity == 0 and size_resource is None:
            continue
        if size_resource is None or len(size_resource) < 10:
            raise ValueError('Retro68 output is missing SIZE (-1)')
        preferred, minimum = struct.unpack_from('>II', size_resource, 2)
        if preferred != 4 * 1024 * 1024 or minimum != 2 * 1024 * 1024:
            raise ValueError(f'Wrong classic Mac SIZE ({identity}): '
                             f'preferred={preferred}, minimum={minimum}; expected 4 MB/2 MB')
    return raw[128:128 + data_len], fork, raw[65:73]


def _compile(builds: Path, scratch: Path) -> Path:
    """Compile from current source, never silently reuse a stale .bin."""
    from host.lifecycle.amd64_backend import require_managed_t_docker, docker_bind_source
    require_managed_t_docker(builds)
    from host.devtools.devtooling import RETRO68_IMAGE, RETRO68_SCHEMA
    root = builds / 'Toolchains/Retro68'
    archive, receipt = root/'retro68-amd64.tar', root/'retro68-image.json'
    if not archive.is_file() or not receipt.is_file() or root.is_symlink():
        raise RuntimeError('Retro68 DevTools not provisioned; run Setup')
    info = json.loads(receipt.read_text(encoding='utf-8'))
    if info.get('schema') != RETRO68_SCHEMA or info.get('archive_sha256') != _hash(archive):
        raise RuntimeError('Retro68 DevTools archive does not match its sealed Setup receipt')
    docker = builds / 'Toolchains/mac/env/bin/docker'
    if not docker.is_file():
        raise RuntimeError('Setup-owned Docker CLI missing')
    src = scratch / 'Source'
    src.mkdir()
    for filename in ('CMakeLists.txt', 'CinterForgeTerminal.c', 'memory.r'):
        shutil.copy2(SOURCE/'app/68k'/filename, src/filename)
    _icon_resources(SOURCE/'app/build/resources/AppIcon.png', src/'generated_icons.r')
    print('[INFINITE MAC] Compiling CinterForgeTerminal APPL in offline Retro68 68K OCI', flush=True)
    # Always load the checked/sealed OCI archive; never run a stray image tag.
    subprocess.run([str(docker), 'load', '--input', str(archive)], check=True, timeout=1800)
    relative = scratch.relative_to(builds).as_posix()
    mount = 'type=bind,src=' + docker_bind_source(builds) + ',dst=/builds'
    script = ('set -eu; '
              'cmake -S "/builds/' + relative + '/Source" '
              '-B "/builds/' + relative + '/Object" '
              '-DCMAKE_TOOLCHAIN_FILE=/Retro68-build/toolchain/m68k-apple-macos/cmake/retro68.toolchain.cmake '
              '-DCMAKE_BUILD_TYPE=Release; '
              'cmake --build "/builds/' + relative + '/Object" --parallel 2')
    subprocess.run([str(docker), 'run', '--rm', '--platform', 'linux/amd64',
                    '--network', 'none', '--mount', mount, RETRO68_IMAGE,
                    'bash', '-c', script], check=True, timeout=900)
    result = scratch/'Object/CinterForgeTerminal.bin'
    if not result.is_file() or result.stat().st_size < 1024:
        raise RuntimeError('Retro68 did not produce the real CinterForgeTerminal.bin APPL')
    _validated_macbinary(result.read_bytes())
    return result


def setup_check(builds: Path) -> dict:
    """Fast, read-only 68K Setup check: no other platforms or downloads.

    Build independently hashes the full sealed Retro68 image before use; this
    initial preflight intentionally avoids re-reading a multi-GB OCI archive.
    """
    from host.devtools.devtooling import RETRO68_IMAGE, RETRO68_SCHEMA
    if platform.system() != 'Darwin':
        raise RuntimeError('68K Setup and Build require the macOS builder')
    builds = Path(builds).resolve(strict=True)
    root = builds / 'Toolchains/Retro68'
    archive = root / 'retro68-amd64.tar'
    receipt = root / 'retro68-image.json'
    docker = builds / 'Toolchains/mac/env/bin/docker'
    for path in (root, archive, receipt, docker):
        if path.is_symlink() or not path.exists():
            raise RuntimeError('Missing/unsafe 68K Setup input: ' + str(path) +
                               '; run ./Cinter setup all once to provision Retro68')
    if not archive.is_file() or not archive.stat().st_size or not docker.is_file():
        raise RuntimeError('Incomplete Retro68 or Docker Setup inputs')
    metadata = json.loads(receipt.read_text(encoding='utf-8'))
    if (metadata.get('schema') != RETRO68_SCHEMA or
            metadata.get('image') != RETRO68_IMAGE or
            metadata.get('platform') != 'linux/amd64' or
            len(metadata.get('archive_sha256', '')) != 64):
        raise RuntimeError('Invalid Retro68 Setup receipt; run ./Cinter setup all')
    from host.lagomorph.lagomorph_setup import HFS_DEB_NAME, HFS_DEB_SHA256
    hfs_package = builds / 'Toolchains/Linux/hfsutils/3.2.6-15' / HFS_DEB_NAME
    if (hfs_package.is_symlink() or not hfs_package.is_file() or
            _hash(hfs_package) != HFS_DEB_SHA256):
        raise RuntimeError('Missing sealed HFS assembly tool; run ./Cinter setup all')
    for source in (SOURCE / 'app/68k/CMakeLists.txt',
                   SOURCE / 'app/68k/CinterForgeTerminal.c',
                   SOURCE / 'app/68k/memory.r',
                   SOURCE / 'app/build/resources/AppIcon.png',
                   SOURCE / 'webapp/cards/GameCard.png'):
        if not source.is_file() or source.is_symlink():
            raise RuntimeError('Missing/unsafe 68K authored input: ' + str(source))
    print('[68K SETUP] PASS offline Retro68/Docker/HFS inputs; no downloads or other targets',
          flush=True)
    return {'verified': True, 'offline': True}


def _verify_terminal_root_auto_open(image: Path) -> None:
    """The classic Finder should open this HFS volume's root when mounted."""
    size = image.stat().st_size
    if size < 4096 or size % 512:
        raise RuntimeError('Invalid Classic Mac HFS image length')
    with image.open('rb') as stream:
        for offset in (HFS_MDB_OFFSET, size - 1024):
            stream.seek(offset)
            signature = stream.read(2)
            # hfsutils may omit the optional alternate MDB; verify it when
            # present. The primary MDB is mandatory.
            if offset != HFS_MDB_OFFSET and signature != b'BD':
                continue
            if signature != b'BD':
                raise RuntimeError('Invalid classic HFS MDB signature')
            stream.seek(offset + HFS_FINDER_OPEN_FOLDER_OFFSET)
            data = stream.read(4)
            if len(data) != 4 or struct.unpack('>I', data)[0] != HFS_ROOT_CNID:
                raise RuntimeError('Classic HFS image does not auto-open the '
                                   'CinterForge Terminal root in Finder; '
                                   'rebuild with ./Cinter build 68k')


def _set_terminal_root_auto_open(image: Path) -> None:
    """Set FinderInfo[2] to CNID 2 without changing other HFS metadata.

    On both System 7.1 and 7.6 Finder this requests a root window on mount,
    rather than relying on stale writable Desktop files or screen clicks.
    The alternate MDB, if present, must agree with the primary MDB.
    """
    size = image.stat().st_size
    if size < 4096 or size % 512:
        raise RuntimeError('Invalid Classic Mac HFS image length')
    offsets = [HFS_MDB_OFFSET]
    with image.open('r+b') as stream:
        stream.seek(HFS_MDB_OFFSET)
        if stream.read(2) != b'BD':
            raise RuntimeError('Cannot set Finder auto-open on non-HFS image')
        stream.seek(size - 1024)
        if stream.read(2) == b'BD':
            offsets.append(size - 1024)
        for offset in offsets:
            stream.seek(offset + HFS_FINDER_OPEN_FOLDER_OFFSET)
            raw = stream.read(4)
            if len(raw) != 4 or struct.unpack('>I', raw)[0] not in (0, HFS_ROOT_CNID):
                raise RuntimeError('Unexpected pre-existing Finder open-folder ID; '
                                   'refusing to rewrite unrelated Finder metadata')
        for offset in offsets:
            stream.seek(offset + HFS_FINDER_OPEN_FOLDER_OFFSET)
            stream.write(struct.pack('>I', HFS_ROOT_CNID))
        stream.flush()
        os.fsync(stream.fileno())
    _verify_terminal_root_auto_open(image)


def _read_build_receipt(builds: Path) -> tuple[Path, dict]:
    """Check the sealed Build artifact without modifying it."""
    from host.lagomorph.lagomorph_setup import _classic_hfs_signature
    image = builds / BUILD_RELATIVE / IMAGE_NAME
    receipt = builds / RECEIPT_RELATIVE
    if (image.is_symlink() or receipt.is_symlink() or
            not image.is_file() or not receipt.is_file()):
        raise RuntimeError('Missing 68K Build image/receipt; run ./Cinter build 68k')
    data = json.loads(receipt.read_text(encoding='utf-8'))
    if (data.get('schema') != RECEIPT_SCHEMA or
            data.get('image_sha256') != _hash(image) or
            data.get('bytes') != image.stat().st_size):
        raise RuntimeError('68K Build image does not match its verified receipt')
    _classic_hfs_signature(image)
    _verify_terminal_root_auto_open(image)
    return image, data


def build(builds: Path) -> dict:
    """Build only the 68K APPL and raw application disk; never upload here."""
    if platform.system() != 'Darwin':
        raise RuntimeError('Classic Mac APPL/HFS Build requires the managed macOS builder')
    from host.lagomorph.lagomorph_setup import _stage_hfs_linux, _classic_hfs_signature, _setxattr
    builds = Path(builds).resolve()
    root = builds / BUILD_RELATIVE
    receipt = builds / RECEIPT_RELATIVE
    if any(path.is_symlink() for path in (root, root / IMAGE_NAME, receipt)):
        raise RuntimeError('Refusing symlink in 68K application Build outputs')
    root.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    output = root / IMAGE_NAME
    inputs = [Path(__file__), SOURCE / 'app/68k/CinterForgeTerminal.c',
              SOURCE / 'app/68k/CMakeLists.txt', SOURCE / 'app/68k/memory.r',
              SOURCE / 'app/build/resources/AppIcon.png', SOURCE / 'webapp/cards/GameCard.png']
    for source in inputs:
        if not source.is_file() or source.is_symlink():
            raise RuntimeError('Canonical 68K source missing or unsafe: ' + str(source))
    source_hash = hashlib.sha256(''.join(_hash(p) for p in inputs).encode('ascii')).hexdigest()
    previous = {}
    if receipt.is_file():
        try:
            previous = json.loads(receipt.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            previous = {}
    if output.exists():
        _classic_hfs_signature(output)
        current_hash = _hash(output)
        if (previous.get('schema') != RECEIPT_SCHEMA or
                previous.get('image_sha256') != current_hash):
            raise RuntimeError('Existing built 68K disk differs from its receipt; '
                               'preserve the edited image elsewhere before rebuilding')
        if previous.get('source_sha256') == source_hash:
            print('[68K BUILD] Reusing verified unchanged ' + IMAGE_NAME, flush=True)
            return {'image': str(output), 'reused': True, 'appl': True}
    with tempfile.TemporaryDirectory(prefix='classic-games-', dir=receipt.parent) as name:
        scratch = Path(name)
        binary = _compile(builds, scratch)
        data, fork, type_creator = _validated_macbinary(binary.read_bytes())
        contents = scratch / 'Contents'
        contents.mkdir()
        # Match the proven Lagomorph disk layout: a Finder-launchable APPL
        # at the HFS root, with supporting material alongside it. Keep the
        # Terminal APPL alphabetically first in Finder's default icon order;
        # Game.txt stays at the root for SimpleText edits within the guest.
        support_files = contents / 'Support Files'
        support_files.mkdir()
        (contents / 'Game.txt').write_bytes(b'\r')
        (support_files / 'README').write_bytes((
            'CinterForge Terminal - classic Macintosh application\r'
            'CinterForge Terminal starts without automatically launching a game.\r'
            'Type games to list apps; games report opens the five-column cards.\r'
            'The Game Cards show the real Finder application name and icon.\r'
            'To launch a game from Game.txt, edit it in SimpleText then choose\r'
            'Games: Run Game.txt. Games never start automatically at boot.\r'
            'The Terminal stays open and listens for cinter:<os>: commands.\r'
            'This disk is writable only for the current emulator session.\r'
        ).encode('mac_roman'))
        _card_rgb(SOURCE / 'webapp/cards/GameCard.png',
                  support_files / 'GameCard.rgb')
        _rgb555_artwork(SOURCE / 'app/build/resources/AppIcon.png',
                        support_files / 'CinterForgeLogo.rgb', 40, 40)
        for document in (support_files / 'README', contents / 'Game.txt'):
            _setxattr(document, 'com.apple.FinderInfo', b'TEXTttxt' + bytes(24))
        application = contents / FINDER_APPLICATION_NAME
        application.write_bytes(data)
        _setxattr(application, 'com.apple.FinderInfo',
                  type_creator + struct.pack('>H', 0x2000) + bytes(22))
        _setxattr(application, 'com.apple.ResourceFork', fork)
        image = scratch / IMAGE_NAME
        _stage_hfs_linux(builds, scratch, [(contents, image, HFS_VOLUME_NAME, CAPACITY,
                                           ':' + FINDER_APPLICATION_NAME)])
        _classic_hfs_signature(image)
        _set_terminal_root_auto_open(image)
        if output.exists() and _hash(output) != previous.get('image_sha256'):
            raise RuntimeError('68K Build image changed during compilation; preserving edits')
        os.replace(image, output)
        metadata = {'schema': RECEIPT_SCHEMA, 'image_sha256': _hash(output),
                    'source_sha256': source_hash, 'app_sha256': _hash(binary),
                    'bytes': output.stat().st_size, 'compiled': 'retro68-68k',
                    'card': 'webapp/cards/GameCard.png',
                    'icon': 'app/build/resources/AppIcon.png'}
        candidate_receipt = scratch / 'receipt.json'
        candidate_receipt.write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n',
                                     encoding='utf-8')
        os.replace(candidate_receipt, receipt)
    print('[68K BUILD] Raw app disk ready: ' + str(output), flush=True)
    return {'image': str(output), 'appl': True}


def stage(builds: Path) -> dict:
    """Stage the checked raw disk next to the other application distributions."""
    builds = Path(builds).resolve(strict=True)
    image, built = _read_build_receipt(builds)
    root = builds / DISTRIBUTION_RELATIVE
    destination = root / IMAGE_NAME
    stage_receipt = builds / STAGE_RECEIPT_RELATIVE
    if any(path.is_symlink() for path in (root, destination, stage_receipt)):
        raise RuntimeError('Refusing symlink in 68K application Stage outputs')
    root.mkdir(parents=True, exist_ok=True)
    stage_receipt.parent.mkdir(parents=True, exist_ok=True)
    if (not destination.is_file() or
            destination.stat().st_size != built['bytes'] or
            _hash(destination) != built['image_sha256']):
        # The Build master is never modified by Stage. Replace only our own
        # distribution filename; preserve all other platform deliverables.
        with tempfile.NamedTemporaryFile(prefix='68k-stage-', suffix='.img',
                                         dir=root, delete=False) as temporary:
            pending = Path(temporary.name)
            try:
                with image.open('rb') as source:
                    shutil.copyfileobj(source, temporary, length=1024 * 1024)
            except BaseException:
                pending.unlink(missing_ok=True)
                raise
        try:
            if _hash(pending) != built['image_sha256']:
                raise RuntimeError('68K staged copy does not match the Build SHA-256')
            os.replace(pending, destination)
        finally:
            pending.unlink(missing_ok=True)
    record = {'schema': STAGE_SCHEMA, 'filename': IMAGE_NAME,
              'image_sha256': built['image_sha256'], 'bytes': built['bytes'],
              'source_sha256': built['source_sha256']}
    with tempfile.NamedTemporaryFile(mode='w', prefix='68k-receipt-', suffix='.json',
                                     encoding='utf-8', dir=stage_receipt.parent,
                                     delete=False) as temporary:
        pending_receipt = Path(temporary.name)
        json.dump(record, temporary, sort_keys=True, indent=2)
        temporary.write('\n')
    try:
        os.replace(pending_receipt, stage_receipt)
    finally:
        pending_receipt.unlink(missing_ok=True)
    print('[68K STAGE] PASS ' + str(destination), flush=True)
    return {'image': str(destination), 'sha256': built['image_sha256'],
            'bytes': built['bytes']}


def verify_staged(builds: Path) -> dict:
    """Publish gate: require the current Stage AND Build receipts to agree."""
    from host.lagomorph.lagomorph_setup import _classic_hfs_signature
    builds = Path(builds).resolve(strict=True)
    image, built = _read_build_receipt(builds)
    staged_image = builds / DISTRIBUTION_RELATIVE / IMAGE_NAME
    stage_receipt = builds / STAGE_RECEIPT_RELATIVE
    if (staged_image.is_symlink() or stage_receipt.is_symlink() or
            not staged_image.is_file() or not stage_receipt.is_file()):
        raise RuntimeError('68K application has not been staged; run ./Cinter stage 68k')
    staged = json.loads(stage_receipt.read_text(encoding='utf-8'))
    if (staged.get('schema') != STAGE_SCHEMA or staged.get('filename') != IMAGE_NAME or
            staged.get('image_sha256') != built['image_sha256'] or
            staged.get('source_sha256') != built['source_sha256'] or
            staged.get('bytes') != built['bytes'] or
            staged_image.stat().st_size != built['bytes'] or
            _hash(staged_image) != built['image_sha256']):
        raise RuntimeError('68K staged artifact changed or is stale; run ./Cinter stage 68k')
    _classic_hfs_signature(staged_image)
    _verify_terminal_root_auto_open(staged_image)
    return {'image': str(staged_image), 'sha256': built['image_sha256'],
            'bytes': built['bytes'], 'source_sha256': built['source_sha256']}
