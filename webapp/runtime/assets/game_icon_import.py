"""Import actual Macintosh Finder icons exported by the 68K Games Report.

Only the exact CFIG v1 collection is accepted. The classic guest writes its real
'icl8'/'icl4'/'ICN#' resources into a folder. Infinite Mac downloads
that folder as a ZIP. This module converts a user-supplied ZIP to 32x32
editable PNGs in Builds/Assets/GameIcons only when explicitly invoked.

No placeholder icons or synthetic artwork; counts come from the guest.
Existing PNGs are never overwritten: assets may subsequently be hand-edited
and checked into the repository or published as ordinary Assets bytes.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import struct
import zlib
import zipfile

EXPORT_NAME = 'CinterForgeGameIcons.cfi'
MAX_EXPORT_BYTES = 8 * 1024 * 1024
_TOKEN = re.compile(r'[a-z0-9]{1,31}\Z')


def _palette_8() -> list[tuple[int, int, int]]:
    # Classic Macintosh System 8-bit palette used by icl8, not a generic
    # Windows or browser palette. Index 0 is white, 215 is black.
    levels = (255, 204, 153, 102, 51, 0)
    palette = [(r, g, b) for r in levels for g in levels for b in levels]
    ramp = (238, 221, 187, 170, 136, 119, 85, 68, 34, 17)
    palette += [(v, 0, 0) for v in ramp]
    palette += [(0, v, 0) for v in ramp]
    palette += [(0, 0, v) for v in ramp]
    palette += [(v, v, v) for v in ramp]
    assert len(palette) == 256
    return palette


_PALETTE8 = _palette_8()
_PALETTE4 = (
    (255, 255, 255), (252, 243, 5), (255, 100, 2), (221, 8, 6),
    (242, 8, 132), (70, 0, 165), (0, 0, 212), (2, 171, 234),
    (31, 183, 20), (0, 100, 17), (86, 44, 5), (144, 113, 58),
    (192, 192, 192), (128, 128, 128), (64, 64, 64), (0, 0, 0)
)


def _unpack_collection(source: bytes) -> list[dict]:
    if len(source) < 8 or len(source) > MAX_EXPORT_BYTES or source[:4] != b'CFIG':
        raise ValueError('Not a CinterForge icon collection')
    version, count = struct.unpack_from('>HH', source, 4)
    if version != 1:
        raise ValueError(f'Unsupported CFIG version: {version}')
    records = []
    offset = 8
    seen: set[str] = set()
    os_counts = {71: 0, 76: 0}
    for index in range(count):
        if offset + 8 > len(source):
            raise ValueError(f'Truncated icon header {index}')
        os_code, depth, token_len, name_len, data_len, mask_len = struct.unpack_from('>BBBBHH', source, offset)
        offset += 8
        if os_code not in os_counts or depth not in (1, 4, 8) or not (1 <= token_len <= 31) or not (1 <= name_len <= 63):
            raise ValueError(f'Invalid game entry {index}')
        if data_len != {1: 128, 4: 512, 8: 1024}[depth] or mask_len != 128:
            raise ValueError(f'Incorrect icon resource length at {index}')
        end = offset + token_len + name_len + data_len + mask_len
        if end > len(source):
            raise ValueError(f'Truncated icon resources at {index}')
        token_bytes = source[offset:offset + token_len]
        token = token_bytes.decode('ascii')
        if not _TOKEN.fullmatch(token) or token in seen:
            raise ValueError(f'Unsafe or duplicate game ID: {token!r}')
        seen.add(token)
        offset += token_len
        title = source[offset:offset + name_len].decode('mac_roman')
        offset += name_len
        image = source[offset:offset + data_len]
        offset += data_len
        mask = source[offset:offset + mask_len]
        offset += mask_len
        records.append({'os': os_code, 'command': token, 'title': title,
                        'depth': depth, 'image': image, 'mask': mask})
        os_counts[os_code] += 1
    if offset != len(source):
        raise ValueError('Trailing bytes in icon collection')
    return records


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack('>I', len(payload)) + kind + payload +
            struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))


def _png_for_icon(entry: dict) -> bytes:
    pixels, mask, depth = entry['image'], entry['mask'], entry['depth']
    raw = bytearray()
    for y in range(32):
        raw.append(0)  # PNG scanline, filter=None
        for x in range(32):
            i = y * 32 + x
            visible = mask[i // 8] & (0x80 >> (i % 8))
            if depth == 8:
                r, g, b = _PALETTE8[pixels[i]]
            elif depth == 4:
                packed = pixels[i // 2]
                r, g, b = _PALETTE4[(packed >> 4) if (i & 1) == 0 else (packed & 15)]
            else:
                v = 0 if pixels[i // 8] & (0x80 >> (i % 8)) else 255
                r, g, b = v, v, v
            raw.extend((r, g, b, 255 if visible else 0))
    return (b'\x89PNG\r\n\x1a\n' +
            _chunk(b'IHDR', struct.pack('>IIBBBBB', 32, 32, 8, 6, 0, 0, 0)) +
            _chunk(b'IDAT', zlib.compress(bytes(raw), 9)) +
            _chunk(b'IEND', b''))


def import_icon_bytes(data: bytes, assets_folder: Path) -> dict:
    """Decode the actual exported entries before writing any PNGs."""
    records = _unpack_collection(data)
    rendered = {record['command']: _png_for_icon(record) for record in records}
    target = Path(assets_folder).resolve() / 'GameIcons'
    target.mkdir(parents=True, exist_ok=True)
    created = 0
    preserved = 0
    for command, image in rendered.items():
        output = target / f'{command}.png'
        if output.exists():
            preserved += 1
            continue
        temporary = target / f'{command}.png.tmp'
        try:
            with temporary.open('xb') as fp:
                fp.write(image)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        created += 1
    manifest = {
        'schema': 'cinterforge.game-icons.v1',
        'source': 'Infinite HD application Finder icon resources',
        'total': len(records),
        'games': [
            {'command': r['command'], 'title': r['title'],
             'os': '7.1' if r['os'] == 71 else '7.6',
             'icon': r['command'] + '.png', 'resource': 'icl' + str(r['depth']) if r['depth'] != 1 else 'ICN#'}
            for r in records
        ],
    }
    # Regenerate metadata from the actual current guest catalog, but never
    # overwrite existing PNG artwork (which may have been checked into Git).
    manifest_path = target / 'manifest.json'
    temporary_manifest = target / 'manifest.json.tmp'
    temporary_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary_manifest.replace(manifest_path)
    by_os = {'7.1': sum(1 for r in records if r['os'] == 71),
             '7.6': sum(1 for r in records if r['os'] == 76)}
    return {'total': len(records), 'by_os': by_os,
            'created': created, 'preserved': preserved,
            'output': str(target)}


def _extract_export(path: Path) -> bytes:
    """Read a user-supplied ZIP (or CFIG binary) without scanning Downloads."""
    if path.suffix.lower() != '.zip':
        if path.stat().st_size > MAX_EXPORT_BYTES:
            raise ValueError('Icon export exceeds maximum size')
        return path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        candidates = [x for x in archive.infolist() if
                      not x.is_dir() and x.filename.rsplit('/', 1)[-1] == EXPORT_NAME]
        if len(candidates) != 1 or candidates[0].file_size > MAX_EXPORT_BYTES:
            raise ValueError('ZIP must contain exactly one CinterForgeGameIcons.cfi export')
        with archive.open(candidates[0]) as payload:
            return payload.read(MAX_EXPORT_BYTES + 1)


def import_icon_archive(archive: Path, assets_folder: Path) -> dict:
    return import_icon_bytes(_extract_export(Path(archive)), Path(assets_folder))


def main() -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(description='Import downloaded CinterForge Finder icons (DevTools only).')
    parser.add_argument('archive', type=Path, help='ZIP downloaded from Infinite Mac Uploads')
    args = parser.parse_args()
    # Honor the installed DevTools receipt. Never invent a Builds location or
    # create asset outputs in an ordinary installed Terminal.
    project = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(project / 'webapp'))
    from runtime import CinterForgeDevTools
    builds, _destination, _receipt = CinterForgeDevTools._selected_devtools_receipt()
    result = import_icon_archive(args.archive, builds / 'Assets')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
