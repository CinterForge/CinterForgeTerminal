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
from urllib.parse import quote, unquote, urlsplit, parse_qs

from runtime.assets.CinterForgeAssets import inspect_asset, safe_relative

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
    from runtime.assets import CinterForgeAssetOriginal as original
    root = root_for(server)
    if not root.is_dir():
        raise FileNotFoundError('Shared Assets library does not exist: ' + str(root))
    # This is the already-staged Converter AssetGallery generator; there is no
    # second gallery implementation or a browser search API involved.
    import importlib.util
    source = original.source_root() / 'asset_gallery.py'
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
    from runtime.assets import CinterForgeAssetOriginal as original
    result = original.existing_uci(server, uci)
    return {'url': '/assets-library/asset?uci=' + quote(uci, safe=''),
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
    from runtime.assets import CinterForgeAssetOriginal as original
    with original.library(server) as api:
        resolved = api.resolve_uci_asset(uci)
        manifest = resolved['manifest']
        # This recovery is only for Terminal's own imported version records.
        # Migrated legacy assets and authored non-Local representations do not
        # silently acquire new migration jobs when opened for editing.
        versions = [version for version in manifest.get('versions') or []
                    if isinstance(version, dict)
                    and str(version.get('provider') or '').casefold() == 'local'
                    and str(version.get('kind') or '').casefold() == '3d'
                    and Path(str(version.get('file') or '')).suffix.lower() in MODELS]
        if not versions:
            return None
        owned = api.assets_root().resolve() / safe_relative(resolved['folder'])
        from runtime.assets.asset_migration_commands import _asset_migration_destination_status
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
                        'url': '/assets-library/asset?uci=' + quote(args['uci'], safe='')
                               + '&job=' + quote(job, safe='')}
        request = {'kind': 'view', **view(server, args['uci'])}
        if args.get('job'):
            request['job'] = args['job']
            request['url'] += '&job=' + quote(args['job'], safe='')
    elif kind == 'blockify':
        request = {'kind': 'blockify', 'url': '/assets-library/asset?job=' + quote(args['job'], safe=''), 'job': args['job']}
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
            from runtime.assets import CinterForgeAssetOriginal as original
            result = original.import_model(server, source)
        else:
            raise ValueError('Supported imports: UCI asset folder/ZIP, .glb, .gltf')
    else:
        raise ValueError('Unsupported import source')
    # Read back the committed bytes and canonical UCI through the existing
    # Server API BEFORE reporting any import success or opening a new tab.
    from runtime.assets import CinterForgeAssetOriginal as original
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
                if not isinstance(version, dict):
                    continue
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
        from runtime.assets.asset_migration_commands import _asset_migration_destination_status
        migration = _asset_migration_destination_status(owned, 16)
    # Explicit re-import of an existing local UCI may upgrade a completed
    # clipless Mixamo rig, or correct a category written by an old importer.
    # Simply opening its Asset View never mutates the asset or initiates a job.
    if migration['current'] and result.get('existing'):
        original_manifest = resolved['manifest']
        original_version = next((v for v in original_manifest.get('versions') or []
                                 if isinstance(v, dict) and str(v.get('provider') or '').casefold() == 'local'
                                 and Path(str(v.get('file') or '')).suffix.lower() in MODELS), None)
        if original_version:
            from runtime.assets.asset_management import inspect_model
            model = owned / safe_relative(str(original_version['file']))
            # Server's asset inspector resolves its paths and log destination
            # from the selected Assets library context. Do not call it outside
            # the same library scope as the import writer.
            with original.library(server):
                inspected = inspect_model(model)
            embedded = bool(inspected.get('has_embedded_animations'))
            imported = bool(original_manifest.get('import_animation'))
            expected_category = 'Animated' if (embedded or imported or inspected.get('mixamo_compatible_rig')) else 'Objects'
            upgrade_clipless_rig = (bool(inspected.get('mixamo_compatible_rig'))
                                    and not embedded and not imported)
            if upgrade_clipless_rig or str(original_manifest.get('category') or '') != expected_category:
                migration = {'current': False, 'reason': 'reimport-animation-or-category-upgrade'}
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
        # Use the existing glTF inspector; a humanoid-looking skeleton is not
        # proof that an external Mixamo animation can be retargeted.
        from runtime.assets.asset_management import inspect_model
        with original.library(server):
            compatible_rig = bool(inspect_model(model).get('mixamo_compatible_rig'))
        row = {
            'uci': result['uci'], 'name': str(manifest_data.get('name') or model.stem),
            'minecraft_name': str(manifest_data.get('minecraft_name') or manifest_data.get('name') or model.stem),
            'command_name': command_name,
            'pack_name': '', 'top_level': Path(resolved['folder']).parent.as_posix(),
            'source_path': (Path(resolved['folder']) / file).as_posix(),
            'source_format': file.suffix.lower().lstrip('.'),
            'blockify_resolution': 16, 'mixamo': False, 'local_import': True,
            'mixamo_compatible_rig': compatible_rig,
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



# Minecraft pack selection builds on the existing generated MCAddons galleries.
# The authored library is not changed when a pack is selected; only this
# Terminal instance's writable state is changed.
_PACK_STATE_SCHEMA = 'cinterforge.terminal.current-pack.v1'
_PACK_BETA_CDN = 'https://cinterforge.ucreg.io/cinterforge/assets/beta/'
_PACK_VIEW_EXTENSIONS = frozenset(('.html', '.js', '.css', '.json', '.png', '.jpg', '.jpeg', '.webp', '.glb', '.gltf', '.bin', '.mcaddon'))
_PACK_DOWNLOAD_LIMIT = 2 * 1024**3


def _pack_name(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 160
            or value.strip() != value or value in ('.', '..')
            or any(c in value for c in ('/', '\\', '\x00', ':'))):
        raise ValueError('Invalid Minecraft pack name')
    return value


def _pack_state_dir(server) -> Path:
    """One pack cache and current.json per selected Minecraft world."""
    manager = server.bedrock
    if manager is None:
        raise RuntimeError('Bedrock world management is unavailable')
    world = manager.current_world()
    if world is None:
        raise ValueError('Select a Current World with bedrock world <name> first')
    root = manager.world_data_root(world) / 'Packs'
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise RuntimeError('Per-world pack state directory is unsafe')
    return root


def _migrate_previous_pack(server, root: Path) -> None:
    """Adopt the previously global pack only once, for the selected world."""
    previous = server.mutable_builds / 'Packs/CinterForgeTerminal'
    original = previous / 'current.json'
    new = root / 'current.json'
    if new.exists() or not original.is_file() or original.is_symlink():
        return
    record = json.loads(original.read_text(encoding='utf-8'))
    if (record.get('schema') != _PACK_STATE_SCHEMA or
            not re.fullmatch(r'[0-9a-f]{64}', str(record.get('sha256', '')))):
        return
    name = _pack_name(record.get('name'))
    source = previous / 'Downloaded' / name / (name + '.mcaddon')
    if not source.is_file() or source.is_symlink() or source.stat().st_size != record.get('bytes'):
        return
    if hashlib.sha256(source.read_bytes()).hexdigest() != record['sha256']:
        return
    destination = root / 'Downloaded' / name
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination / (name + '.mcaddon'))
    root.mkdir(parents=True, exist_ok=True)
    new.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    # Retain old downloaded bytes for recovery, but prevent silently assigning
    # the old global choice to a second world on the next selection.
    original.rename(previous / 'migrated-current.json')


