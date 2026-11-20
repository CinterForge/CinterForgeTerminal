"""Inspect the native prebuilt Assimp SDK installed by macOS micromamba.

Conda-forge's Assimp package supplies the import/export library and headers,
not an `assimp` command-line executable on macOS ARM64. Do not pretend a
missing CLI is a broken SDK or manufacture a dummy executable.
"""
from __future__ import annotations

import json
from pathlib import Path


def prebuilt_assimp(prefix: Path) -> dict[str, object]:
    """Return verifiable Assimp SDK evidence, or raise on an incomplete package."""
    prefix = Path(prefix)
    header = prefix / 'include/assimp/cimport.h'
    if not header.is_file() or header.is_symlink():
        raise RuntimeError('Assimp import C API header missing: ' + str(header))
    library = prefix / 'lib/libassimp.dylib'
    if not library.is_file():
        # Some Conda releases only ship the versioned dylib name.
        libraries = sorted((prefix / 'lib').glob('libassimp*.dylib'))
        library = next((p for p in libraries if p.is_file()), library)
    if not library.is_file():
        raise RuntimeError('Assimp native library missing: ' + str(library))
    library = library.resolve()
    if not library.is_relative_to(prefix.resolve()):
        raise RuntimeError('Assimp library escaped the managed Conda prefix')
    size = library.stat().st_size
    if size <= 0:
        raise RuntimeError('Assimp prebuilt library is empty: ' + str(library))
    versions = []
    for receipt in (prefix / 'conda-meta').glob('assimp-*.json'):
        try:
            record = json.loads(receipt.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            raise RuntimeError('Unreadable Assimp Conda receipt: ' + str(receipt)) from exc
        if record.get('name') == 'assimp' and record.get('version'):
            versions.append(str(record['version']))
    if len(versions) != 1:
        raise RuntimeError('Exactly one installed Assimp Conda package receipt required')
    return {'version': versions[0], 'size': size, 'path': str(library),
            'header': str(header), 'check': 'prebuilt-Conda-library-and-headers; live-import-in-Test'}


def assimp_ready(prefix: Path) -> bool:
    try:
        prebuilt_assimp(prefix)
    except (RuntimeError, OSError):
        return False
    return True
