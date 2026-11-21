#!/usr/bin/env python3
"""IFPlayer F4 searchable image and animation asset services.

The application ships immutable SQLite indexes.  Mutable thumbnails and selected
asset downloads are cached under the normal CinterForge runtime storage root.
Maintainer builds may also have a full Mixamo mirror under Builds/3D; source
workspaces use that mirror before attempting a network download.
"""
from __future__ import annotations
from .process_launch import run_process
import hashlib
import json
import io
import base64
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import tempfile
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
PD12M_SCHEMA = 'cinterforge.pd12m-search-index.v1'
MIXAMO_SCHEMA = 'cinterforge.mixamo-search-index.v3'
MIXAMO_REPO = 'Linzhan/Mixamo-Animations-Characters'
_LOCK = threading.RLock()
_PIL_TRUNCATED_LOCK = threading.RLock()
_TASKS: dict[str, dict[str, Any]] = {}

def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]

def _external_builds_root() -> Path:
    # Generated development assets live beside Cinter, not inside the checkout.
    from .terminal_paths import cinterforgeBuildsRootPath

    return cinterforgeBuildsRootPath(_project_root())

def _install_roots() -> list[Path]:
    roots: list[Path] = []
    for name in ('CINTERFORGE_INSTALL_ROOT', 'CINTERFORGE_STORAGE_ROOT'):
        value = os.environ.get(name, '').strip()
        if value:
            roots.append(Path(value).expanduser())
    roots.append(_external_builds_root())
    executable = Path(os.environ.get('CINTERFORGE_APP_ROOT', '') or Path.cwd())
    roots.extend([executable, executable / 'CinterForge', executable / 'Resources' / 'CinterForge'])
    try:
        resolved = Path(__file__).resolve()
        for parent in resolved.parents:
            roots.extend([parent / 'CinterForge', parent / 'Resources' / 'CinterForge'])
    except Exception:
        pass
    unique = []
    seen = set()
    for root in roots:
        key = str(root.resolve()) if root.exists() else str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique

def _index_path(kind: str) -> Path:
    env = 'CINTERFORGE_PD12M_INDEX' if kind == 'image' else 'CINTERFORGE_MIXAMO_INDEX'
    value = os.environ.get(env, '').strip()
    if value:
        path = Path(value).expanduser()
        if path.is_file():
            return path
    relative = Path('Media') / ('PD12MSearch' if kind == 'image' else 'MixamoSearch') / ('pd12m_index.db' if kind == 'image' else 'mixamo_index.db')
    for root in _install_roots():
        # Installed layouts may expose Media directly or beneath Builds.
        # Development Builds is already an explicit root, not a child of Cinter.
        candidates = [root / relative]
        if root != _external_builds_root():
            candidates.append(root / 'Builds' / relative)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return _external_builds_root() / relative

def _storage_root() -> Path:
    value = os.environ.get('CINTERFORGE_STORAGE_ROOT', '').strip()
    if value:
        root = Path(value).expanduser()
    elif os.name == 'nt':
        root = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'CinterForge'
    elif sys_platform() == 'darwin':
        root = Path.home() / 'Library' / 'Application Support' / 'CinterForge'
    else:
        root = Path.home() / '.cinterforge'
    path = root / '3D' / 'AssetLibrary'
    path.mkdir(parents=True, exist_ok=True)
    return path

def sys_platform() -> str:
    import sys
    return sys.platform

def _connect(kind: str) -> sqlite3.Connection:
    path = _index_path(kind)
    if not path.is_file():
        raise FileNotFoundError(f'{kind.title()} asset index is not installed: {path}')
    db = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=10)
    expected = PD12M_SCHEMA if kind == 'image' else MIXAMO_SCHEMA
    meta = dict(db.execute('SELECT key,value FROM meta'))
    if meta.get('schema') != expected:
        db.close()
        raise RuntimeError(f"Unsupported {kind} asset index schema: {meta.get('schema')!r}")
    return db

def _fts_query(text: str) -> str:
    tokens = [token.replace('"', '') for token in str(text or '').split() if token.strip()]
    return ' AND '.join((f'"{token}"*' for token in tokens[:12]))

