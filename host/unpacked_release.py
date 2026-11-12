"""Unpacked CinterForgeTerminal release lifecycle.

Setup alone hydrates verified published archives. Build captures exactly four
uncompressed file inventories under sibling Builds. Stage copies those files
straight into Steam (no archive operations). Publish alone creates the Mac DMG and three ZIPs.

The inventories refer to immutable Build-produced files and snapshot small
source-authored resources into Builds; they do not duplicate the multi-GB SDK.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

BUILT_SCHEMA = 'cinterforge.terminal.unpacked-build.v1'


def _up():
    from host import universal_package
    return universal_package


def _safe_builds(builds: Path) -> Path:
    builds = builds.resolve(strict=True)
    if builds.name != 'Builds' or not builds.is_dir() or builds.is_symlink():
        raise RuntimeError('Unpacked lifecycle requires the existing sibling Builds directory')
    return builds


def _fingerprint(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _stat(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as src:
        for data in iter(lambda: src.read(4 * 1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def _atomic_json(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix='Receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(record, out, indent=2, sort_keys=True)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        Path(filename).replace(path)
    finally:
        Path(filename).unlink(missing_ok=True)


def _built_receipt(builds: Path, release: str) -> Path:
    return builds / 'CinterForgeTerminal/Distribution/four-built.json'


def _load(builds: Path, release: str) -> dict:
    up = _up()
    path = _built_receipt(builds, release)
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError('Run ./Cinter build cfterminal all before Stage or Publish: ' + str(path))
    record = json.loads(path.read_text('utf-8'))
    if (record.get('schema') != BUILT_SCHEMA or record.get('release') != release or
            set(record.get('targets', {})) != set(up.FOUR_TARGETS)):
        raise RuntimeError('Incomplete four-target unpacked Build receipt')
    return record


def _safe_name(name: str, target: str) -> None:
    root = _up().APP_ROOTS[target]
    path = Path(name)
    if (not isinstance(name, str) or not name.startswith(root + '/') or
            '..' in path.parts or '\\' in name or path.is_absolute() or
            _up()._python_cache_member(name) or '/Servers/Bedrock/' in name):
        raise RuntimeError('Unsafe unpacked release path: ' + str(name))


def _source(builds: Path, name: str, data: dict, target: str, *, verify: bool = False) -> Path:
    _safe_name(name, target)
    source = Path(data['source'])
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError('Unpacked Build input missing: ' + str(source))
    real = source.resolve(strict=True)
    if not real.is_relative_to(builds):
        raise RuntimeError('Stage/Publish input escaped canonical Builds: ' + str(real))
    if _stat(real) != data['stat']:
        raise RuntimeError('Build input changed after Build: ' + name + '; rebuild before Stage/Publish')
    if verify and _sha(real) != data['sha256']:
        raise RuntimeError('Build input SHA-256 mismatch: ' + name)
    return real


def _snapshot_authored(builds: Path, target: str, name: str, value: Path | bytes) -> Path:
    """Only copy authored files outside Builds or generated short byte assets."""
    prepared = builds / 'CinterForgeTerminal/Prepared/Authored' / target / name
    if prepared.is_symlink():
        raise RuntimeError('Unsafe Build authored snapshot: ' + str(prepared))
    prepared.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='Input-', dir=prepared.parent)
    try:
        with os.fdopen(fd, 'wb') as dst:
            if isinstance(value, bytes):
                dst.write(value)
            else:
                with value.open('rb') as src:
                    shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
        Path(tmp).chmod(_up()._release_file_mode(name, value))
        Path(tmp).replace(prepared)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return prepared


def _input_identity(assets: dict[str, Path | bytes], target: str) -> dict:
    identity = {}
    for name, value in sorted(assets.items()):
        _safe_name(name, target)
        if isinstance(value, bytes):
            identity[name] = ['bytes', len(value), hashlib.sha256(value).hexdigest()]
        else:
            if not value.is_file():
                raise FileNotFoundError('Required Build input missing: ' + str(value))
            identity[name] = [str(value.resolve()), *_stat(value)]
    if target == 'mac':
        identity['@signing'] = [os.environ.get('CINTER_MAC_SIGN_IDENTITY', '') or 'ad-hoc',
                                os.environ.get('CINTER_MAC_NOTARY_PROFILE', '')]
    return identity


def build_four(builds: Path, *, release: str | None = None) -> dict:
    """Capture verified unpacked Build outputs; never ZIP or unzip."""
    up = _up()
    release = release or up.HOST_RELEASE
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', release):
        raise ValueError('Invalid release identifier')
    builds = _safe_builds(builds)
    previous = {}
    try:
        previous = _load(builds, release)
    except (FileNotFoundError, ValueError, RuntimeError, KeyError):
        pass
    targets: dict[str, dict] = {}
    for target in up.FOUR_TARGETS:
        print(f'[TERMINAL BUILD] Gathering unpacked {target} inputs...', flush=True)
        assets = up._devtools_assets(builds) if target == 'devtools' else up._app_assets(builds, target, release)
        identity = _fingerprint(_input_identity(assets, target))
        old = previous.get('targets', {}).get(target, {})
        if old.get('input_identity') == identity and old.get('files'):
            try:
                for name, detail in old['files'].items():
                    _source(builds, name, detail, target)
                targets[target] = old
                print(f'[TERMINAL BUILD] {target} unchanged; reusing unpacked Build inputs', flush=True)
                continue
            except (OSError, RuntimeError, ValueError, KeyError):
                pass
        if target == 'mac' and assets and all(isinstance(value, Path) for value in assets.values()):
            # Sign a filtered, complete release bundle once IN BUILD, not Stage
            # or Publish; Stage uses the verified seal verbatim.
            signed_root = builds / 'CinterForgeTerminal/Prepared/MacSigned'
            signed_root.mkdir(parents=True, exist_ok=True)
            old_signed = signed_root / 'MacSignedRelease' / up.APP_ROOTS[target]
            if old_signed.exists():
                if old_signed.is_symlink() or not old_signed.is_dir():
                    raise RuntimeError('Unsafe old signed Mac Build copy')
                shutil.rmtree(old_signed)
            assets = up._signed_mac_zip_assets(assets, signed_root, notarize=False)
        entries: dict[str, dict] = {}
        for name, value in sorted(assets.items()):
            if isinstance(value, Path) and value.resolve(strict=True).is_relative_to(builds):
                source = value.resolve(strict=True)
            else:
                source = _snapshot_authored(builds, target, name, value)
            entries[name] = {'source': str(source), 'sha256': _sha(source),
                             'bytes': source.stat().st_size,
                             'mode': up._release_file_mode(name, value),
                             'stat': _stat(source)}
        fingerprint = _fingerprint({k: [v['sha256'], v['mode']] for k,v in entries.items()})
        targets[target] = {'input_identity': identity, 'fingerprint': fingerprint,
                           'bytes': sum(item['bytes'] for item in entries.values()),
                           'file_count': len(entries), 'files': entries}
        print(f'[TERMINAL BUILD] {target}: {len(entries)} unpacked files; '
              f'{targets[target]["bytes"] / 1048576:.1f} MiB, no ZIP', flush=True)
    output = {'schema': BUILT_SCHEMA, 'release': release, 'targets': targets,
              'bedrock': 'excluded-until-app-eula-acceptance'}
    _atomic_json(_built_receipt(builds, release), output)
    return {'schema': BUILT_SCHEMA, 'release': release,
            'artifacts': {target: {'bytes': data['bytes'], 'files': data['file_count'],
                                   'fingerprint': data['fingerprint']}
                          for target, data in targets.items()}}


def _installed_state(root: Path, files: dict) -> str | None:
    state = {}
    for name in files:
        path = root / name
        if path.is_symlink() or not path.is_file():
            return None
        state[name] = _stat(path)
    return _fingerprint(state)


def _copy_verified(source: Path, dest: Path, detail: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='Staging-', dir=dest.parent)
    try:
        with os.fdopen(fd, 'wb') as out, source.open('rb') as inp:
            digest = hashlib.sha256()
            for data in iter(lambda: inp.read(4*1024*1024), b''):
                out.write(data)
                digest.update(data)
        if digest.hexdigest() != detail['sha256']:
            raise RuntimeError('Build input changed during Stage: ' + str(source))
        Path(tmp).chmod(detail['mode'])
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _mac_install(builds: Path, root: Path, files: dict, *, target: str) -> int:
    """Same-volume atomic app replacement with rollback, but no archive step."""
    up = _up()
    app_name = up.APP_ROOTS['mac']
    up._recover_interrupted_mac_stage(root, app_name, builds)
    candidate = Path(tempfile.mkdtemp(prefix='Copy-mac-', dir=root))
    current, backup = root / app_name, root / (app_name + '.previous-stage')
    moved = False
    promoted = False
    try:
        for name, detail in files.items():
            _copy_verified(_source(builds, name, detail, target), candidate / name, detail)
        if __import__('platform').system() == 'Darwin':
            from host.mac_app import _verify_signature
            _verify_signature(candidate / app_name)
        if current.exists() or current.is_symlink():
            if current.is_symlink() or not current.is_dir():
                raise RuntimeError('Unsafe existing signed app destination')
            current.replace(backup)
            moved = True
        (candidate / app_name).replace(current)
        promoted = True
        if __import__('platform').system() == 'Darwin':
            from host.mac_app import _verify_signature
            _verify_signature(current)
        if moved:
            shutil.rmtree(backup)
        return len(files)
    except BaseException:
        if moved and backup.is_dir() and not backup.is_symlink():
            if promoted and current.is_dir() and not current.is_symlink():
                current.replace(candidate / app_name)
            if not current.exists():
                backup.replace(current)
        raise
    finally:
        shutil.rmtree(candidate, ignore_errors=True)


def stage_four(builds: Path, *, release: str | None = None,
               steamapps: Path | None = None, targets: tuple[str, ...] | None = None) -> Path:
    """Install directly from Build inventory. Absolutely no ZIP reads/writes."""
    up = _up()
    release = release or up.HOST_RELEASE
    builds = _safe_builds(builds)
    record = _load(builds, release)
    selected = targets or up.FOUR_TARGETS
    if not selected or not set(selected) <= set(up.FOUR_TARGETS) or len(set(selected)) != len(selected):
        raise ValueError('Invalid Stage targets')
    steam = up.steamapps_root(steamapps)
    for target in selected:
        info = record['targets'][target]
        files = info['files']
        root = up._steam_release_path(steam, target, release)
        if root.is_symlink():
            raise RuntimeError('Unsafe Steam Stage target: ' + str(root))
        root.mkdir(parents=True, exist_ok=True)
        # Recover interrupted macOS app replacement even when its receipt
        # was clean before the crash. The old version's backup must survive.
        if target == 'mac':
            up._recover_interrupted_mac_stage(root, up.APP_ROOTS['mac'], builds)
        receipt_path = root / 'stage-receipt.json'
        try:
            previous = json.loads(receipt_path.read_text('utf-8'))
        except (OSError, ValueError, TypeError):
            previous = {}
        # Fast and complete stat-only clean check; Stage never hashes or
        # transfers the multi-GB SDK again when its installed files are intact.
        if (previous.get('build_fingerprint') == info['fingerprint'] and
                previous.get('installed_state') == _installed_state(root, files)):
            print(f'[TERMINAL STAGE] {target} unchanged; no copy or ZIP', flush=True)
            continue
        for name, detail in files.items():
            _source(builds, name, detail, target)
        if target == 'mac':
            changed = _mac_install(builds, root, files, target=target)
        else:
            changed = 0
            for name, detail in files.items():
                dest = root / name
                current = root
                for part in Path(name).parts[:-1]:
                    current = current / part
                    if current.is_symlink():
                        raise RuntimeError('Unsafe Steam Stage directory symlink: ' + str(current))
                    if current.exists() and not current.is_dir():
                        raise RuntimeError('File blocks Steam Stage directory: ' + str(current))
                if dest.is_symlink():
                    raise RuntimeError('Unsafe Steam Stage file symlink: ' + str(dest))
                if dest.is_file() and dest.stat().st_size == detail['bytes'] and _sha(dest) == detail['sha256']:
                    continue
                if dest.is_dir():
                    raise RuntimeError('Directory blocks Stage file: ' + str(dest))
                _copy_verified(_source(builds, name, detail, target), dest, detail)
                changed += 1
        state = _installed_state(root, files)
        if state is None:
            raise RuntimeError('Incomplete unpacked Stage: ' + target)
        _atomic_json(receipt_path, {'schema': BUILT_SCHEMA, 'release': release,
                                     'target': target, 'build_fingerprint': info['fingerprint'],
                                     'installed_state': state})
        print(f'[TERMINAL STAGE] {target}: {changed} refreshed files directly from Builds', flush=True)
    return steam / 'common/CinterForge/Applications'


def status_four(builds: Path, *, release: str | None = None, steamapps: Path | None = None) -> dict:
    up = _up()
    release = release or up.HOST_RELEASE
    builds = builds.resolve()
    try:
        record = _load(builds, release)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        record = {}
    try:
        steam = up.steamapps_root(steamapps)
    except (OSError, RuntimeError, ValueError):
        steam = None
    states = {}
    for target in up.FOUR_TARGETS:
        info = record.get('targets', {}).get(target, {})
        root = up._steam_release_path(steam, target, release) if steam else None
        try:
            receipt = json.loads((root / 'stage-receipt.json').read_text()) if root else {}
        except (OSError, ValueError, TypeError):
            receipt = {}
        correct = bool(info and root and receipt.get('build_fingerprint') == info.get('fingerprint')
                       and receipt.get('installed_state') == _installed_state(root, info['files']))
        states[target] = {'build_present': bool(info), 'build_bytes': info.get('bytes'),
                          'steam_root': str(root) if root else None,
                          'steam_receipt_matches': correct, 'steam_app_root_present': bool(
                              correct and (root / up.APP_ROOTS[target]).is_dir()) if root else False}
    return {'release': release,
            'distribution': str(builds / 'CinterForgeTerminal/Distribution'),
            'steam_destination': str(steam / 'common/CinterForge/Applications') if steam else None,
            'targets': states,
            'complete_build_receipt': set(record.get('targets', {})) == set(up.FOUR_TARGETS),
            'complete_stage_receipt': all(s['steam_receipt_matches'] for s in states.values())}


def prepare_publish_packages(builds: Path, *, release: str | None = None,
                             notarize: bool = True,
                             reuse_remote_devtools: dict | None = None) -> dict:
    """Only Publish calls this. No Stage and no Setup dependency."""
    up = _up()
    release = release or up.HOST_RELEASE
    builds = _safe_builds(builds)
    record = _load(builds, release)
    root = builds / 'CinterForgeTerminal/Distribution'
    try:
        previous = json.loads((root / 'four-artifacts.json').read_text())
    except (OSError, ValueError, TypeError):
        previous = {}
    artifacts = {}
    for target, info in record['targets'].items():
        name = up.artifact_filename(target)
        old = previous.get('artifacts', {}).get(target, {})
        path = root / name
        if (old.get('build_fingerprint') == info['fingerprint'] and
                (target != 'mac' or (old.get('format') == 'dmg' and
                 (not notarize or not os.environ.get('CINTER_MAC_SIGN_IDENTITY', '').strip() or
                  old.get('notarized') is True))) and
                path.is_file() and not path.is_symlink() and
                path.stat().st_size == old.get('bytes') and
                # A matching previously SHA-verified archive can be reused
                # without reading/recompressing a multi-GB SDK on every Publish.
                (target == 'devtools' and old.get('archive_identity') ==
                 up._release_archive_identity(path) or _sha(path) == old.get('sha256'))):
            artifacts[target] = old.copy()
            if target == 'devtools':
                artifacts[target]['archive_identity'] = up._release_archive_identity(path)
            print(f'[TERMINAL PUBLISH] {target} clean; reusing verified distribution package', flush=True)
            continue
        if target == 'devtools' and reuse_remote_devtools:
            candidate = reuse_remote_devtools
            if (candidate.get('build_fingerprint') == info['fingerprint'] and
                    candidate.get('target') == 'devtools' and
                    candidate.get('filename') == name and
                    isinstance(candidate.get('sha256'), str) and
                    len(candidate['sha256']) == 64 and
                    isinstance(candidate.get('bytes'), int) and candidate['bytes'] > 0):
                # R2 verified this exact Build content already. No local SDK
                # ZIP is needed on a new BUILDER; only the small release
                # manifest is rebuilt and uploaded after changed applications.
                artifacts[target] = {**candidate, 'remote_only': True}
                print('[TERMINAL PUBLISH] devtools unchanged on R2; '
                      'skipping local ZIP (0 bytes compressed)', flush=True)
                continue
        inputs = {n: _source(builds, n, d, target, verify=True) for n,d in info['files'].items()}
        if target == 'mac':
            # Build supplied a sealed, signed .app; Publish alone builds DMG.
            # Staple a Publish-only copy so Stage never changes signed bytes.
            signed = (builds / 'CinterForgeTerminal/Prepared/MacSigned/MacSignedRelease' /
                      up.APP_ROOTS['mac'])
            if not signed.is_dir() or signed.is_symlink():
                raise RuntimeError('Signed macOS Build app missing before DMG Publish')
            publish_app = signed
            public = notarize and bool(os.environ.get('CINTER_MAC_SIGN_IDENTITY', '').strip())
            if public:
                temporary = builds / 'CinterForgeTerminal/Distribution/PublishMac'
                publish_app = temporary / up.APP_ROOTS['mac']
                if publish_app.exists():
                    if publish_app.is_symlink() or not publish_app.is_dir():
                        raise RuntimeError('Unsafe prior Publish-only Mac app')
                    shutil.rmtree(publish_app)
                publish_app.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(signed, publish_app, symlinks=True)
                up._notarize_mac_app(publish_app, temporary)
            from host.mac_dmg import create_dmg
            artifact = create_dmg(publish_app, path, builds,
                                  volume_name='CinterForge Terminal',
                                  public=public,
                                  notary_profile=os.environ.get('CINTER_MAC_NOTARY_PROFILE', '').strip())
        else:
            artifact = up._write_release_zip(path, inputs, target=target, release=release,
                                             root=up.APP_ROOTS[target])
        artifact['build_fingerprint'] = info['fingerprint']
        if target == 'devtools':
            artifact['archive_identity'] = up._release_archive_identity(path)
        artifacts[target] = artifact
    receipt = {'schema': up.FOUR_SCHEMA, 'release': release, 'artifacts': artifacts,
               'bedrock': 'excluded-until-app-eula-acceptance',
               'native_acceptance': 'separate-target-tests-required'}
    _atomic_json(root / 'four-artifacts.json', receipt)
    return receipt


def setup_unpack_published(builds: Path, *, release: str | None = None) -> dict:
    """Setup-only: verified published DMG/ZIPs => canonical Builds outputs.

    Never called by Build/Stage/Test. The distribution must already be fetched
    by Setup's network phase; checksums and manifests gate every extracted file.
    """
    up = _up()
    release = release or up.HOST_RELEASE
    builds = _safe_builds(builds)
    root = builds / 'CinterForgeTerminal/Distribution'
    _, report, paths = up._distribution_files(builds, release)
    count = {}
    for target in up.FOUR_TARGETS:
        archive = paths[up.artifact_filename(target)]
        if target == 'mac':
            # Only macOS can mount a DMG; cross-platform Setup restores its
            # own toolchains without pretending it installed a Mac app.
            import sys
            if sys.platform != 'darwin':
                count[target] = 0
                continue
            from host.mac_dmg import restore_dmg
            dest = (builds / 'CinterForgeTerminal/Applications/mac/arm64/Release' /
                    up.APP_ROOTS['mac'])
            restore_dmg(archive, builds, app_name=up.APP_ROOTS['mac'], destination=dest)
            count[target] = sum(p.is_file() for p in dest.rglob('*'))
            print(f'[TERMINAL SETUP] restored signed mac app from DMG into Builds', flush=True)
            continue
        members = up._verify_release_zip(archive, target=target, release=release)
        files = members['files']
        if target == 'devtools':
            base = builds
            trim = Path('DevTools/Builds')
        elif target == 'mac':
            base = builds / 'CinterForgeTerminal/Applications/mac/arm64/Release'
            trim = Path('.')
        else:
            base = builds / 'CinterForgeTerminal/Distribution/Unpacked' / target
            trim = Path('.')
        with __import__('zipfile').ZipFile(archive) as source:
            for name, detail in files.items():
                path = Path(name)
                if target == 'devtools':
                    if path.is_relative_to(trim):
                        base = builds
                        rel = path.relative_to(trim)
                    elif path == Path('DevTools/README.txt'):
                        # Supplemental distribution documentation is not a
                        # toolchain. Preserve it in the Setup-only Builds area.
                        base = builds / 'CinterForgeTerminal/Prepared/DevTools'
                        rel = Path('README.txt')
                    else:
                        raise RuntimeError('Unexpected DevTools ZIP layout: ' + name)
                else:
                    rel = path
                dest = base / rel
                if dest.is_symlink() or not dest.resolve().is_relative_to(builds):
                    raise RuntimeError('Unsafe Setup unpack destination: ' + str(dest))
                if dest.is_file() and dest.stat().st_size == detail['bytes'] and _sha(dest) == detail['sha256']:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix='Setup-', dir=dest.parent)
                try:
                    digest = hashlib.sha256()
                    with os.fdopen(fd, 'wb') as out, source.open(name) as inp:
                        for block in iter(lambda: inp.read(4*1024*1024), b''):
                            digest.update(block)
                            out.write(block)
                    if digest.hexdigest() != detail['sha256']:
                        raise RuntimeError('Setup unpack checksum mismatch: ' + name)
                    Path(tmp).chmod(detail['mode'])
                    os.replace(tmp, dest)
                finally:
                    Path(tmp).unlink(missing_ok=True)
        count[target] = len(files)
        print(f'[TERMINAL SETUP] restored {target}: {len(files)} files into Builds', flush=True)
    return {'release': release, 'restored_files': count}


def setup_restore_if_needed(builds: Path, *, release: str | None = None,
                            distribution_url: str = 'https://cinterforge.ucreg.io/CinterForgeTerminal/Distribution') -> dict:
    """Setup-only published-package cache: HTTPS download and extract into Builds.

    No network work occurs when the required producer toolchains already
    exist. A missing/unpublished release falls back to traditional Setup;
    a fetched release with bad checksums fails closed, never silently used.
    """
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    up = _up()
    release = release or up.HOST_RELEASE
    builds = _safe_builds(builds)
    required = (builds / 'CinterForgeTerminal/Toolchains/mac/env/bin/colima',
                builds / 'CinterForgeTerminal/Toolchains/Windows/MSYS2/usr/bin/bash.exe',
                builds / 'CinterForgeTerminal/Toolchains/LinuxSample')
    if all(path.exists() and not path.is_symlink() for path in required):
        return {'source': 'existing-provisioned-Builds', 'downloaded': False}
    destination = builds / 'CinterForgeTerminal/Distribution'
    destination.mkdir(parents=True, exist_ok=True)
    receipt_path = destination / 'four-artifacts.json'
    if receipt_path.is_file() and not receipt_path.is_symlink():
        try:
            if up._distribution_files(builds, release):
                return {'source': 'verified-cache', **setup_unpack_published(builds, release=release)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            pass
    # The four published ZIPs use stable unversioned URLs. Only small manifest
    # is requested until Setup confirms this release actually exists.
    manifest_url = distribution_url.rstrip('/') + '/four-artifacts.json'
    try:
        with urlopen(Request(manifest_url, headers={'Cache-Control': 'no-cache'}), timeout=12) as response:
            raw = response.read(1024 * 1024 + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        print('[TERMINAL SETUP] No published package accessible; provisioning from pinned source: ' + str(error), flush=True)
        return {'source': 'pinned-local-provisioning', 'downloaded': False}
    if len(raw) > 1024 * 1024:
        raise RuntimeError('Oversized published four-artifact manifest')
    report = json.loads(raw)
    if (report.get('schema') != up.FOUR_SCHEMA or report.get('release') != release or
            set(report.get('artifacts', {})) != set(up.FOUR_TARGETS)):
        # A different release is not a valid Setup package for this source.
        print('[TERMINAL SETUP] Published release differs from pinned Build; using source Setup', flush=True)
        return {'source': 'pinned-local-provisioning', 'downloaded': False}
    intermediates = builds / 'Intermediates/CinterForgeTerminal/SetupDownloads'
    intermediates.mkdir(parents=True, exist_ok=True)
    downloads = {}
    for target in up.FOUR_TARGETS:
        entry = report['artifacts'][target]
        name = up.artifact_filename(target)
        if (entry.get('filename') != name or not isinstance(entry.get('sha256'), str)
                or not isinstance(entry.get('bytes'), int) or entry['bytes'] <= 0):
            raise RuntimeError('Unsafe published package metadata: ' + target)
        cached = destination / name
        if cached.is_file() and not cached.is_symlink() and cached.stat().st_size == entry['bytes'] and _sha(cached) == entry['sha256']:
            downloads[target] = cached
            print('[TERMINAL SETUP] Cached ZIP verified: ' + target, flush=True)
            continue
        candidate = intermediates / name
        fd, temporary = tempfile.mkstemp(prefix='Download-', dir=intermediates)
        try:
            os.close(fd)
            digest = hashlib.sha256()
            with urlopen(Request(distribution_url.rstrip('/') + '/' + name), timeout=60) as response, open(temporary, 'wb') as output:
                count = 0
                for block in iter(lambda: response.read(4*1024*1024), b''):
                    count += len(block)
                    if count > entry['bytes']:
                        raise RuntimeError('Published package exceeds advertised size: ' + name)
                    digest.update(block)
                    output.write(block)
            if count != entry['bytes'] or digest.hexdigest() != entry['sha256']:
                raise RuntimeError('Published package SHA-256 verification failed: ' + name)
            Path(temporary).replace(candidate)
            if target == 'mac' and __import__('sys').platform == 'darwin':
                from host.mac_dmg import verify_dmg
                verify_dmg(candidate, builds, app_name=up.APP_ROOTS['mac'])
            else:
                up._verify_release_zip(candidate, target=target, release=release)
            downloads[target] = candidate
            print('[TERMINAL SETUP] Download verified: ' + target, flush=True)
        finally:
            Path(temporary).unlink(missing_ok=True)
    for target, source in downloads.items():
        if source != destination / source.name:
            up._replace_generated_output(source, destination / source.name, builds)
    _atomic_json(receipt_path, report)
    restored = setup_unpack_published(builds, release=release)
    return {'source': manifest_url, 'downloaded': True, **restored}


# Compatibility for callers predating the Mac DMG distribution format.
prepare_publish_zips = prepare_publish_packages
