"""Terminal HTTP/tab bridge for original Server, Plugins and WebApp asset code."""
from __future__ import annotations

import base64
import hashlib
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
VIEW_EXTENSIONS = frozenset(('.html', '.js', '.css', '.gltf', '.glb', '.bin', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.json', '.ktx2', '.dds', '.mcaddon'))
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
        except RuntimeError as error:
            # The installed app's mutable Steam state is NOT the canonical
            # shared Assets library. Never silently import a second copy there.
            raise RuntimeError('No selected shared Builds/Assets library: ' + str(error)) from error
        root = builds / 'Assets'
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise RuntimeError('Assets library root is not a normal directory: ' + str(root))
    return root


def root_for(server) -> Path:
    return root_for_state(server.mutable_builds)


def report(server) -> dict:
    """Regenerate the established Converter AssetGallery.html, not IFPlayer search."""
    from runtime import CinterForgeAssetOriginal as original
    root = root_for(server)
    if not root.is_dir():
        raise FileNotFoundError('Shared Assets library does not exist: ' + str(root))
    # This is the already-staged Converter AssetGallery generator; there is no
    # second gallery implementation or a browser search API involved.
    import importlib.util
    source = original.source_root().parent / 'api/asset_gallery.py'
    if not source.is_file():
        raise FileNotFoundError('Staged original AssetGallery generator missing: ' + str(source))
    spec = importlib.util.spec_from_file_location('cinterforge_original_asset_gallery', source)
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load original AssetGallery generator')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.generate(root.parent)
    generated = Path(result['path']).resolve()
    if generated != (root / 'AssetGallery.html').resolve() or not generated.is_file():
        raise RuntimeError('Original AssetGallery did not write the selected Assets report')
    # Original Server is authoritative for UCI discovery; the gallery must not
    # silently omit UCI records (including nested Animated/Mixamo assets).
    groups = original.saved_list(server)['groups']
    expected = sum(len(rows) for group in groups.values()
                   for rows in (group.values() if isinstance(group, dict) else (group,)))
    if result['assets'] != expected:
        raise RuntimeError(f'AssetGallery includes {result["assets"]} assets, original Server finds {expected}; refusing incomplete report at {generated}')
    return {'url': '/assets-library/SVG/Assets/AssetGallery.html', 'count': expected,
            'path': str(generated), 'root': str(root)}


def view(server, uci: str) -> dict:
    from runtime import CinterForgeAssetOriginal as original
    result = original.existing_uci(server, uci)
    return {'url': '/assets-library/workspace?uci=' + quote(uci, safe=''),
            'uci': uci, 'name': result['name'], 'root': str(root_for(server)),
            'path': str(root_for(server) / result['folder'])}



def _resume_incomplete_local_import(server, uci: str) -> dict | None:
    """Resume the original migration for a previously saved local model.

    A previous import may have committed the original GLB and blockify.json
    before the browser conversion failed. Opening that UCI must not mistake
    a source-only folder for a completed import or create a new asset identity.
    Use the same import_path -> original migration job as a repeat import;
    never generate representations or rewrite authored Blockify edits here.
    """
    from runtime import CinterForgeAssetOriginal as original
    with original.library(server) as api:
        resolved = api.resolve_uci_asset(uci)
        manifest = resolved['manifest']
        # This recovery is only for Terminal's own imported version records.
        # Migrated legacy assets and authored non-Local representations do not
        # silently acquire new migration jobs when opened for editing.
        versions = [version for version in manifest.get('versions') or []
                    if str(version.get('provider') or '').casefold() == 'local'
                    and str(version.get('kind') or '').casefold() == '3d'
                    and Path(str(version.get('file') or '')).suffix.lower() in MODELS]
        if not versions:
            return None
        owned = api.assets_root().resolve() / safe_relative(resolved['folder'])
        from cf.ifserver.assets.asset_migration_commands import _asset_migration_destination_status
        if _asset_migration_destination_status(owned, 16)['current']:
            return None
        version = versions[-1]
        model = owned / safe_relative(str(version['file']))
        if model.is_symlink() or not model.is_file() or not model.resolve().is_relative_to(owned.resolve()):
            raise FileNotFoundError('Saved source model for incomplete import is missing: ' + uci)
    # The existing original import implementation deduplicates by source SHA,
    # validates the same UCI, checks migration completeness, and queues the
    # original CIFBlockifyBatch.MigrateCatalogAsset job if still incomplete.
    # Serialize recovery requests across simultaneous gallery/CLI opens. The
    # Terminal owns an RLock here; import_path and its original queue adapter
    # intentionally use the same lock while committing the one migration job.
    with server.asset_actions_lock:
        result = import_path(server, str(model))
    if result['uci'] != uci:
        raise RuntimeError('Incomplete import recovery changed canonical UCI: ' + uci)
    if not result.get('conversion_job'):
        raise RuntimeError('Incomplete import recovery did not queue original migration: ' + uci)
    print('[ASSETS MIGRATION] RESUME existing UCI=' + uci
          + ' job=' + str(result['conversion_job']), flush=True)
    return result


def queue_tab(server, kind: str, **args) -> dict:
    if kind == 'report':
        request = {'kind': 'report', **report(server)}
    elif kind == 'view':
        # On an older, interrupted local import the source GLB already exists
        # but Solid/Block migration never finished. A normal view request must
        # resume the same original migration; otherwise every rebuild simply
        # reopens the broken UCI forever. The nested job-bearing queue_tab from
        # import_path is authoritative, and this branch returns that request.
        if not args.get('job'):
            resumed = _resume_incomplete_local_import(server, args['uci'])
            if resumed:
                job = resumed['conversion_job']
                return {'kind': 'view', **view(server, args['uci']), 'job': job,
                        'url': '/assets-library/workspace?uci=' + quote(args['uci'], safe='')
                               + '&job=' + quote(job, safe='')}
        request = {'kind': 'view', **view(server, args['uci'])}
        if args.get('job'):
            request['job'] = args['job']
            request['url'] += '&job=' + quote(args['job'], safe='')
    elif kind == 'blockify':
        request = {'kind': 'blockify', 'url': '/assets-library/workspace?job=' + quote(args['job'], safe=''), 'job': args['job']}
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
    """Copy an existing UCI folder unchanged; never synthesize asset metadata."""
    if not ASSET_DIR.fullmatch(staged.name):
        raise ValueError('Asset directory is not a CinterForge UCI')
    for member in staged.rglob('*'):
        if member.is_symlink() or not (member.is_file() or member.is_dir()):
            raise ValueError('Canonical asset contains unsafe member: ' + str(member))
    manifest = json.loads((staged / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('uci') != staged.name:
        raise ValueError('Manifest UCI and asset folder must agree')
    if isinstance(manifest.get('versions'), list):
        # Validate the exact original Server version/dependency paths.
        for version in manifest['versions']:
            if not isinstance(version, dict) or not isinstance(version.get('file'), str):
                raise ValueError('Invalid original saved version')
            source = staged / safe_relative(version['file'])
            if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(staged.resolve()):
                raise ValueError('Missing original saved version: ' + version['file'])
    elif isinstance(manifest.get('representations'), dict):
        inspect_asset(staged)
    else:
        raise ValueError('Unknown canonical UCI format; refusing rewrite')
    category = _manifest_category(manifest)
    subgroup = str(manifest.get('subgroup') or '').strip()
    if subgroup not in ('Mixamo', 'Fixed', ''):
        subgroup = ''
    dest = assets_root / category / subgroup / staged.name
    # A UCI is global, including nested original animated categories.
    for old in assets_root.rglob(staged.name) if assets_root.is_dir() else ():
        if old.is_dir() and not old.is_symlink():
            if _files_equal(old, staged):
                return {'uci': staged.name, 'category': category, 'existing': True}
            raise FileExistsError('UCI already exists with different bytes: ' + staged.name)
    if dest.exists() or dest.is_symlink():
        raise FileExistsError('Asset UCI destination already occupied')
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.parent.is_symlink() or assets_root.is_symlink():
        raise RuntimeError('Unsafe destination Assets directory')
    shutil.copytree(staged, dest, symlinks=False)
    if not _files_equal(dest, staged):
        shutil.rmtree(dest)
        raise RuntimeError('Original asset changed while copying')
    return {'uci': staged.name, 'category': category, 'existing': False}


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
            from runtime import CinterForgeAssetOriginal as original
            result = original.import_model(server, source)
        else:
            raise ValueError('Supported imports: UCI asset folder/ZIP, .glb, .gltf')
    else:
        raise ValueError('Unsupported import source')
    # Read back the committed bytes and canonical UCI through the existing
    # Server API BEFORE reporting any import success or opening a new tab.
    from runtime import CinterForgeAssetOriginal as original
    with original.library(server) as api:
        resolved = api.resolve_uci_asset(result['uci'])
        listed = api.list_saved()['groups']
        folders = [*(listed.get('Images') or []), *(listed.get('Objects') or []),
                   *(listed.get('Animated', {}).get('Mixamo') or []),
                   *(listed.get('Animated', {}).get('Fixed') or []),
                   *(listed.get('Animations') or [])]
        if not any(item.get('uci') == result['uci'] for item in folders):
            raise RuntimeError('Imported UCI was not discoverable by original Server: ' + result['uci'])
        owned = api.assets_root().resolve() / resolved['folder']
        manifest = owned / 'manifest.json'
        if not manifest.is_file() or not manifest.resolve().is_relative_to(api.assets_root().resolve()):
            raise RuntimeError('Imported UCI manifest is not readable under selected Assets root')
        if source.is_file() and source.suffix.lower() in MODELS:
            # An original GLTF save may rewrite portable dependency paths.
            # Check source byte identity for self-contained GLB; for GLTF the
            # Server's recorded digest is authoritative after copying assets.
            matched = False
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest() if source.suffix.lower() == '.glb' else None
            for version in resolved['manifest'].get('versions') or []:
                candidate = owned / safe_relative(str(version.get('file') or ''))
                if candidate.is_file() and candidate.suffix.lower() == source.suffix.lower():
                    if version.get('sha256') == hashlib.sha256(candidate.read_bytes()).hexdigest():
                        if source_sha is None or hashlib.sha256(candidate.read_bytes()).hexdigest() == source_sha:
                            matched = True
            if not matched:
                raise RuntimeError('Imported model bytes or recorded SHA-256 did not verify: ' + str(owned))
        result['saved_path'] = str(owned)
        result['assets_root'] = str(api.assets_root().resolve())
    # Every import format commits ONE canonical UCI folder first. An imported
    # UCI directory/ZIP needs the same original migration as a newly saved GLB;
    # copying its manifest alone does not repair missing Solid/Block artifacts.
    # This is deliberately independent of `assets blockify` and ContentPacks.csv.
    with original.library(server):
        from cf.ifserver.assets.asset_migration_commands import _asset_migration_destination_status
        migration = _asset_migration_destination_status(owned, 16)
    result['migration'] = migration['reason']
    if not migration['current']:
        manifest_data = resolved['manifest']
        versions = manifest_data.get('versions') or []
        candidates = [safe_relative(str(v['file'])) for v in versions
                      if isinstance(v, dict) and isinstance(v.get('file'), str)
                      and Path(v['file']).suffix.lower() in MODELS]
        # Older canonical UCI folders may predate the saved-version schema.
        # Inspect only this UCI's root-level source models, never legacy or CSV
        # folders and never generated Solid/Block representations.
        if not candidates:
            candidates = [Path(p.name) for p in sorted(owned.iterdir())
                          if p.is_file() and not p.is_symlink() and p.suffix.lower() in MODELS
                          and not p.name.endswith('.preview.glb')]
        if not candidates:
            raise FileNotFoundError('Incomplete imported UCI has no original GLB/glTF source: '
                                    + result['uci'] + ' (' + migration['reason'] + ')')
        file = candidates[0]
        model = owned / file
        if (model.is_symlink() or not model.is_file()
                or not model.resolve().is_relative_to(owned.resolve())):
            raise FileNotFoundError('Imported UCI source model missing or unsafe: ' + str(file))
        command_name = result['uci'].partition('#')[2].removesuffix('en').lower()
        if not command_name:
            raise RuntimeError('Canonical UCI has no original asset command identifier')
        row = {
            'uci': result['uci'], 'name': str(manifest_data.get('name') or model.stem),
            'minecraft_name': str(manifest_data.get('minecraft_name') or manifest_data.get('name') or model.stem),
            'command_name': command_name,
            'pack_name': '', 'top_level': Path(resolved['folder']).parent.as_posix(),
            'source_path': (Path(resolved['folder']) / file).as_posix(),
            'source_format': file.suffix.lower().lstrip('.'),
            'blockify_resolution': 16, 'mixamo': False, 'local_import': True,
            'reuse_completed': False,
        }
        # The browser runs the ORIGINAL migration for precisely this one UCI,
        # writing into its existing folder. No CSV rows or pack assembly.
        with server.asset_actions_lock:
            active_job = next((job for job, record in
                getattr(server, 'asset_blockify_jobs', {}).items()
                if record.get('state') in ('queued', 'running') and any(
                    item.get('uci') == result['uci'] for item in record.get('rows') or [])), None)
        if active_job:
            queue_tab(server, 'view', uci=result['uci'], job=active_job)
            result['conversion_job'] = active_job
        else:
            queued = original._queue_blockify_rows(server, [row],
                assemble_packs=False, view_uci=result['uci'])
            result['conversion_job'] = queued['job']
        return result
    queue_tab(server, 'view', uci=result['uci'])
    return result


def _html_response(handler, page: bytes) -> None:
    # The existing Converter AssetGallery and Server Pack galleries embed
    # generated scripts. Hash-allow only those exact bytes instead of turning
    # on unsafe-inline for arbitrary Assets content.
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
        "; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' blob:; worker-src 'self' blob:; " +
        "frame-src 'self'; frame-ancestors 'self'; base-uri 'self'; object-src 'none'")
    handler.end_headers()
    handler.wfile.write(page)


def serve_voxel(handler, url_path: str) -> bool:
    """Serve the verified staged CinterForgeVoxel browser editor, never source."""
    if not url_path.startswith('/blockbench/'):
        return False
    if not handler._local() or not handler._lan_logged_in():
        handler._json(403, {'error': 'not authorized'})
        return True
    from runtime import CinterForgeSettings
    from pathlib import Path
    try:
        root = CinterForgeSettings.ASSETS / 'data/asset_engine/voxel'
        relative = safe_relative(unquote(url_path[len('/blockbench/'):]))
        file = root / relative
        if (not root.is_dir() or root.is_symlink() or file.is_symlink() or
                not file.is_file() or not file.resolve().is_relative_to(root.resolve()) or
                any(p.is_symlink() for p in file.parents if p == root or root in p.parents)):
            raise FileNotFoundError('CinterForgeVoxel browser file missing: ' + relative.as_posix())
        # The Server builder has already selected the browser-only runtime
        # members, including the original CinterForge bridge. Never serve
        # source, Python, metadata secrets or native executables.
        if file.suffix.lower() not in ('.html', '.js', '.css', '.json', '.webmanifest',
                    '.png', '.svg', '.jpg', '.jpeg', '.webp', '.gif', '.ico', '.woff',
                    '.woff2', '.ttf', '.otf', '.wasm', '.bin', '.glb', '.mp3', '.ogg'):
            raise FileNotFoundError('Unsupported Voxel browser resource')
        data = file.read_bytes()
        mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
        if file.suffix.lower() == '.js':
            mime = 'text/javascript'
        if file.suffix.lower() == '.html':
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/html; charset=utf-8')
            handler.send_header('Content-Length', str(len(data)))
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.send_header('X-Frame-Options', 'SAMEORIGIN')
            handler.send_header('Content-Security-Policy',
                "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
                "worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; connect-src 'self' https:; "
                "img-src 'self' data: blob:; font-src 'self' data:; "
                "frame-src 'self' blob:; frame-ancestors 'self'; object-src 'none'")
            handler.end_headers()
            handler.wfile.write(data)
        else:
            handler._send(200, data, mime, same_origin_frame=True)
    except (OSError, ValueError) as exc:
        handler._json(404, {'error': str(exc)})
    return True


def serve(handler, url_path: str) -> bool:
    """Authenticated, same-origin host for ORIGINAL IFPlayer Asset Browser."""
    if not url_path.startswith('/assets-library/'):
        return False
    if not handler._local() or not handler._lan_logged_in():
        handler._json(403, {'error': 'not authorized'})
        return True
    from runtime import CinterForgeAssetOriginal as original
    try:
        base = original.source_root()
        relative = unquote(url_path[len('/assets-library/'):])
        if relative == 'workspace':
            _html_response(handler, (base / 'workspace.html').read_bytes())
            return True
        if relative.startswith('SVG/Assets/'):
            root = root_for(handler.server)
            rel = safe_relative(relative[len('SVG/Assets/'):])
            file = root / rel
            authority = root
        elif relative.startswith('original/webapp/'):
            rel = safe_relative(relative[len('original/webapp/'):])
            file = base / 'webapp' / rel
            authority = base / 'webapp'
        elif relative.startswith('vendor/'):
            rel = safe_relative(relative[len('vendor/'):])
            if rel.as_posix() not in ('babylon.js', 'babylonjs.loaders.min.js'):
                raise FileNotFoundError('Unknown pinned Babylon file')
            file = base.parent / 'vendor' / rel
            authority = base.parent / 'vendor'
        else:
            raise FileNotFoundError('Unrecognized Asset Browser resource')
        if (file.suffix.lower() not in VIEW_EXTENSIONS or file.is_symlink() or
                not file.is_file() or not file.resolve().is_relative_to(authority.resolve()) or
                any(part.is_symlink() for part in file.parents if part == authority or authority in part.parents)):
            raise FileNotFoundError('Asset Browser resource unavailable: ' + relative)
        # Never expose Python, authoring manifests or unrelated source modules.
        if not relative.startswith('SVG/') and file.suffix.lower() not in ('.html', '.js', '.css'):
            raise FileNotFoundError('Not a browser resource')
        if file.stat().st_size > 512 * 1024 * 1024:
            raise ValueError('Asset resource exceeds HTTP limit')
        mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
        if file.suffix.lower() in ('.glb', '.bin'):
            mime = 'application/octet-stream'
        if relative.startswith('SVG/Assets/') and file.suffix.lower() == '.html':
            # These are the actual original Converter/Server generated viewers;
            # their existing inline scripts require a per-response hash CSP.
            _html_response(handler, file.read_bytes())
        else:
            handler._send(200, file.read_bytes(), mime)
    except (OSError, ValueError, RuntimeError) as exc:
        handler._json(404, {'error': str(exc)})
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
        # A browser upload is stored under an opaque transfer token, but the
        # IFEditor authoring writer MUST see the selected original filename.
        # Give every upload a unique temporary directory so simultaneous
        # Hope.glb picks cannot collide or overwrite one another.
        temporary_dir = session['path'].with_name(session['path'].name + '-selected')
        temporary_dir.mkdir(exist_ok=False)
        temporary = temporary_dir / session['name']
        try:
            session['path'].rename(temporary)
            return import_path(server, str(temporary))
        finally:
            temporary.unlink(missing_ok=True)
            temporary_dir.rmdir()
    finally:
        session['path'].unlink(missing_ok=True)


def upload_cancel(server, token: str) -> dict:
    with server.asset_upload_lock:
        session = server.asset_uploads.pop(token, None)
    if session:
        session['path'].unlink(missing_ok=True)
    return {'cancelled': True}
