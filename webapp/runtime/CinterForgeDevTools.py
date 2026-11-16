"""CinterForgeDevTools — CinterForge Terminal application runtime.

PURPOSE: Optional on-demand DevTools download, verification, receipts, installation and development root selection.
METHODS: _devtools_release, _devtools_builds_pin, _devtools_builds_root, _devtools_destination, _legacy_devtools_destination, _save_devtools_builds_root, _public_manifest, _file_sha256, _devtools_download_partial, _download_public_devtools, _stage_devtools_zip, _verify_native_devtools, install_devtools
STARTUP: Loaded by CinterForgeTerminal.py, the sole staged service entry.
SHUTDOWN: The native owner supervises runtime shutdown. Never execute Setup.
DEPENDENCIES: Standard library and directly imported runtime owners; no host/lifecycle imports.
All function bodies and HTTP/CLI interfaces are retained from the Phase 4 source.
"""
from __future__ import annotations
import base64
import hashlib
import ctypes
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import platform
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from typing import Callable
from urllib.parse import unquote, urlsplit, parse_qs, urlparse
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
import zipfile
if os.name != 'nt':
    import fcntl
    import pty
    import select
    import struct
    import termios
from runtime.CinterForgeVM import CinterForgeVM
from runtime.CinterForgeLogging import CinterForgeRuntimeLog
from runtime.CinterForgeCode import CinterForgeCodeWorkspace
from runtime.CinterForgeGuest import CinterForgeGuestSupervisor

from runtime import CinterForgeSettings as _CinterForgeSettings


def _devtools_release() -> str:
    from runtime import CinterForgeSettings as _CinterForgeSettings
    import re
    from pathlib import Path
    value = os.environ.get('CINTER_TERMINAL_RELEASE', '')
    if not value:
        # Staged entry point: <Host>/<platform>/<arch>/<release>/webapp/CinterForgeTerminal.py
        value = _CinterForgeSettings.ASSETS.parent.name
    if not re.fullmatch(r'[0-9]{4}\.[0-9]+\.[A-Za-z0-9._-]+', value):
        value = _CinterForgeSettings.CINTERFORGE_TERMINAL_RELEASE
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value):
        raise RuntimeError('Invalid packaged release identifier')
    return value


def _devtools_builds_pin() -> Path | None:
    """Remember the consented development Builds location across workspaces.

    This file is user configuration in the selected Steam library, not an SDK
    or transient build artifact. Merely checking DevTools must not create it.
    """
    from runtime import CinterForgeSessions as _CinterForgeSessions
    from runtime import CinterForgeSettings as _CinterForgeSettings
    steam = _CinterForgeSessions._steam_library_for_installed_root(_CinterForgeSettings.ASSETS.parent)
    if steam is None:
        return None
    return steam / 'common/CinterForge/Applications/TerminalState/devtools-builds.json'


def _devtools_builds_root() -> Path:
    """Resolve the actual developer Builds location without creating it.

    A downloaded Terminal has no right to create Builds on normal launch.
    `devtools install` is the explicit opt-in which creates that folder.
    Repository development uses the same sibling Builds as ./Cinter.
    """
    from runtime import CinterForgeSessions as _CinterForgeSessions
    supplied = os.environ.get('CINTER_BUILDS_ROOT', '').strip()
    if supplied:
        root = Path(supplied).expanduser()
    else:
        pinned = _devtools_builds_pin()
        if pinned is not None and pinned.is_file():
            if pinned.is_symlink():
                raise RuntimeError('Unsafe DevTools Builds selection')
            try:
                record = json.loads(pinned.read_text(encoding='utf-8'))
                if record.get('schema') != 'cinterforge.devtools-builds.v1':
                    raise ValueError('Unknown DevTools Builds configuration')
                root = Path(record['builds'])
            except (ValueError, KeyError, TypeError, OSError) as error:
                raise RuntimeError('Invalid saved DevTools Builds location: ' + str(pinned)) from error
        else:
            home = _CinterForgeSessions._real_login_home()
            selected = os.environ.get('CINTER_CODE_WORKSPACE', '').strip()
            workspace = Path(selected).expanduser() if selected else None
            if workspace is not None and (not workspace.is_absolute() or
                                          not workspace.is_dir()):
                raise RuntimeError('Selected Code-OSS workspace is not an existing absolute folder')
            if workspace is None:
                base = (home / 'Source' if (home / 'Source').is_dir() else
                        home / 'Documents' if (home / 'Documents').is_dir() else home)
            else:
                workspace = workspace.resolve(strict=True)
                if workspace.name == 'Builds':
                    base = workspace.parent
                elif (workspace / 'Cinter').is_file() and (workspace / 'CMakeLists.txt').is_file():
                    base = workspace.parent  # the repository's sibling Builds
                elif (home / 'Source').is_dir() and workspace.is_relative_to(home / 'Source'):
                    base = home / 'Source'
                elif (home / 'Documents').is_dir() and workspace.is_relative_to(home / 'Documents'):
                    base = home / 'Documents'
                else:
                    base = workspace
            root = base / 'Builds'
    if not root.is_absolute() or root.name != 'Builds':
        raise RuntimeError('Developer toolchains require an absolute Builds directory: ' + str(root))
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise RuntimeError('Unsafe developer Builds location: ' + str(root))
    resolved = root.resolve(strict=False)
    # Never extract DevTools into the immutable app, a Steam library, or a
    # packaged subdirectory merely named Builds.
    if any(part.lower() == 'steamapps' or part.endswith('.app')
           for part in resolved.parts):
        raise RuntimeError('DevTools Builds must be outside Steam and signed apps: ' + str(resolved))
    return resolved


