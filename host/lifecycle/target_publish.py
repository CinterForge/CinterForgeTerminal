"""One-platform release publisher; never reads or modifies four-artifacts.json.

Windows also publishes a WiX MSI ZIP with a separate content-hash manifest. This
module is strictly Publish-only: inputs must already be built and Setup-tested.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from zipfile import BadZipFile, ZIP_STORED, ZipFile

SCHEMA = 'cinterforge.terminal.scoped-distribution.v1'
TARGETS = ('mac', 'linux', 'win')
MSI_NAME = 'CinterForgeTerminal-win.msi'
MSI_ZIP_NAME = 'CinterForgeTerminal-win-msi.zip'


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _zip_contains_msi(archive: Path, *, sha256: str, size: int) -> bool:
    """Verify the actual uncompressed MSI bytes, not just the ZIP CRC/header."""
    if not archive.is_file() or archive.is_symlink():
        return False
    try:
        with ZipFile(archive, 'r') as bundle:
            if bundle.namelist() != [MSI_NAME] or bundle.getinfo(MSI_NAME).file_size != size:
                return False
            digest = hashlib.sha256()
            extracted = 0
            with bundle.open(MSI_NAME) as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                    digest.update(block)
                    extracted += len(block)
            return extracted == size and digest.hexdigest() == sha256
    except (BadZipFile, EOFError, OSError, ValueError, RuntimeError):
        return False


def package_msi_zip(builds: Path, installer: dict) -> dict:
    """Wrap a verified MSI in its own browser-downloadable ZIP, atomically.

    Do not sign/recompile the MSI here. Authenticode signing, if configured,
    must be completed and verified *before* the bytes in this ZIP are sealed.
    """
    if installer.get('filename') != MSI_NAME or not installer.get('release'):
        raise RuntimeError('Expected a verified Windows MSI packaging receipt')
    root = builds / 'CinterForgeTerminal/Distribution'
    source = root / MSI_NAME
    expected_sha = installer.get('sha256')
    expected_size = installer.get('bytes')
    if (not isinstance(expected_sha, str) or len(expected_sha) != 64 or
            not isinstance(expected_size, int) or expected_size <= 0 or
            not source.is_file() or source.is_symlink() or
            source.stat().st_size != expected_size or _sha(source) != expected_sha):
        raise RuntimeError('Windows MSI changed after compiler verification; refusing ZIP')
    root.mkdir(parents=True, exist_ok=True)
    destination = root / MSI_ZIP_NAME
    if not _zip_contains_msi(destination, sha256=expected_sha, size=expected_size):
        fd, tmp_name = tempfile.mkstemp(prefix='.win-msi-', suffix='.zip.part', dir=root)
        os.close(fd)
        pending = Path(tmp_name)
        try:
            # MSI already contains compressed CAB data. ZIP_STORED avoids
            # wasting CPU compressing that data a second time.
            with ZipFile(pending, 'w', compression=ZIP_STORED, allowZip64=True) as bundle:
                bundle.write(source, arcname=MSI_NAME)
            if not _zip_contains_msi(pending, sha256=expected_sha, size=expected_size):
                raise RuntimeError('New Windows MSI ZIP failed byte-for-byte verification')
            os.replace(pending, destination)
            print(f'[WINDOWS MSI] ZIP verified: {destination}', flush=True)
        finally:
            pending.unlink(missing_ok=True)
    else:
        print(f'[WINDOWS MSI] Reusing verified MSI ZIP: {destination}', flush=True)
    return {'filename': MSI_ZIP_NAME, 'release': installer['release'],
            'bytes': destination.stat().st_size, 'sha256': _sha(destination),
            'msi_sha256': expected_sha}


def _reusable(cfg: dict, prefix: str, remote: dict, manifest: str,
              expected: dict[str, dict]) -> set[str]:
    """Use the small committed remote manifest only when object age/size agrees."""
    from host.publishing import cloudflare_r2_sync as r2
    descriptor = remote.get(manifest, {})
    if not 0 < int(descriptor.get('size', -1)) < 1024 * 1024:
        return set()
    try:
        timestamp = datetime.fromisoformat(descriptor['last_modified'].replace('Z', '+00:00'))
        _, _, data = r2._request(cfg, 'GET', key=f'{prefix}/{manifest}')
        if len(data) != descriptor['size']:
            return set()
        doc = json.loads(data)
        if doc.get('schema') != SCHEMA:
            return set()
        verified = set()
        for name, detail in expected.items():
            current = remote.get(name, {})
            if (doc.get('artifacts', {}).get(name) == detail and
                    current.get('size') == detail['bytes'] and
                    datetime.fromisoformat(current['last_modified'].replace('Z', '+00:00')) < timestamp):
                verified.add(name)
        return verified
    except (KeyError, TypeError, ValueError, OSError):
        return set()


def _publish_files(builds: Path, *, channel: str, target: str, files: dict[str, Path],
                   document: dict, config: Path | None = None) -> dict:
    from host.lifecycle.universal_package import _cloudflare_config, _publish_destination, _public_http_attempt
    from host.publishing import cloudflare_r2_sync as r2
    if channel not in ('dryrun', 'internal', 'beta', 'release'):
        raise ValueError('Invalid Publish channel: ' + channel)
    if channel == 'dryrun':
        connections = {}
        for destination in ('internal', 'release'):
            cfg = _cloudflare_config(config, channel=destination)
            _, prefix = _publish_destination(destination)
            r2._list_remote(cfg, prefix)
            connections[destination] = cfg['bucket']
        print(f'[TERMINAL PUBLISH] DRY RUN {target}: package(s) verified, 0 uploads', flush=True)
        return {'channel': channel, 'target': target, 'connections': connections, 'uploaded': False}
    if os.environ.get('CINTERFORGE_SETUP_DISABLE_UPLOADS') == '1':
        raise RuntimeError('Publishing disabled on Setup/test machine')
    cfg = _cloudflare_config(config, channel=channel)
    provider, prefix = _publish_destination(channel)
    remote = r2._list_remote(cfg, prefix)
    manifest_name = f'{target}-artifacts.json'
    root = builds / 'CinterForgeTerminal/Distribution'
    root.mkdir(parents=True, exist_ok=True)
    (builds / 'Intermediates/CinterForgeTerminal').mkdir(parents=True, exist_ok=True)
    manifest = root / manifest_name
    # This manifest is distinct from four-artifacts.json. A scoped run can
    # never accidentally replace the production all-platform release index.
    document['schema'] = SCHEMA
    document['channel'] = channel
    manifest.write_text(json.dumps(document, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    reusable = _reusable(cfg, prefix, remote, manifest_name, document['artifacts'])
    for name, path in files.items():
        if name in reusable:
            print(f'[TERMINAL PUBLISH] {target}: remote verified, reusing {name}', flush=True)
            continue
        r2._put_file(cfg, f'{prefix}/{name}', path)
        # Download only changed artifacts. Local receipt may not mark them
        # as verified before the authenticated round trip completes.
        with tempfile.TemporaryDirectory(prefix=f'{target}-publish-',
                                         dir=builds / 'Intermediates/CinterForgeTerminal') as tmp:
            checked = Path(tmp) / name
            r2._get_file(cfg, f'{prefix}/{name}', checked,
                         expected_size=document['artifacts'][name]['bytes'])
            if _sha(checked) != document['artifacts'][name]['sha256']:
                raise RuntimeError('R2 roundtrip SHA-256 mismatch: ' + name)
    # Manifest is the commit record and must be uploaded *last*.
    r2._put_file(cfg, f'{prefix}/{manifest_name}', manifest)
    _, _, payload = r2._request(cfg, 'GET', key=f'{prefix}/{manifest_name}')
    if hashlib.sha256(payload).hexdigest() != _sha(manifest):
        raise RuntimeError('R2 manifest roundtrip SHA-256 mismatch: ' + manifest_name)
    urls = {}
    if provider == 'public':
        base = 'https://cinterforge.ucreg.io'
        for name, detail in document['artifacts'].items():
            url = f'{base}/{prefix}/{name}'
            _public_http_attempt(name, url, detail['bytes'])
            urls[name] = url
        _public_http_attempt('manifest', f'{base}/{prefix}/{manifest_name}',
                             manifest.stat().st_size, _sha(manifest))
    print(f'[TERMINAL PUBLISH] {target}: verified scoped publication to {cfg["bucket"]}/{prefix}',
          flush=True)
    return {'channel': channel, 'target': target, 'bucket': cfg['bucket'],
            'prefix': prefix, 'artifacts': document['artifacts'], 'public_urls': urls,
            'roundtrip': 'sha256-verified'}


def publish(builds: Path, *, target: str, channel: str = 'dryrun',
            release: str | None = None, config: Path | None = None) -> dict:
    """Prepare/publish one app only; WiX MSI is added for Windows."""
    from host.lifecycle import universal_package as up
    from host.lifecycle.unpacked_release import _load, prepare_publish_packages
    if target not in TARGETS:
        raise ValueError('Expected mac|win|linux scoped Publish')
    builds = builds.resolve(strict=True)
    release = release or up.HOST_RELEASE
    # Public Mac signing must fail before a 1GB Windows/SDK archive is built.
    if target == 'mac' and channel in ('beta', 'release'):
        identity = os.environ.get('CINTER_MAC_SIGN_IDENTITY', '').strip()
        if not identity:
            raise RuntimeError('Public macOS Publish requires a usable Developer ID Application identity/private key')
    report = prepare_publish_packages(builds, release=release, targets=(target,),
                                      # Keep local compression reuse separate from the
                                      # published R2 manifest's different schema.
                                      receipt_name=f'{target}-packages.json',
                                      notarize=channel in ('beta', 'release'))
    entry = report['artifacts'][target]
    filename = up.artifact_filename(target)
    output = builds / 'CinterForgeTerminal/Distribution' / filename
    files = {filename: output}
    if target == 'mac' and channel in ('beta', 'release'):
        up._require_notarized_mac_release(output, builds)
    artifacts = {filename: {'sha256': entry['sha256'], 'bytes': entry['bytes'],
                            'build_fingerprint': entry['build_fingerprint']}}
    if target == 'win':
        from host.windows.msi_installer import publish as make_msi
        record = _load(builds, release)['targets']['win']
        installer = make_msi(builds, release=release, entries=record['files'])
        archive = package_msi_zip(builds, installer)
        path = builds / 'CinterForgeTerminal/Distribution' / archive['filename']
        files[path.name] = path
        artifacts[path.name] = {'sha256': archive['sha256'], 'bytes': archive['bytes'],
                                'build_fingerprint': record['fingerprint'],
                                'contains': MSI_NAME, 'msi_sha256': archive['msi_sha256']}
    return _publish_files(builds, channel=channel, target=target, files=files,
                          document={'release': release, 'target': target,
                                    'artifacts': artifacts}, config=config)


def publish_msi_addendum(builds: Path, *, channel: str, release: str | None = None,
                         config: Path | None = None, packaged: dict | None = None) -> dict:
    """Full four-artifact Publish also emits enterprise MSI via the same path."""
    from host.lifecycle import universal_package as up
    from host.lifecycle.unpacked_release import _load
    from host.windows.msi_installer import publish as make_msi
    release = release or up.HOST_RELEASE
    record = _load(builds.resolve(), release)['targets']['win']
    entry = packaged if packaged is not None else make_msi(builds, release=release, entries=record['files'])
    if entry.get('release') != release or entry.get('filename') != MSI_NAME:
        raise RuntimeError('Invalid prepared Windows MSI publication receipt')
    archive = package_msi_zip(builds, entry)
    path = builds / 'CinterForgeTerminal/Distribution' / archive['filename']
    return _publish_files(builds, channel=channel, target='win', files={path.name: path},
                          document={'release': release, 'target': 'win', 'artifacts': {
                              path.name: {'sha256': archive['sha256'], 'bytes': archive['bytes'],
                                          'build_fingerprint': record['fingerprint'],
                                          'contains': MSI_NAME, 'msi_sha256': archive['msi_sha256']}}}, config=config)
