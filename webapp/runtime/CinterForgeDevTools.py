"""CinterForgeDevTools — CinterForge Terminal application runtime.

PURPOSE: Optional on-demand DevTools download, verification, receipts, installation and development root selection.
METHODS: _devtools_release, _devtools_builds_pin, _devtools_builds_root, _devtools_destination, _save_devtools_builds_root, _public_manifest, _file_sha256, _devtools_download_partial, _download_public_devtools, _stage_devtools_zip, _verify_native_devtools, install_devtools
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
    """One installed-app Source selection, never a home-cache fallback.

    The developer command uses CINTER_BUILDS_ROOT directly and has no pin.
    Installed Terminal requires its actual Steam library's one selection file.
    No lookup or copying of alternate DevTools/Toolchains installations.
    """
    if os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'developer':
        return None
    from runtime import CinterForgeSessions as _CinterForgeSessions
    from runtime import CinterForgeSettings as _CinterForgeSettings
    steam = _CinterForgeSessions._steam_library_for_installed_root(_CinterForgeSettings.ASSETS.parent)
    if steam is None:
        raise RuntimeError('Installed DevTools require the actual Steam application library; '
                           'no alternate user-cache installation is supported')
    return steam / 'common/CinterForge/Applications/TerminalState/devtools-builds.json'


def _devtools_builds_root() -> Path:
    """One explicit location: developer Builds or the installed Source pin.

    No scanning HOME/Source, Documents, Code-OSS workspace, Steam SDKs, or
    packaged application resources. A missing selection means not installed.
    """
    raw = os.environ.get('CINTER_BUILDS_ROOT', '').strip()
    if os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'developer':
        if not raw:
            raise RuntimeError('Developer CINTER_BUILDS_ROOT is not set')
        root = Path(raw).expanduser()
    elif raw:
        # An explicit process environment override selects exactly one Builds.
        # There is never a search through other installed toolchain locations.
        root = Path(raw).expanduser()
    else:
        pin = _devtools_builds_pin()
        if pin is None or pin.is_symlink() or not pin.is_file():
            raise RuntimeError('No DevTools Source selected: run devtools install <path/to/source>')
        try:
            record = json.loads(pin.read_text(encoding='utf-8'))
            if record.get('schema') != 'cinterforge.devtools-builds.v1':
                raise ValueError('Invalid selection schema')
            root = Path(record['builds'])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RuntimeError('Invalid DevTools Source selection: ' + str(pin)) from error
    if (not root.is_absolute() or root.name != 'Builds' or root.is_symlink() or
            not root.is_dir()):
        raise RuntimeError('Selected Builds directory is missing or unsafe: ' + str(root))
    root = root.resolve(strict=True)
    if any(part.lower() == 'steamapps' or part.endswith('.app') for part in root.parts):
        raise RuntimeError('DevTools Builds must be outside Steam and app bundles: ' + str(root))
    # Installed apps inherit the selection from their saved user configuration.
    # Export the canonical environment key for the entire runtime (including
    # sessions and managed-VM helpers) after checking the ONE selected root.
    os.environ['CINTER_BUILDS_ROOT'] = str(root)
    return root


def _builds_from_source(source_path: str) -> Path:
    """Explicit, existing Source directory; never silently choose a workspace."""
    source = Path(source_path).expanduser()
    if not source.is_absolute() or not source.is_dir() or source.is_symlink():
        raise ValueError('devtools install requires an existing absolute Source directory')
    source = source.resolve(strict=True)
    if source.name == 'Builds' or any(part.lower() == 'steamapps' or part.endswith('.app')
                                     for part in source.parts):
        raise ValueError('Select the Source directory, outside Steam and application bundles')
    builds = source / 'Builds'
    if builds.is_symlink() or (builds.exists() and not builds.is_dir()):
        raise ValueError('Unsafe existing Builds directory: ' + str(builds))
    if (builds / 'Toolchains').is_symlink():
        raise ValueError('Unsafe Toolchains redirect: ' + str(builds / 'Toolchains'))
    return builds


def _setup_receipt_verifies_toolchains(builds: Path) -> bool:
    """Recognize Terminal Setup's fully completed shared toolchain closure.

    Setup creates this receipt only after provisioning and verifying its
    dependencies. This is read-only: installed app launch never repairs or
    creates Builds. Runtime also checks its actual required executables.
    """
    receipt = builds / 'Intermediates/CinterForgeTerminal/SetupCache/all.json'
    if receipt.is_symlink() or not receipt.is_file():
        return False
    try:
        record = json.loads(receipt.read_text(encoding='utf-8'))
        if not isinstance(record, dict):
            return False
        required = ('Toolchains/mac', 'Toolchains/WindowsCross',
                    'Toolchains/Windows', 'Toolchains/WebView', 'Toolchains/WebView2')
        outputs = record.get('outputs')
        if (record.get('schema') != 'cinterforge.terminal.setup-dirty.v1' or
                record.get('scope') != 'all' or
                record.get('builds') != str(builds.resolve(strict=True)) or
                not isinstance(outputs, dict)):
            return False
        return all((builds / name).is_dir() and not (builds / name).is_symlink() and
                   isinstance(outputs.get(name), dict) and
                   isinstance(outputs[name].get('fingerprint'), str) and
                   len(outputs[name]['fingerprint']) == 64 and
                   isinstance(outputs[name].get('files'), int) and
                   outputs[name]['files'] > 0 for name in required)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _devtools_destination(release: str) -> Path:
    """Small installation receipt; tools themselves go in canonical Builds."""
    root = _devtools_builds_root() / 'CinterForgeTerminal/DevTools'
    current = root / 'current'
    legacy = root / release
    return legacy if not current.exists() and (legacy / 'stage-receipt.json').is_file() else current


def _save_devtools_builds_root(builds: Path) -> None:
    """Persist the selected destination only after a verified installation."""
    pin = _devtools_builds_pin()
    if pin is None:
        os.environ['CINTER_BUILDS_ROOT'] = str(builds)
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
                       'builds': str(builds)}, stream, indent=2)
            stream.write('\n')
        os.replace(name, pin)
        # A child CLI process cannot mutate its parent's environment. The pin
        # above is therefore the durable handoff for the NEXT app launch.
        # Set this process's CINTER_BUILDS_ROOT too for subsequent commands.
        os.environ['CINTER_BUILDS_ROOT'] = str(builds)
    finally:
        Path(name).unlink(missing_ok=True)


def _public_manifest(release: str, *, distribution: str | None = None) -> tuple[dict, str]:
    """Read the public release manifest, tolerating short CDN visibility delays.

    The user has not approved a download at this point: this GET is only a
    small release manifest. It never fetches the multi-GB DevTools archive.
    """
    from runtime import CinterForgeSettings as _CinterForgeSettings
    import re
    import time
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    url = f'{distribution or _CinterForgeSettings.PUBLIC_DISTRIBUTION}/devtools-artifacts.json'
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
    name = 'CinterForgeTerminal-devtools.zip'
    sdk_release = data.get('release')
    if (data.get('schema') != 'cinterforge.terminal.scoped-distribution.v1' or
            data.get('target') != 'devtools' or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', str(sdk_release or '')) or
            not isinstance(data.get('artifacts'), dict) or
            set(data['artifacts']) != {name} or not isinstance(data['artifacts'][name], dict)):
        raise RuntimeError('Independent DevTools manifest is invalid')
    entry = dict(data['artifacts'][name], filename=name, release=sdk_release)
    if (not isinstance(entry.get('bytes'), int) or entry['bytes'] <= 0 or
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


def _devtools_download_partial(builds: Path, release: str, filename: str) -> Path:
    """One download location inside selected Builds/Toolchains, never OS cache."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', filename):
        raise ValueError('Unsafe DevTools download filename')
    return builds / 'Toolchains/Downloads' / release / (filename + '.partial')


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
    installed_members = {}
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
                     (len(parts) < 4 or parts[:3] != ('DevTools', 'Builds', 'Toolchains'))) or
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
            # Avoid rewriting multi-GB unchanged toolchains and invalidating
            # Ninja/Setup incremental-build timestamps on every update.
            if (item.is_file() and not item.is_symlink() and
                    item.stat().st_size == info['bytes'] and
                    _file_sha256(item) == info['sha256']):
                if (item.stat().st_mode & 0o777) != info['mode']:
                    item.chmod(info['mode'])
                installed_members[name] = info['sha256']
                completed += info['bytes']
                continue
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
            installed_members[name] = info['sha256']
            completed += info['bytes']
            now = time.monotonic()
            if now - last >= 2:
                print(f'[DEVTOOLS] install {100 * completed / total:.1f}% '
                      f'({completed / 1073741824:.2f}/{total / 1073741824:.2f} GiB)', flush=True)
                last = now
    # Do not publish an installation receipt for an SDK whose host-native
    # binaries cannot execute. Runtime readiness must never trust that state.
    if not _verify_native_devtools(destination):
        raise RuntimeError('DevTools native binary verification failed')
    destination.mkdir(parents=True, exist_ok=True)
    receipt = {'schema': _CinterForgeSettings.FOUR_SCHEMA, 'release': release, 'target': 'devtools',
               'artifact': artifact, 'source': 'public', 'files': installed_members,
               'bedrock': 'excluded-until-app-eula-acceptance'}
    temp_receipt = destination / 'stage-receipt.pending'
    temp_receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    temp_receipt.replace(destination / 'stage-receipt.json')
    print('[DEVTOOLS] install 100% — all ZIP members verified', flush=True)


