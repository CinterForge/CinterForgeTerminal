"""CinterForgeHTTPCode — CinterForge Terminal HTTP request owner.

PURPOSE: Privileged Code-OSS shell authorization and Code workspace operations.
METHODS: _code_shell_action, _code_shell_output
STARTUP: Mixed into the single CinterForgeHTTPRequestHandler, no second HTTP listener.
SHUTDOWN: HTTP server owner releases sessions; no guest VM/Build/Test operations here.
DEPENDENCIES: Existing same-origin Python HTTP API and runtime feature owners.
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





class CinterForgeHTTPCode:
    def _code_shell_output(self, code_id: str) -> dict:
        from runtime import CinterForgeGuestShells as _CinterForgeGuestShells
        if not re.fullmatch(r'terminal-[a-z0-9-]{1,48}', code_id):
            raise ValueError('Invalid Code-OSS terminal ID')
        with self.server.code_sessions_lock:
            entry = self.server.code_sessions.get(code_id)
        if entry is None:
            raise FileNotFoundError('Code-OSS terminal is not open')
        if isinstance(entry, dict):
            batch = self.server.log.since(entry['cursor'])
            entry['cursor'] = batch['cursor']
            return {'rows': [{'text': line + '\r\n'} for line in batch['lines']], 'closed': False}
        data, closed = entry.poll(300)
        if closed:
            # The client retains the final transcript, but an exited PTY must
            # not keep occupying one of the six Code-OSS session slots.
            with self.server.code_sessions_lock:
                owned = self.server.code_sessions.get(code_id) is entry
                if owned:
                    self.server.code_sessions.pop(code_id)
            if owned and not _CinterForgeGuestShells._is_owned_guest_shell(self.server, entry):
                entry.close()
        return {'rows': [{'text': data.decode('utf-8', errors='replace')}] if data else [],
                'closed': closed}


    def _code_shell_action(self, route: str, body: dict) -> dict:
        from runtime import CinterForgeGuestShells as _CinterForgeGuestShells
        from runtime import CinterForgeSessions as _CinterForgeSessions
        from runtime import CinterForgeSettings as _CinterForgeSettings
        if route == '/host/code-shell-close-owner':
            owner = body.get('owner')
            if not isinstance(owner, str) or not re.fullmatch(r'[a-z0-9]{1,24}', owner):
                raise ValueError('Invalid Code-OSS owner')
            prefix = 'terminal-' + owner + '-'
            with self.server.code_sessions_lock:
                self.server.code_closed_owners.add(owner)
                # Bound tombstones; closed owners are unguessable short-lived
                # workbench IDs, and this is only an additional race barrier.
                if len(self.server.code_closed_owners) > 256:
                    self.server.code_closed_owners.clear()
                    self.server.code_closed_owners.add(owner)
                names = [name for name in self.server.code_sessions if name.startswith(prefix)]
                entries = [self.server.code_sessions.pop(name) for name in names]
            for entry in entries:
                if isinstance(entry, _CinterForgeSessions.CinterForgeTerminalSession) and not _CinterForgeGuestShells._is_owned_guest_shell(self.server, entry):
                    entry.close()
            return {'closed': len(entries)}
        code_id = body.get('id')
        if not isinstance(code_id, str) or not re.fullmatch(r'terminal-[a-z0-9-]{1,48}', code_id):
            raise ValueError('Invalid Code-OSS terminal ID')
        if route == '/host/code-shell-open':
            backend = body.get('backend', 'native')
            if backend not in ('native', 'server', 'linux', 'win', 'minecraft'):
                raise ValueError('Unknown Code-OSS terminal backend')
            # Dead guest wrappers may exit before their worker can poll the
            # final output. Release those PTYs before checking the bound, but
            # never evict a live shell to make space for another one.
            with self.server.code_sessions_lock:
                ended_ids = [key for key, entry in self.server.code_sessions.items()
                             if isinstance(entry, _CinterForgeSessions.CinterForgeTerminalSession) and entry.closed]
                ended = [self.server.code_sessions.pop(key) for key in ended_ids]
            for entry in ended:
                if not _CinterForgeGuestShells._is_owned_guest_shell(self.server, entry):
                    entry.close()
            with self.server.code_sessions_lock:
                owner = code_id[len('terminal-'):].rsplit('-', 1)[0]
                if owner in self.server.code_closed_owners:
                    raise RuntimeError('Code-OSS workbench is closed; refusing a late PTY open')
                if code_id in self.server.code_sessions:
                    raise ValueError('Code-OSS terminal ID already in use')
                if backend in ('linux', 'win'):
                    # Defense in depth: even multiple Code-OSS clients may not
                    # allocate a second Windows/Linux development shell.
                    if any(getattr(item, 'code_backend', None) == backend
                           for item in self.server.code_sessions.values()):
                        raise RuntimeError(backend.title() + ' Bash is already open in Code-OSS')
                if len(self.server.code_sessions) >= _CinterForgeSettings.MAX_SESSIONS:
                    raise RuntimeError('Maximum Code-OSS terminal sessions reached')
                if backend == 'server':
                    self.server.code_sessions[code_id] = {'cursor': 0}
                    return {'opened': True, 'backend': backend}
                command = None
                if backend == 'minecraft':
                    if self.server.bedrock is None:
                        raise RuntimeError('Minecraft needs an installed Bedrock server')
                    state = self.server.bedrock.status()
                    if not (state.get('running') or state.get('starting')):
                        raise RuntimeError('Minecraft disabled: start Bedrock with bedrock run first')
                    if state.get('console_available') is False:
                        raise RuntimeError('Bedrock console attachment unavailable')
                    # Reuse the exact console bridge from /api/terminal/open.
                    # This is a Code-OSS PTY tab, not a second Bedrock VM.
                    command = [sys.executable, str(_CinterForgeSettings.ASSETS / 'CinterForgeTerminal.py'),
                               '--terminal-cli', 'minecraft']
                elif self.server.system == 'mac' and backend in ('linux', 'win'):
                    # Code-OSS attaches to the already-running app-owned PTY.
                    # Never fork another docker exec, Bash or VM for a tab.
                    with self.server.guest_shells_lock:
                        terminal = self.server.guest_shells.get(backend)
                        if terminal is None or terminal.closed or terminal.process.poll() is not None:
                            # Code-OSS loads while the app-owned VM is still
                            # booting. A pending attach is not a failed Bash:
                            # the adapter retries this same ID after readiness.
                            with self.server.guest_start_lock:
                                state = self.server.guest_start_state
                                cause = self.server.guest_errors.get(backend) or self.server.guest_start_error
                            if state == 'starting':
                                return {'opened': False, 'pending': True, 'backend': backend}
                            raise RuntimeError(backend.title() + ' Bash unavailable: ' +
                                               (cause or ('guest state=' + state)) + '; see Server Log')
                        terminal.code_backend = backend
                        self.server.code_sessions[code_id] = terminal
                        self.server.devtools_requested = True
                    return {'opened': True, 'backend': backend, 'cwd':
                            self.server.code_workspace.details()['cwd'], 'reused': True}
                elif backend != 'native' and backend != self.server.system:
                    raise RuntimeError(backend + ' backend unavailable on ' + self.server.system)
                cwd = Path(self.server.code_workspace.details()['cwd'])
                terminal = _CinterForgeSessions.CinterForgeTerminalSession(
                    self.server.mutable_builds, self.server.system, 100, 30,
                    url=self.server.origin, token=self.server.token,
                    command=command, working_directory=cwd,
                    internal_token=self.server.cli_token)
                terminal.code_backend = backend
                self.server.code_sessions[code_id] = terminal
            return {'opened': True, 'backend': backend, 'cwd': str(cwd)}
        with self.server.code_sessions_lock:
            entry = self.server.code_sessions.get(code_id)
            if route == '/host/code-shell-close':
                self.server.code_sessions.pop(code_id, None)
        if entry is None:
            raise FileNotFoundError('Code-OSS terminal is not open')
        if route == '/host/code-shell-close':
            if isinstance(entry, _CinterForgeSessions.CinterForgeTerminalSession) and not _CinterForgeGuestShells._is_owned_guest_shell(self.server, entry):
                entry.close()
            return {'closed': True}
        if isinstance(entry, dict):
            raise ValueError('Server Log is read-only')
        if route == '/host/code-shell-write':
            entry.write(body.get('data', ''))
            return {'ok': True}
        if route == '/host/code-shell-resize':
            entry.resize(body.get('cols', 100), body.get('rows', 30))
            return {'ok': True}
        raise ValueError('Unknown Code-OSS shell operation')

