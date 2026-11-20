"""CinterForge authored asset and content-pack contracts.

Asset identity and format are owned by the existing CinterForgeServer asset
library.  This module validates and indexes its wire formats for Terminal;
it does NOT generate a second asset converter, exporter or UCI allocator.

The corrected ContentPacks.csv is authoritative about per-asset pack membership.
The example UCI contains a nonstandard timestamp in one row; UCIs are opaque
unique identifiers here, not dates to repair or silently regenerate.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

CSV_COLUMNS = (
    'Asset Name', 'Minecraft Name', 'Command Name', 'Pack Name',
    'Top Level Folder', 'UCI', 'Source Format', 'Blockify Resolution', 'Notes',
)
ASSET_MANIFEST_SCHEMA = 1
BLOCKIFY_RESOLUTIONS = frozenset((0, 16, 32, 48, 96))


def safe_relative(raw: str) -> Path:
    """Path contract for canonical asset members, including glTF dependencies."""
    if not isinstance(raw, str) or not raw or '\\' in raw or '\x00' in raw:
        raise ValueError('Invalid relative asset path')
    p = PurePosixPath(raw)
    if (p.is_absolute() or raw.startswith('//') or re.match(r'^[A-Za-z]:', raw)
            or any(s in ('', '.', '..') or ':' in s for s in raw.split('/'))):
        raise ValueError(f'Unsafe relative asset path: {raw!r}')
    return Path(*p.parts)


@dataclass(frozen=True)
class AssetRow:
    row_number: int
    name: str
    minecraft_name: str
    command_name: str
    pack_name: str
    top_level_folder: str
    uci: str
    source_format: str
    blockify_resolution: int
    notes: str
    source_path: str | None


def load_content_packs(path: Path) -> tuple[AssetRow, ...]:
    """Parse the supplied nine-field sheet without altering source values.

    Duplicate *command names* are permitted because the actual sheet includes
    two such names in different asset records. The UCI, not the command slug,
    is the unique asset key; generators must scope command slugs to a pack.
    """
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f'ContentPacks.csv not found: {path}')
    result: list[AssetRow] = []
    ucis: set[str] = set()
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
            raise ValueError('ContentPacks.csv requires exactly: ' + ', '.join(CSV_COLUMNS))
        for number, record in enumerate(reader, 2):
            if None in record or not all(isinstance(record.get(c), str) and record[c].strip()
                                         for c in CSV_COLUMNS):
                raise ValueError(f'ContentPacks.csv row {number}: incomplete or extra fields')
            uci = record['UCI'].strip()
            if not uci.startswith('CINTERASST') or '#' not in uci or '/' in uci or '\\' in uci:
                raise ValueError(f'ContentPacks.csv row {number}: invalid UCI')
            if uci in ucis:
                raise ValueError(f'ContentPacks.csv row {number}: duplicate UCI {uci}')
            ucis.add(uci)
            try:
                resolution = int(record['Blockify Resolution'].strip())
            except ValueError as error:
                raise ValueError(f'ContentPacks.csv row {number}: invalid Blockify resolution') from error
            if resolution not in BLOCKIFY_RESOLUTIONS:
                raise ValueError(f'ContentPacks.csv row {number}: unsupported resolution {resolution}')
            source_format = record['Source Format'].strip().lower()
            if source_format not in ('glb', 'gltf'):
                raise ValueError(f'ContentPacks.csv row {number}: unsupported source format')
            folder = record['Top Level Folder'].strip()
            safe_relative(folder)
            pack = record['Pack Name'].strip()
            if '/' in pack or '\\' in pack or pack in ('.', '..'):
                raise ValueError(f'ContentPacks.csv row {number}: invalid pack name')
            notes = record['Notes']
            source_path = None
            if notes.startswith('Source Path: '):
                source_path = notes[len('Source Path: '):].strip()
                safe_relative(source_path)
            result.append(AssetRow(
                number, record['Asset Name'].strip(), record['Minecraft Name'].strip(),
                record['Command Name'].strip(), pack, folder, uci,
                source_format, resolution, notes, source_path,
            ))
    return tuple(result)


def by_pack(rows: Iterable[AssetRow]) -> dict[str, tuple[AssetRow, ...]]:
    grouped: dict[str, list[AssetRow]] = {}
    for row in rows:
        grouped.setdefault(row.pack_name, []).append(row)
    return {name: tuple(values) for name, values in grouped.items()}


def command_collisions(rows: Iterable[AssetRow]) -> dict[str, tuple[str, ...]]:
    by_command: dict[str, set[str]] = {}
    for row in rows:
        by_command.setdefault(row.command_name.casefold(), set()).add(row.uci)
    return {slug: tuple(sorted(ids)) for slug, ids in by_command.items() if len(ids) > 1}


def inspect_asset(root: Path) -> dict:
    """Inspect a complete old IF asset folder without rewriting its manifest."""
    if root.is_symlink() or not root.is_dir() or root.name.startswith('.'):
        raise ValueError('Asset folder must be a real UCI directory')
    manifest = root / 'manifest.json'
    if manifest.is_symlink() or not manifest.is_file():
        raise FileNotFoundError('Canonical asset manifest is missing')
    payload = json.loads(manifest.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or payload.get('uci') != root.name or payload.get('version') != 1:
        raise ValueError('Invalid canonical UCI asset manifest')
    representations = payload.get('representations')
    if not isinstance(representations, dict) or 'solid' not in representations:
        raise ValueError('Canonical asset needs its original solid representation')
    checked = []
    for kind, representation in representations.items():
        if kind != 'solid' and not kind.startswith('block_'):
            raise ValueError(f'Unknown asset representation: {kind}')
        if not isinstance(representation, dict) or not isinstance(representation.get('model'), str):
            raise ValueError(f'Invalid asset representation: {kind}')
        for rel in [representation['model'], *representation.get('files', []),
                    *[representation[k] for k in ('edits', 'thumbnail', 'minecraft')
                      if isinstance(representation.get(k), str)]]:
            file = root / safe_relative(rel)
            if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
                raise FileNotFoundError(f'Asset dependency missing or unsafe: {rel}')
            checked.append(rel)
    return {'uci': payload['uci'], 'name': payload.get('name'), 'pack_name': payload.get('pack_name'),
            'representations': tuple(sorted(representations)), 'files_verified': len(set(checked))}


def inventory(assets_root: Path) -> dict:
    """Non-destructive inventory of actual Assets files for backup receipts."""
    files = {}
    if assets_root.is_symlink() or not assets_root.is_dir():
        raise FileNotFoundError(f'Assets directory missing: {assets_root}')
    for path in sorted(assets_root.rglob('*')):
        rel = path.relative_to(assets_root)
        if any(part.startswith('.') for part in rel.parts):
            continue  # Only authored and generated library files are backed up.
        if path.is_symlink():
            raise ValueError(f'Symlinks are forbidden in the asset backup: {rel}')
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f'Unsupported Assets entry: {rel}')
        h = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(chunk)
        files[rel.as_posix()] = {'sha256': h.hexdigest(), 'bytes': path.stat().st_size}
    return {'schema': 'cinterforge.assets.backup.v1', 'files': files}