def search_images(text: str, limit: int=50, offset: int=0) -> dict[str, Any]:
    query = _fts_query(text)
    limit = max(1, min(240, int(limit)))
    offset = max(0, int(offset or 0))
    db = _connect('image')
    if query:
        total = int(db.execute('SELECT COUNT(*) FROM images_fts WHERE images_fts MATCH ?', (query,)).fetchone()[0])
        rows = db.execute('SELECT i.sample_id,i.caption,i.source,i.image_url,i.license,i.license_uri,i.width,i.height,i.mime_type\n          FROM images_fts f JOIN images i ON i.rowid=f.rowid WHERE images_fts MATCH ? ORDER BY bm25(images_fts) LIMIT ? OFFSET ?', (query, limit, offset)).fetchall()
    else:
        total = int(db.execute('SELECT COUNT(*) FROM images').fetchone()[0])
        rows = db.execute('SELECT sample_id,caption,source,image_url,license,license_uri,width,height,mime_type FROM images ORDER BY rowid LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    db.close()
    return {'kind': 'image', 'total': total, 'offset': offset, 'limit': limit, 'results': [{'id': r[0], 'title': r[1], 'caption': r[1], 'source': r[2], 'source_url': r[3], 'license': r[4], 'license_uri': r[5], 'width': r[6], 'height': r[7], 'mime_type': r[8], 'thumbnail_url': f'/assets/image/thumb/{r[0]}'} for r in rows]}

def character_thumbnail_relative_path(character_id: str) -> str:
    return f'Thumbnails/Mixamo/{hashlib.sha256(str(character_id).encode()).hexdigest()[:24]}.jpg'

def character_thumbnail_recipe_relative_path(character_id: str) -> str:
    return f'Thumbnails/Mixamo/{hashlib.sha256(str(character_id).encode()).hexdigest()[:24]}.recipe.json'

def character_thumbnail_recipe(character_id: str) -> dict[str, Any]:
    from runtime.assets.asset_management import assets_root
    root = assets_root().expanduser().resolve()
    path = (root / Path(character_thumbnail_recipe_relative_path(character_id))).resolve()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}

def character_thumbnail_path(character_id: str) -> Path:
    from runtime.assets.asset_management import assets_root
    relative = character_thumbnail_relative_path(character_id)
    path = (assets_root().expanduser().resolve() / Path(relative)).resolve()
    if not path.is_file():
        raise FileNotFoundError(f'Mixamo thumbnail is not cached locally: {relative}')
    return path