def _devtools_destination(release: str) -> Path:
    """Small installation receipt; tools themselves go in canonical Builds."""
    return _devtools_builds_root() / 'CinterForgeTerminal/DevTools' / release


def _staged_devtools_destination(release: str) -> Path | None:
    """Resolve the exact unified SDK installed by ``./Cinter stage all``.

    Stage places its complete, hashed DevTools payload alongside the platform
    apps, under the *same* Steam library. This is a read-only lookup; normal
    application launch must not download tools, create Builds, or copy an SDK.
    """
    from runtime import CinterForgeSessions as _CinterForgeSessions
    from runtime import CinterForgeSettings as _CinterForgeSettings
    steam = _CinterForgeSessions._steam_library_for_installed_root(_CinterForgeSettings.ASSETS.parent)
    if steam is None:
        return None
    return steam / 'common/CinterForge/Applications/DevTools/CinterForge Terminal' / release


def _legacy_devtools_destination(release: str) -> Path | None:
    """Read only: previously installed Steam SDKs remain usable until moved."""
    from runtime import CinterForgeSessions as _CinterForgeSessions
    from runtime import CinterForgeSettings as _CinterForgeSettings
    steam = _CinterForgeSessions._steam_library_for_installed_root(_CinterForgeSettings.ASSETS.parent)
    if steam is None:
        return None
    return steam / 'common/CinterForge/Applications/DevTools/CinterForge Terminal' / release


def _save_devtools_builds_root() -> None:
    """Persist the selected destination only after a verified installation."""
    pin = _devtools_builds_pin()
    if pin is None:
        return  # explicit developer source tree; no installed-app preference
    if pin.is_symlink() or (pin.exists() and not pin.is_file()):
        raise RuntimeError('Unsafe DevTools Builds selection file: ' + str(pin))
    pin.parent.mkdir(parents=True, exist_ok=True)
    if pin.parent.is_symlink():
        raise RuntimeError('Unsafe DevTools configuration directory')
    import tempfile
    fd, name = tempfile.mkstemp(prefix='devtools-builds-', suffix='.tmp', dir=pin.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'schema': 'cinterforge.devtools-builds.v1',
                       'builds': str(_devtools_builds_root())}, stream, indent=2)
            stream.write('\n')
        os.replace(name, pin)
    finally:
        Path(name).unlink(missing_ok=True)


