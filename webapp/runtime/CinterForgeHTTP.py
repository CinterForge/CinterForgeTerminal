"""CinterForgeHTTP — CinterForge Terminal application runtime.

PURPOSE: Single HTTP server and route handler, authentication and HTTP asset/terminal/guest APIs.
METHODS: CinterForgeHTTPServer, CinterForgeHTTPRequestHandler
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



from runtime.CinterForgeHTTPAuthorization import CinterForgeHTTPAuthorization
from runtime.CinterForgeHTTPCode import CinterForgeHTTPCode
from runtime.CinterForgeHTTPRoutes import CinterForgeHTTPRoutes

class CinterForgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, builds: Path, *, lan: bool = False):
        from runtime import CinterForgeAuthentication as _CinterForgeAuthentication
        from runtime import CinterForgeDiscovery as _CinterForgeDiscovery
        from runtime import CinterForgeGuestShells as _CinterForgeGuestShells
        from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
        from runtime import CinterForgeService as _CinterForgeService
        from runtime import CinterForgeSessions as _CinterForgeSessions
        from runtime import CinterForgeSettings as _CinterForgeSettings
        from runtime import CinterForgeVMOperations as _CinterForgeVMOperations
        self.lan = lan
        self.lan_id = uuid.uuid4().hex
        self.discovery = None
        self.sidecars = None  # Same owner; HTTP viewer ports + browser WS ports.
        self.builds = builds
        self.mutable_builds = _CinterForgeSessions._terminal_mutable_root(builds)
        self.system = _CinterForgeSessions.target_platform()
        self.code_workspace = CinterForgeCodeWorkspace(self.mutable_builds, system=self.system)
        self.code_sessions = {}  # Code-OSS terminal name -> real host Session or Server Log cursor
        # App-owned Bash PTYs survive Code-OSS tab close/reopen. Exactly one
        # Linux and one Windows process; only this owner may close them.
        self.guest_shells: dict[str, _CinterForgeSessions.CinterForgeTerminalSession] = {}
        self.guest_shells_lock = threading.RLock()
        self.code_sessions_lock = threading.Lock()
        self.code_closed_owners = set()  # Late worker requests cannot revive a closed CodeOSS tab.
        self.lan_login_lock = threading.Lock()
        self.lan_login = _CinterForgeAuthentication._load_lan_login(self.mutable_builds)
        self.lan_cookie = secrets.token_urlsafe(36)
        # Only the local native launcher receives this grant via its private
        # RPC pipe; normal browser pages must use the configured setpass login.
        self.native_grant = secrets.token_urlsafe(48)
        self.native_cookie = secrets.token_urlsafe(48)
        self.cli_token = secrets.token_urlsafe(36)
        log_directory = _CinterForgeSessions._runtime_logs_root(self.mutable_builds)
        self._stopping_lock = threading.Lock()
        self._stopping = False
        self.native_parent_pid = os.getppid()
        self._http_serving = threading.Event()
        # Expose the already-designated Logs tree to the read-only Code-OSS
        # host-file provider; a symlink resolving outside it is still rejected.
        self.code_workspace.host_log_roots = [next(
            (ancestor for ancestor in (log_directory, *log_directory.parents)
             if ancestor.name == 'Logs'), log_directory)]
        self.token = secrets.token_urlsafe(36)
        self.sessions: dict[str, _CinterForgeSessions.CinterForgeTerminalSession] = {}
        self.sessions_lock = threading.Lock()
        self.bedrock_terminal_lock = threading.Lock()
        self.bedrock_terminal_requests = deque()
        self.devtools_requested = False
        # One app-owned guest initialization, never one per opened terminal tab.
        self.guest_start_lock = threading.Lock()
        self.guest_start_state = 'not-installed'
        self.guest_start_error = ''
        self.guest_ready = []  # Per-platform readiness survives a failed Wine probe.
        self.guest_vnc_ready = []  # Verified VNC endpoint, independent of Bash health.
        self.guest_errors = {}
        # Native app consumes these atomic reboot requests over its existing
        # private JSON-RPC pipe, never through an unauthenticated browser menu.
        self.lifecycle_actions = deque()
        self.debug_requests = 0
        self.lagomorph_requests = 0
        self.macgame_request = None
        self.wargames_requests = 0
        # Ordered shell-to-Code actions: run/reselect versus settings refresh.
        self.code_actions = deque()
        self.html_menus_override = False  # macOS development inspection only
        # Never overwrite pending browser story commands with a later click.
        # Each title has one live interpreter tab and one launch generation.
        self.if_actions = deque()
        self.if_active: dict[str, str] = {}
        self.discord_actions = deque()
        self.discord_channels = {}
        self.debug_lock = threading.Lock()
        self.eula_lock = threading.Lock()
        self.eula_requested = False
        # Bedrock is optional. A missing user Steam library must never prevent
        # the WebView, real PTY, Code, debugger or Help from starting.
        self.bedrock = None
        self.bedrock_error = ''
        try:
            self.bedrock = _CinterForgeSessions._load_bedrock_manager()(builds, self.system, user_install=True)
        except RuntimeError as exc:
            self.bedrock_error = str(exc)
            print('[CinterForgeTerminal] Bedrock unavailable: ' + self.bedrock_error,
                  file=sys.stderr, flush=True)
        # Bind the reserved port before creating the persistent Server log.
        # Retrying after Xcode Stop must not create a new log file per failed bind.
        super().__init__((('0.0.0.0', _CinterForgeAuthentication.LAN_HTTP_PORT) if lan else ('127.0.0.1', 0)), CinterForgeHTTPRequestHandler)
        try:
            self.log = CinterForgeRuntimeLog(log_directory,
                memory_lines=_CinterForgeSettings.SERVER_LOG_MEMORY_LINES, batch_lines=_CinterForgeSettings.SERVER_LOG_BATCH_LINES)
        except Exception:
            self.server_close()
            raise
        if lan:
            try:
                self.discovery = _CinterForgeDiscovery.CinterForgeDiscovery(_CinterForgeSettings.ASSETS.parent, socket.gethostname(),
                                                       self.server_port, self.log_event)
            except (OSError, RuntimeError, AttributeError) as error:
                self.log_event('Bonjour unavailable (rebuild staged Host library): ' + str(error))
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.log_event('backend ready; platform=' + self.system + ' port=' + str(self.server_port))
        # One guest supervisor belongs to this server owner, not to each tab
        # or remote browser. It delegates to the already-existing runtime.
        self.guest_supervisor = CinterForgeGuestSupervisor(
            self, sdk_root=_CinterForgeRuntimeBootstrap.sdk_root, runtime_logs_root=_CinterForgeSessions._runtime_logs_root,
            start_runtime_watchdog=_CinterForgeGuestShells._start_runtime_watchdog,
            guest_boot_cancel=_CinterForgeService._guest_boot_cancel,
            boot_runtime_guests=_CinterForgeVMOperations.boot_runtime_guests, stop_runtime_vm=_CinterForgeVMOperations.stop_runtime_vm,
            close_owned_guest_shells=_CinterForgeGuestShells._close_owned_guest_shells,
            new_owned_guest_shell=_CinterForgeGuestShells._new_owned_guest_shell,
            check_owned_guest_vnc=_CinterForgeGuestShells._check_owned_guest_vnc)

    def log_event(self, message: str) -> None:
        self.log.append(message)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._http_serving.set()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self._http_serving.clear()

    def stop(self) -> None:
        from runtime import CinterForgeAuthentication as _CinterForgeAuthentication
        from runtime import CinterForgeGuestShells as _CinterForgeGuestShells
        from runtime import CinterForgeService as _CinterForgeService
        from runtime import CinterForgeSessions as _CinterForgeSessions
        with self._stopping_lock:
            if self._stopping:
                return
            self._stopping = True
        # Signal the guest startup thread immediately. The native quit and
        # independent watchdog both need it to release the Colima lock before
        # attempting their own owned-VM shutdown.
        _CinterForgeService._guest_boot_cancel.set()
        # Stop every owned browser listener, including VNC and PTY sockets.
        if self.sidecars is not None:
            self.sidecars.stop()
            self.sidecars = None
        # Release the listener FIRST. Slow PTY/Bedrock cleanup must not hold
        # 20300 after a native Quit, Xcode Stop, or an authenticated recovery.
        try:
            if self._http_serving.is_set():
                self.shutdown()
        finally:
            self.server_close()
            if self.lan:
                _CinterForgeAuthentication._remove_lan_owner(self)
        try:
            if self.discovery is not None:
                self.discovery.close()
            for item in list(self.sessions.values()):
                item.close()
            with self.code_sessions_lock:
                code_sessions = list(self.code_sessions.values())
                self.code_sessions.clear()
            for entry in code_sessions:
                if isinstance(entry, _CinterForgeSessions.CinterForgeTerminalSession) and not _CinterForgeGuestShells._is_owned_guest_shell(self, entry):
                    entry.close()
            _CinterForgeGuestShells._close_owned_guest_shells(self)
            self.sessions.clear()
            # Guest VM is application-owned, not HTTP-reload-owned.
            if self.bedrock is not None:
                self.bedrock.stop()
            self.log_event('backend shutdown completed')
        finally:
            self.log.close()


class CinterForgeHTTPRequestHandler(CinterForgeHTTPAuthorization, CinterForgeHTTPCode,
              CinterForgeHTTPRoutes, BaseHTTPRequestHandler):
    """Single request-handler class composed from three explicit API owners."""
    server: CinterForgeHTTPServer

