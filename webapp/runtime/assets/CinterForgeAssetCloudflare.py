"""Cloudflare transport for the existing shared CinterForge Assets library.

Use the existing CinterForge R2 accounts, buckets and provider object_prefix.
Every asset stays at its own HTTPS-readable relative path. A small SHA-256
file index is committed last, only after all individual objects are verified.
Legacy ZIP bundles are never created, read or retained by this transport.

Internal: existing private cdn-assets-dev bucket.
Beta: existing public cinterforge bucket, isolated under the Assets beta folder.
Release: existing public cinterforge bucket, Assets root.
The case of ``assets`` is retained from the Server's published R2 contract.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from runtime.assets.CinterForgeAssets import inventory, safe_relative

CHANNELS = {
    'internal': 'cdn-assets-dev',
    'beta': 'cinterforge',
    'release': 'cinterforge',
}
INDEX_NAME = 'index.json'  # Reserved remote metadata key for direct-file inventory.
INDEX_SCHEMA = 'cinterforge.assets-files.v1'
LEGACY_BUNDLE_SCHEMAS = frozenset(('cinterforge.assets-bundles.v1',
                                   'cinterforge.assets-bundles.v2'))
# Exact format produced by the old Terminal bundler. Never delete arbitrary ZIPs.
LEGACY_BUNDLE_NAME = re.compile(r'[A-Za-z0-9._-]+-[0-9]{4}-[0-9a-f]{12}\.zip\Z')
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_FILES = 200000
MAX_FILE_BYTES = 100 * 1024**3


def _transport():
    """Use the existing Host R2 transport staged with Terminal."""
    from runtime import CinterForgeSettings as settings
    stage = settings.ASSETS / 'data/cloudflare_r2_sync.py'
    if not stage.is_file():
        source_root = Path(__file__).resolve().parents[3]
        if (settings.ASSETS.resolve() == (source_root / 'webapp').resolve()
                and (source_root / 'Cinter').is_file()):
            stage = source_root / 'host/publishing/cloudflare_r2_sync.py'
    if stage.is_symlink() or not stage.is_file():
        raise RuntimeError('Verified Cloudflare transport missing from Terminal stage')
    spec = importlib.util.spec_from_file_location('_cinterforge_terminal_r2', stage)
    if not spec or not spec.loader:
        raise RuntimeError('Cannot load staged Cloudflare transport')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config_path() -> Path:
    provided = (os.environ.get('CINTERFORGE_CLOUDFLARE_CONFIG') or
                os.environ.get('CINTER_CLOUDFLARE_CONFIG') or
                os.environ.get('CINTERFORGE_CONFIG') or
                os.environ.get('CINTERFORGE_CONFIG_JSON'))
    if provided:
        return Path(provided).expanduser()
    user_home = Path(os.environ.get('CINTER_USER_HOME') or Path.home()).expanduser()
    # Host transport already searches alternative known config paths.
    return user_home / 'cfdev/config.json'


def _asset_prefix(cfg: dict, channel: str) -> str:
    """Mirror Server asset_transport._cloudflare_prefix(), then isolate beta.

    Never bypass the configured R2 provider's object_prefix. In particular,
    with object_prefix=cinterforge, Assets keys are cinterforge/assets/...,
    NOT assets/... at the bucket root. This is a key prefix, not a new bucket.
    """
    provider_prefix = str(cfg.get('object_prefix') or '').strip('/')
    explicit = os.environ.get('CINTERFORGE_ASSETS_CLOUDFLARE_PREFIX', '').strip().strip('/')
    # The original Server supports an explicit Assets key prefix. Preserve its
    # provider-prefix-aware meaning rather than adding an independent namespace.
    if explicit:
        if explicit == 'assets' and provider_prefix and provider_prefix != 'assets':
            base = (provider_prefix if provider_prefix.endswith('/assets')
                    else provider_prefix + '/assets')
        else:
            base = explicit
    elif not provider_prefix:
        base = 'assets'
    elif provider_prefix == 'assets' or provider_prefix.endswith('/assets'):
        base = provider_prefix
    else:
        base = provider_prefix + '/assets'
    return base + ('/beta' if channel == 'beta' else '')


def _credential_config(channel: str, transport=None) -> tuple[Any, dict, str]:
    if channel not in CHANNELS:
        raise ValueError('Expected Cloudflare channel: internal|beta|release')
    r2 = transport or _transport()
    provider = os.environ.get('CINTERFORGE_ASSETS_CLOUDFLARE_PROVIDER', '').strip() or 'auto'
    cfg = r2._load_config(_config_path(), provider, required_bucket=CHANNELS[channel])
    return r2, cfg, _asset_prefix(cfg, channel)


def probe_read(channel: str, *, transport=None) -> bool:
    """Use authenticated R2 listing for launch-time capability gating."""
    try:
        r2, cfg, prefix = _credential_config(channel, transport)
        status, headers, response = r2._request(cfg, 'GET', query_params={
            'list-type': '2', 'prefix': prefix.rstrip('/') + '/', 'max-keys': '1',
        })
        from xml.etree import ElementTree
        return ElementTree.fromstring(response).tag.endswith('ListBucketResult')
    except Exception:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_destination(root: Path, relative: str) -> Path:
    path = root
    for part in safe_relative(relative).parts:
        path /= part
        if path.is_symlink():
            raise RuntimeError('Symlinked asset-file path: ' + relative)
    return path


def _fetch_index(r2, cfg: dict, prefix: str, objects: dict[str, dict]) -> dict | None:
    """Read only the current direct-file index or metadata for ZIP migration.

    Old bundle *contents* are not read or extracted; the original local Assets
    tree is authoritative for migration. An unknown index is never overwritten.
    """
    if INDEX_NAME not in objects:
        return None
    size = int(objects[INDEX_NAME]['size'])
    if not 0 < size <= MAX_INDEX_BYTES:
        raise ValueError('Invalid remote Assets index size')
    _, _, raw = r2._request(cfg, 'GET', key=f'{prefix}/{INDEX_NAME}')
    if len(raw) != size or len(raw) > MAX_INDEX_BYTES:
        raise ValueError('Remote Assets index changed while reading it')
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError('Remote Assets index is not an object')
    schema = doc.get('schema')
    if schema in LEGACY_BUNDLE_SCHEMAS:
        if not isinstance(doc.get('bundles'), list):
            raise ValueError('Malformed old Assets ZIP index')
        # Old bundle metadata is only used for safe cleanup after a new direct
        # index is committed. We never support downloading the old ZIPs.
        return doc
    if schema != INDEX_SCHEMA:
        raise ValueError(f'Unrecognized Assets index schema: {schema!r}; refusing to overwrite')
    files = doc.get('files')
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ValueError('Invalid Assets direct-file index')
    if 'bundles' in doc:
        raise ValueError('Direct-file Assets index unexpectedly lists bundles')
    directories = doc.get('directories', [])  # Optional for historical v1 indexes.
    if (not isinstance(directories, list) or len(directories) > MAX_FILES or
            not all(isinstance(rel, str) for rel in directories) or
            len(set(directories)) != len(directories)):
        raise ValueError('Invalid Assets directory inventory')
    for rel in directories:
        _safe_destination(Path('/ASSET-VALIDATION'), rel)
        if rel in files:
            raise ValueError('Assets file/directory name collision: ' + rel)
    if not files and not directories:
        raise ValueError('Empty Assets index')
    for rel, record in files.items():
        _safe_destination(Path('/ASSET-VALIDATION'), rel)
        if rel == INDEX_NAME:
            raise ValueError('Authored Assets cannot replace their remote index')
        if (not isinstance(record, dict)
                or not re.fullmatch(r'[0-9a-f]{64}', str(record.get('sha256')))
                or type(record.get('bytes')) is not int
                or not 0 <= record['bytes'] <= MAX_FILE_BYTES):
            raise ValueError('Invalid Assets direct-file record: ' + rel)
    return doc


def _canonical_inventory(root: Path) -> dict:
    """Publish and restore the *entire* Assets directory, without a file allowlist.

    Keep the existing SHA-256 integrity, symlink and path-safety checks. No
    file or folder is excluded based on name, format, depth or provenance.
    """
    return inventory(root)


def _source_file_list(root: Path) -> tuple[dict, list[tuple[str, Path]]]:
    doc = _canonical_inventory(root)
    files = doc.get('files') or {}
    if len(files) > MAX_FILES:
        raise ValueError('Too many Assets files')
    if INDEX_NAME in files:
        raise ValueError('Local Assets/index.json conflicts with the published file index')
    rows = []
    for rel in sorted(files):
        file = _safe_destination(root, rel)
        if not file.is_file():
            raise FileNotFoundError('Assets source disappeared: ' + rel)
        if not 0 <= files[rel]['bytes'] <= MAX_FILE_BYTES:
            raise ValueError('Asset exceeds maximum direct object size: ' + rel)
        rows.append((rel, file))
    return doc, rows


def _old_generated_zips(objects: dict[str, dict], old: dict | None) -> set[str]:
    """Recognize only the old publisher's own bundles, never authored ZIP files."""
    legacy = {key for key in objects if LEGACY_BUNDLE_NAME.fullmatch(key)}
    if old and old.get('schema') in LEGACY_BUNDLE_SCHEMAS:
        for entry in old['bundles']:
            if not isinstance(entry, dict):
                raise ValueError('Invalid old Assets bundle entry')
            name = entry.get('file')
            # An old index may reference a bundle with a different hash label.
            # Top-level basename and hash evidence prevent nested UCI files
            # from being mistaken for generated legacy bundles.
            if (isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9._-]+\.zip', name)
                    and re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256')))
                    and name in objects):
                legacy.add(name)
    return legacy