def _public_manifest(release: str) -> tuple[dict, str]:
    """Read the public release manifest, tolerating short CDN visibility delays.

    The user has not approved a download at this point: this GET is only a
    small release manifest. It never fetches the multi-GB DevTools archive.
    """
    from runtime import CinterForgeSettings as _CinterForgeSettings
    import re
    import time
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    url = f'{_CinterForgeSettings.PUBLIC_DISTRIBUTION}/{release}/four-artifacts.json'
    headers = {'Accept': 'application/json', 'Cache-Control': 'no-cache',
               'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/131.0.0.0 Safari/537.36')}
    retry_delays = (2, 4, 8, 12, 12)
    for index in range(len(retry_delays) + 1):
        try:
            with urlopen(Request(url, headers=headers), timeout=40) as stream:
                if getattr(stream, 'status', 200) != 200:
                    raise RuntimeError('Public DevTools manifest did not return HTTP 200')
                raw = stream.read(1024 * 1024 + 1)
            break
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as exc:
            if index == len(retry_delays):
                raise RuntimeError(f'Public DevTools manifest unavailable at {url} after '
                                   f'{index + 1} checks: {exc}') from exc
            print(f'[DEVTOOLS] Public manifest not ready ({exc}); retrying in '
                  f'{retry_delays[index]}s, no package downloaded', flush=True)
            time.sleep(retry_delays[index])
    if len(raw) > 1024 * 1024:
        raise RuntimeError('Public release manifest is unexpectedly large')
    data = json.loads(raw)
    if (data.get('schema') != _CinterForgeSettings.FOUR_SCHEMA or data.get('release') != release or
            not isinstance(data.get('artifacts'), dict) or
            set(data['artifacts']) != {'mac', 'linux', 'win', 'devtools'}):
        raise RuntimeError('Public release manifest does not identify all four bundles')
    entry = data['artifacts']['devtools']
    if (entry.get('filename') != f'CinterForgeTerminal-devtools-{release}.zip' or
            not isinstance(entry.get('bytes'), int) or entry['bytes'] <= 0 or
            not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256', '')))):
        raise RuntimeError('Public DevTools release manifest is invalid')
    return entry, url


def _file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _devtools_download_partial(release: str, filename: str) -> Path:
    """Store multi-GB resumable downloads outside installed Steam libraries.

    Only the verified, unpacked DevTools distribution is an installed app;
    incomplete ZIPs are disposable OS-cache content.
    """
    from runtime import CinterForgeSessions as _CinterForgeSessions
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', filename):
        raise ValueError('Unsafe DevTools download filename')
    return (_CinterForgeSessions._installed_cache_root(_CinterForgeSessions._real_login_home()) / 'Downloads' / release /
            (filename + '.partial'))


def _download_public_devtools(url: str, partial, *, length: int, digest: str):
    """Download a verified DevTools ZIP using bounded HTTPS ranges and retries.

    The public archive is several GiB. A single CDN/TLS stream may be severed
    before the transfer completes, so transfer 64 MiB at a time and resume from
    the last fully written byte after a transient disconnect. Preserve an
    incomplete .partial file across sessions; never extract unverified bytes.
    """
    import time
    from http.client import HTTPException
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlsplit
    from urllib.request import Request, urlopen

    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'cinterforge.ucreg.io':
        raise RuntimeError('DevTools must come from the approved public Cloudflare host')
    partial.parent.mkdir(parents=True, exist_ok=True)
    if partial.is_symlink():
        raise RuntimeError('Unsafe DevTools download target')
    current = partial.stat().st_size if partial.is_file() else 0
    if current > length:
        partial.unlink()
        current = 0
    if current == length:
        if _file_sha256(partial) == digest:
            print('[DEVTOOLS] previously downloaded package verified', flush=True)
            return
        partial.unlink()
        current = 0
    if current:
        print(f'[DEVTOOLS] resuming saved download at '
              f'{current / 1073741824:.2f}/{length / 1073741824:.2f} GiB', flush=True)

    window = 64 * 1024 * 1024
    consecutive_failures = 0
    max_failures = 12
    last_report = time.monotonic()
    while current < length:
        requested_end = min(current + window, length) - 1
        headers = {'User-Agent': 'CinterForgeTerminal-devtools/1',
                   'Accept-Encoding': 'identity',
                   'Range': f'bytes={current}-{requested_end}'}
        try:
            with urlopen(Request(url, headers=headers), timeout=90) as source:
                status = getattr(source, 'status', 200)
                if status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',
                                         source.headers.get('Content-Range', '').strip())
                    if (not match or int(match[1]) != current or
                            int(match[2]) < current or int(match[2]) > requested_end or
                            int(match[3]) != length):
                        raise RuntimeError('Public DevTools CDN returned an invalid byte range; '
                                           'saved download was not modified')
                    stop = int(match[2]) + 1
                elif status == 200 and current == 0:
                    # A CDN that ignores Range can still deliver one full stream,
                    # but a later disconnect cannot safely resume on that CDN.
                    stop = length
                    print('[DEVTOOLS] CDN ignored Range; streaming full archive', flush=True)
                else:
                    raise RuntimeError(f'Public DevTools CDN did not honor resume '
                                       f'(HTTP {status}); saved download retained')
                with partial.open('ab') as output:
                    while current < stop:
                        block = source.read(min(4 * 1024 * 1024, stop - current))
                        if not block:
                            raise ConnectionError('Remote stream closed before the requested bytes arrived')
                        output.write(block)
                        current += len(block)
                        now = time.monotonic()
                        if now - last_report >= 2 or current == length:
                            print(f'[DEVTOOLS] download {100 * current / length:.1f}% '
                                  f'({current / 1073741824:.2f}/{length / 1073741824:.2f} GiB)',
                                  flush=True)
                            last_report = now
            consecutive_failures = 0
        except HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504):
                raise RuntimeError(f'Public DevTools download HTTP {error.code}; '
                                   f'saved download retained at {partial}') from error
            failure = error
        except (URLError, HTTPException, OSError, TimeoutError) as error:
            # Includes BrokenPipeError, ConnectionResetError and incomplete TLS reads.
            failure = error
        else:
            continue
        # Read failures can happen after writing part of the requested range.
        # Recompute the actual durable offset before starting the next request.
        current = partial.stat().st_size if partial.is_file() else 0
        consecutive_failures += 1
        if consecutive_failures > max_failures:
            raise RuntimeError('Public DevTools download repeatedly disconnected; '
                               f'{current / 1073741824:.2f} GiB retained at {partial}. '
                               'The next approved INSTALL resumes this file.') from failure
        delay = min(2 ** min(consecutive_failures - 1, 4), 16)
        print(f'[DEVTOOLS] connection interrupted ({type(failure).__name__}: {failure}); '
              f'resuming from {current / 1073741824:.2f} GiB in {delay}s '
              f'({consecutive_failures}/{max_failures})', flush=True)
        time.sleep(delay)

    if partial.stat().st_size != length or _file_sha256(partial) != digest:
        # Never stage an archive that differs from the signed-release manifest.
        partial.unlink(missing_ok=True)
        raise RuntimeError('Public DevTools ZIP size/SHA-256 mismatch; invalid partial discarded')
    print('[DEVTOOLS] download 100% — verified published SHA-256', flush=True)