def _pack_local_mode() -> bool:
    """The requested cfdev JSON switches the view to local Builds output."""
    from runtime.assets import CinterForgeAssetCloudflare as cloudflare
    config = cloudflare._config_path()
    if config.is_symlink() or not config.is_file():
        return False
    try:
        parsed = json.loads(config.read_text(encoding='utf-8'))
        return isinstance(parsed, dict) and bool(parsed)
    except (OSError, UnicodeError, ValueError):
        return False


def _pack_beta_base() -> str:
    # Public beta is served under the published R2 object_prefix=cinterforge.
    # There is no cfdev credential/config in public mode from which to infer it.
    return _PACK_BETA_CDN


def _pack_remote_url(relative: str) -> str:
    from urllib.parse import quote
    rel = safe_relative(relative)
    return _pack_beta_base() + '/'.join(quote(part, safe='') for part in rel.parts)


def _pack_remote_open(relative: str):
    from urllib.request import Request, urlopen
    url = _pack_remote_url(relative)
    response = urlopen(Request(url, headers={'User-Agent': 'CinterForgeTerminal/Packs'}), timeout=30)
    actual = urlsplit(response.geturl())
    expected = urlsplit(_pack_beta_base())
    # A Cloudflare redirect must not turn a pack fetch into arbitrary host IO.
    if (actual.scheme != 'https' or actual.hostname != expected.hostname or
            not actual.path.startswith(expected.path)):
        response.close()
        raise RuntimeError('Pack download redirected outside the published beta Assets folder')
    return response