def _verify_native_devtools(destination) -> bool:
    """Actually execute local tools where possible; never invent probe success."""
    import platform
    import subprocess
    targets = []
    root = destination.parents[2] / 'Toolchains'
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


def _verify_installed_members(builds: Path, destination: Path, record: dict) -> bool:
    """Check every receipt-owned byte, not just a few executable probes.

    A stale receipt must not cause an interrupted update, missing MSYS2 DLL or
    corrupted OCI archive to be advertised as current. This read-only check is
    used only for an explicit install/update command, not at app startup.
    """
    from pathlib import PurePosixPath
    files = record.get('files')
    if not isinstance(files, dict) or not files:
        return False
    for name, expected_sha in files.items():
        parts = PurePosixPath(name).parts
        if name == 'DevTools/README.txt':
            path = destination / 'README.txt'
        elif (len(parts) >= 4 and parts[:3] == ('DevTools', 'Builds', 'Toolchains') and
              '..' not in parts and '.' not in parts and '\\' not in name):
            path = builds.joinpath(*parts[2:])
        else:
            return False
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            return False
        parent = path.parent
        while parent != builds.parent:
            if parent.is_symlink():
                return False
            if parent == parent.parent:
                return False
            parent = parent.parent
        if not path.is_file() or path.is_symlink() or _file_sha256(path) != expected_sha:
            return False
    return True


