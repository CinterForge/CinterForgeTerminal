"""Explicit split-channel Lagomorph publication and restoration.

Only the three staged HFS media images go to the public CDN. All original
Builds/Lagomorph bytes, including the staging manifest metadata, stay on the
authenticated internal (cdn-assets-dev) R2 bucket. Never run from Setup/Build.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import platform
import binascii
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from host import HOST_RELEASE
from host.publishing import cloudflare_r2_sync as r2

SNAPSHOT_SCHEMA = 'cinterforge.lagomorph.private-snapshot.v3'
PREVIOUS_SNAPSHOT_SCHEMA = 'cinterforge.lagomorph.private-snapshot.v2'
LEGACY_SNAPSHOT_SCHEMA = 'cinterforge.lagomorph.private-snapshot.v1'
INTERNAL_PREFIX = 'CinterForgeTerminal/Lagomorph'
SOURCE_PREFIX = INTERNAL_PREFIX + '/Builds'  # legacy object-per-file source
SOURCE_ARCHIVE_KEY = INTERNAL_PREFIX + '/Builds-Lagomorph.zip'
INDEX_KEY = INTERNAL_PREFIX + '/Builds-manifest.json'
STAGE_KEY = INTERNAL_PREFIX + '/Staged-manifest.json'
PUBLIC_ROOT = 'CinterForgeTerminal/Distribution'


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as inp:
        for part in iter(lambda: inp.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def _regular_tree(folder: Path) -> tuple[dict[str, Path], list[str]]:
    if folder.is_symlink() or not folder.is_dir():
        raise FileNotFoundError('Missing regular Builds/Lagomorph source directory: ' + str(folder))
    files: dict[str, Path] = {}
    directories: list[str] = []
    for p in sorted(folder.rglob('*')):
        if p.is_symlink():
            raise RuntimeError('Private Lagomorph source contains a symlink; refusing to follow: ' + str(p))
        rel = p.relative_to(folder).as_posix()
        _safe_rel(rel)
        if p.is_dir():
            directories.append(rel)
        elif p.is_file():
            files[rel] = p
        else:
            raise RuntimeError('Unsupported Lagomorph source file type: ' + str(p))
    if not files:
        raise RuntimeError('Refusing to publish empty Builds/Lagomorph source')
    return files, directories


def _safe_rel(value: str) -> str:
    if (not isinstance(value, str) or not value or value.startswith('/') or
            '\\' in value or '\x00' in value or
            any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Unsafe Lagomorph remote relative path: ' + repr(value))
    return value


def _guard_parent(root: Path, target: Path) -> None:
    root = root.resolve()
    if not target.resolve(strict=False).is_relative_to(root):
        raise RuntimeError('Lagomorph pull escaped Builds: ' + str(target))
    cursor = root
    for name in target.relative_to(root).parts:
        cursor = cursor / name
        if cursor.is_symlink():
            raise RuntimeError('Lagomorph pull refuses symlink target or parent: ' + str(cursor))


def _copy_remote(cfg: dict, key: str, destination: Path, size: int, digest: str) -> None:
    """Download and hash-verify before overwriting any existing source file."""
    if destination.is_file() and not destination.is_symlink() and destination.stat().st_size == size and _sha(destination) == digest:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    # _get_file is allowed to skip an existing file by SIZE, so the scratch
    # destination must be unique. Integrity is verified before os.replace.
    with tempfile.TemporaryDirectory(prefix='lagomorph-pull-', dir=destination.parent) as scratch:
        candidate = Path(scratch) / 'payload'
        r2._get_file(cfg, key, candidate, expected_size=size)
        if candidate.stat().st_size != size or _sha(candidate) != digest:
            raise RuntimeError('Lagomorph SHA-256 mismatch for remote object: ' + key)
        os.replace(candidate, destination)


def _write_index(cfg: dict, key: str, data: dict, scratch_root: Path) -> None:
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lagomorph-index-', dir=scratch_root) as tmp:
        source = Path(tmp) / 'manifest.json'
        source.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        r2._put_file(cfg, key, source)


def _read_mac_metadata(source: Path) -> bytes | None:
    """Encode classic Mac forks explicitly; ZIP does not preserve Darwin xattrs."""
    if platform.system() != 'Darwin':
        return None
    from host.lagomorph_setup import _getxattr
    attrs = {}
    for name in ('com.apple.FinderInfo', 'com.apple.ResourceFork'):
        try:
            value = _getxattr(source, name)
        except OSError:
            continue
        if value:
            attrs[name] = base64.b64encode(value).decode('ascii')
    return json.dumps(attrs, sort_keys=True, separators=(',', ':')).encode() if attrs else None


def _apply_mac_metadata(target: Path, payload: bytes) -> None:
    """Restore the xattrs after data-copy, including when data was already current."""
    if platform.system() != 'Darwin':
        return
    from host.lagomorph_setup import _setxattr
    attrs = json.loads(payload)
    if not isinstance(attrs, dict) or not attrs or set(attrs) - {'com.apple.FinderInfo', 'com.apple.ResourceFork'}:
        raise RuntimeError('Invalid classic Macintosh metadata in Lagomorph source ZIP')
    for key, value in attrs.items():
        if not isinstance(value, str):
            raise RuntimeError('Invalid Macintosh metadata value')
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError('Invalid Macintosh metadata encoding') from exc
        if not decoded or len(decoded) > 16 * 1024 * 1024:
            raise RuntimeError('Invalid Macintosh metadata length')
        _setxattr(target, key, decoded)


def _make_source_zip(destination: Path, files: dict[str, Path], directories: list[str]) -> tuple[dict, dict]:
    """Make one complete, deterministic snapshot, hashing the actual archived bytes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    mac_metadata: dict[str, dict] = {}
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as archive:
        for directory in sorted(directories):
            _safe_rel(directory)
            info = zipfile.ZipInfo('Lagomorph/' + directory + '/', date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFDIR | 0o755) << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, b'')
        for rel, source in sorted(files.items()):
            _safe_rel(rel)
            info = zipfile.ZipInfo('Lagomorph/' + rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | (source.stat().st_mode & 0o777)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            total = 0
            digest = hashlib.sha256()
            with source.open('rb') as inp, archive.open(info, 'w', force_zip64=True) as out:
                for part in iter(lambda: inp.read(1024 * 1024), b''):
                    digest.update(part)
                    out.write(part)
                    total += len(part)
            entries[rel] = {'bytes': total, 'sha256': digest.hexdigest()}
            metadata = _read_mac_metadata(source)
            if metadata:
                archive.writestr('MacMetadata/' + rel + '.json', metadata)
                mac_metadata[rel] = {'bytes': len(metadata), 'sha256': hashlib.sha256(metadata).hexdigest()}
    with zipfile.ZipFile(destination) as archive:
        invalid = archive.testzip()
        if invalid is not None:
            raise RuntimeError('Invalid Lagomorph archive after packaging: ' + invalid)
    return entries, mac_metadata


def _validated_source_tree(archive_path: Path, index: dict, stage: Path) -> None:
    """Reject traversal, symlinks, duplicates, archive bombs and altered bytes.

    No local Builds/Lagomorph file is overwritten until the entire extracted
    candidate has been validated against the authenticated snapshot index.
    """
    files = index['files']
    directories = index['directories']
    mac_metadata = index.get('mac_metadata', {})
    if not isinstance(mac_metadata, dict) or not set(mac_metadata).issubset(files):
        raise RuntimeError('Invalid classic Macintosh metadata index')
    if len(files) > 100000 or len(directories) > 100000:
        raise RuntimeError('Lagomorph snapshot contains too many members')
    max_bytes = 8 * 1024**3
    expected_bytes = sum(entry['bytes'] for entry in files.values())
    if expected_bytes > max_bytes:
        raise RuntimeError('Lagomorph snapshot exceeds 8 GiB decompression limit')
    expected = {'Lagomorph/' + rel for rel in files}
    expected.update('Lagomorph/' + rel + '/' for rel in directories)
    expected.update('MacMetadata/' + rel + '.json' for rel in mac_metadata)
    observed = set()
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            name = member.filename
            if not (name.startswith('Lagomorph/') or name.startswith('MacMetadata/')) or name in observed:
                raise RuntimeError('Unexpected or duplicate Lagomorph ZIP member: ' + name)
            observed.add(name)
            metadata_member = name.startswith('MacMetadata/')
            rel = name[len('MacMetadata/'):] if metadata_member else name[len('Lagomorph/'):].rstrip('/')
            _safe_rel(rel)
            mode = (member.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK or (mode not in (0, stat.S_IFDIR, stat.S_IFREG)):
                raise RuntimeError('Lagomorph archive contains a symlink or special file: ' + name)
            if name not in expected:
                raise RuntimeError('Unexpected Lagomorph ZIP entry: ' + name)
            if metadata_member:
                source_rel = rel[:-5]
                info = mac_metadata[source_rel]
                if member.file_size != info['bytes'] or member.file_size > 32 * 1024 * 1024:
                    raise RuntimeError('Lagomorph metadata size mismatch: ' + name)
                payload = archive.read(member)
                if hashlib.sha256(payload).hexdigest() != info['sha256']:
                    raise RuntimeError('Lagomorph metadata SHA-256 mismatch: ' + name)
                # Validate all encodings before mutating the existing source tree.
                attrs = json.loads(payload)
                if not isinstance(attrs, dict) or not attrs or set(attrs) - {'com.apple.FinderInfo', 'com.apple.ResourceFork'}:
                    raise RuntimeError('Invalid classic Macintosh metadata: ' + name)
                for value in attrs.values():
                    if not isinstance(value, str):
                        raise RuntimeError('Invalid classic Macintosh metadata: ' + name)
                    try:
                        decoded = base64.b64decode(value, validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise RuntimeError('Invalid classic Macintosh metadata encoding: ' + name) from exc
                    if not decoded or len(decoded) > 16 * 1024 * 1024:
                        raise RuntimeError('Invalid classic Macintosh metadata length: ' + name)
                target = stage / 'MacMetadata' / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                continue
            target = stage / rel
            if member.is_dir():
                if name not in {'Lagomorph/' + d + '/' for d in directories} or member.file_size:
                    raise RuntimeError('Invalid Lagomorph ZIP directory: ' + name)
                target.mkdir(parents=True, exist_ok=True)
                continue
            if name not in {'Lagomorph/' + f for f in files}:
                raise RuntimeError('Invalid Lagomorph ZIP file entry: ' + name)
            entry = files[rel]
            if member.file_size != entry['bytes']:
                raise RuntimeError('Lagomorph ZIP file size mismatch: ' + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with archive.open(member) as inp, target.open('xb') as out:
                for part in iter(lambda: inp.read(1024 * 1024), b''):
                    digest.update(part)
                    out.write(part)
                    size += len(part)
                    if size > entry['bytes']:
                        raise RuntimeError('Lagomorph ZIP extraction exceeded declared size')
            if size != entry['bytes'] or digest.hexdigest() != entry['sha256']:
                raise RuntimeError('Lagomorph ZIP file SHA-256 mismatch: ' + name)
    if observed != expected:
        raise RuntimeError('Lagomorph ZIP has missing source files or empty directories')


def _restore_source_zip(builds: Path, cfg: dict, index: dict, entry: dict) -> None:
    archive_meta = index['archive']
    key = SOURCE_ARCHIVE_KEY
    if (archive_meta.get('key') != key or
            int(entry['size']) != archive_meta['bytes']):
        raise RuntimeError('Lagomorph internal ZIP is missing or its size changed')
    scratch = builds / 'Intermediates/CinterForgeTerminal/LagomorphPull'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lagomorph-zip-', dir=scratch) as tmp:
        temp = Path(tmp)
        zipped = temp / 'Lagomorph.zip'
        print(f'[LAGOMORPH PULL] downloading one private ZIP ({archive_meta["bytes"]} bytes)', flush=True)
        r2._get_file(cfg, key, zipped, expected_size=archive_meta['bytes'])
        if _sha(zipped) != archive_meta['sha256']:
            raise RuntimeError('Lagomorph private ZIP SHA-256 mismatch')
        extracted = temp / 'verified'
        _validated_source_tree(zipped, index, extracted)
        # Preserve local-only source edits; update only the tracked snapshot files.
        source = builds / 'Lagomorph'
        _guard_parent(builds, source)
        for rel in index['directories']:
            destination = source / rel
            _guard_parent(builds, destination)
            destination.mkdir(parents=True, exist_ok=True)
        for rel, info in sorted(index['files'].items()):
            target = source / rel
            _guard_parent(builds, target)
            if target.is_file() and target.stat().st_size == info['bytes'] and _sha(target) == info['sha256']:
                if rel in index.get('mac_metadata', {}):
                    _apply_mac_metadata(target, (extracted / 'MacMetadata' / (rel + '.json')).read_bytes())
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            pending = target.with_name(target.name + '.lagomorph-part')
            if pending.exists() or pending.is_symlink():
                raise RuntimeError('Lagomorph destination has an occupied staging path: ' + str(pending))
            try:
                shutil.copy2(extracted / rel, pending)
                os.replace(pending, target)
                if rel in index.get('mac_metadata', {}):
                    _apply_mac_metadata(target, (extracted / 'MacMetadata' / (rel + '.json')).read_bytes())
            finally:
                pending.unlink(missing_ok=True)
    print('[LAGOMORPH PULL] private ZIP checksum, all files and empty directories VERIFIED', flush=True)


def publish_private_source(builds: Path, *, media: dict[str, Path],
                           release: str = HOST_RELEASE, config: Path | None = None) -> dict:
    """Publish ONE private ZIP and index last; public HFS media stays separate."""
    from host.universal_package import _cloudflare_config, LAGOMORPH_STAGE
    builds = builds.resolve()
    files, directories = _regular_tree(builds / 'Lagomorph')
    stage_manifest = builds / LAGOMORPH_STAGE / 'manifest.json'
    if stage_manifest.is_symlink() or not stage_manifest.is_file():
        raise FileNotFoundError('Missing staged Lagomorph HFS manifest: ' + str(stage_manifest))
    cfg = _cloudflare_config(config, channel='dev')
    if cfg['bucket'] != 'cdn-assets-dev':
        raise RuntimeError('Private Lagomorph content must use internal cdn-assets-dev')
    scratch = builds / 'Intermediates/CinterForgeTerminal/LagomorphPublish'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lagomorph-publish-', dir=scratch) as tmp:
        zipped = Path(tmp) / 'Builds-Lagomorph.zip'
        entries, mac_metadata = _make_source_zip(zipped, files, directories)
        archive_entry = {'key': SOURCE_ARCHIVE_KEY, 'bytes': zipped.stat().st_size,
                         'sha256': _sha(zipped)}
        remote = r2._list_remote(cfg, INTERNAL_PREFIX)
        old = remote.get('Builds-Lagomorph.zip')
        md5 = str(old.get('etag', '')).strip('"').lower() if old else ''
        reusable = (old is not None and int(old.get('size', -1)) == archive_entry['bytes'] and
                    len(md5) == 32 and md5 == r2._md5_file(zipped))
        if not reusable:
            print(f'[LAGOMORPH INTERNAL] uploading single ZIP ({archive_entry["bytes"]} bytes)', flush=True)
            r2._put_file(cfg, SOURCE_ARCHIVE_KEY, zipped,
                         uploaded_before=0, upload_total=archive_entry['bytes'])
        refreshed = r2._list_remote(cfg, INTERNAL_PREFIX)
        confirmed = refreshed.get('Builds-Lagomorph.zip')
        if not confirmed or int(confirmed['size']) != archive_entry['bytes']:
            raise RuntimeError('Lagomorph internal ZIP upload not verified in R2')
    stage_entry = {'bytes': stage_manifest.stat().st_size, 'sha256': _sha(stage_manifest)}
    r2._put_file(cfg, STAGE_KEY, stage_manifest)
    media_entries = {name: {'bytes': path.stat().st_size, 'sha256': _sha(path)}
                     for name, path in sorted(media.items())}
    index = {'schema': SNAPSHOT_SCHEMA, 'release': release,
             'bucket': cfg['bucket'], 'prefix': INTERNAL_PREFIX,
             'archive': archive_entry, 'files': entries, 'directories': directories,
             'mac_metadata': mac_metadata,
             'stage_manifest': stage_entry, 'media': media_entries}
    _write_index(cfg, INDEX_KEY, index, scratch)
    print(f'[LAGOMORPH INTERNAL] verified one ZIP ({len(entries)} files) in '
          f'{cfg["bucket"]}/{SOURCE_ARCHIVE_KEY}; snapshot committed', flush=True)
    return index


def pull_lagomorph(builds: Path, *, release: str = HOST_RELEASE,
                   config: Path | None = None, source_only: bool = False) -> dict:
    """Restore Lagomorph source from internal CDN, plus public media when requested.

    Setup uses source_only=True: it needs the original Builds/Lagomorph tree
    before producing HFS images and must not require a public release first.
    The explicit 'pull lagomorph' command also downloads the public HFS images.
    Never delete unrelated local source files. Hash-verify incoming bytes.
    """
    from host.universal_package import _cloudflare_config, LAGOMORPH_STAGE, LAGOMORPH_PUBLIC_NAME, LAGOMORPH_FLOPPIES
    from host.lagomorph_setup import _classic_hfs_signature
    builds = builds.resolve()
    internal = _cloudflare_config(config, channel='dev')
    # The lifecycle's source-only pull must not touch the public CDN at all;
    # the public ISO/floppies are produced by the subsequent Lagomorph stage.
    public = None if source_only else _cloudflare_config(config, channel='public')
    if internal['bucket'] != 'cdn-assets-dev' or (public and public['bucket'] != 'cinterforge'):
        raise RuntimeError('Lagomorph pull requires the configured internal cdn-assets-dev bucket'
                           + (' and public cinterforge bucket' if not source_only else ''))
    index_remote = r2._list_remote(internal, INTERNAL_PREFIX)
    record = index_remote.get('Builds-manifest.json')
    if not record:
        raise FileNotFoundError('Internal Lagomorph snapshot missing: ' + INDEX_KEY)
    scratch = builds / 'Intermediates/CinterForgeTerminal/LagomorphPull'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lagomorph-snapshot-', dir=scratch) as tmp:
        index_file = Path(tmp) / 'snapshot.json'
        r2._get_file(internal, INDEX_KEY, index_file, expected_size=int(record['size']))
        index = json.loads(index_file.read_text(encoding='utf-8'))
    v3 = index.get('schema') == SNAPSHOT_SCHEMA
    v2 = index.get('schema') == PREVIOUS_SNAPSHOT_SCHEMA
    v1 = index.get('schema') == LEGACY_SNAPSHOT_SCHEMA
    if (not (v1 or v2 or v3) or
            (not source_only and index.get('release') != release) or
            index.get('bucket') != internal['bucket'] or
            index.get('prefix') != (SOURCE_PREFIX if v1 else INTERNAL_PREFIX)):
        raise RuntimeError('Lagomorph private snapshot/release mismatch; refusing to pull')
    files = index.get('files')
    media = index.get('media')
    expected_media = {LAGOMORPH_PUBLIC_NAME, *LAGOMORPH_FLOPPIES}
    if (not isinstance(files, dict) or not files or
            (not source_only and (not isinstance(media, dict) or set(media) != expected_media))):
        raise RuntimeError('Incomplete Lagomorph private snapshot')
    directories = index.get('directories', [])
    if not isinstance(directories, list):
        raise RuntimeError('Invalid Lagomorph directories list')
    roots = [builds / 'Lagomorph']
    if not source_only:
        roots.append(builds / LAGOMORPH_STAGE)
    for root in roots:
        _guard_parent(builds, root)
    remote_files = r2._list_remote(internal, SOURCE_PREFIX) if v1 else {}
    public_files = {} if source_only else r2._list_remote(public, f'{PUBLIC_ROOT}/{release}')
    # Preflight everything before touching user files.
    for rel in [*files, *directories]:
        _safe_rel(rel)
        _guard_parent(builds, builds / 'Lagomorph' / rel)
    for rel, entry in files.items():
        if not isinstance(entry, dict) or not isinstance(entry.get('bytes'), int) or entry['bytes'] < 0 or not isinstance(entry.get('sha256'), str):
            raise RuntimeError('Invalid Lagomorph source index: ' + rel)
        if v1:
            item = remote_files.get(rel)
            if item is None or int(item['size']) != entry['bytes']:
                raise FileNotFoundError('Internal Lagomorph object missing: ' + rel)
    if not source_only:
        for name, entry in media.items():
            item = public_files.get(name)
            if item is None or int(item['size']) != entry.get('bytes'):
                raise FileNotFoundError('Public Lagomorph media missing: ' + name)
            _guard_parent(builds, builds / LAGOMORPH_STAGE / name)
        stage_entry = index.get('stage_manifest', {})
        stage_remote = index_remote.get('Staged-manifest.json')
        if (not isinstance(stage_entry, dict) or stage_remote is None or
                int(stage_remote['size']) != stage_entry.get('bytes')):
            raise FileNotFoundError('Internal Lagomorph stage metadata missing')
    if v2 or v3:
        archive_record = index_remote.get('Builds-Lagomorph.zip')
        if archive_record is None:
            raise FileNotFoundError('Lagomorph internal ZIP missing: ' + SOURCE_ARCHIVE_KEY)
        _restore_source_zip(builds, internal, index, archive_record)
    else:
        # Compatible with already-published pre-ZIP private snapshots until
        # the first explicit `publish lagomorph public` commits a ZIP index.
        for rel in directories:
            (builds / 'Lagomorph' / rel).mkdir(parents=True, exist_ok=True)
        for i, (rel, entry) in enumerate(sorted(files.items()), start=1):
            destination = builds / 'Lagomorph' / rel
            print(f'[LAGOMORPH PULL] legacy private {i}/{len(files)} {rel}', flush=True)
            _copy_remote(internal, SOURCE_PREFIX + '/' + rel, destination, entry['bytes'], entry['sha256'])
    if source_only:
        print('[LAGOMORPH PULL] VERIFIED internal Builds/Lagomorph source; public media left untouched',
              flush=True)
        return index
    stage_root = builds / LAGOMORPH_STAGE
    stage_root.mkdir(parents=True, exist_ok=True)
    for name in sorted(media):
        entry = media[name]
        print(f'[LAGOMORPH PULL] public media {name}', flush=True)
        destination = stage_root / name
        _copy_remote(public, f'{PUBLIC_ROOT}/{release}/{name}', destination, entry['bytes'], entry['sha256'])
        _classic_hfs_signature(destination)
    _guard_parent(builds, stage_root / 'manifest.json')
    _copy_remote(internal, STAGE_KEY, stage_root / 'manifest.json',
                 stage_entry['bytes'], stage_entry['sha256'])
    print('[LAGOMORPH PULL] VERIFIED private Builds/Lagomorph and public HFS media; local extras untouched', flush=True)
    return index