def _pack_state(server) -> dict | None:
    try:
        root = _pack_state_dir(server)
    except ValueError:
        return None
    if not (root / 'current.json').exists():
        _migrate_previous_pack(server, root)
    path = root / 'current.json'
    if not path.exists():
        return None  # Default: no current pack. This is a valid state.
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('Current pack selection is unsafe')
    record = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(record, dict) or record.get('schema') != _PACK_STATE_SCHEMA
            or not re.fullmatch(r'[0-9a-f]{64}', str(record.get('sha256') or ''))):
        raise ValueError('Current pack selection has an invalid manifest')
    name = _pack_name(record.get('name'))
    file = root / 'Downloaded' / name / (name + '.mcaddon')
    if (file.is_symlink() or not file.is_file() or
            not file.resolve().is_relative_to(root.resolve()) or
            file.stat().st_size != record.get('bytes')):
        raise FileNotFoundError('Current pack is missing or incomplete: ' + name)
    return record


def pack_current(server) -> dict:
    with server.asset_pack_lock:
        record = _pack_state(server)
        return {'current': record['name'] if record else None}


def pack_open(server) -> dict:
    # No DevTools library is required for the published beta gallery.
    if _pack_local_mode():
        index = root_for(server) / 'MCAddons/index.html'
        if not index.is_file() or index.is_symlink():
            raise FileNotFoundError('Local Builds/Assets/MCAddons/index.html is missing')
    request = {'kind': 'packs', 'url': '/assets-library/packs/index.html'}
    with server.asset_actions_lock:
        server.asset_actions = type(server.asset_actions)(row for row in server.asset_actions if row['kind'] != 'packs')
        server.asset_actions.append(request)
    return request