def _activate_selected_devtools() -> None:
    """Apply a new source selection to the native HTTP owner.

    CLI commands run in child processes; their environment changes cannot
    reach an already-running native owner. The durable Builds pin is read on
    restart, where CINTER_BUILDS_ROOT is exported before guest startup.
    """
    from runtime import CinterForgeCommands as commands
    try:
        commands._api('/api/lifecycle/reboot', body={'component': 'app'})
    except (OSError, RuntimeError, ValueError) as error:
        print('[DEVTOOLS] Source selected; relaunch Terminal to activate: ' +
              str(error), flush=True)


def install_devtools(source_path: str | None = None, *, update: bool = False) -> int:
    """Explicit workspace install or pinned-workspace update from Cloudflare."""
    from runtime import CinterForgeSettings as _CinterForgeSettings
    release = _devtools_release()
    if update:
        if source_path is not None:
            raise ValueError('devtools update uses the previously installed Source directory')
        if os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') != 'developer':
            pin = _devtools_builds_pin()
            if pin is None or not pin.is_file() or pin.is_symlink():
                raise RuntimeError('No selected Source directory; first run devtools install <path/to/source>')
        builds = _devtools_builds_root()
    else:
        if not source_path:
            raise ValueError('Usage: devtools install <path/to/source>')
        builds = _builds_from_source(source_path)
    destination = builds / 'CinterForgeTerminal/DevTools' / 'current'
    receipt_file = destination / 'stage-receipt.json'
    # An existing successful ./Cinter setup all IS an installed local SDK.
    # Selecting it must not require a separate DevTools download/receipt.
    if (not update and _setup_receipt_verifies_toolchains(builds) and
            _verify_native_devtools(destination)):
        _save_devtools_builds_root(builds)
        print('[DEVTOOLS] selected existing Setup-verified toolchains: ' +
              str(builds / 'Toolchains'), flush=True)
        _activate_selected_devtools()
        return 0
    # Always check published metadata on explicit update; no install is
    # considered current merely because the receipt file exists.
    artifact, manifest = _public_manifest(release)
    url = f'{_CinterForgeSettings.PUBLIC_DISTRIBUTION}/{artifact["filename"]}'
    previous = {}
    if receipt_file.is_file() and not receipt_file.is_symlink():
        try:
            previous = json.loads(receipt_file.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            previous = {}
    previous_artifact = previous.get('artifact')
    if (isinstance(previous_artifact, dict) and
            previous_artifact.get('sha256') == artifact['sha256'] and
            previous.get('source') == 'public' and
            (builds / 'Toolchains').is_dir() and
            _verify_installed_members(builds, destination, previous) and
            _verify_native_devtools(destination)):
        _save_devtools_builds_root(builds)
        print(f'[DEVTOOLS] current: {builds / "Toolchains"} ({release})', flush=True)
        _activate_selected_devtools()
        return 0
    print(f'[DEVTOOLS] {"Update" if update else "Install"} unified macOS/Linux/Windows toolchains:', flush=True)
    print(f'  Source: {builds.parent}', flush=True)
    print(f'  Toolchains: {builds / "Toolchains"}', flush=True)
    print(f'  Public manifest: {manifest}', flush=True)
    print(f'  Package: {url}', flush=True)
    print(f'  Transfer size: {artifact["bytes"] / 1073741824:.2f} GiB', flush=True)
    if input('Type INSTALL to confirm: ').strip() != 'INSTALL':
        print('[DEVTOOLS] canceled; no files changed', flush=True)
        return 1
    pending = _devtools_download_partial(builds, artifact['release'], artifact['filename'])
    try:
        _download_public_devtools(url, pending, length=artifact['bytes'], digest=artifact['sha256'])
        _stage_devtools_zip(pending, destination, artifact['release'], artifact)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        print(f'[DEVTOOLS] install failed: {error}; verified download retained for repair', flush=True)
        return 1
    _save_devtools_builds_root(builds)
    pending.unlink(missing_ok=True)
    print(f'[DEVTOOLS] ready: {builds / "Toolchains"}', flush=True)
    _activate_selected_devtools()
    return 0


def _selected_devtools_receipt() -> tuple[Path, Path, dict]:
    builds = _devtools_builds_root()
    destination = _devtools_destination(_devtools_release())
    receipt = destination / 'stage-receipt.json'
    if receipt.is_symlink() or not receipt.is_file():
        if _setup_receipt_verifies_toolchains(builds):
            return builds, destination, {'source': 'setup', 'release': _devtools_release()}
        raise RuntimeError('DevTools installation missing at ' + str(builds / 'Toolchains'))
    record = json.loads(receipt.read_text(encoding='utf-8'))
    if record.get('target') != 'devtools':
        raise RuntimeError('Invalid DevTools receipt: ' + str(receipt))
    return builds, destination, record


def version_devtools() -> int:
    builds, _destination, record = _selected_devtools_receipt()
    print('DevTools: ' + str(record.get('release', 'unknown')))
    print('Source: ' + str(record.get('source', 'unknown')))
    print('Toolchains: ' + str(builds / 'Toolchains'))
    print('SHA-256: ' + str(record.get('artifact', {}).get('sha256', 'Setup-verified')))
    return 0


def _media_tool_inventory(builds: Path) -> list[tuple[str, str, str]]:
    """Read-only inventory of staged developer tools, never launches a guest."""
    tools = builds / 'Toolchains'
    mac = tools / 'mac/env/bin'
    windows = tools / 'Windows/MSYS2/ucrt64/bin'
    entries = []
    from host.devtools.assimp_library import prebuilt_assimp
    try:
        assimp = prebuilt_assimp(tools / 'mac/env')
        assimp_location = str(assimp['path'])
    except (OSError, RuntimeError):
        assimp_location = 'MISSING'
    entries.append(('macOS ARM64', 'Assimp SDK', assimp_location))
    for label, filename in (('FFmpeg', 'ffmpeg'),
                             ('FFprobe', 'ffprobe'), ('ImageMagick', 'magick')):
        file = mac / filename
        entries.append(('macOS ARM64', label, str(file) if file.is_file() and not file.is_symlink() else 'MISSING'))
    for label, filename in (('Assimp', 'assimp.exe'), ('FFmpeg', 'ffmpeg.exe'),
                             ('FFprobe', 'ffprobe.exe'), ('ImageMagick', 'magick.exe')):
        file = windows / filename
        entries.append(('Windows x64', label, str(file) if file.is_file() and not file.is_symlink() else 'MISSING'))
    # The Linux tools live in the managed x86-64 Docker image; checking the
    # recipe or a same-named file on the Mac would incorrectly claim readiness.
    cross = tools / 'WindowsCross'
    images = tuple(cross.glob('*.tar')) if cross.is_dir() and not cross.is_symlink() else ()
    for label in ('Assimp', 'FFmpeg', 'FFprobe', 'ImageMagick'):
        entries.append(('Linux x86-64', label,
                        'staged image (use devtools report for executed probes)' if images else 'MISSING'))
    return entries


def list_devtools() -> int:
    builds, _, record = _selected_devtools_receipt()
    print('CinterForge DevTools — ' + str(record.get('release', 'unknown')))
    for platform_name, label, status in _media_tool_inventory(builds):
        print(f'  {platform_name:<14} {label:<12} {status}', flush=True)
    print('Run devtools report for the complete toolchain inventory.', flush=True)
    return 0


def report_devtools() -> int:
    """Write the explicit selected toolchain inventory into Builds/Logs."""
    builds, _destination, record = _selected_devtools_receipt()
    tools = builds / 'Toolchains'
    names = ('mac/env/bin/colima', 'mac/env/bin/docker',
             'mac/env/bin/cmake', 'mac/env/bin/ninja',
             'Windows/MSYS2/usr/bin/bash.exe', 'WindowsCross')
    installed_release = str(record.get('release', 'unknown'))
    staged_release = _devtools_release()
    lines = ['# CinterForge DevTools', '',
             'Installed release: ' + installed_release, '',
             'Staged release (this Terminal): ' + staged_release, '',
             'Source: ' + str(record.get('source', 'unknown')), '',
             'Toolchains: `' + str(tools) + '`', '',
             '| Component | Present |', '|---|---|']
    for name in names:
        path = tools / name
        present = path.exists() and not path.is_symlink()
        lines.append('| `' + name + '` | ' + ('yes' if present else 'MISSING') + ' |')
    lines.extend(['', '| Platform | Media tool | Source |', '|---|---|---|'])
    for platform_name, label, state in _media_tool_inventory(builds):
        lines.append(f'| {platform_name} | {label} | `{state}` |')
    lines.append('')
    lines.append('Managed archive members: ' + str(len(record.get('files', {}))))
    location = builds / 'Logs/CinterForgeTerminal/DevToolsReport.md'
    if location.parent.is_symlink():
        raise RuntimeError('Unsafe toolchain report directory')
    location.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=location.parent,
                                     prefix='devtools-report-', delete=False) as handle:
        temp = Path(handle.name)
        handle.write('\n'.join(lines) + '\n')
    temp.replace(location)
    print('Installed DevTools version: ' + installed_release, flush=True)
    print('Staged DevTools version (this Terminal): ' + staged_release, flush=True)
    print('DevTools report: ' + str(location), flush=True)
    return 0