def publish(channel: str, assets_root: Path, *, transport=None) -> dict:
    """Publish unrolled Assets: individual R2 files then one SHA-256 index.

    The original Assets folder is never zipped or moved. Generated old ZIP
    bundles are deleted *only after* all direct objects and the new index
    round-trip successfully. Unrelated remote objects are not removed.
    """
    if os.environ.get('CINTERFORGE_SETUP_DISABLE_UPLOADS') == '1':
        raise RuntimeError('Cloudflare publishing disabled during Setup/Test')
    r2, cfg, prefix = _credential_config(channel, transport)
    root = Path(assets_root).resolve()
    # A previously generated gallery must match the currently present UCI
    # folders before it is included in the published file inventory. Otherwise
    # a deleted asset's stale report card would be republished on Cloudflare.
    if (root / 'AssetGallery.html').is_file():
        from runtime.assets import asset_gallery
        asset_gallery.generate(root.parent)
    doc, files = _source_file_list(root)
    if not files and not doc['directories']:
        raise RuntimeError('Assets library is empty; refusing to publish')
    remote = r2._list_remote(cfg, prefix)
    old = _fetch_index(r2, cfg, prefix, remote)
    # Mirror the complete *indexed* Assets snapshot. Do not delete unindexed
    # objects belonging to another workflow or bucket prefix.
    deleted_files = sorted(rel for rel in ((old or {}).get('files') or {})
                           if old and old.get('schema') == INDEX_SCHEMA
                           and rel not in doc['files'])
    legacy_zips = _old_generated_zips(remote, old)
    collision = legacy_zips.intersection(doc['files'])
    if collision:
        raise RuntimeError('Generated old ZIP name collides with an authored Assets file: '
                           + sorted(collision)[0])
    total = sum(row['bytes'] for row in doc['files'].values())
    print(f'[assets publish] START bucket={cfg["bucket"]}/{prefix} '
          f'direct_files={len(files)} total_bytes={total} old_generated_zips={len(legacy_zips)}',
          flush=True)
    workspace = root.parent / 'Intermediates/CinterForgeTerminal/AssetsPublish'
    if workspace.is_symlink():
        raise RuntimeError('Unsafe Assets publishing workspace')
    workspace.mkdir(parents=True, exist_ok=True)
    new_files = 0
    reused_files = 0
    verified_bytes = 0
    with tempfile.TemporaryDirectory(prefix='direct-', dir=workspace) as temporary:
        out = Path(temporary)
        probe = out / 'remote-file'
        for number, (rel, source) in enumerate(files, 1):
            expected = doc['files'][rel]
            digest, size = expected['sha256'], expected['bytes']
            if source.stat().st_size != size or _sha256(source) != digest:
                raise RuntimeError('Local Assets changed during upload: ' + rel)
            key = prefix + '/' + rel
            # Skip files already committed by a verified direct-file publish.
            # Everything else must be PUT and downloaded for SHA-256 verification.
            prior = old['files'].get(rel) if old and old.get('schema') == INDEX_SCHEMA else None
            unchanged = (prior == expected and rel in remote and
                         int(remote[rel]['size']) == size)
            if unchanged:
                reused_files += 1
            else:
                r2._put_file(cfg, key, source)
                new_files += 1
                probe.unlink(missing_ok=True)
                r2._get_file(cfg, key, probe, expected_size=size)
                if not probe.is_file() or probe.stat().st_size != size or _sha256(probe) != digest:
                    raise RuntimeError('Remote Assets SHA-256 mismatch: ' + rel)
                probe.unlink(missing_ok=True)
            verified_bytes += size
            if number % 25 == 0 or number == len(files):
                print(f'[assets publish] Direct files {number}/{len(files)} '
                      f'uploaded={new_files} reused={reused_files} '
                      f'verified_bytes={verified_bytes}/{total}', flush=True)
        # Only commit an inventory that still describes the complete original
        # tree. Do not create a ZIP intermediate or write inside Assets itself.
        if _canonical_inventory(root) != doc:
            raise RuntimeError('Local Assets changed during upload; index not committed')
        # Delete previously indexed files removed locally. Keep unindexed
        # objects and all local files untouched.
        for number, rel in enumerate(deleted_files, 1):
            if rel in remote:
                r2._delete(cfg, remote[rel]['key'])
            print(f'[assets publish] Removed deleted Assets file {number}/{len(deleted_files)}: {rel}', flush=True)
        if deleted_files:
            remaining = r2._list_remote(cfg, prefix)
            stale = [rel for rel in deleted_files if rel in remaining]
            if stale:
                raise RuntimeError('Deleted Assets files remain on Cloudflare: ' + stale[0])
        index = {'schema': INDEX_SCHEMA,
                 'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                 'file_count': len(files),
                 'files': {rel: detail for rel, detail in sorted(doc['files'].items())},
                 'directories': doc['directories']}
        index_path = out / INDEX_NAME
        index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        if index_path.stat().st_size > MAX_INDEX_BYTES:
            raise ValueError('Assets file index exceeds supported size')
        r2._put_file(cfg, prefix + '/' + INDEX_NAME, index_path)
        _, _, response = r2._request(cfg, 'GET', key=prefix + '/' + INDEX_NAME)
        if response != index_path.read_bytes():
            raise RuntimeError('Published direct-file index did not round-trip exactly')
    # The new index now references only individually published paths. Remove
    # the generated bundle objects from the failed/old publisher, never user
    # supplied asset ZIPs nested under their authored folder names.
    for number, name in enumerate(sorted(legacy_zips), 1):
        try:
            r2._delete(cfg, remote[name]['key'])
        except Exception as exc:
            raise RuntimeError(f'Direct Assets publish committed, but old ZIP cleanup failed '
                               f'at {number}/{len(legacy_zips)}: {name}: {exc}') from exc
        print(f'[assets publish] Removed generated old ZIP {number}/{len(legacy_zips)}: {name}', flush=True)
    if legacy_zips:
        remaining = r2._list_remote(cfg, prefix)
        if any(name in remaining for name in legacy_zips):
            raise RuntimeError('Direct Assets published but old generated ZIPs remain in R2')
    print(f'[assets publish] PASS direct_files={len(files)} uploaded={new_files} '
          f'reused={reused_files} deleted_files={len(deleted_files)} '
          f'deleted_old_zips={len(legacy_zips)} '
          f'bucket={cfg["bucket"]}/{prefix}', flush=True)
    return {'channel': channel, 'files': len(files), 'published': True,
            'uploaded': new_files, 'reused': reused_files,
            'deleted_files': len(deleted_files),
            'deleted_old_zips': len(legacy_zips),
            'destination': cfg['bucket'] + '/' + prefix}


