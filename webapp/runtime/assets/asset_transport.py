#========= Copyright 2025+, CinterForge, Inc. All rights reserved. ============
"""Cloudflare asset transport, Push/Pull, publication, and official-library transfer.

This module owns remote transport mechanics. Asset identity, saved-library
persistence, manifests, and resolution remain in ``asset_management``.
``asset_management`` re-exports this module's entry points for compatibility.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from .terminal_paths import cinterforgeBuildsRootPath
from .asset_blockify import _blockbench_resource_session, save_blockbench_override, save_blockify
from .asset_management import ASSET_REFERENCE_VERSION, SCHEMA, _ASSET_PROGRESS_CONTEXT, _ASSETS_ROOT_CONTEXT, _authoritative_tree_snapshot, _load_manifest, _overlay_authoritative_tree, _sha256, assets_root, log, merge_library, project_root
_SYNC_LOCK = threading.Lock()
_SYNC_TASK: dict[str, Any] = {'state': 'idle', 'phase': 'idle', 'message': '', 'percent': 0, 'current': 0, 'total': 0, 'result': None, 'error': ''}

def _set_sync_progress(*, state: str | None=None, phase: str | None=None, message: str | None=None, percent: int | None=None, current: int | None=None, total: int | None=None, result: Any=None, error: str | None=None) -> None:
    with _SYNC_LOCK:
        if state is not None:
            _SYNC_TASK['state'] = state
        if phase is not None:
            _SYNC_TASK['phase'] = phase
        if message is not None:
            _SYNC_TASK['message'] = message
        if percent is not None:
            _SYNC_TASK['percent'] = max(0, min(100, int(percent)))
        if current is not None:
            _SYNC_TASK['current'] = int(current)
        if total is not None:
            _SYNC_TASK['total'] = int(total)
        if result is not None:
            _SYNC_TASK['result'] = result
        if error is not None:
            _SYNC_TASK['error'] = str(error)

def sync_status() -> dict[str, Any]:
    with _SYNC_LOCK:
        return dict(_SYNC_TASK)

def _sync_worker() -> None:
    try:
        result = sync_official(progress=lambda **kw: _set_sync_progress(**kw))
        _set_sync_progress(state='complete', phase='complete', message='Asset sync complete.', percent=100, result=result, error='')
    except Exception as exc:
        log(f'official-sync-background-failed error={exc!r}')
        _set_sync_progress(state='failed', phase='failed', message=f'Sync failed: {exc}', error=str(exc))

def start_sync_official() -> dict[str, Any]:
    with _SYNC_LOCK:
        if _SYNC_TASK.get('state') == 'running':
            return dict(_SYNC_TASK)
        _SYNC_TASK.update({'state': 'running', 'phase': 'starting', 'message': 'Starting asset sync…', 'percent': 0, 'current': 0, 'total': 0, 'result': None, 'error': ''})
    thread = threading.Thread(target=_sync_worker, name='cinterforge-assets-sync', daemon=True)
    thread.start()
    return sync_status()
_LEGACY_BUNDLE_SCHEMA = 'cinterforge.assets-bundles.v1'
_BUNDLE_SCHEMA = 'cinterforge.assets-bundles.v2'
_PORTABLE_CONTRACT_VERSION = 1
_BUNDLE_TARGET_BYTES = 384 * 1024 * 1024
_BUNDLE_GROUPS = (('Images', Path('Images')), ('Objects', Path('Objects')), ('Animated-Mixamo', Path('Animated') / 'Mixamo'), ('Animated-Fixed', Path('Animated') / 'Fixed'), ('Animations', Path('Animations')))

def _folder_bytes(folder: Path) -> int:
    return sum((p.stat().st_size for p in folder.rglob('*') if p.is_file()))

def _build_transport_bundles(source: Path, transport: Path, *, progress: Callable[..., None] | None=None) -> dict[str, Any]:
    """Package the complete Builds/Assets tree exactly as it exists on disk.

    The Cloudflare transport is a byte transport, not an asset-schema migration.
    It must not rewrite manifests or require per-version runtime descriptors merely
    to move an already-working asset library between machines.
    """
    source = Path(source).resolve()
    transport = Path(transport).resolve()
    if transport.exists():
        shutil.rmtree(transport)
    transport.mkdir(parents=True, exist_ok=True)
    files = sorted((p for p in source.rglob('*') if p.is_file() and (not p.name.startswith('.')) and ('/.' not in p.relative_to(source).as_posix())), key=lambda p: p.relative_to(source).as_posix().casefold()) if source.is_dir() else []
    snapshot = {p.relative_to(source).as_posix(): _sha256(p) for p in files}
    by_group: dict[str, list[Path]] = {}
    for path in files:
        rel = path.relative_to(source)
        label = rel.parts[0] if rel.parts else 'Assets'
        by_group.setdefault(label, []).append(path)
    bundles: list[dict[str, Any]] = []
    total_files = len(files)
    done = 0
    for raw_label in sorted(by_group, key=str.casefold):
        safe_label = re.sub('[^A-Za-z0-9._-]+', '-', raw_label).strip('-') or 'Assets'
        rows = by_group[raw_label]
        batch: list[Path] = []
        batch_bytes = 0
        part = 1

        def flush(batch_rows: list[Path], part_no: int) -> None:
            nonlocal done
            if not batch_rows:
                return
            temp_path = transport / f'{safe_label}-{part_no:04d}.zip'
            with zipfile.ZipFile(temp_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for file_path in batch_rows:
                    zf.write(file_path, file_path.relative_to(source).as_posix())
                    done += 1
                    if progress:
                        progress(state='running', phase='bundle', current=done, total=total_files, percent=35 + 10 * done // max(1, total_files), message=f'Packing assets… {done}/{total_files} files')
            digest = _sha256(temp_path)
            final_name = f'{safe_label}-{part_no:04d}-{digest[:12]}.zip'
            final = transport / final_name
            temp_path.replace(final)
            bundles.append({'file': final_name, 'sha256': digest, 'size_bytes': final.stat().st_size, 'root': raw_label, 'file_count': len(batch_rows)})
        for file_path in rows:
            size = file_path.stat().st_size
            if batch and batch_bytes + size > _BUNDLE_TARGET_BYTES:
                flush(batch, part)
                part += 1
                batch = []
                batch_bytes = 0
            batch.append(file_path)
            batch_bytes += size
        flush(batch, part)
    index = {'schema': _BUNDLE_SCHEMA, 'portable_contract_version': 0, 'created_at': datetime.now(timezone.utc).isoformat(), 'target_bundle_bytes': _BUNDLE_TARGET_BYTES, 'bundles': bundles, 'files': snapshot}
    (transport / 'index.json').write_text(json.dumps(index, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return index

def _safe_relative(rel: str) -> Path:
    value = PurePosixPath(str(rel))
    if value.is_absolute() or '..' in value.parts:
        raise ValueError(f'Unsafe asset transport path: {rel!r}')
    return Path(*value.parts)

class AssetTransportContentMismatch(RuntimeError):

    def __init__(self, *, missing: list[str], extra: list[str], changed: list[str]):
        self.missing = list(missing)
        self.extra = list(extra)
        self.changed = list(changed)
        super().__init__(f'Asset transport content mismatch: missing={self.missing} extra={self.extra} changed={self.changed}')

def _extract_transport_bundles(transport: Path, destination: Path, *, progress: Callable[..., None] | None=None, approved_conflicts: set[str] | None=None) -> dict[str, Any]:
    transport = Path(transport).resolve()
    destination = Path(destination).resolve()
    index_path = transport / 'index.json'
    if not index_path.is_file():
        merge = merge_library(transport, destination)
        for rel in sorted(set(_tree_snapshot(transport)) - set(_tree_snapshot(destination))):
            _copy_tree_file(transport, destination, rel)
        return {'legacy': True, 'merged': merge, 'bundles': 0}
    index = json.loads(index_path.read_text(encoding='utf-8'))
    schema = str(index.get('schema') or '')
    if schema not in {_BUNDLE_SCHEMA, _LEGACY_BUNDLE_SCHEMA}:
        raise ValueError(f'Unsupported Assets bundle index: {schema!r}')
    bundles = index.get('bundles') or []
    total = len(bundles)
    destination.mkdir(parents=True, exist_ok=True)
    for i, row in enumerate(bundles, 1):
        archive = transport / str(row.get('file') or '')
        if not archive.is_file():
            raise FileNotFoundError(f'Asset bundle missing: {archive.name}')
        if _sha256(archive) != str(row.get('sha256') or ''):
            raise RuntimeError(f'Asset bundle hash mismatch: {archive.name}')
        with zipfile.ZipFile(archive, 'r') as zf:
            zf.extractall(destination)
        if progress:
            progress(state='running', phase='extract', current=i, total=total, percent=25 + 10 * i // max(1, total), message=f'Restoring assets… {i}/{total} bundles')
    if schema == _BUNDLE_SCHEMA:
        expected = {str(k): str(v) for k, v in (index.get('files') or {}).items()}
        actual = _tree_snapshot(destination)
        if actual != expected:
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            changed = sorted((k for k in set(expected) & set(actual) if expected[k] != actual[k]))
            approved = set(approved_conflicts or ())
            unapproved = [p for p in changed if p not in approved]
            if missing or extra or unapproved:
                raise AssetTransportContentMismatch(missing=missing[:10], extra=extra[:10], changed=unapproved[:10])
            for rel in changed:
                target = destination / _safe_relative(rel)
                if target.is_file():
                    target.unlink()
            log(f'asset-transport-quarantine approved={changed}')
            return {'legacy': False, 'bundles': total, 'files': len(expected) - len(changed), 'portable_contract_version': 0, 'schema': schema, 'quarantined': changed}
    return {'legacy': schema == _LEGACY_BUNDLE_SCHEMA, 'bundles': total, 'files': len(index.get('files') or {}), 'portable_contract_version': 0, 'schema': schema, 'quarantined': []}

def _sync_state_path() -> Path:
    storage = os.environ.get('CINTERFORGE_STORAGE_ROOT', '').strip()
    builds = Path(storage).expanduser() if storage else cinterforgeBuildsRootPath()
    path = builds / 'Manifest' / 'shared_asset_sync_state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def _tree_snapshot(root: Path) -> dict[str, str]:
    root = Path(root).resolve()
    if not root.is_dir():
        return {}
    result: dict[str, str] = {}
    for path in sorted((p for p in root.rglob('*') if p.is_file())):
        rel = path.relative_to(root).as_posix()
        if rel.startswith('.') or '/.' in rel:
            continue
        result[rel] = _sha256(path)
    return result

def _load_sync_state() -> dict[str, Any]:
    path = _sync_state_path()
    if not path.is_file():
        return {'schema': 'cinterforge.assets-sync-state.v1', 'files': {}}
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('files'), dict):
        raise ValueError(f'Invalid shared asset sync state: {path}')
    return payload

def _official_transport_cache_path() -> Path:
    """Persistent cache for the authenticated R2 Assets transport.

    The R2 helper already knows how to compare a populated local transport
    against the remote prefix.  Keeping this cache across editor launches means
    launch-time receive checks the same 21 transport objects in place instead of
    creating an empty temporary directory and downloading ~1.2 GB every time.
    This cache contains transport bundles only; the central Assets library remains
    the authoritative extracted destination.
    """
    storage = os.environ.get('CINTERFORGE_STORAGE_ROOT', '').strip()
    builds = Path(storage).expanduser() if storage else cinterforgeBuildsRootPath()
    path = builds / 'Manifest' / 'OfficialAssetsTransportCache'
    path.mkdir(parents=True, exist_ok=True)
    return path

def _write_sync_state(files: dict[str, str], *, transport_revision: str='', quarantined: list[str] | None=None) -> None:
    payload = {'schema': 'cinterforge.assets-sync-state.v1', 'updated_at': datetime.now(timezone.utc).isoformat(), 'files': dict(sorted(files.items())), 'transport_revision': str(transport_revision or ''), 'quarantined': sorted(set(quarantined or []))}
    path = _sync_state_path()
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(path)
    log(f'sync-state-written path={path} files={len(files)}')

def _copy_tree_file(source_root: Path, destination_root: Path, rel: str) -> None:
    source = source_root / rel
    destination = destination_root / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)

def _remove_empty_parents(path: Path, root: Path) -> None:
    parent = path.parent
    root = root.resolve()
    while parent != root and root in parent.resolve().parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent

def _reconcile_official(incoming: Path, destination: Path) -> dict[str, Any]:
    """Three-way reconcile local and official assets against the last published sync state."""
    incoming = Path(incoming).resolve()
    destination = Path(destination).resolve()
    base = {str(k): str(v) for k, v in (_load_sync_state().get('files') or {}).items()}
    local = _authoritative_tree_snapshot(destination)
    remote = _authoritative_tree_snapshot(incoming)
    if not base:
        merged = merge_library(incoming, destination)
        destination_snapshot = _tree_snapshot(destination)
        for rel in sorted(set(remote) - set(destination_snapshot)):
            _copy_tree_file(incoming, destination, rel)
        resolved = _authoritative_tree_snapshot(destination)
        return {'initial': True, 'merged': merged, 'resolved_files': len(resolved), 'deleted_local': 0, 'pulled': len(set(remote) - set(local)), 'kept_local': len(local), 'conflicts': []}
    pulled = deleted_local = kept_local = 0
    conflicts: list[str] = []
    for rel in sorted(set(base) | set(local) | set(remote)):
        b = base.get(rel)
        l = local.get(rel)
        r = remote.get(rel)
        if l == r:
            if l is not None:
                kept_local += 1
            continue
        if b is None:
            if l is None and r is not None:
                _copy_tree_file(incoming, destination, rel)
                pulled += 1
            elif r is None and l is not None:
                kept_local += 1
            else:
                conflicts.append(rel)
            continue
        local_changed = l != b
        remote_changed = r != b
        if local_changed and remote_changed:
            conflicts.append(rel)
        elif remote_changed:
            target = destination / rel
            if r is None:
                if target.is_file():
                    target.unlink()
                    _remove_empty_parents(target, destination)
                    deleted_local += 1
            else:
                _copy_tree_file(incoming, destination, rel)
                pulled += 1
        else:
            kept_local += 1
    if conflicts:
        raise RuntimeError('Shared asset sync conflict(s); both local and remote changed since last sync: ' + ', '.join(conflicts[:20]) + (' ...' if len(conflicts) > 20 else ''))
    return {'initial': False, 'merged': {'folders': 0, 'copied': 0, 'skipped': 0, 'renamed': 0}, 'resolved_files': len(_authoritative_tree_snapshot(destination)), 'deleted_local': deleted_local, 'pulled': pulled, 'kept_local': kept_local, 'conflicts': []}

def _config_path() -> Path:
    explicit = os.environ.get('CINTERFORGE_CONFIG_JSON', '').strip()
    if explicit:
        return Path(explicit).expanduser()
    primary = Path.home() / 'dev' / 'config.json'
    fallback = Path.home() / 'cfdev' / 'config.json'
    return primary if primary.is_file() or not fallback.is_file() else fallback

def _cloudflare_provider() -> str:
    return os.environ.get('CINTERFORGE_ASSETS_CLOUDFLARE_PROVIDER', 'cloudflare_r2_usdz').strip() or 'cloudflare_r2_usdz'

def _asset_provider_config() -> dict:
    config = _config_path()
    if not config.is_file():
        return {}
    try:
        data = json.loads(config.read_text(encoding='utf-8'))
    except Exception:
        return {}
    provider = _cloudflare_provider()
    providers = data.get('providers') if isinstance(data, dict) else None
    cfg = providers.get(provider) if isinstance(providers, dict) else data.get(provider) if isinstance(data, dict) else None
    if not isinstance(cfg, dict):
        return {}
    merged = dict(cfg)
    for key in ('env', 'environment'):
        if isinstance(cfg.get(key), dict):
            merged.update(cfg[key])
    return merged

def _cloudflare_prefix() -> str:
    explicit = os.environ.get('CINTERFORGE_ASSETS_CLOUDFLARE_PREFIX', '').strip().strip('/')
    provider_prefix = str(_asset_provider_config().get('object_prefix') or '').strip().strip('/')
    if explicit:
        if explicit == 'assets' and provider_prefix and (provider_prefix != 'assets'):
            if provider_prefix.endswith('/assets'):
                return provider_prefix
            return provider_prefix + '/assets'
        return explicit
    if not provider_prefix:
        return 'assets'
    if provider_prefix == 'assets' or provider_prefix.endswith('/assets'):
        return provider_prefix
    return provider_prefix + '/assets'

def public_asset_base_url() -> str:
    """Return the public URL for the exact remote prefix used by asset sync."""
    explicit = os.environ.get('CINTERFORGE_IF_ASSETS_PUBLIC_BASE_URL', '').strip().rstrip('/')
    if explicit:
        return explicit
    cfg = _asset_provider_config()
    for key in ('public_base_url', 'publicBaseUrl', 'CINTERFORGE_CLOUDFLARE_R2_USDZ_PUBLIC_BASE_URL', 'CINTERFORGE_CLOUDFLARE_R2_DEV_PUBLIC_BASE_URL', 'CINTERFORGE_CLOUDFLARE_R2_JSON_PUBLIC_BASE_URL', 'CINTERFORGE_CLOUDFLARE_R2_PUBLIC_BASE_URL'):
        base = str(cfg.get(key) or '').strip().rstrip('/')
        if base:
            prefix = _cloudflare_prefix()
            return base + ('/' + prefix if prefix else '')
    return ''

def request_asset_url(relative_path: str) -> str:
    """Resolve one bucket-independent managed asset path to its public Cloudflare URL."""
    from urllib.parse import quote
    relative = str(relative_path or '').strip().replace('\\', '/')
    for prefix in ('<builds>/Assets/', 'Builds/Assets/', '/Assets/', 'Assets/'):
        if relative.startswith(prefix):
            relative = relative[len(prefix):]
            break
    relative = relative.lstrip('/')
    if not relative or '..' in Path(relative).parts or re.match('^(?:https?:|file:|blob:|data:)', relative, re.I):
        raise ValueError(f'managed asset request must be a relative path: {relative_path}')
    base = public_asset_base_url().rstrip('/')
    if not base:
        raise RuntimeError('CinterForge public Assets base URL is unavailable')
    return base + '/' + '/'.join((quote(part, safe='') for part in relative.split('/')))

def _cloudflare_script() -> Path:
    return project_root() / 'CinterForgeServer' / 'cinter' / 'build_cinterforge' / 'publishing' / 'cloudflare_r2_sync.py'

def _run_cloudflare(mode: str, local: Path, *, allow_upload: bool=False, no_delete: bool=False, bundle_index: str='', prefix: str | None=None, progress: Callable[..., None] | None=None, exclude_relative: tuple[str, ...]=(), backup_replaced: Path | None=None) -> str:
    script = _cloudflare_script()
    config = _config_path()
    if not script.is_file():
        raise FileNotFoundError(f'Cloudflare sync helper is missing: {script}')
    if not config.is_file():
        raise FileNotFoundError(f'Cloudflare config is missing: {config}')
    purge_transport = purge_cfg = None
    if mode == 'upload-sync' and allow_upload:
        from runtime.assets.CinterForgeAssetCloudflare import _transport
        purge_transport = _transport()
        purge_cfg = purge_transport._load_config(config, _cloudflare_provider())
        purge_transport._cache_purge_settings(purge_cfg)
    remote_prefix = str(prefix or _cloudflare_prefix()).strip('/')
    args = [sys.executable, str(script), mode, '--config', str(config), '--provider', _cloudflare_provider(), '--prefix', remote_prefix, '--local', str(local), '--extension', '']
    if allow_upload:
        args.append('--allow-upload')
    if no_delete:
        args.append('--no-delete')
    if bundle_index:
        args.extend(['--bundle-index', bundle_index])
    if backup_replaced is not None:
        args.extend(['--backup-replaced', str(Path(backup_replaced).resolve())])
    for relative_path in exclude_relative:
        args.extend(['--exclude-relative', relative_path])
    env = os.environ.copy()
    if allow_upload:
        env['CINTERFORGE_CLOUDFLARE_UPLOADS_ALLOWED'] = '1'
        env['CINTERFORGE_SETUP_DISABLE_UPLOADS'] = '0'
    log(f'cloudflare-command mode={mode} provider={_cloudflare_provider()} prefix={remote_prefix} local={local} allow_upload={allow_upload} no_delete={no_delete}')
    lines = []
    if progress is None:
        completed = subprocess.run(args, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900, check=False)
        output = completed.stdout or ''
        rc = completed.returncode
        lines = output.splitlines()
        for line in lines:
            log(f'cloudflare {line}')
    else:
        proc = subprocess.Popen(args, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip('\r\n')
            lines.append(line)
            log(f'cloudflare {line}')
            byte_match = re.search('upload-progress bytes=(\\d+) total_bytes=(\\d+)', line)
            match = re.search('\\((\\d+)/(\\d+)\\)', line)
            cur = int(match.group(1)) if match else 0
            total = int(match.group(2)) if match else 0
            if mode == 'download-sync':
                pct = 5 + 20 * cur // max(1, total) if total else 5
                msg = f'Downloading official assets… {cur}/{total}' if total else 'Downloading official assets…'
                phase = 'download'
            elif byte_match:
                done_bytes = int(byte_match.group(1))
                total_bytes = max(1, int(byte_match.group(2)))
                pct = 45 + 54 * done_bytes // total_bytes
                done_mb = done_bytes / (1024 * 1024)
                total_mb = total_bytes / (1024 * 1024)
                msg = f'Uploading assets… {done_mb:.1f}/{total_mb:.1f} MB'
                phase = 'upload'
                cur = done_bytes
                total = total_bytes
            else:
                pct = 45 + 54 * cur // max(1, total) if total else 45
                verb = 'Uploading' if 'upload' in line else 'Publishing'
                msg = f'{verb} assets… {cur}/{total}' if total else 'Publishing assets…'
                phase = 'upload'
            progress(state='running', phase=phase, current=cur, total=total, percent=pct, message=msg)
        rc = proc.wait()
        output = '\n'.join(lines)
    if rc != 0:
        lowered = output.casefold()
        if mode == 'download-sync' and ('nosuchbucket' in lowered or 'no such bucket' in lowered or 'http 404' in lowered):
            log(f'cloudflare-bucket-missing provider={_cloudflare_provider()} prefix={_cloudflare_prefix()} action=skip')
            return '__CINTERFORGE_BUCKET_MISSING__'
        raise RuntimeError(f'Cloudflare {mode} failed rc={rc}: {output[-4000:]}')
    if purge_transport is not None:
        purge_transport.purge_bucket_cache(purge_cfg)
    return output

@contextmanager
def asset_progress_output(progress: Callable[..., None] | None):
    """Mirror asset-management log lines to one request-local progress sink."""
    token = _ASSET_PROGRESS_CONTEXT.set(progress)
    try:
        yield
    finally:
        _ASSET_PROGRESS_CONTEXT.reset(token)

@contextmanager
def _temporary_assets_root(root: Path):
    """Scope legacy asset-generation helpers to an isolated temp workspace.

    This is request/thread-local via ContextVar; normal runtime Assets never point at
    Builds/Assets.
    """
    token = _ASSETS_ROOT_CONTEXT.set(Path(root).resolve())
    try:
        yield
    finally:
        _ASSETS_ROOT_CONTEXT.reset(token)

def _cloudflare_download_to(relative_path: str, destination: Path) -> None:
    from cf.ifserver.assets.collaboration_sync import _r2_config, _r2_key
    rel = str(relative_path or '').replace('\\', '/').lstrip('/')
    if not rel or '..' in Path(rel).parts or Path(rel).is_absolute():
        raise ValueError(f'invalid Cloudflare asset path: {relative_path}')
    r2, cfg = _r2_config()
    key = _r2_key(_cloudflare_prefix(), rel)
    _status, _headers, body = r2._request(cfg, 'GET', key=key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)

def cloudflare_blockify_load(relative_path: str) -> dict[str, Any]:
    """Load the Blockify payload for one Cloudflare-owned 3D asset by portable identity."""
    rel = str(relative_path or '').replace('\\', '/').lstrip('/')
    if not rel or '..' in PurePosixPath(rel).parts:
        raise ValueError(f'invalid Cloudflare asset path: {relative_path}')
    folder_rel = PurePosixPath(rel).parent.as_posix()
    filename = PurePosixPath(rel).name
    with tempfile.TemporaryDirectory(prefix='cinter-cloudflare-blockify-load-') as td:
        root = Path(td)
        folder = root / folder_rel
        folder.mkdir(parents=True, exist_ok=True)
        manifest_path = folder / 'manifest.json'
        _cloudflare_download_to(f'{folder_rel}/manifest.json' if folder_rel not in ('', '.') else 'manifest.json', manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        version = next((v for v in manifest.get('versions') or [] if isinstance(v, dict) and str(v.get('file') or '') == filename), None)
        if not isinstance(version, dict):
            raise FileNotFoundError(f'Cloudflare asset version is not declared for {rel}')
        meta = version.get('blockify') if isinstance(version.get('blockify'), dict) else {}
        block_file = str(meta.get('file') or '').strip()
        if not block_file:
            artifacts = version.get('artifacts') if isinstance(version.get('artifacts'), dict) else {}
            artifact = artifacts.get('blockify') if isinstance(artifacts.get('blockify'), dict) else {}
            block_file = str(artifact.get('file') or '').strip()
        if not block_file:
            raise FileNotFoundError(f'Blockify artifact is not declared for {rel}')
        block_path = folder / block_file
        _cloudflare_download_to(f'{folder_rel}/{block_file}' if folder_rel not in ('', '.') else block_file, block_path)
        return json.loads(block_path.read_text(encoding='utf-8'))

def cloudflare_blockify_save(relative_path: str, *, width: int, height: int, depth: int, resolution: int, voxels: list[dict[str, Any]], product_name: str='') -> dict[str, Any]:
    """Persist authored voxels for a Cloudflare-owned asset without touching the local Assets tree."""
    rel = str(relative_path or '').replace('\\', '/').lstrip('/')
    if not rel or '..' in PurePosixPath(rel).parts:
        raise ValueError(f'invalid Cloudflare asset path: {relative_path}')
    folder_rel = PurePosixPath(rel).parent.as_posix()
    filename = PurePosixPath(rel).name
    with tempfile.TemporaryDirectory(prefix='cinter-cloudflare-blockify-save-') as td:
        root = Path(td)
        folder = root / folder_rel
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / filename
        manifest_path = folder / 'manifest.json'
        _cloudflare_download_to(rel, source)
        manifest_rel = f'{folder_rel}/manifest.json' if folder_rel not in ('', '.') else 'manifest.json'
        _cloudflare_download_to(manifest_rel, manifest_path)
        with _temporary_assets_root(root):
            meta = save_blockify(folder_rel, filename, width=width, height=height, depth=depth, resolution=resolution, voxels=voxels, product_name=product_name, source_up_normalized=True, regenerate_base=True)
        published = []
        for name in {'manifest.json', str(meta.get('file') or ''), str(meta.get('mcaddon_file') or '')}:
            candidate = folder / name if name else None
            if candidate and candidate.is_file():
                target = f'{folder_rel}/{name}' if folder_rel not in ('', '.') else name
                published.append(publish_cloudflare_file(candidate, target))
        return {'blockify': meta, 'cloudflare_paths': published}

def cloudflare_blockbench_session(relative_path: str, *, width: int, height: int, depth: int, voxels: list[dict[str, Any]], product_name: str='') -> dict[str, Any]:
    """Build a Minecraft/Blockbench edit session from a Cloudflare asset in temp storage only."""
    rel = str(relative_path or '').replace('\\', '/').lstrip('/')
    folder_rel = PurePosixPath(rel).parent.as_posix()
    filename = PurePosixPath(rel).name
    with tempfile.TemporaryDirectory(prefix='cinter-cloudflare-edit-') as td:
        root = Path(td)
        folder = root / folder_rel
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / filename
        _cloudflare_download_to(rel, source)
        manifest = {'schema': SCHEMA, 'asset_reference_version': ASSET_REFERENCE_VERSION, 'name': folder.name, 'version_count': 1, 'versions': [{'file': filename, 'provider': 'Cloudflare', 'artifacts': {'source': {'file': filename}}}], 'last_viewed': '', 'default_version': filename, 'default_animation': {}}
        (folder / 'manifest.json').write_text(json.dumps(manifest, separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
        with _temporary_assets_root(root):
            meta = save_blockify(folder_rel, filename, width=width, height=height, depth=depth, resolution=16, voxels=voxels, product_name=product_name, source_up_normalized=True, regenerate_base=True)
            session = _blockbench_resource_session(folder_rel, filename, product_name=product_name, regenerate=False)
        for name in (str(meta.get('file') or ''), str(meta.get('mcaddon_file') or '')):
            if name and (folder / name).is_file():
                publish_cloudflare_file(folder / name, f'{folder_rel}/{name}' if folder_rel not in ('', '.') else name)
        return {'session': session, 'blockify': meta}

def cloudflare_blockbench_save(relative_path: str, *, width: int, height: int, depth: int, voxels: list[dict[str, Any]], product_name: str, geometry: dict[str, Any], animations: dict[str, Any], texture_data_url: str) -> dict[str, Any]:
    """Round-trip Blockbench edits using only temp workspace + Cloudflare persistence."""
    rel = str(relative_path or '').replace('\\', '/').lstrip('/')
    folder_rel = PurePosixPath(rel).parent.as_posix()
    filename = PurePosixPath(rel).name
    with tempfile.TemporaryDirectory(prefix='cinter-cloudflare-edit-save-') as td:
        root = Path(td)
        folder = root / folder_rel
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / filename
        _cloudflare_download_to(rel, source)
        manifest = {'schema': SCHEMA, 'asset_reference_version': ASSET_REFERENCE_VERSION, 'name': folder.name, 'version_count': 1, 'versions': [{'file': filename, 'provider': 'Cloudflare', 'artifacts': {'source': {'file': filename}}}], 'last_viewed': '', 'default_version': filename, 'default_animation': {}}
        (folder / 'manifest.json').write_text(json.dumps(manifest, separators=(',', ':'), ensure_ascii=False), encoding='utf-8')
        with _temporary_assets_root(root):
            meta = save_blockify(folder_rel, filename, width=width, height=height, depth=depth, resolution=16, voxels=voxels, product_name=product_name, source_up_normalized=True, regenerate_base=True)
            result = save_blockbench_override(folder_rel, filename, geometry=geometry, animations=animations, texture_data_url=texture_data_url, product_name=product_name)
        manifest = _load_manifest(folder)
        version = (manifest.get('versions') or [{}])[0]
        meta = version.get('blockify') or meta
        names = {str(meta.get('file') or ''), str(meta.get('mcaddon_file') or ''), str(meta.get('blockbench_file') or '')}
        for name in names:
            if name and (folder / name).is_file():
                publish_cloudflare_file(folder / name, f'{folder_rel}/{name}' if folder_rel not in ('', '.') else name)
        return result

def publish_cloudflare_asset_bundle(files: dict[str, Path], catalog_rows: list[dict[str, Any]]) -> None:
    """Publish normal Asset-screen outputs directly from temp/source files to Cloudflare.

    The central Builds/.../Assets tree is never read or written. The authenticated
    catalog is merged and uploaded in the same no-delete commit.
    """
    current = load_cloudflare_visual_catalog()
    rows = {str(r.get('path') or ''): dict(r) for r in current.get('files') or [] if isinstance(r, dict) and r.get('path')}
    for row in catalog_rows:
        rel = str(row.get('path') or '').replace('\\', '/').lstrip('/')
        if not rel or '..' in Path(rel).parts:
            raise ValueError(f'invalid catalog path: {rel}')
        clean = dict(row)
        clean['path'] = rel
        rows[rel] = clean
    payload = {'schema': 'cinterforge.cloudflare-visual-catalog.v1', 'root': 'Assets', 'authority': 'cloudflare', 'files': sorted(rows.values(), key=lambda r: str(r.get('path') or '').casefold())}
    with tempfile.TemporaryDirectory(prefix='cinter-cloudflare-bundle-') as td:
        stage = Path(td)
        for rel, source in files.items():
            rel = str(rel).replace('\\', '/').lstrip('/')
            source = Path(source).resolve()
            if not source.is_file() or source.stat().st_size <= 0:
                raise FileNotFoundError(source)
            if not rel or '..' in Path(rel).parts:
                raise ValueError(f'invalid portable asset path: {rel}')
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (stage / '.cinterforge_catalog.json').write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
        _run_cloudflare('upload-sync', stage, allow_upload=True, no_delete=True)
    log(f'asset-cloudflare-bundle-published files={len(files)} catalog_rows={len(catalog_rows)}')

def publish_cloudflare_file(source: Path, relative_path: str) -> str:
    """Publish a temporary/generated file directly to Cloudflare without touching Assets/."""
    source = Path(source).resolve()
    rel = str(relative_path or '').replace('\\', '/').lstrip('/')
    if not source.is_file() or source.stat().st_size <= 0:
        raise FileNotFoundError(source)
    if not rel or '..' in Path(rel).parts or Path(rel).is_absolute():
        raise ValueError(f'invalid portable asset path: {relative_path}')
    with tempfile.TemporaryDirectory(prefix='cinter-cloudflare-direct-') as td:
        stage = Path(td)
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        _run_cloudflare('upload-sync', stage, allow_upload=True, no_delete=True)
    log(f'asset-cloudflare-direct-published file={rel}')
    return rel

def publish_managed_asset_file(path: Path) -> str:
    """Publish exactly one managed asset file at its canonical Cloudflare path."""
    source = Path(path).resolve()
    root = assets_root().resolve()
    try:
        rel = source.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f'Managed asset file escapes cache root: {source}') from exc
    if not source.is_file() or source.stat().st_size <= 0:
        raise FileNotFoundError(source)
    with tempfile.TemporaryDirectory(prefix='cinter-asset-file-publish-') as td:
        stage = Path(td)
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        _run_cloudflare('upload-sync', stage, allow_upload=True, no_delete=True)
    log(f'asset-cloudflare-published file={rel.as_posix()}')
    return rel.as_posix()

def _publish_managed_asset_folder(folder: Path) -> None:
    """Publish one saved managed folder at its canonical raw Cloudflare path."""
    folder = Path(folder).resolve()
    root = assets_root().resolve()
    try:
        rel = folder.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f'Managed asset folder escapes cache root: {folder}') from exc
    with tempfile.TemporaryDirectory(prefix='cinter-asset-publish-') as td:
        stage = Path(td)
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(folder, target)
        _run_cloudflare('upload-sync', stage, allow_upload=True, no_delete=True)
    log(f'asset-cloudflare-published folder={rel.as_posix()}')

def _linux_simulated_cloudflare_root() -> Path:
    override = os.environ.get('CINTERFORGE_ASSETS_SIMULATED_CLOUDFLARE_ROOT', '').strip()
    return (Path(override).expanduser() if override else project_root().parent / 'Cloudflare' / 'Assets').resolve()

def _using_linux_cloudflare_simulation() -> bool:
    enabled = sys.platform.startswith('linux') and os.environ.get('CINTERFORGE_ASSETS_DISABLE_LINUX_CLOUDFLARE_SIM', '0') != '1'
    log(f'cloudflare-mode platform={sys.platform} linux_simulation={enabled}')
    return enabled

def _replace_simulated_cloudflare_from_local(source: Path, simulated_root: Path) -> dict[str, Any]:
    source = Path(source).resolve()
    simulated_root = Path(simulated_root).resolve()
    if source == simulated_root or source in simulated_root.parents or simulated_root in source.parents:
        raise ValueError(f'Unsafe simulated Cloudflare path relationship: source={source} destination={simulated_root}')
    simulated_root.parent.mkdir(parents=True, exist_ok=True)
    staging = simulated_root.parent / f'.{simulated_root.name}.staging-{os.getpid()}'
    if staging.exists():
        shutil.rmtree(staging)
    copied_files = 0
    try:
        shutil.copytree(source, staging)
        copied_files = sum((1 for p in staging.rglob('*') if p.is_file()))
        if simulated_root.exists():
            shutil.rmtree(simulated_root)
        staging.replace(simulated_root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    log(f'linux-cloudflare-upload-sim source={source} destination={simulated_root} files={copied_files}')
    return {'destination': str(simulated_root), 'files': copied_files}

def _additive_simulated_cloudflare_from_local(source: Path, simulated_root: Path) -> dict[str, Any]:
    """Simulation of Push: add/replace local files without deleting remote-only files."""
    source = Path(source).resolve()
    simulated_root = Path(simulated_root).resolve()
    if source == simulated_root or source in simulated_root.parents or simulated_root in source.parents:
        raise ValueError(f'Unsafe simulated Cloudflare path relationship: source={source} destination={simulated_root}')
    simulated_root.mkdir(parents=True, exist_ok=True)
    added = changed = unchanged = 0
    for local_file in sorted((p for p in source.rglob('*') if p.is_file()), key=lambda p: p.as_posix().casefold()):
        relative = local_file.relative_to(source)
        remote_file = simulated_root / relative
        if remote_file.is_file() and _sha256(remote_file) == _sha256(local_file):
            unchanged += 1
            continue
        if remote_file.is_file():
            changed += 1
        else:
            added += 1
        remote_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_file, remote_file)
    log(f'linux-cloudflare-additive-upload source={source} destination={simulated_root} added={added} changed={changed} unchanged={unchanged}')
    return {'destination': str(simulated_root), 'files': added + changed, 'added': added, 'changed': changed, 'unchanged': unchanged, 'deleted': 0, 'remote_deletion_allowed': False, 'summary': f'Push complete: {added} file(s) added, {changed} file(s) changed in Cloudflare.'}

def download_official(*, destination_root: Path | None=None, progress: Callable[..., None] | None=None, approved_conflicts: set[str] | None=None) -> dict[str, Any]:
    """Setup-side pull: download official bundles and non-destructively merge into local Assets."""
    destination = Path(destination_root).resolve() if destination_root else assets_root().resolve()
    if progress:
        progress(state='running', phase='download', percent=2, message='Checking official assets…')
    if _using_linux_cloudflare_simulation():
        simulated = _linux_simulated_cloudflare_root()
        if not simulated.is_dir():
            log(f'linux-cloudflare-download-sim bucket-missing source={simulated} action=skip')
            return {'ok': True, 'skipped': True, 'reason': 'bucket-missing', 'root': str(destination), 'simulated': True, 'cloudflare_root': str(simulated)}
        with tempfile.TemporaryDirectory(prefix='cinter-official-assets-unpack-') as td:
            incoming = Path(td) / 'Assets'
            incoming.mkdir(parents=True, exist_ok=True)
            extracted = _extract_transport_bundles(simulated, incoming, progress=progress, approved_conflicts=approved_conflicts)
            remote_snapshot = _authoritative_tree_snapshot(incoming)
            merged = {'authoritative': _overlay_authoritative_tree(incoming, destination)}
            if destination == assets_root().resolve():
                _write_sync_state(remote_snapshot)
        return {'ok': True, 'skipped': False, 'merged': merged, 'transport': extracted, 'root': str(destination), 'simulated': True, 'cloudflare_root': str(simulated), 'library': None}
    transport = _official_transport_cache_path()
    output = _run_cloudflare('download-sync', transport, bundle_index='index.json', progress=progress)
    if output == '__CINTERFORGE_BUCKET_MISSING__':
        return {'ok': True, 'skipped': True, 'reason': 'bucket-missing', 'root': str(destination), 'simulated': False}
    index_path = transport / 'index.json'
    transport_revision = _sha256(index_path) if index_path.is_file() else ''
    central_destination = destination == assets_root().resolve()
    if central_destination and transport_revision and destination.is_dir():
        accepted = _load_sync_state()
        if str(accepted.get('transport_revision') or '') == transport_revision:
            quarantined = sorted(set((str(p) for p in accepted.get('quarantined') or [] if str(p))))
            log(f'official-assets-revision-current revision={transport_revision[:12]} quarantined={quarantined} action=skip-extract')
            if progress:
                progress(state='running', phase='current', percent=100, message='Official assets already current.')
            return {'ok': True, 'skipped': False, 'accepted_revision_unchanged': True, 'transport_revision': transport_revision, 'quarantined': quarantined, 'root': str(destination), 'simulated': False, 'library': None}
    with tempfile.TemporaryDirectory(prefix='cinter-official-assets-unpack-') as td:
        incoming = Path(td) / 'Assets'
        incoming.mkdir(parents=True, exist_ok=True)
        extracted = _extract_transport_bundles(transport, incoming, progress=progress, approved_conflicts=approved_conflicts)
        remote_snapshot = _authoritative_tree_snapshot(incoming)
        merged = {'authoritative': _overlay_authoritative_tree(incoming, destination)}
        if central_destination:
            _write_sync_state(remote_snapshot, transport_revision=transport_revision, quarantined=list(extracted.get('quarantined') or []))
    log(f'official-assets-downloaded destination={destination} merged={merged}')
    return {'ok': True, 'skipped': False, 'merged': merged, 'transport': extracted, 'transport_revision': transport_revision, 'root': str(destination), 'simulated': False, 'library': None}

def _asset_pending_root(destination: Path) -> Path:
    """Return the recovery root kept outside Assets so Push can never publish it."""
    return destination.parent / 'Pending' / 'Assets'

def _expire_asset_pending(pending_root: Path, *, now: datetime | None=None) -> int:
    """Remove completed Pull recovery folders after their 24-hour protection window."""
    if not pending_root.is_dir():
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=24)
    removed = 0
    for child in pending_root.iterdir():
        if not child.is_dir():
            continue
        try:
            created = datetime.strptime(child.name, '%Y%m%dT%H%M%S.%fZ').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created <= cutoff:
            shutil.rmtree(child)
            removed += 1
    return removed

def _transfer_counts(output: str, operation: str) -> dict[str, int]:
    """Parse the helper's final transfer summary into stable command-level counts."""
    pattern = 'upload-sync complete .*?uploaded=(\\d+) added=(\\d+) changed=(\\d+) unchanged=(\\d+) deleted=(\\d+)' if operation == 'upload' else 'download-sync complete .*?downloaded=(\\d+) added=(\\d+) changed=(\\d+) unchanged=(\\d+) deleted=(\\d+)'
    match = re.search(pattern, str(output or ''))
    if not match:
        return {'transferred': 0, 'added': 0, 'changed': 0, 'unchanged': 0, 'deleted': 0}
    return {'transferred': int(match.group(1)), 'added': int(match.group(2)), 'changed': int(match.group(3)), 'unchanged': int(match.group(4)), 'deleted': int(match.group(5))}