def _stage_devtools_zip(archive_path, destination, release: str, artifact: dict):
    from runtime import CinterForgeSettings as _CinterForgeSettings
    import hashlib
    import os as _os
    from pathlib import Path, PurePosixPath
    import stat
    import tempfile
    import time
    import zipfile
    if destination.is_symlink():
        raise RuntimeError('Unsafe DevTools destination redirect')
    builds = destination.parents[2]
    if builds.name != 'Builds' or builds.is_symlink():
        raise RuntimeError('DevTools must be extracted into the developer Builds directory')
    with zipfile.ZipFile(archive_path) as archive:
        contents = archive.namelist()
        if len(set(contents)) != len(contents) or 'release-manifest.json' not in contents:
            raise RuntimeError('DevTools ZIP has duplicate entries or no manifest')
        meta = json.loads(archive.read('release-manifest.json'))
        files = meta.get('files')
        if (meta.get('schema') != _CinterForgeSettings.FOUR_SCHEMA or meta.get('release') != release or
                meta.get('target') != 'devtools' or meta.get('root') != 'DevTools' or
                not isinstance(files, dict) or set(contents) != set(files) | {'release-manifest.json'}):
            raise RuntimeError('DevTools ZIP member manifest invalid')
        for name, info in files.items():
            parts = PurePosixPath(name).parts
            # README is an authored root-level member of the published ZIP,
            # generated by _devtools_assets. It is not a traversal or SDK path.
            if (not isinstance(info, dict) or
                    (name != 'DevTools/README.txt' and
                     (len(parts) < 4 or parts[:2] != ('DevTools', 'Builds'))) or
                    name.startswith('/') or '\\' in name or '..' in parts or '.' in parts or
                    archive.getinfo(name).is_dir() or
                    stat.S_IFMT(archive.getinfo(name).external_attr >> 16) == stat.S_IFLNK or
                    not isinstance(info.get('bytes'), int) or info['bytes'] < 0 or
                    info.get('mode') not in (0o644, 0o755) or
                    ((archive.getinfo(name).external_attr >> 16) & 0o777) != info.get('mode') or
                    not isinstance(info.get('sha256'), str) or len(info['sha256']) != 64 or
                    archive.getinfo(name).file_size != info['bytes'] or
                    '/Servers/Bedrock/' in name):
                raise RuntimeError('Unsafe DevTools ZIP member: ' + name)
        total = sum(x['bytes'] for x in files.values())
        completed = 0
        last = time.monotonic()
        for name, info in files.items():
            # The published ZIP has a portable DevTools/Builds wrapper.
            # Strip it: the real destination is <workspace>/Builds/<source path>.
            parts = PurePosixPath(name).parts
            item = (destination / 'README.txt' if name == 'DevTools/README.txt' else
                    builds.joinpath(*parts[2:]))
            for parent in [builds, destination, *reversed(list(item.parents)[:-1])]:
                # Every parent in the final destination must remain real.
                if parent.is_symlink():
                    raise RuntimeError('Unsafe DevTools install symlink: ' + str(parent))
            item.parent.mkdir(parents=True, exist_ok=True)
            if item.is_symlink():
                raise RuntimeError('Unsafe DevTools symlink redirect: ' + str(item))
            if item.is_dir():
                # Build-owned file paths can be obstructed by an old directory.
                # Preserve the previous directory; never delete user-authored content.
                import uuid
                backup = destination.parent / 'Previous DevTools Conflicts' / uuid.uuid4().hex / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                item.replace(backup)
            fd, temp_path = tempfile.mkstemp(prefix='Installing-', dir=item.parent)
            _os.close(fd)
            temp = Path(temp_path)
            try:
                h = hashlib.sha256()
                size = 0
                with archive.open(name) as src, temp.open('wb') as dst:
                    for chunk in iter(lambda: src.read(4 * 1024 * 1024), b''):
                        dst.write(chunk)
                        size += len(chunk)
                        h.update(chunk)
                if size != info['bytes'] or h.hexdigest() != info['sha256']:
                    raise RuntimeError('DevTools installed member failed SHA-256: ' + name)
                temp.chmod(info['mode'])
                temp.replace(item)
            finally:
                temp.unlink(missing_ok=True)
            completed += info['bytes']
            now = time.monotonic()
            if now - last >= 2:
                print(f'[DEVTOOLS] install {100 * completed / total:.1f}% '
                      f'({completed / 1073741824:.2f}/{total / 1073741824:.2f} GiB)', flush=True)
                last = now
    destination.mkdir(parents=True, exist_ok=True)
    receipt = {'schema': _CinterForgeSettings.FOUR_SCHEMA, 'release': release, 'target': 'devtools',
               'artifact': artifact, 'source': 'public', 'bedrock': 'excluded-until-app-eula-acceptance'}
    temp_receipt = destination / 'stage-receipt.pending'
    temp_receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    temp_receipt.replace(destination / 'stage-receipt.json')
    print('[DEVTOOLS] install 100% — all ZIP members verified', flush=True)