def _inspect_mcaddon_contents(path: Path) -> dict:
    """Verify the real RP/BP closure, script entries, items and payload size.

    An Add-On is nested ZIP archives. A small compressed download does not
    establish missing content; inspect both layers before selecting it.
    """
    total_unpacked = 0
    total_members = 0
    item_ids: set[str] = set()
    modules = []
    behavior_count = resource_count = script_count = 0
    with zipfile.ZipFile(path) as archive:
        nested = [part for part in archive.infolist()
                  if part.filename.lower().endswith('.mcpack') and not part.is_dir()]
        if not 1 <= len(nested) <= 32:
            raise ValueError('MCAddon must contain 1–32 .mcpack modules')
        if any(part.file_size > _PACK_DOWNLOAD_LIMIT for part in nested):
            raise ValueError('Nested Minecraft pack exceeds the download size limit')
        if sum(part.file_size for part in nested) > 4 * 1024**3:
            raise ValueError('Nested Minecraft packs exceed the combined size limit')
        for part in nested:
            with tempfile.SpooledTemporaryFile(max_size=16 * 1024**2) as storage:
                with archive.open(part) as compressed:
                    shutil.copyfileobj(compressed, storage, 1024 * 1024)
                storage.seek(0)
                if not zipfile.is_zipfile(storage):
                    raise ValueError('Invalid nested Minecraft pack: ' + part.filename)
                storage.seek(0)
                with zipfile.ZipFile(storage) as inner:
                    files = [row for row in inner.infolist() if not row.is_dir()]
                    total_members += len(files)
                    if total_members > 100000:
                        raise ValueError('MCAddon contains too many files')
                    total_unpacked += sum(row.file_size for row in files)
                    if total_unpacked > 4 * 1024**3:
                        raise ValueError('Unpacked Minecraft Add-On exceeds 4 GiB')
                    manifest_candidates = [row for row in files if
                        row.filename == 'manifest.json' or
                        (row.filename.endswith('/manifest.json') and
                         len(row.filename.split('/')) == 2)]
                    if len(manifest_candidates) != 1:
                        raise ValueError('MCAddon module needs one root manifest.json')
                    manifest = json.loads(inner.read(manifest_candidates[0]).decode('utf-8'))
                    header = manifest.get('header') or {}
                    entries = manifest.get('modules') or []
                    if not isinstance(entries, list):
                        raise ValueError('Invalid Minecraft pack module declarations')
                    types = {row.get('type') for row in entries if isinstance(row, dict)}
                    kind = ('resource' if 'resources' in types else
                            'behavior' if types.intersection(('data', 'script')) else None)
                    if (not kind or not re.fullmatch(r'[0-9a-fA-F-]{36}', str(header.get('uuid', '')))
                            or not isinstance(header.get('version'), list)
                            or len(header['version']) != 3):
                        raise ValueError('Invalid nested Minecraft pack manifest')
                    prefix = manifest_candidates[0].filename[:-len('manifest.json')]
                    names = {row.filename for row in files}
                    if kind == 'behavior':
                        behavior_count += 1
                        for module in entries:
                            if not isinstance(module, dict) or module.get('type') != 'script':
                                continue
                            entry = module.get('entry')
                            if (not isinstance(entry, str) or not entry.endswith(('.js', '.mjs'))
                                    or not re.fullmatch(r'[A-Za-z0-9_./-]+', entry)
                                    or any(part in ('', '.', '..') for part in entry.split('/'))
                                    or prefix + entry not in names):
                                raise ValueError('Behavior pack declares missing/unsafe script code: ' + str(entry))
                            script_count += 1
                        for row in files:
                            relative = row.filename.removeprefix(prefix)
                            if relative.startswith('items/') and relative.endswith('.json'):
                                if row.file_size > 2 * 1024 * 1024:
                                    raise ValueError('Minecraft item definition is unexpectedly large')
                                item = json.loads(inner.read(row).decode('utf-8'))
                                identifier = str((((item.get('minecraft:item') or {})
                                                  .get('description') or {}).get('identifier') or ''))
                                if identifier.startswith('cinter:'):
                                    item_ids.add(identifier)
                    else:
                        resource_count += 1
                    broken = inner.testzip()
                    if broken is not None:
                        raise ValueError('Corrupted nested Minecraft pack member: ' + broken)
                    modules.append({'type': kind, 'uuid': header['uuid']})
    if not behavior_count:
        raise ValueError('Selected Minecraft Add-On contains only resources; behavior definitions are missing')
    return {'unpacked_bytes': total_unpacked, 'mcpack_count': len(modules),
            'member_count': total_members, 'cinter_items': len(item_ids),
            'behavior_packs': behavior_count, 'resource_packs': resource_count,
            'script_entries': script_count, 'modules': modules}