def _merge_missing(root: Path, incoming: Path, files: dict[str, str]) -> dict:
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise RuntimeError('Unsafe existing Assets root')
    root.mkdir(parents=True, exist_ok=True)
    installed, reused, conflicts = 0, 0, []
    for number, (rel, digest) in enumerate(sorted(files.items()), 1):
        dest = _safe_destination(root, rel)
        if dest.exists():
            if dest.is_file() and _sha256(dest) == digest:
                reused += 1
            else:
                conflicts.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        source = _safe_destination(incoming, rel)
        with tempfile.NamedTemporaryFile(dir=dest.parent, prefix='Assets-Restore-', delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copyfile(source, temporary)
            if _sha256(temporary) != digest:
                raise RuntimeError('Assets restore copy failed verification: ' + rel)
            try:
                os.link(temporary, dest)  # no clobber, atomic
                installed += 1
            except FileExistsError:
                conflicts.append(rel)
            except OSError:
                try:
                    with dest.open('xb') as out, temporary.open('rb') as file:
                        shutil.copyfileobj(file, out, length=4 * 1024 * 1024)
                    if _sha256(dest) != digest:
                        raise RuntimeError('Assets restore destination digest mismatch: ' + rel)
                    installed += 1
                except FileExistsError:
                    conflicts.append(rel)
                except Exception:
                    dest.unlink(missing_ok=True)
                    raise
        finally:
            temporary.unlink(missing_ok=True)
        if number % 25 == 0 or number == len(files):
            print(f'[assets setup] Installed {installed}, reused {reused}, conflicts {len(conflicts)} ({number}/{len(files)})', flush=True)
    return {'installed': installed, 'reused': reused, 'conflicts': conflicts}


def setup(channel: str, assets_root: Path, *, transport=None) -> dict:
    """Restore the direct-file index over HTTPS-compatible object keys.

    Each object is downloaded independently, checked against the committed
    SHA-256, then merged without overwriting edited or existing local files.
    Never download or unpack old ZIP bundles.
    """
    r2, cfg, prefix = _credential_config(channel, transport)
    remote = r2._list_remote(cfg, prefix)
    index = _fetch_index(r2, cfg, prefix, remote)
    if not index:
        raise FileNotFoundError(f'No direct-file Assets index under {cfg["bucket"]}/{prefix}. '
                                'Publish the local Assets library first using assets publish <channel>.')
    if index['schema'] != INDEX_SCHEMA:
        raise RuntimeError(f'Cloudflare Assets still uses an old ZIP inventory under '
                           f'{cfg["bucket"]}/{prefix}. Publish local Assets directly first; '
                           'ZIP restoration has been removed.')
    root = Path(assets_root).absolute()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise RuntimeError('Unsafe existing Assets root')
    # Every indexed file and directory belongs to the full Assets snapshot.
    files = index['files']
    directories = index.get('directories', [])
    for rel, entry in files.items():
        if rel not in remote:
            raise FileNotFoundError('Assets index references missing direct object: ' + rel)
        if int(remote[rel]['size']) != entry['bytes']:
            raise RuntimeError('Assets indexed object has incorrect size: ' + rel)
    revision = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:20]
    incoming = root.parent / 'Run/CinterForgeTerminal/AssetsIncoming' / channel / revision
    if incoming.is_symlink():
        raise RuntimeError('Unsafe Assets restore workspace')
    incoming.mkdir(parents=True, exist_ok=True)
    hashes = {}
    reused, downloaded = 0, 0
    print(f'[assets setup] START bucket={cfg["bucket"]}/{prefix} '
          f'direct_files={len(files)}', flush=True)
    for number, (rel, entry) in enumerate(sorted(files.items()), 1):
        local = _safe_destination(root, rel)
        digest, size = entry['sha256'], entry['bytes']
        # A valid local file needs no download; an edited local file remains
        # untouched but we verify the published version for future restores.
        if local.is_file() and not local.is_symlink() and local.stat().st_size == size and _sha256(local) == digest:
            reused += 1
            hashes[rel] = digest
            continue
        dest = _safe_destination(incoming, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not (dest.is_file() and dest.stat().st_size == size and _sha256(dest) == digest):
            dest.unlink(missing_ok=True)
            r2._get_file(cfg, remote[rel]['key'], dest, expected_size=size)
            downloaded += 1
        if not dest.is_file() or dest.stat().st_size != size or _sha256(dest) != digest:
            dest.unlink(missing_ok=True)
            raise RuntimeError('Assets remote file digest mismatch: ' + rel)
        hashes[rel] = digest
        if number % 25 == 0 or number == len(files):
            print(f'[assets setup] Verified {number}/{len(files)} direct files '
                  f'downloaded={downloaded} already_local={reused}', flush=True)
    result = _merge_missing(root, incoming, hashes)
    # Object stores represent files only; explicitly restore even empty folders.
    for relative in directories:
        destination = _safe_destination(root, relative)
        if destination.exists() and not destination.is_dir():
            raise RuntimeError('Assets directory conflicts with existing file: ' + relative)
        destination.mkdir(parents=True, exist_ok=True)
    print(f'[assets setup] PASS direct_files={len(files)} downloaded={downloaded} '
          f'installed={result["installed"]} reused={result["reused"]} '
          f'conflicts={len(result["conflicts"])}', flush=True)
    return {'channel': channel, 'files': len(files), 'downloaded': downloaded,
            'destination': str(root), **result}
