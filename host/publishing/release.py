"""Immutable CinterForgeTerminal SDK candidate publication via Companion's R2 transport.

Publication is deliberately independent from Setup, Build and Stage. Dev candidates
need three staged platforms; production additionally requires matching native
acceptance receipts and explicit promotion approval. Only the Cloudflare target
bucket changes with channel. No credentials or upload state enter the archives.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile

from . import cloudflare_r2_sync as r2

SCHEMA = 'cinterforge.cinterterminal.release.v1'
TEST_SCHEMA = 'cinterforge.cinterterminal.native-test.v1'
APP_TEST_SCHEMA = 'cinterforge.cinterterminal.app.native-test.v1'
APP_STAGE_SCHEMA = 'cinterforge.cinterterminal.app.stage.v1'
PRODUCTS = ('terminal', 'host-sdk')
BUCKETS = {'dev': 'cdn-assets-dev', 'production': 'cinterforge'}
PUBLIC_URLS = {'dev': 'https://cdn-dev.ucreg.io', 'production': 'https://cinterforge.ucreg.io'}
TARGETS = ('mac', 'linux', 'win')
OBJECT_ROOTS = {'terminal': 'CinterForgeTerminal/Applications', 'host-sdk': 'CinterForgeTerminal/Host'}


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode('utf-8')


def _safe_files(stage: Path, manifest: dict, *, product: str) -> list[Path]:
    files = manifest.get('files')
    schema = APP_STAGE_SCHEMA if product == 'terminal' else 'cinterforge.python-host.stage.v1'
    if manifest.get('schema') != schema or not isinstance(files, dict) or not files:
        raise ValueError(f'Invalid CinterForgeTerminal staged SDK manifest: {stage}')
    if product == 'terminal' and manifest.get('entrypoint') not in files:
        raise ValueError(f'Application manifest must identify a staged executable entrypoint: {stage}')
    actual = {p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file() or p.is_symlink()}
    expected = set(files) | {'manifest.json'}
    if actual != expected:
        raise ValueError(f'Staged SDK file set differs from manifest at {stage}: missing={sorted(expected-actual)} extra={sorted(actual-expected)}')
    result = []
    for rel, expected_digest in sorted(files.items()):
        if not isinstance(rel, str) or not rel or rel.startswith('/') or '..' in Path(rel).parts or '\\' in rel:
            raise ValueError(f'Unsafe staged SDK path: {rel!r}')
        path = stage / rel
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(stage.resolve()):
            raise ValueError(f'Unsafe staged SDK file: {path}')
        if not re.fullmatch(r'[0-9a-f]{64}', str(expected_digest)) or _digest(path) != expected_digest:
            raise ValueError(f'Staged SDK checksum mismatch: {path}')
        result.append(path)
    if (stage / 'manifest.json').is_symlink():
        raise ValueError('SDK manifest may not be a symlink')
    return [stage / 'manifest.json', *result]


def _test_receipt(builds: Path, platform: str, arch: str, release: str, stage_sha256: str, product: str) -> str:
    tests = builds / ('CinterForgeTerminal/Tests' if product == 'terminal' else 'Python/Host/Tests')
    receipt = tests / platform / arch / release / 'receipt.json'
    if not receipt.is_file():
        return 'missing'
    try:
        data = json.loads(receipt.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 'invalid'
    expected_schema = APP_TEST_SCHEMA if product == 'terminal' else TEST_SCHEMA
    if (data.get('schema') != expected_schema or data.get('result') != 'passed'
            or data.get('platform') != platform or data.get('arch') != arch
            or data.get('release') != release or data.get('stage_manifest_sha256') != stage_sha256):
        return 'invalid'
    return 'native' if data.get('execution') == 'native' else 'emulated'


def _stage_entries(builds: Path, release: str, channel: str, product: str) -> list[dict]:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', release):
        raise ValueError('Unsafe release identifier')
    root = (builds / ('CinterForgeTerminal/Staged' if product == 'terminal' else 'Python/Host')).resolve()
    entries = []
    for platform in TARGETS:
        platform_dir = root / platform
        found = sorted(platform_dir.glob(f'*/{release}/manifest.json'))
        if not found:
            raise FileNotFoundError(f'No verified staged {platform} Host artifact for {release} under {platform_dir}')
        for path in found:
            stage = path.parent
            if not stage.resolve().is_relative_to(root) or stage.is_symlink():
                raise ValueError(f'Unsafe host stage path: {stage}')
            data = json.loads(path.read_text(encoding='utf-8'))
            arch = stage.parent.name
            if data.get('platform') != platform or data.get('arch') != arch:
                raise ValueError(f'Stage platform/arch mismatch: {path}')
            if product == 'terminal' and channel == 'production':
                permission = data.get('distribution')
                if (not isinstance(permission, dict)
                        or permission.get('public_approved') is not True
                        or not isinstance(permission.get('license'), str)
                        or not permission['license'].strip()):
                    raise RuntimeError(f'Public release rights are not approved for {platform}/{arch}: {path}')
            files = _safe_files(stage, data, product=product)
            stage_sha = _digest(path)
            execution = _test_receipt(builds, platform, arch, release, stage_sha, product)
            if channel == 'production' and execution != 'native':
                raise RuntimeError(f'Production requires a matching native test receipt for {platform}/{arch}: {execution}')
            entries.append({'platform': platform, 'arch': arch, 'stage': stage,
                            'files': files, 'stage_sha256': stage_sha,
                            'native_test': execution})
    return entries


def _zip_stage(stage: Path, files: list[Path], destination: Path) -> None:
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9, allowZip64=True) as archive:
        for path in sorted(files, key=lambda p: p.relative_to(stage).as_posix()):
            rel = path.relative_to(stage).as_posix()
            info = zipfile.ZipInfo('CinterForgeTerminal/' + rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = stat.S_IMODE(path.stat().st_mode)
            info.external_attr = ((stat.S_IFREG | mode) << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open('rb') as src, archive.open(info, 'w', force_zip64=True) as dst:
                while True:
                    data = src.read(1024 * 1024)
                    if not data:
                        break
                    dst.write(data)


def prepare(builds: Path, release: str, channel: str, *, product: str = 'terminal',
            approve_production: bool = False) -> tuple[Path, dict]:
    """Verify staged files and prepare deterministic immutable upload inputs under Builds."""
    if channel not in BUCKETS:
        raise ValueError(f'Invalid channel: {channel}')
    if product not in PRODUCTS:
        raise ValueError(f'Invalid product: {product}')
    if product == 'host-sdk' and channel == 'production':
        raise RuntimeError('Generic Host SDK is development-only by Cloudflare publishing policy')
    if channel == 'production' and not approve_production:
        raise RuntimeError('Public promotion requires --approve-production and native receipts')
    builds = builds.resolve()
    entries = _stage_entries(builds, release, channel, product)
    identity = {'release': release, 'channel': channel, 'product': product,
                'stages': [{'platform': x['platform'], 'arch': x['arch'],
                            'stage_sha256': x['stage_sha256'], 'native_test': x['native_test']}
                           for x in entries]}
    build_id = hashlib.sha256(_canonical(identity)).hexdigest()[:20]
    prefix = f'{OBJECT_ROOTS[product]}/releases/{release}/{build_id}'
    outbox = builds / 'CinterForgeTerminal/Publish/Outbox' / product / channel / release / build_id
    outbox.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='candidate-', dir=outbox.parent) as td:
        temp = Path(td)
        artifacts = []
        for item in entries:
            name = f'CinterForgeTerminal-{product}-{release}-{item["platform"]}-{item["arch"]}.zip'
            path = temp / name
            _zip_stage(item['stage'], item['files'], path)
            artifacts.append({'platform': item['platform'], 'arch': item['arch'],
                              'file': name, 'bytes': path.stat().st_size,
                              'sha256': _digest(path), 'stage_manifest_sha256': item['stage_sha256'],
                              'native_test': item['native_test'],
                              'url': f'{PUBLIC_URLS[channel]}/{prefix}/{name}'})
        manifest = {'schema': SCHEMA, 'product': product, 'release': release,
                    'build_id': build_id, 'channel': channel, 'bucket': BUCKETS[channel],
                    'object_prefix': prefix, 'artifacts': artifacts,
                    'native_certified': all(x['native_test'] == 'native' for x in artifacts)}
        (temp / 'manifest.json').write_bytes(_canonical(manifest))
        if outbox.exists():
            for path in temp.iterdir():
                existing = outbox / path.name
                if not existing.is_file() or _digest(path) != _digest(existing):
                    raise RuntimeError(f'Immutable candidate has changed: {outbox}; publish a new release/build')
        else:
            os.replace(temp, outbox)
    return outbox, manifest


def _verify_existing_remote(cfg: dict, prefix: str, outbox: Path) -> None:
    """Never overwrite immutable build objects, even if an R2 ETag isn't MD5."""
    objects = r2._list_remote(cfg, prefix)
    for item in outbox.iterdir():
        if item.name not in objects:
            continue
        if int(objects[item.name].get('size', -1)) != item.stat().st_size:
            raise RuntimeError(f'Immutable remote object differs in size: {prefix}/{item.name}')
        etag = str(objects[item.name].get('etag', '')).strip('\"').lower()
        if re.fullmatch(r'[0-9a-f]{32}', etag) and r2._md5_file(item) == etag:
            continue
        # Multipart R2 ETags are not content hashes. Reuse Companion's
        # streaming downloader: never materialize a large package in RAM.
        with tempfile.TemporaryDirectory(prefix='r2-verify-', dir=outbox.parent) as td:
            candidate = Path(td) / item.name
            r2._get_file(cfg, f'{prefix}/{item.name}', candidate, expected_size=item.stat().st_size)
            if _digest(candidate) != _digest(item):
                raise RuntimeError(f'Immutable remote object differs in content: {prefix}/{item.name}')