def _acquire_verified_pack(server, name: str, directory: Path) -> tuple[Path, dict]:
    """Stream and validate one authored MCAddon; caller owns the returned temp file.

    Download Pack and Set Current share the identical local/beta source and
    integrity checks. Neither may publish a partial or unverified archive.
    """
    name = _pack_name(name)
    relative = 'MCAddons/' + name + '/' + name + '.mcaddon'
    source_type = 'local' if _pack_local_mode() else 'beta'
    expected = None
    if source_type == 'local':
        # Read the real authored library without synthesizing an Assets tree.
        library = root_for(server)
        source = library / safe_relative(relative)
        if (source.is_symlink() or not source.is_file()
                or not source.resolve().is_relative_to(library.resolve())):
            raise FileNotFoundError('Local Minecraft pack was not assembled: ' + relative)
        stream = source.open('rb')
    else:
        from runtime.assets.CinterForgeAssetCloudflare import INDEX_SCHEMA
        with _pack_remote_open('index.json') as response:
            data = response.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise RuntimeError('Published beta Assets index is too large')
        published = json.loads(data)
        if published.get('schema') != INDEX_SCHEMA or not isinstance(published.get('files'), dict):
            raise ValueError('Published beta Assets index is invalid')
        expected = published['files'].get(relative)
        if (not isinstance(expected, dict) or type(expected.get('bytes')) is not int
                or not 0 < expected['bytes'] <= _PACK_DOWNLOAD_LIMIT
                or not re.fullmatch(r'[0-9a-f]{64}', str(expected.get('sha256') or ''))):
            raise FileNotFoundError('Pack is missing from the verified beta index: ' + relative)
        stream = _pack_remote_open(relative)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        stream.close()
        raise RuntimeError('Unsafe pack download directory')
    temp_path = None
    try:
        digest = hashlib.sha256()
        size = 0
        with stream, tempfile.NamedTemporaryFile(dir=directory, prefix='pack-', delete=False) as target:
            temp_path = Path(target.name)
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _PACK_DOWNLOAD_LIMIT:
                    raise ValueError('Downloaded Minecraft pack exceeds 2 GiB limit')
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        checksum = digest.hexdigest()
        if not size or (expected and (size != expected['bytes'] or checksum != expected['sha256'])):
            raise RuntimeError('Downloaded Minecraft pack failed beta index SHA-256 verification')
        if not zipfile.is_zipfile(temp_path):
            raise ValueError('Downloaded Minecraft Add-On is not a valid .mcaddon archive')
        detail = _inspect_mcaddon_contents(temp_path)
        result = {'schema': _PACK_STATE_SCHEMA, 'name': name, 'bytes': size,
                  'sha256': checksum, 'source': source_type, **detail}
        verified = temp_path
        temp_path = None
        return verified, result
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def pack_download(server, name: str) -> dict:
    """Save an MCAddon to the logged-in host Downloads folder, without selecting it.

    The embedded WebView does not guarantee handling of HTML's download
    attribute. A browser action therefore saves through the authorized Python
    host, and returns the real destination or a visible error.
    """
    from runtime import CinterForgeSessions
    home = CinterForgeSessions._real_login_home()
    downloads = home / 'Downloads'
    if downloads.is_symlink() or not downloads.is_dir():
        raise RuntimeError('Host Downloads directory is not available')
    name = _pack_name(name)
    with server.asset_pack_lock:
        staging, record = _acquire_verified_pack(server, name, downloads)
        try:
            for i in range(1, 10000):
                destination = downloads / (name + '.mcaddon' if i == 1 else
                                           f'{name} ({i}).mcaddon')
                # Exclusive creation: never overwrite another user download.
                try:
                    with destination.open('xb') as output, staging.open('rb') as source:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    return {'downloaded': True, 'name': name, 'path': str(destination),
                            'bytes': record['bytes'], 'sha256': record['sha256'],
                            'unpacked_bytes': record['unpacked_bytes'],
                            'mcpack_count': record['mcpack_count'],
                            'cinter_items': record['cinter_items'],
                            'behavior_packs': record['behavior_packs'],
                            'resource_packs': record['resource_packs'],
                            'script_entries': record['script_entries']}
                except FileExistsError:
                    continue
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
            raise RuntimeError('Too many existing Minecraft pack downloads')
        finally:
            staging.unlink(missing_ok=True)