def _verify_native_devtools(destination) -> bool:
    """Actually execute local tools where possible; never invent probe success."""
    import platform
    import subprocess
    targets = []
    root = destination.parents[2] / 'CinterForgeTerminal/Toolchains'
    if platform.system() == 'Darwin':
        for name in ('cmake', 'ninja', 'python3', 'node'):
            targets.append(root / 'mac/env/bin' / name)
    elif platform.system() == 'Windows':
        targets.append(root / 'Windows/MSYS2/usr/bin/bash.exe')
    else:
        for name in ('gcc', 'cmake', 'ninja'):
            candidate = root / 'LinuxSample/bin' / name
            if candidate.is_file():
                targets.append(candidate)
    if not targets:
        print('[DEVTOOLS] No directly runnable host tools in archive; OCI toolchain acceptance requires its native target runner.', flush=True)
        return True
    complete = True
    for exe in targets:
        if not exe.is_file():
            print(f'[DEVTOOLS] version check unavailable: {exe}', flush=True)
            complete = False
            continue
        try:
            run = subprocess.run([str(exe), '--version'], text=True, capture_output=True,
                                 check=False, timeout=30)
            if run.returncode:
                raise RuntimeError(run.stderr.strip() or f'exit={run.returncode}')
            print(f'[DEVTOOLS] version check {exe.name}: '
                  f'{(run.stdout or run.stderr).strip().splitlines()[0]}', flush=True)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, IndexError) as error:
            print(f'[DEVTOOLS] version check FAILED {exe}: {error}', flush=True)
            complete = False
    return complete