def uninstall_devtools() -> int:
    """Delete only verifiably owned toolchain members; retain all user outputs."""
    pin = _devtools_builds_pin()
    if pin is None or not pin.is_file() or pin.is_symlink():
        raise RuntimeError('No explicit DevTools installation to uninstall')
    builds = _devtools_builds_root()
    destination = _devtools_destination(_devtools_release())
    receipt = destination / 'stage-receipt.json'
    if receipt.is_symlink() or not receipt.is_file():
        raise RuntimeError('Missing managed DevTools receipt; refusing to delete shared toolchains')
    record = json.loads(receipt.read_text(encoding='utf-8'))
    files = record.get('files')
    if (record.get('source') != 'public' or not isinstance(files, dict) or
            not files or record.get('target') != 'devtools'):
        raise RuntimeError('No complete managed-file inventory; refusing unsafe uninstall')
    toolchains = builds / 'Toolchains'
    if toolchains.is_symlink():
        raise RuntimeError('Unsafe shared Toolchains redirect')
    print(f'[DEVTOOLS] Remove {len(files)} verified managed files from {toolchains}', flush=True)
    print('Other applications, outputs, and user-authored files will be preserved.', flush=True)
    if input('Type UNINSTALL to confirm: ').strip() != 'UNINSTALL':
        return 1
    from pathlib import PurePosixPath
    deleted = 0
    for name, sha in files.items():
        parts = PurePosixPath(name).parts
        if name == 'DevTools/README.txt':
            target = destination / 'README.txt'
        elif (len(parts) >= 4 and parts[:3] == ('DevTools', 'Builds', 'Toolchains') and
              '..' not in parts and '.' not in parts and '\\' not in name):
            target = builds.joinpath(*parts[2:])
        else:
            raise RuntimeError('Unsafe managed member: ' + name)
        if not isinstance(sha, str) or len(sha) != 64:
            raise RuntimeError('Invalid managed SHA-256: ' + name)
        for ancestor in target.parents:
            if ancestor == builds.parent:
                break
            if ancestor.is_symlink():
                raise RuntimeError('Unsafe managed-file parent: ' + str(ancestor))
        if target.is_file() and not target.is_symlink() and _file_sha256(target) == sha:
            target.unlink()
            deleted += 1
    # Keep a stale receipt from authorizing a second destructive operation.
    receipt.unlink()
    print(f'[DEVTOOLS] removed {deleted} unchanged managed files; other content preserved', flush=True)
    return 0


def devtools_help() -> int:
    from runtime.CinterForgeCommands import DEVTOOLS_HELP
    print(DEVTOOLS_HELP)
    return 0