def pack_set_current(server, name: str | None) -> dict:
    if isinstance(name, str) and name.casefold() in ('none', 'remove', 'clear'):
        name = None
    if name is not None:
        name = _pack_name(name)
    root = _pack_state_dir(server)
    with server.asset_pack_lock:
        previous = _pack_state(server)
        if name is None or previous and previous['name'] == name:
            (root / 'current.json').unlink(missing_ok=True)
            return {'current': None, 'removed': previous['name'] if previous else None}
        root.mkdir(parents=True, exist_ok=True)
        destination_dir = root / 'Downloaded' / name
        if destination_dir.is_symlink() or (destination_dir.exists() and not destination_dir.is_dir()):
            raise RuntimeError('Unsafe downloaded pack directory')
        staging, record = _acquire_verified_pack(server, name, destination_dir)
        try:
            destination = destination_dir / (name + '.mcaddon')
            if destination.is_symlink():
                raise RuntimeError('Unsafe downloaded pack file')
            os.replace(staging, destination)
        finally:
            staging.unlink(missing_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=root,
                                         prefix='current-', delete=False) as state_file:
            state_temp = Path(state_file.name)
            json.dump(record, state_file, indent=2)
            state_file.write('\n')
            state_file.flush()
            os.fsync(state_file.fileno())
        try:
            os.replace(state_temp, root / 'current.json')
        finally:
            state_temp.unlink(missing_ok=True)
        return {'current': name, 'downloaded_bytes': record['bytes'],
                'sha256': record['sha256'], 'unpacked_bytes': record['unpacked_bytes'],
                'mcpack_count': record['mcpack_count'],
                'cinter_items': record['cinter_items'],
                'behavior_packs': record['behavior_packs'],
                'resource_packs': record['resource_packs'],
                'script_entries': record['script_entries']}


# The generator's already-published HTML may still contain its original inert
# Minecraft element. Adapt that one element when served by Terminal so both
# local and existing beta gallery pages have the same working current-pack UI.
_PACK_BUTTON_SCRIPT = r'''(()=>{"use strict";
  const d=window.CINTERFORGE_PACK_DATA;
  const button=document.getElementById("minecraft");
  const download=document.getElementById("download");
  if(!d||typeof d.currentPack!=="string"||!d.currentPack)return;
  const name=d.currentPack;
  const actions=button?.parentElement||download?.parentElement;
  if(!actions)return;
  const label=document.createElement("span");label.setAttribute("role","status");
  label.setAttribute("aria-live","polite");
  label.style.cssText="color:#b5d7ea;align-self:center;font-size:12px;flex-basis:100%;overflow-wrap:anywhere";
  actions.append(label);
  async function request(path){
    const r=await fetch(path,{method:"POST",credentials:"same-origin",cache:"no-store",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});
    const j=await r.json();if(!r.ok)throw Error(j.error||"Pack request failed (HTTP "+r.status+")");
    return j;
  }
  const size=bytes=>Number.isFinite(Number(bytes))?(Number(bytes)/1048576).toFixed(1)+" MiB":"unknown size";
  const sizes=j=>" · "+size(j.downloaded_bytes??j.bytes)+" archive / "+size(j.unpacked_bytes)+" unpacked"+
    " · "+j.mcpack_count+" modules ("+j.behavior_packs+" BP / "+j.resource_packs+" RP)"+
    " · "+j.cinter_items+" cinter items · "+j.script_entries+" scripts";
  async function status(){
    const r=await fetch("/api/assets/packs/current",{credentials:"same-origin",cache:"no-store"});
    const j=await r.json();if(!r.ok)throw Error(j.error||"Current-pack lookup failed");
    if(button)button.textContent=j.current===name?"Remove as Current Pack":"Set as Current Pack";
  }
  if(button)button.onclick=async()=>{
    button.disabled=true;label.textContent="Downloading and verifying pack…";
    try{
      const j=await request("/api/assets/packs/set");
      button.textContent=j.current===name?"Remove as Current Pack":"Set as Current Pack";
      label.textContent=j.current===name?"Current Pack: "+name+sizes(j):"Current Pack removed";
      parent.postMessage({type:"cinter-bedrock-pack-changed"},location.origin);
    }catch(e){label.textContent="Pack selection failed: "+e.message;console.error("[bedrock packs]",e)}
    finally{button.disabled=false}
  };
  if(download){
    // Host downloads are explicit: WebView navigation to .mcaddon often does
    // not invoke a native download delegate and silently does nothing.
    download.style.visibility="visible";
    download.removeAttribute("href");
    download.removeAttribute("download");
    download.setAttribute("role","button");
    download.tabIndex=0;
    const save=async(event)=>{
      event.preventDefault();if(download.dataset.busy)return;
      download.dataset.busy="1";download.setAttribute("aria-disabled","true");
      label.textContent="Downloading and verifying "+name+"…";
      try{
        const j=await request("/api/assets/packs/download");
        label.textContent="Pack downloaded: "+j.path+sizes(j);
      }catch(e){label.textContent="Pack download failed: "+e.message;console.error("[bedrock packs]",e)}
      finally{delete download.dataset.busy;download.removeAttribute("aria-disabled")}
    };
    download.addEventListener("click",save);
    download.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" ")void save(e)});
  }
  if(button)status().catch(e=>{label.textContent=e.message;console.error("[bedrock packs]",e)});
})();'''