def refresh_testing_grounds(*, destination_root: Path | None=None, progress: Callable[..., None] | None=None) -> dict[str, Any]:
    """Restore the complete published TestingGrounds subtree from Cloudflare.

    TestingGrounds is required app/build content.  Pull must recover it even when
    the generic Assets transport/index does not enumerate it.  The published
    Cloudflare visual catalog is authoritative for this subtree.
    """
    destination = Path(destination_root).resolve() if destination_root else assets_root().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    master_relative = 'TestingGrounds/testing_grounds.gltf'
    if _using_linux_cloudflare_simulation():
        remote_root = _linux_simulated_cloudflare_root()
        remote_testing_grounds = remote_root / 'TestingGrounds'
        remote_master = remote_root / master_relative
        if not remote_master.is_file() or remote_master.stat().st_size <= 0:
            raise FileNotFoundError('Published Assets do not contain TestingGrounds/testing_grounds.gltf')
        testing_grounds_paths = [source.relative_to(remote_root).as_posix() for source in remote_testing_grounds.rglob('*') if source.is_file()]
        for relative in sorted(testing_grounds_paths, key=str.casefold):
            source = remote_root / relative
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    else:
        catalog = load_cloudflare_visual_catalog()
        rows = catalog.get('files') if isinstance(catalog, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError('Cloudflare Assets catalog is invalid while restoring TestingGrounds')
        testing_grounds_paths: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            relative = str(row.get('path') or '').replace('\\', '/').lstrip('/')
            if relative.startswith('TestingGrounds/') and '..' not in PurePosixPath(relative).parts:
                testing_grounds_paths.add(relative)
            thumbnail = str(row.get('thumbnail_path') or '').replace('\\', '/').lstrip('/')
            if thumbnail.startswith('TestingGrounds/') and '..' not in PurePosixPath(thumbnail).parts:
                testing_grounds_paths.add(thumbnail)
        if master_relative not in testing_grounds_paths:
            raise FileNotFoundError('Cloudflare Assets catalog does not contain TestingGrounds/testing_grounds.gltf')
        total = len(testing_grounds_paths)
        downloaded = 0
        reused = 0
        for index, relative in enumerate(sorted(testing_grounds_paths, key=str.casefold), start=1):
            target = destination / relative
            if target.is_file() and target.stat().st_size > 0:
                reused += 1
                continue
            _cloudflare_download_to(relative, target)
            if not target.is_file() or target.stat().st_size <= 0:
                raise IOError(f'Cloudflare TestingGrounds download produced an empty file: {relative}')
            downloaded += 1
            if progress is not None:
                progress(state='running', output=f'[ifplayer assets command] testing-grounds-pull {index}/{total} {relative}')
    master = destination / master_relative
    if not master.is_file() or master.stat().st_size <= 0:
        raise FileNotFoundError('TestingGrounds restore completed without a usable TestingGrounds/testing_grounds.gltf')
    file_count = sum((1 for path in (destination / 'TestingGrounds').rglob('*') if path.is_file()))
    downloaded = locals().get('downloaded', 0)
    reused = locals().get('reused', file_count)
    log(f'testing-grounds-pull-complete files={file_count} downloaded={downloaded} reused={reused} master={master_relative!r}')
    return {'ok': True, 'files': file_count, 'downloaded': downloaded, 'reused': reused, 'path': master_relative}

def refresh_uci_assets(*, destination_root: Path | None=None, progress: Callable[..., None] | None=None) -> dict[str, Any]:
    """Restore every published UCI asset folder from the remote Assets inventory.

    UCI discovery must not depend on local manifests: a new machine can legitimately
    have zero ``CINTERASST*`` folders.  Enumerate the remote Assets tree, identify
    canonical ``<category>/CINTERASST*/manifest.json`` roots, and restore every file
    belonging to those roots before the local library is enumerated.
    """
    destination = Path(destination_root).resolve() if destination_root else assets_root().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if _using_linux_cloudflare_simulation():
        remote_root = _linux_simulated_cloudflare_root()
        remote_paths = [path.relative_to(remote_root).as_posix() for path in remote_root.rglob('*') if path.is_file()] if remote_root.is_dir() else []
    else:
        remote_paths = _authenticated_cloudflare_asset_keys()
    normalized_paths: set[str] = set()
    uci_roots: set[str] = set()
    for raw_relative in remote_paths:
        relative = str(raw_relative or '').replace('\\', '/').lstrip('/')
        parts = PurePosixPath(relative).parts
        if not relative or '..' in parts:
            continue
        normalized_paths.add(relative)
        if len(parts) == 3 and parts[1].upper().startswith('CINTERASST') and (parts[2].casefold() == 'manifest.json'):
            uci_roots.add('/'.join(parts[:2]))
    if not uci_roots:
        raise FileNotFoundError('Published Assets inventory contains no UCI asset manifests')
    uci_paths = sorted((relative for relative in normalized_paths if any((relative == root or relative.startswith(root + '/') for root in uci_roots))), key=str.casefold)
    downloaded = 0
    reused = 0
    total = len(uci_paths)
    for index, relative in enumerate(uci_paths, start=1):
        target = destination / Path(relative)
        if target.is_file() and target.stat().st_size > 0:
            reused += 1
            continue
        if _using_linux_cloudflare_simulation():
            source = _linux_simulated_cloudflare_root() / Path(relative)
            if not source.is_file():
                raise FileNotFoundError(f'Published UCI asset file disappeared during Pull: {relative}')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            _cloudflare_download_to(relative, target)
        if not target.is_file() or target.stat().st_size <= 0:
            raise IOError(f'Cloudflare UCI asset download produced an empty file: {relative}')
        downloaded += 1
        if progress is not None:
            progress(state='running', output=f'[ifplayer assets command] uci-pull {index}/{total} {relative}')
    missing_manifests = [root for root in sorted(uci_roots, key=str.casefold) if not (destination / root / 'manifest.json').is_file()]
    if missing_manifests:
        raise FileNotFoundError('UCI Pull completed without required manifest(s): ' + ', '.join(missing_manifests))
    log(f'uci-assets-pull-complete assets={len(uci_roots)} downloaded={downloaded} reused={reused}')
    return {'ok': True, 'assets': len(uci_roots), 'files': downloaded, 'reused': reused}

def pull_official(*, destination_root: Path | None=None, progress: Callable[..., None] | None=None) -> dict[str, Any]:
    """Pull Cloudflare Assets additively, protecting every replaced local file for 24 hours.

    Pull never deletes either side. New remote files are added locally; changed remote
    files replace their local counterpart only after the exact old bytes are copied to
    ``Builds/InteractiveFiction/Pending/Assets/<UTC timestamp>/``. Local-only files
    remain untouched.
    """
    destination = Path(destination_root).resolve() if destination_root else assets_root().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    pending_root = _asset_pending_root(destination)
    expired = _expire_asset_pending(pending_root)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    pending_run = pending_root / timestamp
    if _using_linux_cloudflare_simulation():
        remote = _linux_simulated_cloudflare_root()
        added = changed = unchanged = 0
        if remote.is_dir():
            for source in sorted((p for p in remote.rglob('*') if p.is_file()), key=lambda p: p.as_posix().casefold()):
                relative = source.relative_to(remote)
                target = destination / relative
                if target.is_file() and _sha256(target) == _sha256(source):
                    unchanged += 1
                    continue
                if target.is_file():
                    backup = pending_run / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                    changed += 1
                else:
                    added += 1
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        output = ''
    else:
        output = _run_cloudflare('download-sync', destination, no_delete=True, progress=progress, backup_replaced=pending_run)
        counts = _transfer_counts(output, 'download')
        added = counts['added']
        changed = counts['changed']
        unchanged = counts['unchanged']
        if counts['deleted']:
            raise RuntimeError('Pull violated no-delete contract')
    uci_assets = refresh_uci_assets(destination_root=destination, progress=progress)
    testing_grounds = refresh_testing_grounds(destination_root=destination, progress=progress)
    if pending_run.is_dir() and (not any(pending_run.rglob('*'))):
        pending_run.rmdir()
    snapshot = _authoritative_tree_snapshot(destination)
    _write_sync_state(snapshot)
    log(f"official-assets-pull-complete added={added} changed={changed} unchanged={unchanged} deleted=0 pending={(pending_run if changed else '')!s} expired_pending={expired}")
    return {'ok': True, 'skipped': False, 'mode': 'pull', 'root': str(destination), 'added': added, 'changed': changed, 'unchanged': unchanged, 'deleted': 0, 'pending': str(pending_run) if changed else '', 'expired_pending': expired, 'uci_assets': uci_assets, 'testing_grounds': testing_grounds, 'summary': f"Pull complete: {added} file(s) added, {changed} file(s) changed. UCI library restored ({int(uci_assets.get('assets') or 0)} asset(s)); TestingGrounds restored ({int(testing_grounds.get('files') or 0)} file(s))."}

def sync_official(progress: Callable[..., None] | None=None, *, reconcile_remote: bool=True) -> dict[str, Any]:
    """Publish Builds/Assets through one transport.

    Interactive/collaborative callers may request the historical three-way reconcile.
    Explicit maintainer publish uses ``reconcile_remote=False``: the current local
    Builds/Assets tree is authoritative and is uploaded as one complete snapshot.
    """
    log(f'official-sync-start reconcile_remote={reconcile_remote}')
    root = assets_root().resolve()
    if progress:
        progress(state='running', phase='download' if reconcile_remote else 'bundle', percent=2, message='Checking official assets…' if reconcile_remote else 'Packing authoritative asset library…')
    reconcile = {'authoritative_publish': not reconcile_remote, 'conflicts': []}
    extracted = {'bundles': 0, 'skipped': not reconcile_remote}
    if _using_linux_cloudflare_simulation():
        remote = _linux_simulated_cloudflare_root()
        with tempfile.TemporaryDirectory(prefix='cinter-official-assets-sync-') as td:
            if reconcile_remote and remote.is_dir():
                incoming = Path(td) / 'Assets'
                incoming.mkdir(parents=True, exist_ok=True)
                extracted = _extract_transport_bundles(remote, incoming, progress=progress)
                reconcile = _reconcile_official(incoming, root)
            elif reconcile_remote:
                reconcile = {'initial': True, 'bootstrap': True, 'resolved_files': len(_tree_snapshot(root)), 'conflicts': []}
            transport = Path(td) / 'Transport'
            if progress:
                progress(state='running', phase='bundle', percent=35, message='Packing asset library…')
            index = _build_transport_bundles(root, transport, progress=progress)
            upload = _replace_simulated_cloudflare_from_local(transport, remote)
    else:
        with tempfile.TemporaryDirectory(prefix='cinter-official-assets-sync-') as td:
            if reconcile_remote:
                transport_in = Path(td) / 'Remote'
                transport_in.mkdir(parents=True, exist_ok=True)
                output = _run_cloudflare('download-sync', transport_in, bundle_index='index.json', progress=progress)
                if output == '__CINTERFORGE_BUCKET_MISSING__':
                    raise RuntimeError('Official Assets Cloudflare bucket does not exist; create/configure the bucket before first publish')
                incoming = Path(td) / 'Assets'
                incoming.mkdir(parents=True, exist_ok=True)
                extracted = _extract_transport_bundles(transport_in, incoming, progress=progress)
                reconcile = _reconcile_official(incoming, root)
            transport_out = Path(td) / 'Publish'
            if progress:
                progress(state='running', phase='bundle', percent=35, message='Packing asset library…')
            index = _build_transport_bundles(root, transport_out, progress=progress)
            _run_cloudflare('upload-sync', transport_out, allow_upload=True, progress=progress)
            upload = {'destination': f'cloudflare:{_cloudflare_provider()}/{_cloudflare_prefix()}', 'files': len(index.get('bundles') or []) + 1}
    snapshot = _authoritative_tree_snapshot(root)
    _write_sync_state(snapshot)
    if progress:
        progress(state='running', phase='finalize', percent=99, message='Finalizing asset sync…')
    log(f'official-sync-complete reconcile={reconcile} upload={upload}')
    return {'ok': True, 'skipped': False, 'reconcile': reconcile, 'transport': extracted, 'root': 'Builds/Assets', 'library': None, 'simulated': _using_linux_cloudflare_simulation(), 'upload': upload}

def _cloudflare_visual_catalog(root: Path) -> dict[str, Any]:
    from cf.ifserver.assets.asset_cloudflare_catalog import build_visual_catalog
    return build_visual_catalog(root, sha256_file=_sha256)

def cloudflare_visual_catalog_url() -> str:
    from cf.ifserver.assets.asset_cloudflare_catalog import visual_catalog_url
    return visual_catalog_url(public_asset_base_url())

def _authenticated_cloudflare_asset_keys() -> list[str]:
    from cf.ifserver.assets.asset_cloudflare_catalog import authenticated_asset_keys
    return authenticated_asset_keys(prefix=_cloudflare_prefix(), log_message=log)

def _visual_catalog_from_cloudflare_keys(keys: list[str]) -> dict[str, Any]:
    from cf.ifserver.assets.asset_cloudflare_catalog import visual_catalog_from_keys
    return visual_catalog_from_keys(keys)

def load_cloudflare_visual_catalog() -> dict[str, Any]:
    from cf.ifserver.assets.asset_cloudflare_catalog import load_visual_catalog
    return load_visual_catalog(prefix=_cloudflare_prefix(), list_asset_keys=_authenticated_cloudflare_asset_keys, catalog_from_keys=_visual_catalog_from_cloudflare_keys)

def sync_cloudflare_visual_assets(progress: Callable[..., None] | None=None) -> dict[str, Any]:
    """Non-destructively receive the expanded Cloudflare Assets catalog.

    This is the runtime/editor counterpart to Build publication.  It deliberately
    avoids ``cloudflare_r2_sync download-sync`` because that command must enumerate
    the entire R2 prefix before it can report progress.  The Build-published
    ``.cinterforge_catalog.json`` is the commit inventory for visual/app assets, so
    launch/F4/F5/Save can check it with one authenticated R2 GET and fetch only
    files that are missing or are known-safe remote updates.

    Existing local authoring work is never overwritten without a common base.  A
    Build-generated catalog carries SHA-256 values; older catalogs fall back to
    size checks and preserve ambiguous local files.
    """
    root = assets_root().resolve()
    root.mkdir(parents=True, exist_ok=True)
    catalog = load_cloudflare_visual_catalog()
    rows = catalog.get('files') if isinstance(catalog, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError('Cloudflare visual asset catalog is invalid')
    base = {str(k): str(v) for k, v in (_load_sync_state().get('files') or {}).items()}
    downloaded = unchanged = preserved = conflicts = 0
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        relative = str(row.get('path') or '').replace('\\', '/').lstrip('/')
        if not relative or '..' in PurePosixPath(relative).parts:
            continue
        destination = root / Path(relative)
        remote_size = int(row.get('size') or 0)
        remote_sha = str(row.get('sha256') or '').strip().casefold()
        local_sha = _sha256(destination) if destination.is_file() else ''
        base_sha = base.get(relative, '').casefold()
        should_download = not destination.is_file()
        if destination.is_file() and remote_sha:
            if local_sha == remote_sha:
                unchanged += 1
                continue
            if base_sha and local_sha == base_sha:
                should_download = True
            elif base_sha and remote_sha == base_sha:
                preserved += 1
                continue
            else:
                conflicts += 1
                log(f'cloudflare-assets-sync-preserve-local path={relative!r} reason=no-safe-common-base')
                continue
        elif destination.is_file():
            if destination.stat().st_size == remote_size:
                unchanged += 1
            else:
                preserved += 1
                log(f'cloudflare-assets-sync-preserve-local path={relative!r} reason=legacy-catalog-size-mismatch')
            continue
        if should_download:
            _cloudflare_download_to(relative, destination)
            if not destination.is_file() or (remote_size > 0 and destination.stat().st_size != remote_size):
                raise IOError(f'Cloudflare asset download verification failed: {relative}')
            if remote_sha and _sha256(destination).casefold() != remote_sha:
                raise IOError(f'Cloudflare asset SHA-256 verification failed: {relative}')
            downloaded += 1
        if progress:
            percent = 5 + 90 * index // max(1, total)
            progress(state='running', phase='download', current=index, total=total, percent=percent, message=f'Checking Cloudflare Assets… {index}/{total}')
    log(f'cloudflare-assets-sync-complete catalog_files={total} downloaded={downloaded} unchanged={unchanged} preserved_local={preserved} conflicts={conflicts}')
    return {'ok': True, 'root': 'Builds/Assets', 'catalog_files': total, 'downloaded': downloaded, 'unchanged': unchanged, 'preserved_local': preserved, 'conflicts': conflicts}

def publish_official(progress: Callable[..., None] | None=None) -> dict[str, Any]:
    """Publish the authored Assets tree directly at the canonical Cloudflare ``assets/`` prefix.

    Build publication is deliberately *not* the portable bundle transport used by
    ``sync_official``.  Runtime asset URLs (including TestingGrounds external GLBs)
    address files directly beneath the public Assets prefix, so Build must mirror
    the raw hierarchy there.  ``cloudflare_r2_sync upload-sync`` already performs
    the incremental hash comparison; unchanged files are not uploaded.
    """
    root = assets_root().resolve()
    log(f'official-publish-start root={root} prefix={_cloudflare_prefix()}')
    if progress:
        progress(state='running', phase='upload', percent=2, message='Publishing authoritative Assets tree…')
    catalog = _cloudflare_visual_catalog(root)
    log(f"cloudflare-visual-catalog-generated files={len(catalog['files'])}")
    if _using_linux_cloudflare_simulation():
        upload = _additive_simulated_cloudflare_from_local(root, _linux_simulated_cloudflare_root())
        catalog_target = _linux_simulated_cloudflare_root() / '.cinterforge_catalog.json'
        catalog_target.parent.mkdir(parents=True, exist_ok=True)
        catalog_target.write_text(json.dumps(catalog, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        destination = str(_linux_simulated_cloudflare_root())
    else:
        upload_outputs = [_run_cloudflare('upload-sync', root, allow_upload=True, no_delete=True, progress=progress, exclude_relative=('.cinterforge_catalog.json',))]
        with tempfile.TemporaryDirectory(prefix='cinter-cloudflare-catalog-') as td:
            catalog_dir = Path(td)
            (catalog_dir / '.cinterforge_catalog.json').write_text(json.dumps(catalog, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            upload_outputs.append(_run_cloudflare('upload-sync', catalog_dir, allow_upload=True, no_delete=True, prefix=_cloudflare_prefix(), progress=progress))
        push_counts = [_transfer_counts(output, 'upload') for output in upload_outputs]
        added = sum((row['added'] for row in push_counts))
        changed = sum((row['changed'] for row in push_counts))
        unchanged = sum((row['unchanged'] for row in push_counts))
        deleted = sum((row['deleted'] for row in push_counts))
        if deleted:
            raise RuntimeError('Push violated no-delete contract')
        snapshot = _tree_snapshot(root)
        upload = {'destination': f'cloudflare:{_cloudflare_provider()}/{_cloudflare_prefix()}', 'files': len(snapshot), 'added': added, 'changed': changed, 'unchanged': unchanged, 'deleted': 0, 'remote_deletion_allowed': False, 'summary': f'Push complete: {added} file(s) added, {changed} file(s) changed in Cloudflare.'}
        destination = upload['destination']
    snapshot = _authoritative_tree_snapshot(root)
    _write_sync_state(snapshot)
    if progress:
        progress(state='running', phase='finalize', percent=100, message='Cloudflare Assets publish complete.')
    log(f"official-publish-complete destination={destination} files={len(snapshot)} added={int(upload.get('added') or 0)} changed={int(upload.get('changed') or 0)} deleted=0")
    return {'ok': True, 'skipped': False, 'root': 'Builds/Assets', 'simulated': _using_linux_cloudflare_simulation(), 'upload': upload}