def save_character_thumbnail(character_id: str, data_url: str, recipe: str='', animation_id: str='') -> tuple[Path, str]:
    """Encode one rendered Mixamo frame into the canonical local Assets tree."""
    character_id = str(character_id or '').strip()
    if not character_id:
        raise ValueError('character id is required')
    value = str(data_url or '').strip()
    if ',' not in value or not value.casefold().startswith('data:image/'):
        raise ValueError('character thumbnail must be an image data URL')
    header, encoded = value.split(',', 1)
    if ';base64' not in header.casefold():
        raise ValueError('character thumbnail must be base64 encoded')
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('character thumbnail is too large')
    from PIL import Image
    from runtime.assets.asset_management import assets_root
    relative = character_thumbnail_relative_path(character_id)
    root = assets_root().expanduser().resolve()
    path = (root / Path(relative)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f'Mixamo thumbnail path escapes local Assets root: {relative}') from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(raw)) as opened:
        image = opened.convert('RGB')
        image.thumbnail((512, 512))
        image.save(path, 'JPEG', quality=82, optimize=True)
    recipe_path = (root / Path(character_thumbnail_recipe_relative_path(character_id))).resolve()
    recipe_path.write_text(json.dumps({'schema': 'cinterforge.mixamo-character-thumbnail-cache.v1', 'recipe': str(recipe or '').strip(), 'animation_id': str(animation_id or '').strip()}, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return (path, relative)

def search_3d(text: str, limit: int=50, offset: int=0) -> dict[str, Any]:
    query = _fts_query(text)
    limit = max(1, min(240, int(limit)))
    offset = max(0, int(offset or 0))
    db = _connect('animation')
    if query:
        total = int(db.execute('SELECT COUNT(*) FROM characters_fts WHERE characters_fts MATCH ?', (query,)).fetchone()[0])
        rows = db.execute('SELECT c.character_id,c.file,c.name,c.bone_count,c.covers_animation_rig FROM characters_fts f JOIN characters c ON c.rowid=f.rowid WHERE characters_fts MATCH ? ORDER BY bm25(characters_fts) LIMIT ? OFFSET ?', (query, limit, offset)).fetchall()
    else:
        total = int(db.execute('SELECT COUNT(*) FROM characters').fetchone()[0])
        rows = db.execute('SELECT character_id,file,name,bone_count,covers_animation_rig FROM characters ORDER BY rowid LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    db.close()
    results = []
    for r in rows:
        has_thumbnail = True
        results.append({'id': r[0], 'provider': 'mixamo', 'file': r[1], 'name': r[2], 'title': r[2], 'bone_count': r[3], 'covers_animation_rig': bool(r[4]), 'license': 'Mixamo', 'source': 'Mixamo', 'source_url': 'https://huggingface.co/datasets/Linzhan/Mixamo-Animations-Characters', 'has_thumbnail': has_thumbnail})
    return {'kind': '3d', 'total': total, 'offset': offset, 'limit': limit, 'results': results}

def search_animations(text: str, limit: int=50, offset: int=0) -> dict[str, Any]:
    query = _fts_query(text)
    limit = max(1, min(10000, int(limit)))
    offset = max(0, int(offset or 0))
    db = _connect('animation')
    if query:
        total = int(db.execute('SELECT COUNT(*) FROM animations_fts WHERE animations_fts MATCH ?', (query,)).fetchone()[0])
        rows = db.execute('SELECT a.motion_id,a.file,a.prompt,a.description,a.text,a.frames\n          FROM animations_fts f JOIN animations a ON a.rowid=f.rowid WHERE animations_fts MATCH ? ORDER BY bm25(animations_fts) LIMIT ? OFFSET ?', (query, limit, offset)).fetchall()
    else:
        total = int(db.execute('SELECT COUNT(*) FROM animations').fetchone()[0])
        rows = db.execute('SELECT motion_id,file,prompt,description,text,frames FROM animations ORDER BY rowid LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    db.close()
    return {'kind': 'animation', 'total': total, 'offset': offset, 'limit': limit, 'results': [{'id': r[0], 'file': r[1], 'title': r[2] or Path(r[1]).stem.replace('_', ' '), 'description': r[3] or r[4], 'text': r[4], 'frames': r[5], 'source': 'Mixamo', 'source_url': 'https://www.mixamo.com/'} for r in rows]}

def _image_record(sample_id: str) -> tuple[str, str]:
    db = _connect('image')
    row = db.execute('SELECT image_url,mime_type FROM images WHERE sample_id=?', (sample_id,)).fetchone()
    db.close()
    if not row:
        raise KeyError(f'PD12M image not found: {sample_id}')
    return (str(row[0]), str(row[1] or 'image/jpeg'))

def image_thumbnail(sample_id: str) -> Path:
    cache = _storage_root() / 'PD12M' / 'Thumbnails'
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f'{hashlib.sha256(sample_id.encode()).hexdigest()[:24]}.jpg'
    from PIL import Image, ImageFile
    if path.is_file() and path.stat().st_size > 0:
        # Older builds could leave the provider's truncated source JPEG at the
        # cache path when thumbnail conversion failed. Do not trust size alone:
        # validate the cached JPEG once and repair poisoned cache entries.
        try:
            with Image.open(path) as cached:
                cached.load()
            return path
        except Exception as exc:
            try:
                path.unlink()
            except OSError:
                pass
            try:
                from .asset_management import log as asset_log
                asset_log(f'pd12m-thumbnail-cache-invalid id={sample_id} path={path} error={exc!r}')
            except Exception:
                pass
    url, _ = _image_record(sample_id)
    request = Request(url, headers={'User-Agent': 'CinterForge/1.0'})
    with urlopen(request, timeout=30) as response:
        data = response.read(12 * 1024 * 1024)
    # A small but real subset of PD12M source JPEGs are missing trailing bytes.
    # Pillow can safely decode those when LOAD_TRUNCATED_IMAGES is enabled. The
    # setting is process-global, so serialize this narrow decode section and
    # restore the prior value immediately afterward instead of weakening every
    # image decoder in IFPlayer. The generated cached thumbnail is a normal JPEG.
    with _PIL_TRUNCATED_LOCK:
        previous = bool(ImageFile.LOAD_TRUNCATED_IMAGES)
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            with Image.open(io.BytesIO(data)) as opened:
                image = opened.convert('RGB')
                image.thumbnail((512, 512))
                temp = path.with_suffix('.tmp.jpg')
                image.save(temp, 'JPEG', quality=82, optimize=True)
                temp.replace(path)
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous
    return path

def _download_image(sample_id: str) -> Path:
    url, mime = _image_record(sample_id)
    suffix = {'image/png': '.png', 'image/webp': '.webp', 'image/jpeg': '.jpg', 'image/jpg': '.jpg'}.get(mime.casefold(), Path(url.split('?', 1)[0]).suffix or '.jpg')
    dest = _storage_root() / 'PD12M' / 'Images' / f'{sample_id}{suffix}'
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    temp = dest.with_suffix(dest.suffix + '.part')
    request = Request(url, headers={'User-Agent': 'CinterForge/1.0'})
    with urlopen(request, timeout=60) as response, temp.open('wb') as output:
        shutil.copyfileobj(response, output, 1024 * 1024)
    temp.replace(dest)
    return dest

def _mixamo_record(table: str, key_name: str, asset_id: str) -> str:
    db = _connect('animation')
    row = db.execute(f'SELECT file FROM {table} WHERE {key_name}=?', (asset_id,)).fetchone()
    db.close()
    if not row:
        raise KeyError(f'Mixamo asset not found: {asset_id}')
    return str(row[0])

def _animation_record(motion_id: str) -> str:
    return _mixamo_record('animations', 'motion_id', motion_id)

def _mixamo_revision() -> str | None:
    path = _index_path('animation')
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=10)
        row = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        db.close()
    except Exception:
        return None
    value = str(row[0] if row else '').strip()
    return value if value and value != 'unknown' else None

def _mixamo_index_asset_root() -> Path | None:
    """Return the checkout that was used to build the installed Mixamo index.

    The index is intentionally relocatable, but development/debug builds often
    stage only the SQLite index into the app while the large Mixamo payload stays
    in Builds/MaintainerTools.  The index already records that checkout as
    meta.asset_root; use it as the authoritative local payload location before
    falling back to install-root discovery or a network fetch.
    """
    path = _index_path('animation')
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=10)
        row = db.execute("SELECT value FROM meta WHERE key='asset_root'").fetchone()
        db.close()
    except Exception:
        return None
    value = str(row[0] if row else '').strip()
    if not value:
        return None
    root = Path(value).expanduser()
    return root if root.is_dir() else None

def _local_mixamo_mirror(relative: str) -> Path | None:
    candidates = []
    indexed_root = _mixamo_index_asset_root()
    if indexed_root is not None:
        candidates.append(indexed_root / relative)
    candidates.extend([_external_builds_root() / 'MaintainerTools' / 'Mixamo' / 'repo' / relative, _external_builds_root() / 'Media' / 'MixamoSearch' / 'mirror' / relative])
    for root in _install_roots():
        candidates.extend([root / 'MaintainerTools' / 'Mixamo' / 'repo' / relative, root / 'Media' / 'MixamoSearch' / 'mirror' / relative])
        if root != _external_builds_root():
            candidates.extend([root / 'Builds' / 'MaintainerTools' / 'Mixamo' / 'repo' / relative, root / 'Builds' / 'Media' / 'MixamoSearch' / 'mirror' / relative])
    return next((p for p in candidates if p.is_file() and p.stat().st_size > 0), None)

def _download_3d(asset_id: str) -> Path:
    relative = _mixamo_record('characters', 'character_id', asset_id)
    # Character preview is read-only. If setup already materialized the GLB,
    # serve that file directly instead of needlessly copying it into runtime
    # storage first. This is important for embedded/app-bundle launches: the
    # immutable search index and the large setup-managed Mixamo payload can
    # legitimately live in different roots.
    local = _local_mixamo_mirror(relative)
    if local is not None:
        return local
    dest = _storage_root() / 'Mixamo' / 'Characters' / Path(relative).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    from huggingface_hub import hf_hub_download
    source = Path(hf_hub_download(repo_id=MIXAMO_REPO, repo_type='dataset', filename=relative, revision=_mixamo_revision()))
    shutil.copy2(source, dest)
    return dest

def _download_animation(motion_id: str) -> Path:
    relative = _animation_record(motion_id)
    dest = _storage_root() / 'Mixamo' / 'Animations' / Path(relative).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    local = _local_mixamo_mirror(relative)
    if local:
        shutil.copy2(local, dest)
        return dest
    from huggingface_hub import hf_hub_download
    source = Path(hf_hub_download(repo_id=MIXAMO_REPO, repo_type='dataset', filename=relative, revision=_mixamo_revision()))
    shutil.copy2(source, dest)
    return dest

def _fbx2gltf() -> Path:
    """Resolve the product/maintainer FBX2glTF converter without machine-specific paths."""
    explicit = os.environ.get('CINTERFORGE_FBX2GLTF', '').strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    exe = 'FBX2glTF.exe' if os.name == 'nt' else 'FBX2glTF'
    for root in _install_roots():
        candidates.extend([root / 'Tools' / 'FBX2glTF' / exe, root / 'MaintainerTools' / 'FBX2glTF' / exe])
        if root != _external_builds_root():
            candidates.append(root / 'Builds' / 'MaintainerTools' / 'FBX2glTF' / exe)
    found = shutil.which(exe) or shutil.which('FBX2glTF')
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError('FBX2glTF is not installed in the CinterForge tool runtime; Mixamo preview requires the setup-managed FBX→GLB converter.')

def _runtime_character_glb(asset_id: str) -> Path:
    """Materialize one Mixamo character FBX as the Babylon-loadable preview GLB.

    This is the same preview boundary used by preview-model: the canonical managed
    asset remains the source FBX; the GLB is a disposable browser representation.
    """
    source = _download_3d(asset_id)
    if source.suffix.casefold() == '.glb':
        return source
    dest = _storage_root() / 'Mixamo' / 'RuntimeCharacters' / f'{asset_id}.glb'
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 20:
        return dest
    converter = _fbx2gltf()
    temp = dest.with_suffix('.part.glb')
    temp.unlink(missing_ok=True)
    # FBX2glTF's -o value is an output stem; --binary emits the .glb sibling.
    stem = temp.with_suffix('')
    command = [str(converter), '--binary', '--input', str(source), '--output', str(stem), '--anim-framerate', 'bake30']
    completed = run_process(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180, check=False)
    produced = Path(str(stem) + '.glb')
    if completed.returncode != 0 or not produced.is_file():
        raise RuntimeError(f"FBX2glTF failed for Mixamo character {source.name}: {(completed.stdout or '').strip()[-1200:]}")
    produced.replace(dest)
    return dest

def _runtime_animation_glb(motion_id: str) -> Path:
    """Materialize one animation-only FBX as a Babylon-loadable GLB, cached by motion id."""
    source = _download_animation(motion_id)
    dest = _storage_root() / 'Mixamo' / 'RuntimeAnimations' / f'{motion_id}.glb'
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 20:
        return dest
    converter = _fbx2gltf()
    temp = dest.with_suffix('.part.glb')
    temp.unlink(missing_ok=True)
    stem = temp.with_suffix('')
    command = [str(converter), '--binary', '--input', str(source), '--output', str(stem), '--anim-framerate', 'bake30']
    completed = run_process(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180, check=False)
    produced = Path(str(stem) + '.glb')
    if completed.returncode != 0 or not produced.is_file():
        raise RuntimeError(f"FBX2glTF failed for {source.name}: {(completed.stdout or '').strip()[-1200:]}")
    produced.replace(dest)
    return dest

def saved_animation_preview_path(relative_path: str) -> Path:
    raise RuntimeError('saved animation previews are Cloudflare assets; use the portable .preview.glb identity')

def preview_asset_path(kind: str, asset_id: str) -> Path:
    kind = str(kind).strip().casefold()
    if kind == '3d':
        return _runtime_character_glb(asset_id)
    if kind == 'animation':
        return _runtime_animation_glb(asset_id)
    raise ValueError(f'Unsupported preview asset kind: {kind}')

def _task_key(kind: str, asset_id: str) -> str:
    return f'{kind}:{asset_id}'

def _download_metadata(kind: str, asset_id: str) -> tuple[str, dict[str, Any]]:
    if kind == 'image':
        db = _connect('image')
        row = db.execute('SELECT sample_id,caption,source,image_url,license,license_uri,width,height,mime_type FROM images WHERE sample_id=?', (asset_id,)).fetchone()
        db.close()
        if not row:
            raise KeyError(f'PD12M image not found: {asset_id}')
        return ('pd12m', {'id': row[0], 'title': row[1], 'caption': row[1], 'source': row[2], 'source_url': row[3], 'license': row[4], 'license_uri': row[5], 'width': row[6], 'height': row[7], 'mime_type': row[8]})
    if kind == '3d':
        db = _connect('animation')
        row = db.execute('SELECT character_id,file,name,bone_count,covers_animation_rig FROM characters WHERE character_id=?', (asset_id,)).fetchone()
        db.close()
        if not row:
            raise KeyError(f'Mixamo character not found: {asset_id}')
        return ('mixamo', {'id': row[0], 'file': row[1], 'name': row[2], 'title': row[2], 'bone_count': row[3], 'covers_animation_rig': bool(row[4]), 'license': 'Mixamo', 'source': 'Mixamo', 'source_url': 'https://huggingface.co/datasets/Linzhan/Mixamo-Animations-Characters', 'mixamo_asset_type': 'character', 'mixamo_compatible': True})
    db = _connect('animation')
    row = db.execute('SELECT motion_id,file,prompt,description,text,frames FROM animations WHERE motion_id=?', (asset_id,)).fetchone()
    db.close()
    if not row:
        raise KeyError(f'Mixamo animation not found: {asset_id}')
    return ('mixamo', {'id': row[0], 'file': row[1], 'title': row[2] or Path(row[1]).stem.replace('_', ' '), 'description': row[3] or row[4], 'text': row[4], 'frames': row[5], 'source': 'Mixamo', 'source_url': 'https://www.mixamo.com/', 'license': 'Mixamo', 'mixamo_asset_type': 'animation', 'mixamo_compatible': True})

def start_download(kind: str, asset_id: str, query: str='') -> dict[str, Any]:
    """Acquire a provider asset into the local canonical Assets intake tree.

    Publication happens only after the browser conversion pipeline has produced
    and validated the managed UCI representation.
    """
    from .asset_management import assets_root, log as asset_log, search_name
    kind = str(kind).casefold()
    key = _task_key(kind, asset_id)
    if kind not in {'image', 'animation', '3d'}:
        raise ValueError(f'Unsupported asset kind: {kind}')
    with _LOCK:
        state = _TASKS.get(key)
        if state and (not state.get('complete')):
            return dict(state)
        state = {'kind': kind, 'id': asset_id, 'query': query, 'stage': 'queued', 'message': 'Queued…', 'complete': False, 'error': '', 'saved_path': '', 'started': time.time()}
        _TASKS[key] = state

    def worker():
        try:
            with _LOCK:
                _TASKS[key].update(stage='downloading', message='Downloading…')
            provider, metadata = _download_metadata(kind, asset_id)
            title = search_name(query, fallback=str(metadata.get('title') or metadata.get('name') or asset_id).split()[0])
            safe = ''.join((ch if ch.isalnum() or ch in '._-' else '_' for ch in title)).strip('._-') or 'asset'
            files = {}
            rows = []
            if kind == 'image':
                source = _download_image(asset_id)
                rel = f"Images/{safe}/{safe}{source.suffix or '.jpg'}"
                files[rel] = source
                rows.append({'path': rel, 'name': Path(rel).name, 'size': source.stat().st_size, 'kind': 'image'})
            elif kind == '3d':
                source = _runtime_character_glb(asset_id)
                rel = f'Animated/Mixamo/{safe}/{safe}.glb'
                files[rel] = source
                rows.append({'path': rel, 'name': Path(rel).name, 'size': source.stat().st_size, 'kind': '3d'})
            else:
                source = _download_animation(asset_id)
                preview = _runtime_animation_glb(asset_id)
                rel = f'Animations/{safe}/{safe}.fbx'
                preview_rel = f'Animations/{safe}/{safe}.preview.glb'
                files[rel] = source
                files[preview_rel] = preview
                rows.append({'path': preview_rel, 'name': Path(preview_rel).name, 'size': preview.stat().st_size, 'kind': '3d'})
            root = assets_root().resolve()
            for relative_path, source_path in files.items():
                destination = (root / relative_path).resolve()
                if root not in destination.parents:
                    raise ValueError(f'asset download path escapes Assets: {relative_path}')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)
            with _LOCK:
                _TASKS[key].update(stage='complete', message='Downloaded; converting managed asset…', complete=True, saved_path=rel, relative_path=rel, needs_conversion=True)
            asset_log(f'download-task-local-complete kind={kind} id={asset_id} saved={rel}')
        except Exception as exc:
            with _LOCK:
                _TASKS[key].update(stage='failed', message=str(exc), complete=True, error=str(exc))
            asset_log(f'download-task-failed kind={kind} id={asset_id} query={query!r} error={exc!r}')
    threading.Thread(target=worker, daemon=True, name=f'ifasset-{kind}-{asset_id[:8]}').start()
    return dict(state)

def download_status(kind: str, asset_id: str) -> dict[str, Any]:
    key = _task_key(kind.casefold(), asset_id)
    with _LOCK:
        state = dict(_TASKS.get(key) or {'kind': kind, 'id': asset_id, 'stage': 'idle', 'message': 'Ready', 'complete': False, 'error': '', 'saved_path': ''})
    if state.get('started'):
        state['elapsed_seconds'] = max(0.0, time.time() - float(state['started']))
    return state