def _pack_gallery_page(page: bytes) -> bytes:
    old = b'<div id="minecraft">View in Minecraft</div>'
    button = b'<button id="minecraft" type="button">Set as Current Pack</button>'
    updated = page.replace(old, button)
    if b'id="minecraft"' not in updated:
        return updated  # Root redirect has no pack action.
    if b'</body>' not in updated:
        raise ValueError('Pack gallery is missing its closing body element')
    return updated.replace(b'</body>', b'<script>' + _PACK_BUTTON_SCRIPT.encode('utf-8') + b'</script></body>', 1)


def serve_packs(handler, relative: str) -> bool:
    if not relative:
        relative = 'index.html'
    rel = safe_relative(relative)
    if rel.suffix.lower() not in _PACK_VIEW_EXTENSIONS:
        raise FileNotFoundError('Unsupported Minecraft pack gallery resource')
    if _pack_local_mode():
        base = root_for(handler.server) / 'MCAddons'
        file = base / rel
        if (base.is_symlink() or file.is_symlink() or not file.is_file() or
                not file.resolve().is_relative_to(base.resolve()) or
                any(p.is_symlink() for p in file.parents if p == base or base in p.parents)):
            raise FileNotFoundError('Local Minecraft pack gallery resource missing: ' + str(rel))
        if file.suffix.lower() == '.html':
            _html_response(handler, _pack_gallery_page(file.read_bytes()))
        else:
            handler._send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream', same_origin_frame=True)
    else:
        with _pack_remote_open('MCAddons/' + rel.as_posix()) as response:
            if rel.suffix.lower() == '.html':
                page = response.read(16 * 1024 * 1024 + 1)
                if len(page) > 16 * 1024 * 1024:
                    raise ValueError('Published Minecraft gallery page is too large')
                _html_response(handler, _pack_gallery_page(page))
            else:
                size_header = response.headers.get('Content-Length')
                if not size_header or not size_header.isdigit() or not 0 < int(size_header) <= _PACK_DOWNLOAD_LIMIT:
                    raise ValueError('Published pack resource has an invalid length')
                size = int(size_header)
                mime = mimetypes.guess_type(rel.name)[0] or 'application/octet-stream'
                handler.send_response(200)
                handler.send_header('Content-Type', mime)
                handler.send_header('Content-Length', str(size))
                handler.send_header('X-Content-Type-Options', 'nosniff')
                handler.send_header('Cache-Control', 'no-store')
                handler.end_headers()
                remaining = size
                while remaining:
                    chunk = response.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError('Truncated published Minecraft pack resource')
                    handler.wfile.write(chunk)
                    remaining -= len(chunk)
    return True

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
    from runtime.assets import CinterForgeAssetOriginal as original
    try:
        base = original.source_root()
        relative = unquote(url_path[len('/assets-library/'):])
        if relative.startswith('packs/'):
            return serve_packs(handler, relative[len('packs/'):])
        if relative == 'workspace' or (relative == 'asset' and not
                any(parse_qs(urlsplit(handler.path).query).get(key) for key in ('uci', 'job'))):
            # Retired source/category library and empty asset views go to the report.
            handler.send_response(302)
            handler.send_header('Location', '/assets-library/SVG/Assets/AssetGallery.html')
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return True
        if relative == 'asset':
            _html_response(handler, (base.parent.parent / 'views/asset.html').read_bytes())
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
            # The pinned Babylon core is staged with the generated Asset Engine,
            # not beside the relocated Python runtime/assets package.
            authority = base.parent.parent / 'data/asset_engine/vendor'
            file = authority / rel
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
