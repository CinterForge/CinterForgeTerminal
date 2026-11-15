"""CinterForgeAuthentication — CinterForge Terminal application runtime.

PURPOSE: LAN login credentials, HTTP service owner receipts and safe authenticated attachment to an existing process.
METHODS: _lan_login_path, _load_lan_login, _save_lan_login, _check_lan_login, _lan_owner_path, _write_lan_owner, _remove_lan_owner, _existing_lan_owner, _request_orphaned_lan_shutdown
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



LAN_HTTP_PORT = 20300


LAN_LOGIN_FILENAME = 'CinterForgeTerminal/Config/lan-login.json'


LAN_LOGIN_ITERATIONS = 200_000


LAN_LOGIN_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')


def _lan_login_path(writable_root: Path) -> Path:
    """Store login state only under developer Builds or installed Steam state."""
    return writable_root / LAN_LOGIN_FILENAME


def _load_lan_login(writable_root: Path) -> dict | None:
    """Read configured credentials; never generate or reveal a password."""
    path = _lan_login_path(writable_root)
    if path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise RuntimeError('Unsafe Terminal LAN login directory')
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise RuntimeError('Unsafe Terminal LAN login state')
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if (record.get('schema') != 'cinterforge.terminal.lan-login.v1' or
                not LAN_LOGIN_NAME.fullmatch(record['username']) or
                not re.fullmatch(r'[0-9a-f]{32}', record['salt']) or
                not re.fullmatch(r'[0-9a-f]{64}', record['digest']) or
                record['iterations'] != LAN_LOGIN_ITERATIONS):
            raise ValueError('invalid state')
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError('Invalid Terminal LAN login state; use setpass to replace it') from error
    return record


def _save_lan_login(writable_root: Path, username: str, password: str) -> dict:
    """Atomically replace the credential record; keep password out of files and logs."""
    if not isinstance(username, str) or not LAN_LOGIN_NAME.fullmatch(username):
        raise ValueError('Username must be 1-64 letters, digits, dots, dashes or underscores')
    if not isinstance(password, str) or not 1 <= len(password) <= 256 or '\x00' in password:
        raise ValueError('Password must contain 1-256 characters')
    directory = _lan_login_path(writable_root).parent
    parent = directory.parent
    for part in (parent, directory):
        if part.is_symlink() or (part.exists() and not part.is_dir()):
            raise RuntimeError('Unsafe Terminal LAN login directory')
        part.mkdir(exist_ok=True)
    path = _lan_login_path(writable_root)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError('Unsafe Terminal LAN login state file')
    salt = secrets.token_bytes(16)
    record = {'schema': 'cinterforge.terminal.lan-login.v1', 'username': username,
              'salt': salt.hex(), 'iterations': LAN_LOGIN_ITERATIONS,
              'digest': hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt,
                                            LAN_LOGIN_ITERATIONS).hex()}
    temporary = directory / ('lan-login-' + uuid.uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
            json.dump(record, file, separators=(',', ':'))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def _check_lan_login(record: dict | None, username: str, password: str) -> bool:
    """Compare username and salted password hash without echoing credentials."""
    if not record or not isinstance(username, str) or not isinstance(password, str):
        return False
    if len(username) > 64 or len(password) > 256:
        return False
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                bytes.fromhex(record['salt']), record['iterations'])
    return (secrets.compare_digest(username, record['username']) and
            secrets.compare_digest(digest, bytes.fromhex(record['digest'])))


LAN_OWNER_FILENAME = 'CinterForgeTerminal/Config/http-owner.json'


def _lan_owner_path(writable_root: Path) -> Path:
    """Location of the current Terminal HTTP process receipt."""
    return writable_root / LAN_OWNER_FILENAME


def _write_lan_owner(server: 'CinterForgeHTTPServer') -> None:
    """Atomically publish a private startup grant after port 20300 is bound."""
    path = _lan_owner_path(server.mutable_builds)
    if path.parent.is_symlink() or path.is_symlink():
        raise RuntimeError('Unsafe Terminal HTTP owner receipt')
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {'schema': 'cinterforge.terminal.http-owner.v1',
               'port': server.server_port, 'instance': server.lan_id,
               'native_grant': server.native_grant, 'pid': os.getpid(),
               'native_pid': os.getppid()}
    temporary = path.parent / ('http-owner-' + uuid.uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(receipt, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_lan_owner(server: 'CinterForgeHTTPServer') -> None:
    """Remove only our own receipt, not that of a replacement service."""
    path = _lan_owner_path(server.mutable_builds)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2048:
            return
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('instance') == server.lan_id:
            path.unlink()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


def _existing_lan_owner(builds: Path, *, orphaned: bool = False) -> dict | None:
    """Find a verified listener owned by this exact runtime environment.

    A developer Xcode process and an installed Steam process do not share a
    DevTools receipt, guest VM state or runtime log root. Attaching to the
    other's HTTP owner caused an apparently healthy Xcode build to display
    the installed server's stuck t guest instead of its developer service.
    Never terminate another runtime owner merely because it uses port 20300.
    """
    from runtime import CinterForgeSessions as _CinterForgeSessions
    if LAN_HTTP_PORT <= 0:
        return None  # In-process smoke fixtures use a randomly assigned port.

    roots = [_CinterForgeSessions._terminal_mutable_root(builds)]
    from urllib.request import Request, urlopen
    for root in roots:
        path = _lan_owner_path(root)
        try:
            if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > 2048 or (os.name != 'nt' and path.stat().st_mode & 0o077):
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            if (record.get('schema') != 'cinterforge.terminal.http-owner.v1' or
                    record.get('port') != LAN_HTTP_PORT or
                    not re.fullmatch(r'[0-9a-f]{32}', str(record.get('instance', ''))) or
                    not re.fullmatch(r'[A-Za-z0-9_-]{40,128}', str(record.get('native_grant', '')))):
                continue
            # Do not mistake a stale receipt (or a reused PID) for authority.
            # Only a live service that proves its private grant AND instance
            # can be reused or asked to release its own listener.
            native_pid = record.get('native_pid')
            if type(native_pid) is not int or native_pid <= 1:
                continue
            try:
                os.kill(native_pid, 0)
                native_alive = True
            except ProcessLookupError:
                native_alive = False
            except PermissionError:
                continue
            if native_alive == orphaned:
                continue
            request = Request(f'http://127.0.0.1:{LAN_HTTP_PORT}/healthz',
                              headers={'X-Cinter-Owner': record['native_grant']})
            with urlopen(request, timeout=1.5) as response:
                status = json.loads(response.read(2048))
            if status.get('ready') and status.get('instance') == record['instance']:
                return record
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return None


def _request_orphaned_lan_shutdown(builds: Path) -> bool:
    """Ask a *verified* leftover Terminal HTTP worker to release port 20300.

    Never kill by port/PID. A 0600 owner receipt, challenge-response over
    loopback, dead native owner, and the server's own orphan check are all
    required. Foreign listeners and healthy Terminal windows remain untouched.
    """
    record = _existing_lan_owner(builds, orphaned=True)
    if record is None:
        return False
    from urllib.request import Request, urlopen
    request = Request(f'http://127.0.0.1:{LAN_HTTP_PORT}/_native_orphan_shutdown',
                      data=b'{}', method='POST', headers={
                          'Content-Type': 'application/json',
                          'X-Cinter-Owner': record['native_grant'],
                          'X-Cinter-Instance': record['instance']})
    try:
        with urlopen(request, timeout=2) as response:
            result = json.loads(response.read(2048))
        return result.get('stopping') is True and result.get('instance') == record['instance']
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False