def install_devtools() -> int:
    """User-initiated public download only; never run during Terminal startup."""
    from runtime import CinterForgeCommands as _CinterForgeCommands
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    from runtime import CinterForgeSettings as _CinterForgeSettings
    import zipfile
    from pathlib import Path
    release = _devtools_release()
    destination = _devtools_destination(release)
    receipt_file = destination / 'stage-receipt.json'
    if receipt_file.is_file() and not receipt_file.is_symlink():
        # The installed-receipt check must agree with the Windows/Linux guest
        # launcher. Directory presence alone wrongly treated partial or
        # manually deleted SDKs as installed and skipped the consent dialog.
        try:
            sdk = _CinterForgeRuntimeBootstrap.sdk_root()
        except (ImportError, OSError, ValueError, RuntimeError):
            sdk = None
        if sdk is not None and sdk == destination.parents[2] / 'CinterForgeTerminal/Toolchains':
            print(f'[DEVTOOLS] already staged in Builds: {destination.parents[2]}', flush=True)
            return 0 if _verify_native_devtools(destination) else 1
        print('[DEVTOOLS] Existing installation incomplete; reinstall requires your consent.', flush=True)
    artifact, manifest = _public_manifest(release)
    url = f'{_CinterForgeSettings.PUBLIC_DISTRIBUTION}/{release}/{artifact["filename"]}'
    print('Unified macOS + Linux + Windows developer toolchains:', flush=True)
    print(f'  Public release: {manifest}', flush=True)
    print(f'  Package: {url}', flush=True)
    print(f'  Download: {artifact["bytes"] / 1073741824:.2f} GiB', flush=True)
    print(f'  Developer Builds: {destination.parents[2]}', flush=True)
    print(f'  SDK files: {destination.parents[2] / "CinterForgeTerminal/Toolchains"}', flush=True)
    print(f'  Installation receipt: {destination / "stage-receipt.json"}', flush=True)
    print('  Builds will be created here after confirmation if it does not exist.', flush=True)
    if input('Type INSTALL to confirm download and installation: ').strip() != 'INSTALL':
        print('[DEVTOOLS] canceled; nothing downloaded', flush=True)
        return 1
    pending = _devtools_download_partial(release, artifact['filename'])
    try:
        _download_public_devtools(url, pending, length=artifact['bytes'], digest=artifact['sha256'])
        _stage_devtools_zip(pending, destination, release, artifact)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        print(f'[DEVTOOLS] installation failed: {type(error).__name__}: {error}', flush=True)
        print('[DEVTOOLS] Mac Session remains open; retry INSTALL to resume any saved download.',
              flush=True)
        return 1
    # Keep the already downloaded, verified archive until the installed SDK
    # has passed its local tool checks. Otherwise a failed version probe forces
    # another multi-GiB network download even though the ZIP is still valid.
    if not _verify_native_devtools(destination):
        print('[DEVTOOLS] Installed files retained, as is the verified ZIP for '
              'a local repair; no re-download is needed.', flush=True)
        return 1
    _save_devtools_builds_root()
    pending.unlink(missing_ok=True)
    print(f'[DEVTOOLS] installed into Builds: {destination.parents[2]}', flush=True)
    # A completed optional install changes startup capabilities. Relaunch the
    # existing native owner so its single boot supervisor checks both shells
    # BEFORE it displays the normal UI. Do not boot Colima in this PTY worker.
    try:
        _CinterForgeCommands._api('/api/lifecycle/reboot', body={'component': 'app'})
        print('[DEVTOOLS] Relaunching CinterForge Terminal to initialize DevTools...', flush=True)
    except (OSError, RuntimeError, ValueError) as error:
        print('[DEVTOOLS] Installed, but automatic relaunch failed: ' + str(error) +
              '; relaunch Terminal normally to initialize the guests.', flush=True)
        return 1
    return 0

