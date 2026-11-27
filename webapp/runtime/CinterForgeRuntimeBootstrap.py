"""CinterForgeRuntimeBootstrap — CinterForge Terminal application runtime.

PURPOSE: Application-owned SDK selection, Colima boot diagnostics and subprocess progress; no Setup/Build imports.
METHODS: sdk_root, _image_archive, _lima_boot_log_directory, _follow_lima_boot_diagnostics, _app_devtools_progress, _run_checked
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

from runtime import CinterForgeSessions as _CinterForgeSessions
from runtime import CinterForgeSettings as _CinterForgeSettings


def sdk_root() -> Path | None:
    """Select one Setup-owned SDK at Builds/Toolchains, never scan fallbacks.

    None means only that no Source was selected. Any incomplete/incorrect
    selected installation is a diagnostic failure, not "not installed".
    """
    from runtime import CinterForgeDevTools as devtools
    developer = os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'developer'
    if not developer and not os.environ.get('CINTER_BUILDS_ROOT', '').strip():
        selection = devtools._devtools_builds_pin()
        if selection is None:
            return None  # DevTools never selected; lightweight Terminal only.
        if selection.is_symlink():
            raise RuntimeError('Unsafe DevTools Source selection: ' + str(selection))
        if not selection.exists():
            return None  # No Source selected; do not create one at startup.
        if not selection.is_file():
            raise RuntimeError('Invalid DevTools Source selection: ' + str(selection))
    builds = devtools._devtools_builds_root()  # EXACTLY one selected Builds.
    if not developer:
        installed = devtools._devtools_destination(devtools._devtools_release())
        receipt = installed / 'stage-receipt.json'
        public_install = False
        if receipt.is_symlink():
            raise RuntimeError('Redirected DevTools installation receipt: ' + str(receipt))
        if receipt.is_file():
            try:
                record = json.loads(receipt.read_text(encoding='utf-8'))
            except (OSError, ValueError, TypeError) as error:
                raise RuntimeError('Malformed DevTools installation receipt: ' + str(receipt)) from error
            if not isinstance(record, dict):
                raise RuntimeError('Malformed DevTools installation receipt: ' + str(receipt))
            public_install = (record.get('schema') == _CinterForgeSettings.FOUR_SCHEMA and
                              record.get('target') == 'devtools' and
                              isinstance(record.get('release'), str) and bool(record['release']) and
                              record.get('source') == 'public' and
                              isinstance(record.get('artifact'), dict))
            if not public_install:
                raise RuntimeError('Unrecognized DevTools installation receipt: ' + str(receipt))
        if not public_install and not devtools._setup_receipt_verifies_toolchains(builds):
            raise RuntimeError('Selected Builds lacks the completed Setup all toolchain receipt: ' +
                               str(builds / 'Intermediates/CinterForgeTerminal/SetupCache/all.json'))
    sdk = builds / 'Toolchains'
    if sdk.is_symlink() or not sdk.is_dir():
        raise RuntimeError('Selected Toolchains missing or redirected: ' + str(sdk))
    needed = ('mac/env/bin/colima', 'mac/env/bin/docker', 'mac/lima/bin/limactl',
              'mac/qemu/bin/qemu-img', 'mac/qemu/bin/qemu-system-x86_64',
              'mac/qemu/cinter-qemu-receipt.json',
              'Windows/MSYS2/usr/bin/bash.exe', 'Windows/MSYS2/usr/bin/msys-2.0.dll')
    for name in needed:
        file = sdk / name
        if file.is_symlink() or not file.is_file():
            raise RuntimeError('Selected Toolchains missing or unsafe file: ' + str(file))
    qemu = sdk / 'mac/qemu/share/qemu'
    if qemu.is_symlink() or not qemu.is_dir():
        raise RuntimeError('Selected QEMU data directory missing: ' + str(qemu))
    # Exactly ONE OCI image. No globbing historical WindowsCross receipts.
    image_dir = sdk / 'WindowsCross'
    selector = image_dir / 'active-image.json'
    if image_dir.is_symlink() or not image_dir.is_dir() or selector.is_symlink() or not selector.is_file():
        raise RuntimeError('Missing single Setup-selected WindowsCross image: ' + str(selector))
    try:
        active = json.loads(selector.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError('Invalid selected WindowsCross image: ' + str(selector)) from error
    if not isinstance(active, dict):
        raise RuntimeError('Invalid selected WindowsCross image: ' + str(selector))
    digest = active.get('dockerfile_sha256')
    archive_sha = active.get('archive_sha256')
    if (active.get('schema') != 'cinterforge.terminal.active-guest-image.v1' or
            active.get('wine_version') != 'wine-11.15' or
            not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest) or
            not isinstance(archive_sha, str) or not re.fullmatch(r'[a-f0-9]{64}', archive_sha) or
            not isinstance(active.get('image'), str)):
        raise RuntimeError('Unsupported selected WindowsCross image: ' + str(selector))
    receipt = image_dir / (digest[:16] + '.json')
    archive = image_dir / (digest[:16] + '.tar')
    if (receipt.is_symlink() or not receipt.is_file() or
            archive.is_symlink() or not archive.is_file()):
        raise RuntimeError('Selected WindowsCross image receipt/archive missing: ' + str(receipt))
    try:
        manifest = json.loads(receipt.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError('Invalid WindowsCross image receipt: ' + str(receipt)) from error
    if not isinstance(manifest, dict) or any(manifest.get(key) != active.get(key) for key in
           ('dockerfile_sha256', 'archive_sha256', 'wine_version', 'image')):
        raise RuntimeError('Selected WindowsCross image does not match its receipt: ' + str(receipt))
    return sdk


_CINTERFORGE_VM = CinterForgeVM(_CinterForgeSettings.PROFILE, real_login_home=_CinterForgeSessions._real_login_home)


def _image_archive(sdk: Path, kind: str) -> tuple[Path, str]:
    if kind not in ('linux', 'win'):
        raise ValueError('Unknown managed guest platform')
    # The same verified amd64 Debian image supplies both native Linux and
    # Wine/MSYS2 Windows tools. Two UI platforms, ONE running container.
    folder = sdk / 'WindowsCross'
    selected_file = folder / 'active-image.json'
    if not selected_file.is_file() or selected_file.is_symlink():
        raise RuntimeError("Missing single Setup-owned WindowsCross active-image.json selector at " + str(selected_file))
    # DevTools Build commits one exact OCI image. Steam Stage can retain
    # older unowned files, so sorting *.json by name is not deterministic.
    if selected_file.is_symlink():
        raise RuntimeError('Active DevTools guest image selection is redirected')
    try:
        active = json.loads(selected_file.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('Invalid active DevTools guest image selector') from exc
    if (active.get('schema') != 'cinterforge.terminal.active-guest-image.v1' or
            active.get('wine_version') != 'wine-11.15' or
            not re.fullmatch(r'[0-9a-f]{64}', str(active.get('dockerfile_sha256', '')))):
        raise RuntimeError('Unsupported active DevTools guest image selector')
    digest = active['dockerfile_sha256']
    receipt_file = folder / (digest[:16] + '.json')
    tar = folder / (digest[:16] + '.tar')
    if receipt_file.is_symlink() or tar.is_symlink() or not tar.is_file():
        raise RuntimeError('Selected DevTools guest OCI image is missing or redirected')
    try:
        receipt = json.loads(receipt_file.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('Cannot read selected DevTools OCI manifest') from exc
    if (any(receipt.get(k) != active.get(k) for k in
            ('dockerfile_sha256', 'image', 'archive_sha256', 'wine_version')) or
            not isinstance(active.get('image'), str) or
            not active['image'].startswith('cinterforge-terminal-windows-cross:')):
        raise RuntimeError('Active OCI image does not match its sealed manifest')
    checksum = hashlib.sha256()
    with tar.open('rb') as src:
        for block in iter(lambda: src.read(4 * 1024 * 1024), b''):
            checksum.update(block)
    if checksum.hexdigest() != active['archive_sha256']:
        raise RuntimeError('Selected DevTools OCI image differs from its sealed SHA-256')
    return tar, active['image']

def _lima_boot_log_directory() -> Path | None:
    """Store native Colima boot evidence beside the user's other Terminal logs."""
    from runtime import CinterForgeSessions as _CinterForgeSessions
    from runtime import CinterForgeService as _CinterForgeService
    app = _CinterForgeService._server
    if app is not None:
        root = _CinterForgeSessions._runtime_logs_root(app.mutable_builds)
        if root.name == 'Runtime':  # Developer run, not installed Steam app.
            root = root.parent
    else:
        configured = os.environ.get('CINTER_BUILDS_ROOT', '')
        if not configured or Path(configured).name != 'Builds':
            return None
        root = Path(configured) / 'Logs/CinterForgeTerminal'
    directory = root / 'VM' / 't' / (
        'boot-' + time.strftime('%Y%m%d-%H%M%S') + '-' +
        str(os.getpid()) + '-' + secrets.token_hex(3))
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _follow_lima_boot_diagnostics(env: dict[str, str], stop: threading.Event,
                                  report: Callable[[str], None],
                                  archive: Path | None,
                                  boot_failures: dict[str, int] | None = None) -> None:
    """Forward *new* Lima host-agent and serial output while Colima waits for SSH.

    These files are where the actual guest/kernel/SSH failure appears; Colima's
    console may only say "SSH connection" for ten minutes. Never replay old
    startup logs from a previous native process, and never follow outside our
    dedicated Lima profile directory.
    """
    from runtime import CinterForgeSettings as _CinterForgeSettings
    directory = Path(env['LIMA_HOME']) / ('colima-' + _CinterForgeSettings.PROFILE)
    names = ('serial.log', 'serialv.log', 'ha.stderr.log', 'ha.stdout.log')
    offsets = {}
    # Skip bytes already present before this startup. New files start at zero.
    for name in names:
        path = directory / name
        try:
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                offsets[name] = (stat.st_ino, stat.st_size)
        except OSError:
            pass
    pending = {name: b'' for name in names}
    def drain() -> None:
        for name in names:
            path = directory / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                previous_ino, offset = offsets.get(name, (stat.st_ino, 0))
                if stat.st_ino != previous_ino or stat.st_size < offset:
                    offset = 0
                    pending[name] = b''
                if stat.st_size <= offset:
                    offsets[name] = (stat.st_ino, offset)
                    continue
                # Bound each read. Serial output is a diagnostic, not a way
                # to overload the persistent Server Log during repeated boots.
                with path.open('rb') as stream:
                    stream.seek(max(offset, stat.st_size - 32768))
                    chunk = stream.read(32768)
                offsets[name] = (stat.st_ino, stat.st_size)
                data = pending[name] + chunk
                lines = data.split(b'\n')
                pending[name] = lines.pop()[-2048:]
                if archive is not None:
                    # The active Lima files cannot be relocated. Preserve a
                    # growing copy under Logs while the VM is still starting.
                    with (archive / name).open('ab') as sink:
                        sink.write(chunk)
                for line in lines[-160:]:
                    clean = line.decode('utf-8', errors='replace').strip()[:1200]
                    if clean:
                        # Keep only first-party guest service evidence. The
                        # installed VM's failing SSH must not silently consume
                        # the entire Colima startup budget. A failed
                        # cloud-final alone is NOT sufficient evidence.
                        if boot_failures is not None and name.startswith('serial'):
                            if 'Failed to start ssh.service' in clean:
                                boot_failures['ssh'] += 1
                            elif 'Failed to start dnsmasq.service' in clean:
                                boot_failures['dnsmasq'] += 1
                            elif 'Failed to start cloud-final.service' in clean:
                                boot_failures['cloud_final'] += 1
                        report('[DEVTOOLS BOOT ' + name + '] ' + clean)
                        if ('cloud-final.service' in clean and
                                ('Failed to start' in clean or '[FAILED]' in clean)):
                            # A cloud-init final-stage failure alone does
                            # not establish that Lima's SSH connection is
                            # impossible. Let the real Colima result decide.
                            report('[DEVTOOLS] Guest cloud-final reported failure; '
                                   'waiting for the actual Colima/SSH result')
            except (OSError, ValueError):
                continue  # Lima can replace its logs while booting.
    try:
        while not stop.wait(1):
            drain()
        drain()
        for name, value in pending.items():
            if value.strip():
                report('[DEVTOOLS BOOT ' + name + '] ' +
                       value.decode('utf-8', errors='replace').strip()[:1200])
    finally:
        if archive is not None:
            # Keep the exact underlying diagnostic files, including output
            # that predates our follower or arrived during log rotation.
            for name in names:
                source = directory / name
                if source.is_file() and not source.is_symlink():
                    try:
                        shutil.copyfile(source, archive / name)
                    except OSError as error:
                        report('[DEVTOOLS BOOT] Could not copy ' + name + ': ' + str(error))


