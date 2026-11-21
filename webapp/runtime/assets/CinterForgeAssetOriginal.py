"""Terminal host adapter to ORIGINAL CinterForgeServer/WebApp/Plugins assets code.

Source is the unmodified or minimally path-adapted snapshot under the staged
Terminal asset_engine/original folder, with SHA-256 receipt. No new asset
formats, voxel algorithms, or identity generators are implemented here.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
import shutil
import sys
import threading
import uuid

from runtime import CinterForgeSettings
from runtime.assets.CinterForgeAssets import load_content_packs, safe_relative

_LOAD_LOCK = threading.RLock()


def source_root() -> Path:
    base = CinterForgeSettings.ASSETS / 'runtime/assets'
    if not (base / 'asset_management.py').is_file():
        raise FileNotFoundError('Original CinterForgeServer Assets source missing from staged Terminal')
    return base


def _server_module():
    with _LOAD_LOCK:
        package = source_root()
        from runtime.assets import asset_management
        if not Path(asset_management.__file__).resolve().is_relative_to(package.resolve()):
            raise RuntimeError('Different CinterForgeServer asset implementation is already loaded')
        return asset_management


@contextmanager
def library(server):
    # ContextVar is the existing Server root-selection contract, safe across
    # concurrent Terminal requests without modifying global environment state.
    from runtime.assets.CinterForgeAssetViews import root_for
    asset_management = _server_module()
    token = asset_management._ASSETS_ROOT_CONTEXT.set(root_for(server).resolve())
    try:
        yield asset_management
    finally:
        asset_management._ASSETS_ROOT_CONTEXT.reset(token)


def existing_uci(server, uci: str) -> dict:
    with library(server) as api:
        return api.resolve_uci_asset(uci)


def saved_list(server) -> dict:
    with library(server) as api:
        return api.list_saved()


def _plugin_uci(name: str, stamp: str) -> str:
    """Execute the two ORIGINAL Plugins migration UCI routines from the staged source.

    Extract functions via AST to avoid importing that one-time migration CLI's
    openpyxl/torch/source-checkout requirements into an installed native app.
    No identity formula is implemented or modified in Terminal.
    """
    source = source_root() / 'migrate_asset_library.py'
    tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('_safe_identifier', '_asset_uci')]
    if len(functions) != 2:
        raise RuntimeError('Original Plugins Asset UCI functions unavailable')
    namespace = {'re': __import__('re'), 'ASSET_UCI_PUBLISHER': 'CINTER', 'ASSET_UCI_TYPE': 'ASST'}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['_asset_uci'](name, stamp)


def import_model(server, source: Path) -> dict:
    if source.suffix.lower() not in ('.glb', '.gltf') or source.is_symlink() or not source.is_file():
        raise ValueError('Original IFEditor import requires a local GLB or glTF file')
    hasher = hashlib.sha256()
    with source.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    with library(server) as api:
        # The original generic find_saved_version also searches provider/cache
        # folders without UCI identities. Those are NOT canonical saved Assets:
        # ignore them for Terminal imports instead of reusing a non-UCI record.
        for folder, manifest in api._uci_asset_folders():
            for version in manifest.get('versions') or []:
                if (str(version.get('provider') or '').casefold() != 'local' or
                        str(version.get('kind') or '').casefold() != '3d' or
                        str(version.get('asset_id') or '') != digest):
                    continue
                candidate = folder / str(version.get('file') or '')
                if not candidate.is_file() or api._sha256(candidate) != digest:
                    raise RuntimeError('Canonical saved UCI source differs from its recorded import: ' + str(candidate))
                identity = str(manifest['uci'])
                # The previous Terminal importer wrongly placed local rigged
                # GLBs under the Mixamo-provider namespace. On explicit repeat
                # import of the identical source, move ONLY this Local-owned
                # UCI intact into Animated/<UCI>. Nothing is moved by report or
                # view, and non-Local Mixamo acquisitions remain untouched.
                if (str(manifest.get('category') or '') == 'Animated' and
                        str(manifest.get('subgroup') or '') == 'Mixamo' and
                        folder.parent.name == 'Mixamo' and folder.parent.parent.name == 'Animated'):
                    destination = folder.parent.parent / identity
                    if destination.exists() or destination.is_symlink():
                        raise FileExistsError('Cannot relocate imported UCI: destination already exists: ' + str(destination))
                    if folder.is_symlink() or destination.parent.is_symlink():
                        raise RuntimeError('Refusing symlinked UCI migration')
                    folder.rename(destination)
                    try:
                        updated = api._load_manifest(destination)
                        updated['subgroup'] = ''
                        api._write_manifest(destination, updated)
                        resolved = api.resolve_uci_asset(identity)
                        if resolved['folder'] != 'Animated/' + identity:
                            raise RuntimeError('Moved UCI did not resolve from Animated')
                    except Exception:
                        if destination.is_dir() and not folder.exists():
                            destination.rename(folder)
                        raise
                    folder = destination
                return {'uci': identity, 'existing': True, 'category': manifest.get('category', 'Objects'),
                        'path': str(folder / candidate.name)}
        name = source.stem
        stamp = datetime.now(timezone.utc).replace(microsecond=0)
        while True:
            uci = _plugin_uci(name, stamp.strftime('%Y%m%d%H%M%S'))
            if not any(folder.name == uci for folder, _ in api._uci_asset_folders()):
                break
            stamp += timedelta(seconds=1)
        # Original IFEditor saved-version writer; no alternative Manifest writer.
        saved = api.save_version(source_path=source, query=name, provider='Local',
                                 kind='3d', asset_id=digest, metadata={'name': name, 'source_filename': source.name},
                                 publish=False, uci=uci)
        if saved['manifest'].get('uci') != uci:
            raise RuntimeError('Original saved-version writer lost the supplied UCI')
        return {'uci': uci, 'existing': False, 'category': saved['manifest']['category'],
                'path': saved['relative_path']}


def delete_uci(server, uci: str) -> dict:
    if not uci.startswith('CINTERASST') or '/' in uci or '\\' in uci:
        raise ValueError('Asset deletion requires a canonical UCI')
    with server.asset_actions_lock:
        for record in getattr(server, 'asset_blockify_jobs', {}).values():
            if record.get('state') in ('queued', 'running') and any(
                    row.get('uci') == uci for row in record.get('rows') or []):
                raise RuntimeError('Wait until active Blockify conversion finishes before deleting ' + uci)
    with library(server) as api:
        return api.delete_uci_asset(uci)


def authoring_command(server, body: dict) -> dict:
    operation = str(body.get('operation') or '').strip()
    with library(server) as api:
        if operation == 'saved-list':
            return {'ok': True, **api.list_saved()}
        if operation == 'saved-delete':
            # Preserve the original per-version operation for existing clients.
            return {'ok': True, **api.delete_version(str(body.get('folder') or ''),
                                                     str(body.get('file') or ''))}
        if operation == 'saved-delete-asset':
            return {'ok': True, **delete_uci(server, str(body.get('uci') or ''))}
        if operation == 'assignment-list':
            # No IF Story is selected by an isolated Terminal Asset tab.
            return {'ok': True, 'games': []}
        if operation == 'search':
            if str(body.get('provider') or 'library').casefold() != 'library':
                raise ValueError('This Terminal workspace has no active external asset provider session')
            from runtime.assets.asset_library import search_3d, search_animations, search_images
            kind = str(body.get('kind') or 'image').casefold()
            searches = {'3d': search_3d, 'animation': search_animations, 'image': search_images}
            if kind not in searches:
                raise ValueError('Unsupported original library asset kind: ' + kind)
            limit = max(1, min(10000, int(body.get('limit') or 50)))
            offset = max(0, int(body.get('offset') or 0))
            return {'ok': True, **searches[kind](str(body.get('q') or ''), limit=limit, offset=offset)}
        if operation == 'stage':
            kind = str(body.get('resource') or '')
            if kind not in ('saved-file', 'saved-thumb', 'saved-runtime-animation'):
                raise ValueError('The standalone Assets tab only stages local saved assets')
            raw = str(body.get('path') or '')
            rel = safe_relative(raw)
            root = api.assets_root().resolve()
            requested = root / rel
            if kind == 'saved-runtime-animation' and requested.suffix.lower() == '.fbx':
                requested = requested.with_suffix('.preview.glb')
            if kind == 'saved-thumb':
                # Thumbnail generation belongs to the original Server module;
                # don't substitute the GLB as an image if not yet available.
                requested = api.ensure_saved_thumbnail(rel.as_posix())
                if requested is None:
                    return {'ok': True, 'available': False, 'url': ''}
            if not requested.resolve().is_relative_to(root) or requested.is_symlink() or not requested.is_file():
                return {'ok': True, 'available': False, 'url': ''}
            # A UCI directory can contain '#'. Send a URL path, not raw filesystem
            # text, or Safari truncates the URL at the fragment marker.
            encoded = '/'.join(quote(part, safe='') for part in requested.relative_to(root).parts)
            return {'ok': True, 'available': True, 'url': 'Assets/' + encoded}
        if operation == 'save-saved-thumbnail':
            file = api.save_generated_thumbnail(str(body.get('path') or ''), str(body.get('data_url') or ''))
            return {'ok': True, 'saved': True, 'thumbnail_path': file.resolve().relative_to(api.assets_root().resolve()).as_posix(), 'authority': 'local'}
        if operation == 'saved-assign-animation':
            return {'ok': True, **api.add_animation_assignment(str(body.get('folder') or ''), str(body.get('animation_folder') or ''), str(body.get('animation_file') or ''), name=str(body.get('name') or ''))}
        if operation == 'saved-remove-animation':
            return {'ok': True, **api.remove_animation_assignment(str(body.get('folder') or ''), str(body.get('animation_folder') or ''), str(body.get('animation_file') or ''))}
        if operation == 'saved-default-animation':
            return {'ok': True, **api.set_default_animation(str(body.get('folder') or ''), str(body.get('animation_folder') or ''), str(body.get('animation_file') or ''))}
        if operation == 'saved-embedded-animation-enabled':
            return {'ok': True, **api.set_embedded_animation_enabled(str(body.get('folder') or ''), str(body.get('animation_name') or ''), bool(body.get('enabled')))}
        if operation in ('asset-migration-save', 'asset-migration-save-proof', 'asset-migration-block-thumbnail',
                         'asset-migration-stage-existing', 'asset-migration-build-packs',
                         'asset-migration-pack-galleries', 'asset-migration-pack-staging-status'):
            from runtime.assets.asset_migration_commands import execute_asset_migration_command
            if operation in ('asset-migration-save', 'asset-migration-save-proof'):
                # The original migration writes into the CSV's target category.
                # Refuse a second folder for the same UCI elsewhere in the library.
                uci = str(body.get('uci') or '')
                destination = (api.assets_root() / str(body.get('top_level') or '') / uci).resolve()
                for existing_folder, _manifest in api._uci_asset_folders():
                    if existing_folder.name == uci and existing_folder.resolve() != destination:
                        raise FileExistsError('Existing UCI belongs to a different canonical Assets folder: ' + str(existing_folder))
            result = execute_asset_migration_command(operation, body)
            if result is None:
                raise RuntimeError('Original Server migration operation not handled: ' + operation)
            if operation == 'asset-migration-build-packs' and result.get('ok'):
                # Copy only finished original Minecraft artifacts into the
                # requested ContentPacks distribution folder. This is packaging,
                # never a second Minecraft pack writer.
                packs = api.assets_root() / 'ContentPacks'
                for entry in result.get('packs') or []:
                    source = Path(entry['file']).resolve()
                    mcaddons = (api.assets_root() / 'MCAddons').resolve()
                    if not source.is_file() or not source.is_relative_to(mcaddons):
                        raise RuntimeError('Original pack builder produced an unsafe file')
                    dst = packs / entry['name'] / source.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.is_file() or dst.read_bytes() != source.read_bytes():
                        temp = dst.with_suffix(dst.suffix + '.partial')
                        shutil.copy2(source, temp)
                        temp.replace(dst)
                    entry['distribution_file'] = str(dst)
            return result
        if operation == 'asset-migration-gallery':
            # The original Converter AssetGallery generator is already staged
            # and is the same authority used by `assets report`.
            from runtime.assets.CinterForgeAssetViews import report
            return {'ok': True, **report(server)}
        if operation == 'local-asset-resolve':
            return {'ok': True, **api.resolve_uci_asset(str(body.get('uci') or ''))}
        if operation == 'local-blockbench-session':
            from runtime.assets.asset_blockify import blockbench_resource_session_for_uci
            return {'ok': True, 'session': blockbench_resource_session_for_uci(
                str(body.get('uci') or ''), product_name=str(body.get('product_name') or ''))}
        if operation == 'local-blockbench-save':
            from runtime.assets.asset_blockify import save_blockbench_override
            rel = safe_relative(str(body.get('path') or ''))
            result = save_blockbench_override(rel.parent.as_posix(), rel.name,
                geometry=body.get('geometry') or {}, animations=body.get('animations') or {},
                texture_data_url=str(body.get('texture_data_url') or ''),
                product_name=str(body.get('product_name') or ''))
            return {'ok': True, 'result': result}
        if operation in ('local-blockify-load', 'local-blockify-save'):
            from runtime.assets.asset_terrain import load_uci_blockify, save_uci_blockify
            resolved = api.resolve_uci_asset(str(body.get('uci') or ''))
            if operation == 'local-blockify-load':
                try:
                    blockify = load_uci_blockify(resolved['uci'])
                except FileNotFoundError:
                    if not resolved.get('block_model'):
                        raise
                    # Original CSV migration stores its GLTF in manifest
                    # representations; the original saved-editor terrain cache
                    # is an optional *different* artifact, not a prerequisite.
                    blockify = {'file': resolved['block_model'],
                                'resolution': resolved['block_resolution']}
                return {'ok': True, 'uci': resolved['uci'], 'model_path': str(resolved.get('block_model') or ''),
                        'resolution': int(blockify.get('resolution') or resolved.get('block_resolution') or 16),
                        'blockify': blockify}
            resolution = int(body.get('resolution') or 16)
            if resolution != 16:
                raise ValueError('Original Server Terrain Blockify resolution must be 16')
            voxels = body.get('voxels')
            if not isinstance(voxels, list) or not voxels:
                raise ValueError('Blockify cannot save an empty voxel representation')
            meta = {'schema': 'cinterforge-blockify-v3', 'width': int(body.get('width') or 1),
                    'height': int(body.get('height') or 1), 'depth': int(body.get('depth') or 1),
                    'resolution': 16, 'voxel_count': len(voxels), 'voxels': voxels,
                    'product_name': str(body.get('product_name') or resolved.get('name') or '')}
            # These are already produced by the original CIFBlockify.js. Preserve
            # them in the original Server's UCI-owned Blockify JSON instead of
            # discarding animations and rig data at the Terminal boundary.
            for field in ('character_voxels', 'character_rig', 'character_animations',
                          'behavior_profile', 'behavior_signature', 'source_up_normalized'):
                if field in body:
                    meta[field] = body[field]
            saved = save_uci_blockify(resolved['uci'], meta)
            return {'ok': True, 'uci': resolved['uci'],
                    'blockify': {**saved, 'file': 'block_16/blockify.json'}, 'local_paths': []}
        if operation in ('saved-blockify-save', 'saved-blockify-export'):
            from runtime.assets.asset_blockify import save_blockify, regenerate_blockify_addon
            if operation == 'saved-blockify-save':
                result = save_blockify(str(body.get('folder') or ''), str(body.get('file') or ''),
                    width=body.get('width'), height=body.get('height'), depth=body.get('depth'),
                    resolution=body.get('resolution') or 16, voxels=body.get('voxels') or [],
                    character_voxels=body.get('character_voxels'), character_rig=body.get('character_rig'),
                    character_animations=body.get('character_animations'), behavior_profile=body.get('behavior_profile'),
                    behavior_signature=str(body.get('behavior_signature') or ''),
                    source_up_normalized=bool(body.get('source_up_normalized')),
                    product_name=str(body.get('product_name') or ''), regenerate_base=bool(body.get('regenerate_base')))
                return {'ok': True, 'blockify': result}
            result = regenerate_blockify_addon(str(body.get('folder') or ''), str(body.get('file') or ''),
                         product_name=str(body.get('product_name') or ''))
            folder = safe_relative(str(body.get('folder') or ''))
            addon = str(result.get('mcaddon_file') or '')
            file = (api.assets_root() / folder / addon).resolve()
            if not addon or not file.is_file() or not file.is_relative_to(api.assets_root().resolve()):
                raise RuntimeError('Original Blockify export did not produce an owned Minecraft Add-On')
            return {'ok': True, 'blockify': result,
                    'url': (folder / addon).as_posix(), 'file': addon,
                    'exported': {'file': addon, 'identifier': result.get('minecraft_identifier') or ''}}
    raise ValueError('Unsupported original IFPlayer asset operation in Terminal: ' + operation)


def _queue_blockify_rows(server, rows: list[dict], *, assemble_packs: bool, view_uci: str = '') -> dict:
    from runtime.assets.CinterForgeAssetViews import queue_tab
    job_id = uuid.uuid4().hex
    with server.asset_actions_lock:
        if getattr(server, 'asset_blockify_jobs', None) is None:
            server.asset_blockify_jobs = {}
        server.asset_blockify_jobs[job_id] = {'rows': rows, 'total': len(rows), 'completed': 0,
                                              'failed': 0, 'state': 'queued', 'messages': [],
                                              'assemble_packs': assemble_packs}
    if view_uci:
        queue_tab(server, 'view', uci=view_uci, job=job_id)
    else:
        queue_tab(server, 'blockify', job=job_id)
    print('[ASSETS MIGRATION] QUEUED job=' + job_id + ' ucis='
          + ','.join(str(row.get('uci') or '') for row in rows), flush=True)
    return {'job': job_id, 'count': len(rows)}


def start_blockify(server, path: str) -> dict:
    source = Path(path).expanduser()
    if not source.is_absolute() or source.is_symlink():
        raise ValueError('assets blockify requires an absolute CSV path')
    rows = load_content_packs(source)
    if not rows:
        raise ValueError('CSV contains no assets')
    from runtime.assets.CinterForgeAssetViews import root_for, queue_tab
    root = root_for(server)
    root.mkdir(parents=True, exist_ok=True)
    target = root / 'ContentPacks.csv'
    if target.is_symlink() or (target.is_file() and target.read_bytes() != source.read_bytes()):
        raise FileExistsError('Existing Builds/Assets/ContentPacks.csv differs; preserve and reconcile it before processing')
    if not target.is_file():
        with target.open('xb') as output, source.open('rb') as data:
            shutil.copyfileobj(data, output)
    fields = []
    # Reuse the original Server's recipe/completeness gate before asking the
    # original WebApp to render anything. The original stage-existing handler
    # repopulates Pack staging from a complete canonical UCI representation.
    with library(server) as api:
        from runtime.assets.asset_migration_commands import _asset_migration_destination_status
        for row in rows:
            destination = root / safe_relative(row.top_level_folder) / row.uci
            status = _asset_migration_destination_status(destination, row.blockify_resolution)
            fields.append({'name': row.name, 'minecraft_name': row.minecraft_name,
                          'command_name': row.command_name, 'pack_name': row.pack_name,
                          'top_level': row.top_level_folder, 'uci': row.uci, 'source_format': row.source_format,
                          'blockify_resolution': row.blockify_resolution, 'source_path': row.source_path or '',
                          'mixamo': bool(row.source_path and '/Mixamo/' in row.source_path),
                          'reuse_completed': bool(status['current'])})
    return {**_queue_blockify_rows(server, fields, assemble_packs=True), 'csv': str(target)}


def blockify_job(server, job_id: str, *, include_rows: bool = False) -> dict:
    with server.asset_actions_lock:
        data = getattr(server, 'asset_blockify_jobs', {}).get(job_id)
        if not data:
            raise FileNotFoundError('Unknown Blockify job')
        result = {k: v for k, v in data.items() if include_rows or k != 'rows'}
        return result


def _validate_completed_migration(server, rows: list[dict]) -> list[str]:
    """Use the original Server recipe/completeness gate before declaring success."""
    failures = []
    with library(server) as api:
        from runtime.assets.asset_migration_commands import _asset_migration_destination_status
        root = api.assets_root().resolve()
        for row in rows:
            top_level = safe_relative(str(row['top_level']))
            uci = str(row['uci'])
            destination = root / top_level / uci
            status = _asset_migration_destination_status(destination, int(row['blockify_resolution']))
            if status['current']:
                continue
            missing = ', '.join(status.get('missing') or [])
            detail = status['reason'] + (': ' + missing if missing else '')
            failures.append(uci + ' (' + detail + ')')
    return failures


def blockify_progress(server, body: dict) -> dict:
    job = str(body.get('job') or '')
    with server.asset_actions_lock:
        record = getattr(server, 'asset_blockify_jobs', {}).get(job)
        if record is None:
            raise FileNotFoundError('Unknown Blockify job')
        rows = list(record.get('rows') or [])

    # A browser job is not complete merely because its JavaScript loop ended.
    # This is the exact original migration validator, including Solid/Block
    # files, recipe identity, and generated representation declarations.
    if body.get('finished') and not body.get('error'):
        incomplete = _validate_completed_migration(server, rows)
        if incomplete:
            body = {**body, 'finished': False,
                    'error': 'Migration did not create complete canonical representations: '
                             + '; '.join(incomplete)}

    with server.asset_actions_lock:
        record = getattr(server, 'asset_blockify_jobs', {}).get(job)
        if record is None:
            raise FileNotFoundError('Unknown Blockify job')
        if body.get('error'):
            record['state'] = 'error'
            record['error'] = str(body['error'])[:4000]
            record['messages'].append('ERROR: ' + record['error'][:1000])
            record['messages'] = record['messages'][-25:]
            print('[ASSETS MIGRATION] FAILED job=' + job + ' uci='
                  + ','.join(str(row.get('uci') or '') for row in rows)
                  + ' reason=' + record['error'], flush=True)
            return {'ok': False, 'state': 'error', 'error': record['error']}
        index = body.get('processed')
        if not isinstance(index, int) or not (0 <= index <= record['total']) or index < record['completed'] + record['failed']:
            raise ValueError('Invalid Blockify progress sequence')
        failed = int(body.get('failed', 0))
        if not 0 <= failed <= index or failed < record['failed']:
            raise ValueError('Invalid Blockify failure count')
        record['completed'] = index - failed
        record['failed'] = failed
        record['state'] = 'finished' if body.get('finished') else 'running'
        message = str(body.get('message') or '')[:1000]
        record['messages'].append(message)
        record['messages'] = record['messages'][-25:]
        print('[ASSETS MIGRATION] job=' + job + ' state=' + record['state']
              + ' processed=' + str(index) + '/' + str(record['total'])
              + ' completed=' + str(record['completed'])
              + ' failed=' + str(record['failed'])
              + ' message=' + message, flush=True)
        return {'ok': True, 'state': record['state']}
