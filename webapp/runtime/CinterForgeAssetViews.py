"""Terminal bridge for the authoritative UCI library and Terminal asset gallery.

Terminal owns the HTTP/tab/native-session glue, canonical asset gallery and
Babylon preview. Converter uses Terminal-built artifacts, not the reverse.
Import copies an existing canonical UCI asset or saves an original solid model;
voxel conversion remains owned by the separately packaged Blockify engine.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from runtime.CinterForgeAssets import inspect_asset, safe_relative

ASSET_DIR = re.compile(r'^CINTERASST[A-Za-z0-9#_-]{5,90}$')
MODELS = frozenset(('.glb', '.gltf'))
VIEW_EXTENSIONS = frozenset(('.html', '.js', '.css', '.gltf', '.glb', '.bin', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.json', '.ktx2', '.dds'))
MAX_IMPORT_BYTES = 2 * 1024**3
MAX_ZIP_MEMBERS = 25000
MAX_UNPACK_BYTES = 3 * 1024**3


def root_for_state(state_root: Path) -> Path:
    """Use the ONE explicitly selected DevTools Builds/Assets library.

    Installed Terminal state belongs under Steam, but the user's established
    asset library belongs to their selected sibling Builds. Never guess a
    ~/Source path or materialize a Builds directory merely to view assets.
    """
    explicit = os.environ.get('CINTERFORGE_ASSETS_ROOT', '').strip()
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_absolute():
            raise RuntimeError('CINTERFORGE_ASSETS_ROOT must be absolute')
    else:
        from runtime import CinterForgeDevTools
        try:
            builds = CinterForgeDevTools._devtools_builds_root()
        except RuntimeError:
            if os.environ.get('CINTER_BUILDS_ROOT', '').strip():
                raise  # An explicit but invalid selection must not be ignored.
            builds = None  # No selected DevTools source; installed-app state only.
        root = (builds if builds is not None else Path(state_root)) / 'Assets'
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise RuntimeError('Assets library root is not a normal directory: ' + str(root))
    return root


def root_for(server) -> Path:
    return root_for_state(server.mutable_builds)


def _gallery_module():
    """Load the Terminal-owned asset gallery from this runtime's resource tree.

    No Converter checkout, Converter Build, or network fallback is permitted.
    """
    from runtime import CinterForgeSettings
    source = CinterForgeSettings.ASSETS / 'data/asset_engine/api/asset_gallery.py'
    if source.is_symlink() or not source.is_file():
        raise RuntimeError('Terminal asset engine not staged; run ./Cinter setup, build, stage')
    spec = importlib.util.spec_from_file_location('_cinterforge_terminal_asset_gallery', source)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load staged Terminal asset gallery')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _viewer_available(gallery, builds: Path) -> None:
    """The existing renderer needs locally pinned Babylon, not a CDN fallback."""
    pins = json.loads((gallery.REPO / 'build/browser-dependencies.json').read_text(encoding='utf-8'))
    choices = (builds / 'Dependencies/Babylon' / pins['version'], gallery.REPO / 'web/vendor',
               gallery.REPO / 'frontend/vendor', gallery.REPO / 'vendor')
    if not any(all((folder / name).is_file() for name in gallery.VIEWER_FILES) for folder in choices):
        raise RuntimeError('Pinned Babylon viewer unavailable. Run ./Cinter setup, build, stage.')


def report(server) -> dict:
    gallery = _gallery_module()
    root = root_for(server)
    if not root.is_dir():
        raise FileNotFoundError('Assets library not found at ' + str(root) + '; select the existing DevTools Builds source or run assets setup.')
    _viewer_available(gallery, root.parent)
    result = gallery.generate(root.parent)
    if result.get('viewer') != 'pinned-local':
        raise RuntimeError('Terminal gallery did not use verified local Babylon files')
    return {'url': '/assets-library/AssetGallery.html', 'count': result['assets']}


def _find_uci(root: Path, uci: str) -> Path:
    if not ASSET_DIR.fullmatch(uci):
        raise ValueError('Invalid UCI')
    matches = []
    if root.is_dir():
        for asset in root.rglob(uci):
            if (asset.is_dir() and not asset.is_symlink() and
                    not any(parent.is_symlink() for parent in asset.parents if
                            parent == root or root in parent.parents)):
                matches.append(asset)
    if len(matches) != 1:
        raise FileNotFoundError('Asset UCI not found or ambiguous: ' + uci)
    folder = matches[0]
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict):
        raise ValueError('Invalid asset manifest')
    if isinstance(manifest.get('representations'), dict):
        inspect_asset(folder)  # New canonical UCI representation format.
    elif isinstance(manifest.get('versions'), list):
        # Existing IFEditor UCI version format. Never mutate its manifest.
        for version in manifest['versions']:
            if not isinstance(version, dict) or not isinstance(version.get('file'), str):
                raise ValueError('Invalid legacy UCI asset version')
            source = folder / safe_relative(version['file'])
            if not source.is_file() or not source.resolve().is_relative_to(folder.resolve()):
                raise ValueError('Legacy UCI asset dependency is missing or unsafe')
    else:
        raise ValueError('Unsupported UCI asset manifest')
    return folder


def view(server, uci: str) -> dict:
    folder = _find_uci(root_for(server), uci)
    _viewer_available(_gallery_module(), root_for(server).parent)
    return {'url': '/assets-library/view/' + quote(uci, safe=''), 'uci': uci,
            'name': json.loads((folder / 'manifest.json').read_text(encoding='utf-8')).get('name') or uci}


def queue_tab(server, kind: str, **args) -> dict:
    if kind == 'report':
        request = {'kind': 'report', **report(server)}
    elif kind == 'view':
        request = {'kind': 'view', **view(server, args['uci'])}
    elif kind == 'picker':
        request = {'kind': 'picker'}
    else:
        raise ValueError('Unknown Assets tab action')
    with server.asset_actions_lock:
        key = (request['kind'], request.get('uci'))
        server.asset_actions = type(server.asset_actions)(
            row for row in server.asset_actions if (row['kind'], row.get('uci')) != key)
        server.asset_actions.append(request)
    return request


def _manifest_category(manifest: dict) -> str:
    value = str(manifest.get('category') or 'Objects').strip()
    return value if re.fullmatch(r'[A-Za-z][A-Za-z0-9 _-]{0,48}', value) else 'Objects'


def _files_equal(first: Path, second: Path) -> bool:
    if not first.is_dir() or first.is_symlink():
        return False
    def fingerprint(root):
        result = {}
        for file in root.rglob('*'):
            if file.is_symlink():
                raise ValueError('Symlinked asset dependency forbidden')
            if file.is_file():
                digest = hashlib.sha256()
                with file.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                        digest.update(chunk)
                result[file.relative_to(root).as_posix()] = (file.stat().st_size, digest.hexdigest())
        return result
    return fingerprint(first) == fingerprint(second)


def _commit_canonical(staged: Path, assets_root: Path) -> dict:
    info = inspect_asset(staged)
    # Canonical assets may contain ancillary animation files not individually
    # named in manifest.json. Never follow a symlink from those assets.
    for member in staged.rglob('*'):
        if member.is_symlink() or not (member.is_file() or member.is_dir()):
            raise ValueError('Canonical asset contains unsafe member: ' + str(member))
    category = _manifest_category(json.loads((staged / 'manifest.json').read_text(encoding='utf-8')))
    # UCI identity is global across all categories, not scoped by folder.
    # Never create two different assets with the same UCI just because their
    # legacy category labels differ.
    if assets_root.is_dir():
        for previous_category in assets_root.iterdir():
            if not previous_category.is_dir() or previous_category.is_symlink():
                continue
            old = previous_category / staged.name
            if old.exists() or old.is_symlink():
                if _files_equal(old, staged):
                    return {'uci': staged.name, 'category': previous_category.name, 'existing': True}
                raise FileExistsError('UCI already exists with different data: ' + staged.name)
    dest = assets_root / category / staged.name
    if dest.exists() or dest.is_symlink():
        raise FileExistsError('Asset destination is already occupied: ' + staged.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.parent.is_symlink() or assets_root.is_symlink():
        raise RuntimeError('Unsafe destination Assets category')
    shutil.copytree(staged, dest, symlinks=False)
    try:
        inspect_asset(dest)
    except Exception:
        shutil.rmtree(dest)
        raise
    return {'uci': info['uci'], 'category': category, 'existing': False}


def _import_canonical_directory(folder: Path, assets_root: Path) -> dict:
    if folder.is_symlink() or not folder.is_dir() or not ASSET_DIR.fullmatch(folder.name):
        raise ValueError('Expected a canonical CINTERASST... directory')
    return _commit_canonical(folder, assets_root)


def _import_archive(source: Path, assets_root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix='cinter-asset-unpack-') as temporary:
        scratch = Path(temporary)
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS or sum(f.file_size for f in members) > MAX_UNPACK_BYTES:
                raise ValueError('Asset ZIP exceeds permitted unpacked size/entry count')
            folders = set()
            for info in members:
                raw = info.filename.rstrip('/')
                if not raw or raw == '__MACOSX' or raw.startswith('__MACOSX/'):
                    continue
                # Finder metadata is never part of a canonical UCI asset.
                if any(segment.startswith('.') or segment.startswith('._') for segment in raw.split('/')):
                    continue
                relative = safe_relative(raw)
                # Finder archives include the top-level UCI directory entry;
                # validate and count it, but do not require a child path there.
                if not ASSET_DIR.fullmatch(relative.parts[0]) or (
                        len(relative.parts) == 1 and not info.is_dir()):
                    raise ValueError('ZIP must contain exactly one canonical UCI asset directory')
                folders.add(relative.parts[0])
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ValueError('Archive symlinks/special members are forbidden')
                if info.is_dir():
                    continue
                dest = scratch / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as reader, dest.open('wb') as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                if dest.stat().st_size != info.file_size:
                    raise ValueError('Truncated asset ZIP entry')
            if len(folders) != 1:
                raise ValueError('Expected exactly one canonical asset in ZIP')
        return _commit_canonical(scratch / next(iter(folders)), assets_root)


def _gltf_dependencies(source: Path) -> list[Path]:
    if source.suffix.lower() != '.gltf':
        if source.open('rb').read(4) != b'glTF':
            raise ValueError('GLB header not recognized')
        return []
    doc = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(doc, dict) or not isinstance(doc.get('asset'), dict):
        raise ValueError('Invalid glTF file')
    dependencies = []
    for key in ('buffers', 'images'):
        for row in doc.get(key, []):
            uri = row.get('uri') if isinstance(row, dict) else None
            if not isinstance(uri, str) or not uri or uri.startswith('data:'):
                continue
            # GLTF dependencies must already be local siblings. HTTP URLs,
            # nested path tricks, and outside-directory references are unsafe.
            rel = safe_relative(uri)
            dep = source.parent / rel
            if dep.is_symlink() or not dep.is_file() or not dep.resolve().is_relative_to(source.parent.resolve()):
                raise FileNotFoundError('GLTF dependency missing or unsafe: ' + uri)
            dependencies.append(dep)
    return list(dict.fromkeys(dependencies))


def _import_model(source: Path, assets_root: Path) -> dict:
    dependencies = _gltf_dependencies(source)
    stem = re.sub(r'[^A-Za-z0-9]+', '', source.stem.upper())[:8] or 'ASSET'
    category = 'Objects'
    assets_root.mkdir(parents=True, exist_ok=True)
    category_root = assets_root / category
    category_root.mkdir(parents=True, exist_ok=True)
    if category_root.is_symlink():
        raise ValueError('Unsafe destination category')
    with tempfile.TemporaryDirectory(prefix='cinter-import-', dir=category_root) as temporary:
        staged = Path(temporary)
        solid = staged / 'solid'
        solid.mkdir()
        # Keep source basenames, and their dependency-relative URI references,
        # intact. This is a solid-only import, NOT a fabricated Blockify result.
        shutil.copy2(source, solid / source.name)
        files = []
        for file in dependencies:
            relative = file.relative_to(source.parent)
            target = solid / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.resolve() == (solid / source.name).resolve():
                continue
            shutil.copy2(file, target)
            files.append((Path('solid') / relative).as_posix())
        stamp = datetime.now(timezone.utc).replace(microsecond=0)
        from datetime import timedelta
        while True:
            uci = f'CINTERASST{stamp:%Y%m%d%H%M%S}#{stem}en'
            final = category_root / uci
            if not final.exists():
                break
            stamp += timedelta(seconds=1)
        manifest = {
            'uci': uci, 'name': source.stem, 'version': 1, 'category': category,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'source_format': source.suffix.lower().lstrip('.'),
            'representations': {'solid': {'model': 'solid/' + source.name, 'files': files}},
        }
        (staged / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        staged.rename(final)
    inspect_asset(final)
    return {'uci': uci, 'category': category, 'existing': False}


def import_path(server, raw: str) -> dict:
    if not isinstance(raw, str) or not raw or '\x00' in raw:
        raise ValueError('Import needs an absolute file or canonical asset directory')
    source = Path(raw).expanduser()
    if not source.is_absolute() or source.is_symlink() or not source.exists():
        raise FileNotFoundError('Import source must be an existing absolute regular path')
    assets_root = root_for(server)
    if source.is_dir():
        result = _import_canonical_directory(source, assets_root)
    elif source.is_file():
        if source.stat().st_size == 0 or source.stat().st_size > MAX_IMPORT_BYTES:
            raise ValueError('Import file is empty or exceeds the 2 GiB limit')
        if source.suffix.lower() == '.zip':
            result = _import_archive(source, assets_root)
        elif source.suffix.lower() in MODELS:
            result = _import_model(source, assets_root)
        else:
            raise ValueError('Supported imports: UCI asset folder/ZIP, .glb, .gltf')
    else:
        raise ValueError('Unsupported import source')
    # Do not mark an import complete unless a usable preview can open.
    try:
        queue_tab(server, 'view', uci=result['uci'])
    except (OSError, RuntimeError):
        # The imported bytes remain canonical even if the optional viewer is
        # unavailable. The caller sees the actual viewer failure.
        raise
    return result


def _html_response(handler, page: bytes) -> None:
    # Converter escapes user-provided metadata and owns all inline JavaScript.
    # Only that exact generated script digest is permitted, never unsafe-inline.
    import re as _re
    scripts = _re.findall(rb'<script>(.*?)</script>', page, _re.S)
    script_hashes = ' '.join("'sha256-" + base64.b64encode(hashlib.sha256(s).digest()).decode('ascii') + "'" for s in scripts)
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(page)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('X-Frame-Options', 'SAMEORIGIN')
    handler.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self' " + script_hashes +
        "; style-src 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; " +
        "frame-ancestors 'self'; base-uri 'self'; object-src 'none'")
    handler.end_headers()
    handler.wfile.write(page)


def serve(handler, url_path: str) -> bool:
    """Serve only user-owned library assets through authenticated same-origin HTTP."""
    if not url_path.startswith('/assets-library/'):
        return False
    if not handler._local() or not handler._lan_logged_in():
        handler._json(403, {'error': 'not authorized'})
        return True
    root = root_for(handler.server)
    relative_raw = unquote(url_path[len('/assets-library/'):])
    if relative_raw.startswith('view/'):
        uci = relative_raw[len('view/'):]
        try:
            folder = _find_uci(root, uci)
            gallery = _gallery_module()
            _viewer_available(gallery, root.parent)
            pins = gallery._viewer_scripts(root.parent, root)
            if any(str(part).startswith('https:') for part in pins):
                raise RuntimeError('Unverified external viewer dependency')
            manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
            card = gallery._card(manifest, folder, root)
            page = (gallery.HTML.replace('__BABYLON__', pins[0]).replace('__LOADERS__', pins[1])
                    .replace('__COUNT__', '1').replace('__CARDS__', card))
            page = page.replace('<head>', '<head><base href="/assets-library/">', 1)
            _html_response(handler, page.encode('utf-8'))
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            handler._json(404, {'error': str(error)})
        return True
    try:
        rel = safe_relative(relative_raw)
        if not root.is_dir() or root.is_symlink():
            raise FileNotFoundError('Assets library unavailable')
        file = root / rel
        if (file.suffix.lower() not in VIEW_EXTENSIONS or file.is_symlink() or not file.is_file()
                or not file.resolve().is_relative_to(root.resolve())
                or any(p.is_symlink() for p in file.parents if p == root or root in p.parents)):
            raise FileNotFoundError('Unknown Assets library file')
        if file.stat().st_size > 512 * 1024 * 1024:
            raise ValueError('Asset browser file too large')
        data = file.read_bytes()
        mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
        if file.suffix.lower() in ('.glb', '.bin'):
            mime = 'application/octet-stream'
        if file.name == 'AssetGallery.html':
            _html_response(handler, data)
        else:
            handler._send(200, data, mime)
    except (OSError, ValueError) as error:
        handler._json(404, {'error': str(error)})
    return True


def upload_start(server, name: str, size: int) -> dict:
    if (not isinstance(name, str) or name != Path(name).name or '/' in name or '\\' in name
            or not isinstance(size, int) or not 0 < size <= MAX_IMPORT_BYTES
            or Path(name).suffix.lower() not in MODELS | {'.zip'}):
        raise ValueError('Invalid asset upload name or size')
    token = uuid.uuid4().hex
    target = server.mutable_builds / 'Run/CinterForgeTerminal/AssetUploads'
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError('Unsafe upload cache')
    output = target / token
    output.touch(exist_ok=False)
    with server.asset_upload_lock:
        server.asset_uploads[token] = {'path': output, 'name': name, 'size': size,
                                        'received': 0, 'hasher': hashlib.sha256()}
    return {'id': token, 'chunk_bytes': 512 * 1024}


def upload_chunk(server, token: str, offset: int, encoded: str) -> dict:
    if not isinstance(encoded, str) or len(encoded) > 800000:
        raise ValueError('Invalid asset upload chunk')
    raw = base64.b64decode(encoded, validate=True)
    if not raw or len(raw) > 512 * 1024:
        raise ValueError('Invalid asset upload chunk size')
    with server.asset_upload_lock:
        session = server.asset_uploads.get(token)
        if session is None or not isinstance(offset, int) or session['received'] != offset or offset + len(raw) > session['size']:
            raise ValueError('Out-of-order or unauthorized asset upload')
        with session['path'].open('ab') as file:
            file.write(raw)
        session['hasher'].update(raw)
        session['received'] += len(raw)
        return {'received': session['received'], 'size': session['size']}


def upload_finish(server, token: str, digest: str) -> dict:
    with server.asset_upload_lock:
        session = server.asset_uploads.pop(token, None)
    if session is None:
        raise ValueError('Unknown asset upload')
    try:
        if (session['received'] != session['size'] or
                not isinstance(digest, str) or digest != session['hasher'].hexdigest()):
            raise ValueError('Incomplete asset upload or SHA-256 mismatch')
        temporary = session['path'].with_suffix(Path(session['name']).suffix.lower())
        session['path'].rename(temporary)
        try:
            return import_path(server, str(temporary))
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        session['path'].unlink(missing_ok=True)


def upload_cancel(server, token: str) -> dict:
    with server.asset_upload_lock:
        session = server.asset_uploads.pop(token, None)
    if session:
        session['path'].unlink(missing_ok=True)
    return {'cancelled': True}