def _app_devtools_progress(message: str) -> None:
    """One progress message to native console and persistent in-app Server Log."""
    from runtime import CinterForgeService as _CinterForgeService
    line = '[DEVTOOLS] ' + message
    print(line, flush=True)
    app = _CinterForgeService._server
    if app is not None and not app._stopping:
        try:
            app.log_event(line)
        except (OSError, RuntimeError, ValueError):
            pass  # Native quit can close the Server Log while guest boot unwinds.


def _run_checked(command: list[str], env: dict[str, str], *, title: str,
                 timeout: int = 600, cancel_event: threading.Event | None = None) -> None:
    """Stream real guest process output and retain its error in the Server Log.

    Previously Colima's entire diagnostic disappeared behind CalledProcessError,
    leaving only a long command and exit=1. There is no Setup/repair fallback
    in the installed application: a failed guest must report its actual cause.
    """
    import selectors
    from runtime import CinterForgeService as _CinterForgeService
    print('[DEVTOOLS] ' + title, flush=True)
    app = _CinterForgeService._server
    def report(message: str) -> None:
        if app is not None and not app._stopping:
            try:
                app.log_event(message)
            except (OSError, RuntimeError, ValueError):
                # HTTP shutdown may close its log concurrently with a Colima
                # output chunk. Never convert a log race into a VM boot error.
                pass
    report('[DEVTOOLS] ' + title)
    diagnostic_stop = threading.Event()
    diagnostic_thread = None
    archive = None
    boot_failures = {'ssh': 0, 'dnsmasq': 0, 'cloud_final': 0}
    if title == 'Starting managed guest VM with host volumes':
        try:
            archive = _lima_boot_log_directory()
        except OSError as error:
            # A log destination problem must never prevent the native app's
            # already-provisioned guest from booting.
            report('[DEVTOOLS] Could not open VM diagnostics folder: ' + str(error))
        diagnostic_thread = threading.Thread(
            target=_follow_lima_boot_diagnostics,
            args=(env, diagnostic_stop, report, archive, boot_failures),
            daemon=True, name='cinter-lima-boot-log')
        diagnostic_thread.start()
        report('[DEVTOOLS] Lima boot diagnostics: ' + str(archive) if archive else
               '[DEVTOOLS] Following Lima host-agent and serial output')
    tail = bytearray()
    started = time.monotonic()
    last_heartbeat = started
    try:
        # Store unfiltered Colima stdout/stderr alongside the original Lima
        # agent/serial logs. Server Log remains a live, readable summary.
        with ((archive / 'colima-start.log').open('wb') if archive is not None
              else open(os.devnull, 'wb')) as raw_log, subprocess.Popen(
                  command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  env=env, bufsize=0) as process:
            assert process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                try:
                    while selector.get_map() or process.poll() is None:
                        if cancel_event is not None and cancel_event.is_set():
                            # Stop the CLI promptly; its Lima hostagent may be a
                            # separate process and is stopped by stop_runtime_vm.
                            raise RuntimeError(title + ' cancelled: native app is closing')
                        elapsed = time.monotonic() - started
                        remaining = timeout - elapsed
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(command, timeout)
                        # Only abort a conclusively unhealthy *installed*
                        # guest, after the usual healthy Colima/SSH startup
                        # interval. Do not penalize successful slow boots or
                        # developer lifecycle VMs. Preserve the diagnostics;
                        # the native guest owner stops its own failed VM.
                        if (os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'installed'
                                and title == 'Starting managed guest VM with host volumes'
                                and elapsed >= 90
                                and boot_failures['ssh'] >= 2
                                and boot_failures['dnsmasq'] >= 1
                                and boot_failures['cloud_final'] >= 1):
                            raise RuntimeError(
                                'Installed t guest failed SSH, dnsmasq and cloud-final; '
                                'Colima cannot reach readiness. Preserving the '
                                'app-owned VM disk for offline repair. '
                                'Captured guest boot logs: ' + str(archive or '(Server Log)'))
                        for key, _ in selector.select(timeout=min(1, remaining)):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            tail.extend(chunk)
                            if len(tail) > 8192:
                                del tail[:-8192]
                            raw_log.write(chunk)
                            raw_log.flush()
                            message = chunk.decode('utf-8', errors='replace')
                            print(message, end='', flush=True)
                            # The native worker's stdout is not the persistent
                            # Server Log. Mirror Colima errors there so a stuck
                            # splash is diagnosable from the installed app.
                            for line in message.splitlines():
                                report('[DEVTOOLS VM] ' + line[:1200])
                        if time.monotonic() - last_heartbeat >= 15:
                            elapsed = int(time.monotonic() - started)
                            heartbeat = '[DEVTOOLS] WORKING ' + title + ' (' + str(elapsed) + 's)'
                            print(heartbeat, flush=True)
                            report(heartbeat)
                            last_heartbeat = time.monotonic()
                    returncode = process.wait(timeout=max(1, remaining))
                except subprocess.TimeoutExpired as error:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    detail = tail.decode('utf-8', errors='replace').strip()[-3200:]
                    raise RuntimeError(title + ' timed out after ' + str(timeout) +
                                       's; VM boot logs: ' + str(archive or '(see Server Log)') +
                                       '; last Colima output: ' + (detail or '(none)')) from error
                except BaseException:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    raise
        if returncode:
            detail = tail.decode('utf-8', errors='replace').strip()[-3200:]
            raise RuntimeError(title + ' failed (exit ' + str(returncode) + '); '
                               'VM boot logs: ' + str(archive or '(see Server Log)') +
                               '; last Colima output: ' +
                               (detail or 'process exited without diagnostics'))

    finally:
        diagnostic_stop.set()
        if diagnostic_thread is not None:
            diagnostic_thread.join(timeout=3)

