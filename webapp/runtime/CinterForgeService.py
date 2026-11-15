"""CinterForgeService — CinterForge Terminal application runtime.

PURPOSE: PythonHost start/stop RPC, owning native-PID listener supervision, and standalone HTTP bootstrap.
METHODS: _watch_native_http_parent, handle, bootstrap_smoke_main
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



_server = None


_server_thread = None


_guest_boot_cancel = threading.Event()


def _watch_native_http_parent(server: _CinterForgeHTTP.CinterForgeHTTPServer, parent_pid: int) -> None:
    """Release this worker's listener when its native window exits abruptly.

    Xcode Stop may terminate the parent without giving the Python RPC worker a
    shutdown message. Do not leave its HTTP service holding the reserved 20300
    port. This observer only shuts down this worker's own server; it never
    inspects, adopts, or terminates another process.
    """
    from runtime import CinterForgeHTTP as _CinterForgeHTTP
    global _server
    while _server is server and os.getppid() == parent_pid:
        time.sleep(0.5)
    if _server is not server:
        return
    server.log_event('Native HTTP owner exited; releasing managed listener and worker')
    try:
        server.stop()
    except Exception as error:
        # The native parent is gone; the worker must not survive just because
        # an inherited stdin pipe is still open in another child process.
        try:
            server.log_event('Native listener cleanup failed: ' + str(error))
        except (OSError, ValueError):
            pass
    finally:
        if _server is server:
            _server = None
        # This is our OWN orphaned worker, not an attached listener or another
        # native owner's process. The independent native-PID watchdog owns VM
        # cleanup, even when Xcode Stop makes C++ destructors impossible.
        # No Python main-loop RPC can arrive from the dead native parent.
        os._exit(0)


def handle(method: str, params: dict) -> dict:
    """Invoked through the already-shipped PythonHost service RPC worker."""
    from runtime import CinterForgeAuthentication as _CinterForgeAuthentication
    from runtime import CinterForgeHTTP as _CinterForgeHTTP
    from runtime import CinterForgeSessions as _CinterForgeSessions
    from runtime import CinterForgeSettings as _CinterForgeSettings
    from runtime import CinterForgeVMOperations as _CinterForgeVMOperations
    global _server, _server_thread
    if method == 'start':
        if _server is not None:
            raise RuntimeError('CinterForgeTerminal HTTP server already started')
        _guest_boot_cancel.clear()
        builds = Path(params['builds']).resolve(strict=True)
        if builds.name != 'Builds' or not _CinterForgeSettings.ASSETS.is_relative_to(builds):
            raise ValueError('Shell resources must be staged inside the shared Builds directory')
        # Opening a second native window should reuse the running local Host,
        # rather than trying to bind a second listener to the reserved port.
        existing = _CinterForgeAuthentication._existing_lan_owner(builds)
        if existing is not None:
            return {'url': f'http://127.0.0.1:{_CinterForgeAuthentication.LAN_HTTP_PORT}/',
                    'native_grant': existing['native_grant'], 'attached': True}
        try:
            _server = _CinterForgeHTTP.CinterForgeHTTPServer(builds, lan=True)
        except OSError as error:
            if getattr(error, 'errno', None) not in (48, 98, 10048):
                raise
            # On abrupt native/Xcode exit a previous worker can survive past
            # the old four-second grace period. Ask only an authenticated,
            # orphaned CinterForge listener to stop itself, then wait for bind.
            # A foreign listener is never killed or re-used.
            recovery_requested = _CinterForgeAuthentication._request_orphaned_lan_shutdown(builds)
            for _ in range(60):
                time.sleep(0.25)
                if not recovery_requested:
                    recovery_requested = _CinterForgeAuthentication._request_orphaned_lan_shutdown(builds)
                existing = _CinterForgeAuthentication._existing_lan_owner(builds)
                if existing is not None:
                    return {'url': f'http://127.0.0.1:{_CinterForgeAuthentication.LAN_HTTP_PORT}/',
                            'native_grant': existing['native_grant'], 'attached': True}
                try:
                    _server = _CinterForgeHTTP.CinterForgeHTTPServer(builds, lan=True)
                    break
                except OSError as again:
                    if getattr(again, 'errno', None) not in (48, 98, 10048):
                        raise
            else:
                raise RuntimeError('HTTP port 20300 is occupied by an unverified listener; '
                                   'no other process was terminated') from error
        try:
            # Startup is app-owned, not PTY-owned. Installed DevTools (or a
            # newly staged bundle) boots one VM and two persistent guests.
            writable = _server.mutable_builds
            if writable.name == 'Builds':
                os.environ['CINTER_BUILDS_ROOT'] = str(writable)
                os.environ['CINTER_TERMINAL_RUNTIME_MODE'] = 'developer'
                # Xcode's native service worker starts before any PTY. Resolve
                # the same real Steam library that a later shell would use,
                # otherwise sdk_root() may report no DevTools until a tab opens.
                # Do not invent a default path or create a library here.
                steam = _CinterForgeSessions._steam_library_for_installed_root(builds)
                if steam is not None:
                    os.environ['CINTER_STEAMAPPS'] = str(steam)
            else:
                steam = _CinterForgeSessions._steam_library_for_installed_root(builds)
                if steam is not None:
                    os.environ['CINTER_STEAMAPPS'] = str(steam)
                    os.environ['CINTER_TERMINAL_RUNTIME_MODE'] = 'installed'
            os.environ['CINTER_CODE_WORKSPACE'] = str(Path(_server.code_workspace.details()['cwd']))
            _CinterForgeAuthentication._write_lan_owner(_server)
            _server_thread = threading.Thread(target=_server.serve_forever, name='cinter-http', daemon=True)
            _server_thread.start()
            threading.Thread(target=_watch_native_http_parent,
                             args=(_server, os.getppid()),
                             name='cinter-native-http-owner', daemon=True).start()
            if platform.system() == 'Darwin':
                # Start as part of native application startup. The WebView can
                # render while the one owned VM and two reusable guests boot.
                # A tab must only wait for this work, never start it again.
                _server.guest_supervisor.schedule()
        except Exception:
            _server.stop()
            _server = None
            raise
        return {'url': _server.origin + '/', 'port': _server.server_port,
                'native_grant': _server.native_grant,
                'platform': _server.system, 'server_log': str(_server.log.path)}
    if method == 'service_health':
        # The native app must not remain alive with a dead HTTP listener while
        # its Colima watchdog sees the native PID and keeps the guests running.
        alive = (_server is not None and _server_thread is not None and
                 _server_thread.is_alive() and _server._http_serving.is_set() and
                 not _server._stopping)
        return {'alive': alive}
    if method == 'next_lifecycle_reboot_request':
        if _server is None:
            return {'requested': False}
        with _server.debug_lock:
            if not _server.lifecycle_actions:
                return {'requested': False}
            action = _server.lifecycle_actions.popleft()
            return {'requested': True, **action}
    if method == 'next_debugger_request':
        if _server is None:
            return {'requested': False}
        with _server.debug_lock:
            if _server.debug_requests:
                _server.debug_requests -= 1
                return {'requested': True}
            return {'requested': False}
    if method == 'shutdown_runtime':
        # Called after HTTP stop by the native application, NOT for server
        # reload. An independent watcher performs the same action on a crash.
        _guest_boot_cancel.set()
        _CinterForgeVMOperations.stop_runtime_vm()
        return {'stopped': True, 'guests': ['linux', 'win'], 'vm': _CinterForgeSettings.PROFILE}
    if method == 'stop':
        closing = _server
        _server = None
        if closing is not None:
            closing.stop()
        return {'stopped': True}
    raise ValueError(f'Unsupported CinterForgeTerminal host method: {method}')


def bootstrap_smoke_main(argv: list[str] | None = None) -> int:
    """Launch the *staged* HTTP service using the packaged Python interpreter.

    This entry point is used only by the CinterForgeTerminal Setup smoke. Readiness
    is sent through a private file in Builds, rather than an unauthenticated
    public API or an application log containing the bearer token.
    """
    from runtime import CinterForgeHTTP as _CinterForgeHTTP
    from runtime import CinterForgeSettings as _CinterForgeSettings
    import argparse
    parser = argparse.ArgumentParser(description='CinterForgeTerminal staged HTTP smoke')
    parser.add_argument('--builds', type=Path, required=True)
    parser.add_argument('--ready', type=Path, required=True)
    args = parser.parse_args(argv)
    builds = args.builds.resolve(strict=True)
    ready = args.ready.resolve()
    if builds.name != 'Builds' or not ready.is_relative_to(builds) or ready.parent.is_symlink():
        raise ValueError('Bootstrap smoke must run entirely under sibling Builds')
    if not _CinterForgeSettings.ASSETS.resolve().is_relative_to(builds):
        raise ValueError('Bootstrap smoke requires staged HTML/service under Builds')
    server = _CinterForgeHTTP.CinterForgeHTTPServer(builds)
    stopped = threading.Event()

    def request_stop(_signum, _frame):
        stopped.set()

    for name in ('SIGINT', 'SIGTERM'):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, request_stop)
    ready.parent.mkdir(parents=True, exist_ok=True)
    with ready.open('x', encoding='utf-8') as stream:
        os.chmod(ready, 0o600) if os.name != 'nt' else None
        json.dump({'schema': 'cinterforge.terminal.http-ready.v1',
                   'origin': server.origin, 'token': server.token,
                   'pid': os.getpid(), 'platform': server.system}, stream)
        stream.flush()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        stopped.wait()
    finally:
        server.stop()
        worker.join(timeout=5)
        ready.unlink(missing_ok=True)
    return 0