def publish(builds: Path, release: str, channel: str, *, config: Path,
            provider: str = 'auto', dry_run: bool = False, product: str = 'terminal',
            approve_production: bool = False) -> dict:
    outbox, manifest = prepare(builds, release, channel, product=product,
                               approve_production=approve_production)
    if dry_run:
        print(f'[CPH publish] DRY RUN bucket={manifest["bucket"]} prefix={manifest["object_prefix"]} packages={len(manifest["artifacts"])} outbox={outbox}', flush=True)
        return manifest
    if os.environ.get('CINTERFORGE_SETUP_DISABLE_UPLOADS') == '1':
        raise RuntimeError('Upload forbidden on a setup/tester machine')
    cfg = r2._load_config(config, provider, required_bucket=BUCKETS[channel])
    prefix = manifest['object_prefix']
    _verify_existing_remote(cfg, prefix, outbox)
    # Companion's sync transport handles multipart, signed requests, retries,
    # remote listing and post-upload object verification. Never delete remotely.
    r2.upload_sync(cfg, outbox, prefix, '', no_delete=True)
    _verify_existing_remote(cfg, prefix, outbox)
    # Update the mutable channel pointer only after the immutable payload passed.
    pointer_key = f'{OBJECT_ROOTS[product]}/channels/{channel}.json'
    pointer = {'schema': SCHEMA, 'channel': channel, 'product': product, 'release': release,
               'build_id': manifest['build_id'], 'bucket': BUCKETS[channel],
               'manifest': f'{PUBLIC_URLS[channel]}/{prefix}/manifest.json',
               'manifest_sha256': _digest(outbox / 'manifest.json')}
    r2._request(cfg, 'PUT', key=pointer_key, body=_canonical(pointer),
                headers={'content-type': 'application/json'})
    _, _, verified = r2._request(cfg, 'GET', key=pointer_key)
    if hashlib.sha256(verified).digest() != hashlib.sha256(_canonical(pointer)).digest():
        raise RuntimeError(f'Cloudflare channel pointer verification failed: {pointer_key}')
    print(f'[CPH publish] VERIFIED bucket={BUCKETS[channel]} manifest={pointer["manifest"]}', flush=True)
    return manifest
