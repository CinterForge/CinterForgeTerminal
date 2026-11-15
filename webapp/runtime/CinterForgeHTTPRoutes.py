"""CinterForgeHTTPRoutes — CinterForge Terminal HTTP request owner.

PURPOSE: Existing GET and POST API dispatch; preserves request URLs, response shapes and token checks.
METHODS: do_GET, do_POST
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





class CinterForgeHTTPRoutes:
    def do_GET(self) -> None:
        from runtime import CinterForgeAuthentication as _CinterForgeAuthentication
        from runtime import CinterForgeDiscord as _CinterForgeDiscord
        from runtime import CinterForgeIF as _CinterForgeIF
        from runtime import CinterForgeSessions as _CinterForgeSessions
        from runtime import CinterForgeSettings as _CinterForgeSettings
        path = urlsplit(self.path).path
        if not self._local():
            self._json(403, {'error': 'invalid host'})
            return
        # Host readiness is not a session and must remain available after
        # setpass; the native launcher probes this route before opening a view.
        if path == '/healthz':
            result = {'ready': True}
            if (self.client_address[0] in ('127.0.0.1', '::1') and
                    secrets.compare_digest(self.headers.get('X-Cinter-Owner', ''),
                                           self.server.native_grant)):
                result['instance'] = self.server.lan_id
            self._json(200, result)
            return
        if path == '/_native_open':
            # The native launcher receives a one-process grant over the Host
            # RPC pipe. Exchange it for an HttpOnly loopback WebView cookie.
            grant = parse_qs(urlsplit(self.path).query).get('key', [''])
            if (len(grant) != 1 or self.client_address[0] not in ('127.0.0.1', '::1') or
                    not secrets.compare_digest(grant[0], self.server.native_grant)):
                self._json(403, {'error': 'native bootstrap unavailable'})
                return
            accept_ui = 'accept-ui' in parse_qs(urlsplit(self.path).query)
            self.send_response(303)
            self.send_header('Location', '/?accept-ui=1' if accept_ui else '/')
            self.send_header('Set-Cookie', 'cinter_native=' + self.server.native_cookie +
                             '; HttpOnly; SameSite=Lax; Path=/')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if not self._lan_logged_in():
            if path in ('/', '/index.html', '/login'):
                self._login_page()
            else:
                self._json(401, {'error': 'LAN login required'})
            return
        if path in ('/host/code-workspace', '/host/code-shell-output'):
            if not self._code_authorized():
                self._json(403, {'error': 'Code-OSS login required'})
                return
            try:
                if path == '/host/code-workspace':
                    guests = self.server.guest_supervisor.snapshot()
                    self._json(200, {**self.server.code_workspace.details(),
                                     'devtools_installed': _CinterForgeSessions._devtools_menu_ready(self.server.system),
                                     'devtools_state': guests['state'],
                                     'devtools_guests': guests['guests'],
                                     'devtools_vnc': guests['vnc']})
                else:
                    code_id = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                    self._json(200, self._code_shell_output(code_id))
            except (OSError, ValueError) as error:
                self._json(400, {'error': str(error)})
            return
        if self._serve_artifact(path):
            return
        if path.startswith('/launcher/'):
            # Fixed, read-only CFOS assets. No user paths or guest files become
            # available through the shared HTTP service. Same-origin iframe.
            rel = unquote(path[len('/launcher/'):])
            if not rel or rel.endswith('/'):
                rel += 'index.html'
            parts = Path(rel)
            root = _CinterForgeSettings.ASSETS / 'launcher'
            candidate = root / parts
            if (parts.is_absolute() or '..' in parts.parts or '\\' in rel or
                    root.is_symlink() or any(item.is_symlink() for item in
                    (candidate, *candidate.parents) if item == root or root in item.parents) or
                    not candidate.is_file()):
                self._json(404, {'error': 'Unknown launcher asset'})
                return
            mime = mimetypes.guess_type(str(candidate))[0] or 'application/octet-stream'
            # The launcher alone embeds the forwarded, localhost-only noVNC
            # viewer; never relax the main Terminal/Code/IF CSP for this.
            if rel == 'index.html':
                body = candidate.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('X-Frame-Options', 'SAMEORIGIN')
                self.send_header('Content-Security-Policy',
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; connect-src 'self'; "
                    "frame-src http://*:20301 http://*:20302; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(200, candidate.read_bytes(), mime)
            return
        if path == '/api/discord/messages':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            params = parse_qs(urlsplit(self.path).query)
            channel_id = params.get('channel', [''])[0]
            if channel_id not in self.server.discord_channels:
                self._json(403, {'error': 'Discord channel not opened in this Terminal'})
                return
            before, after = params.get('before', [''])[0], params.get('after', [''])[0]
            if ((before and after) or any(not re.fullmatch(r'[0-9]{1,22}', value)
                                             for value in (before, after) if value)):
                self._json(400, {'error': 'Invalid Discord history cursor'})
                return
            try:
                query = '?limit=100' + ('&before=' + before if before else '') + ('&after=' + after if after else '')
                messages = _CinterForgeDiscord._discord_api('/channels/' + channel_id + '/messages' + query)
                if not isinstance(messages, list):
                    raise RuntimeError('Discord messages response invalid')
                self._json(200, {'messages': [{'id': str(m.get('id', '')),
                  'author': str(m.get('author', {}).get('global_name') or m.get('author', {}).get('username') or '?')[:80],
                  'content': str(m.get('content') or '')[:2000],
                  'timestamp': str(m.get('timestamp') or '')[:40]} for m in sorted(messages, key=lambda m: int(m.get('id') or 0))],
                  'has_more': len(messages) == 100})
            except (RuntimeError, ValueError) as error:
                self._json(400, {'error': str(error)})
            return
        if path == '/api/lan/info':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            self._json(200, {'enabled': self.server.lan, 'port': _CinterForgeAuthentication.LAN_HTTP_PORT,
                             'name': socket.gethostname(),
                             'username': self.server.lan_login['username'] if self.server.lan_login else None,
                             'configured': self.server.lan_login is not None})
            return
        if path == '/api/lan/peers':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            self._json(200, {'peers': self.server.discovery.peers() if self.server.discovery else []})
            return
        if path == '/api/server/log':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            raw = parse_qs(urlsplit(self.path).query).get('cursor', ['0'])[0]
            try:
                self._json(200, self.server.log.since(int(raw)))
            except (ValueError, OverflowError) as error:
                self._json(400, {'error': str(error)})
            return
        if path == '/api/lifecycle/guest-status':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            self._json(200, self.server.guest_supervisor.snapshot())
            return
        if path == '/api/status':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            guests = self.server.guest_supervisor.snapshot()
            self._json(200, {'platform': self.server.system, 'sessions': len(self.server.sessions),
                             'debug': True, 'devtools_installed':
                             _CinterForgeSessions._devtools_menu_ready(self.server.system),
                             'devtools_state': guests['state'], 'devtools_guests': guests['guests'],
                             'devtools_vnc': guests['vnc']})
            return
        if path.startswith('/zmachine/'):
            # Exact scoped staged runtime, no arbitrary Builds path access.
            # Upstream TypeScript emits extensionless ESM imports (for example
            # ./core/ZMachine). Browsers request those paths verbatim; Python's
            # static server must resolve them to the compiled .js files.
            # Keep all resolution inside the staged dist tree, not the source repo.
            rel = unquote(path[len('/zmachine/'):])
            root = self.server.builds / 'CinterForgeTerminal/Dependencies/ZMachine/0.3.2/dist'
            member = Path(rel)
            if (not rel or '\\' in rel or member.is_absolute() or
                    any(part in ('', '.', '..') for part in rel.split('/'))):
                self._json(400, {'error': 'Invalid ZMachine module path'})
                return
            candidate = root / member
            options = (candidate,) if candidate.suffix else (candidate.with_suffix('.js'), candidate / 'index.js')
            file = next((p for p in options if p.is_file() and not p.is_symlink() and
                         all(not parent.is_symlink() for parent in p.parents if parent != root and root in parent.parents)), None)
            if file is None:
                self._json(404, {'error': 'ZMachine compiled module missing: ' + rel})
                return
            # Normalize upstream TypeScript's extensionless ESM specifiers in
            # the response itself. WebKit requires resolvable module URLs and
            # some deployed hosts do not route extensionless imports reliably.
            # This leaves the SHA-verified compiled artifact untouched.
            source = file.read_text(encoding='utf-8')
            def explicit_js(match):
                spec = match.group('spec')
                target = (file.parent / spec).resolve()
                if not target.is_relative_to(root.resolve()):
                    return match.group(0)
                if target.suffix == '' and target.with_suffix('.js').is_file():
                    return match.group(0).replace(spec, spec + '.js')
                if target.is_dir() and (target / 'index.js').is_file():
                    return match.group(0).replace(spec, spec.rstrip('/') + '/index.js')
                return match.group(0)
            source = re.sub(
                r'''(?P<head>\b(?:from\s*|import\s*|import\s*\(\s*)[\"'])(?P<spec>\.{1,2}/[^\"']+)(?P<tail>[\"'])''',
                explicit_js, source,
            )
            self._send(200, source.encode('utf-8'), 'text/javascript; charset=utf-8')
            return
        if path.startswith('/if/player/'):
            from html import escape
            name = path[len('/if/player/'):]
            if name not in _CinterForgeIF._if_catalog():
                self._json(404, {'error':'Unknown IF title'})
                return
            # Child WebView owns its interpreter; unloading the frame stops it.
            markup = ('<!doctype html><html data-if-name="'+escape(name,quote=True)+'" '
                      'data-cph-token="'+self.server.token+'"><meta charset="utf-8">'
                      '<title>'+escape(name)+'</title><style>'
                      'body{margin:0;padding:1rem;background:#101a12;color:#b5f6a1;font:16px monospace}'
                      '#if-output{min-height:60vh;white-space:pre-wrap;overflow:auto}'
                      '#if-input{font:inherit;width:95%;background:#102d17;color:inherit}'
                      '</style><div id="if-status"></div><div id="if-output"></div>'
                      '<input id="if-input" aria-label="Story command" autofocus>'
                      '<button id="if-stop" type="button">Stop game</button>'
                      + ''.join('<script src="/' + item + '" defer></script>'
                                for item in _CinterForgeSettings.CLIENT_SCRIPTS)
                      + '</html>')
            # Only the dedicated player document may be embedded by this same-origin Terminal.
            self._send(200, markup.encode(), 'text/html; charset=utf-8', same_origin_frame=True)
            return
        if path.startswith('/api/if/save/'):
            if not self._authorized():
                self._json(403, {'error':'not authorized'})
                return
            try:
                self._json(200, _CinterForgeIF._if_load_save(self.server, unquote(path[len('/api/if/save/'):])))
            except (ValueError, FileNotFoundError, OSError, RuntimeError, json.JSONDecodeError) as exc:
                self._json(400, {'error':str(exc)})
            return
        if path.startswith('/api/if/story/'):
            if not self._authorized():
                self._json(403,{'error':'not authorized'})
                return
            try:
                self._send(200,_CinterForgeIF._if_story(self.server,path[len('/api/if/story/'):]),
                           'application/octet-stream')
            except (ValueError,FileNotFoundError,OSError) as exc:
                self._json(404,{'error':str(exc)})
            return
        if path == '/api/if/next':
            if not self._authorized():
                self._json(403,{'error':'not authorized'})
                return
            with self.server.debug_lock:
                action = self.server.if_actions.popleft() if self.server.if_actions else None
            self._json(200,action or {})
            return
        if path == '/api/lagomorph/manual':
            if not self._authorized():
                self._json(403,{'error':'not authorized'})
                return
            root = self.server.builds / 'Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph'
            manual = root / 'Lagomorph Manual.pdf'
            if (root.is_symlink() or manual.is_symlink() or not manual.is_file() or
                    manual.stat().st_size > 40*1024*1024 or manual.read_bytes()[:4] != b'%PDF'):
                self._json(503,{'error':'Missing staged original Lagomorph PDF manual'})
                return
            self._send(200,manual.read_bytes(),'application/pdf')
            return
        if path == '/api/lagomorph/media':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            try:
                self._json(200, _CinterForgeIF._lagomorph_media_url(self.server.builds))
            except (ValueError, OSError, UnicodeError) as error:
                self._json(400, {'error': str(error)})
            return
        if path == '/api/ui/html-menus':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            with self.server.debug_lock:
                self._json(200, {'show': self.server.system != 'mac' or
                                 self.server.html_menus_override})
            return
        if path == '/api/code/next':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            with self.server.debug_lock:
                action = self.server.code_actions.popleft() if self.server.code_actions else None
            self._json(200, {'open': bool(action), 'reload': bool(action and action['reload'])})
            return
        if path == '/api/wargames/next':
            if not self._authorized():
                self._json(403, {'error':'not authorized'})
                return
            with self.server.debug_lock:
                ready = self.server.wargames_requests > 0
                if ready:
                    self.server.wargames_requests -= 1
            self._json(200, {'open':ready})
            return
        if path == '/api/macgames/next':
            if not self._authorized():
                self._json(403, {'error':'not authorized'})
                return
            with self.server.debug_lock:
                request = self.server.macgame_request
                self.server.macgame_request = None
            self._json(200, {'game':request})
            return
        if path == '/api/lagomorph/next':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            with self.server.debug_lock:
                ready = self.server.lagomorph_requests > 0
                if ready:
                    self.server.lagomorph_requests -= 1
            self._json(200, {'open': ready, 'profile': 'quadra-650' if ready else None})
            return
        if path in ('/api/bedrock/log', '/api/bedrock/events', '/api/bedrock/status', '/api/bedrock/worlds', '/api/bedrock/lan') and self.server.bedrock is None:
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
            else:
                self._json(503, {'error': 'Bedrock requires a host Steam library: '
                                 + self.server.bedrock_error})
            return
        if path == '/api/bedrock/terminal/next':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            with self.server.bedrock_terminal_lock:
                pending = self.server.bedrock_terminal_requests.popleft() if self.server.bedrock_terminal_requests else None
            self._json(200, pending or {})
            return
        if path == '/api/bedrock/console/output':
            if self.server.bedrock is None:
                self._json(503, {'error': self.server.bedrock_error or 'Bedrock unavailable'})
                return
            raw = parse_qs(urlsplit(self.path).query).get('offset', ['-1'])[0]
            try:
                offset = int(raw)
            except ValueError:
                self._json(400, {'error': 'Invalid console offset'})
                return
            self._json(200, self.server.bedrock.console_output(offset))
            return
        if path == '/api/bedrock/log':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            self._json(200, self.server.bedrock.log_tail())
            return
        if path == '/api/bedrock/events':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            try:
                cursor = int(urlsplit(self.path).query.removeprefix('cursor=')) if urlsplit(self.path).query else 0
                self._json(200, self.server.bedrock.events_since(cursor))
            except (ValueError, OverflowError) as error:
                self._json(400, {'error': str(error)})
            return
        if path == '/api/bedrock/lan':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            self._json(200, self.server.bedrock.lan_discovery())
            return
        if path == '/api/bedrock/worlds':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            self._json(200, self.server.bedrock.list_worlds())
            return
        if path == '/api/bedrock/status':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            status = self.server.bedrock.status()
            with self.server.eula_lock:
                status['eula_requested'] = self.server.eula_requested
            self._json(200, status)
            return
        if path.startswith('/joshua/'):
            # Staged standalone JOSHUA, same-origin embedded game tab.
            # No arbitrary filesystem traversal, links, or remote fetch.
            relative=path[len('/joshua/'):]
            parts=Path(relative).parts
            root=_CinterForgeSettings.ASSETS / 'joshua'
            if (not parts or any(part in ('..','.') or part.startswith('.') for part in parts)
                    or '\\' in relative or root.is_symlink()):
                self._json(404, {'error':'Unknown JOSHUA asset'});return
            asset=root.joinpath(*parts)
            if asset.is_symlink() or not asset.is_file():
                self._json(404, {'error':'Unknown JOSHUA asset'});return
            mime=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream'
            self._send(200,asset.read_bytes(),mime,same_origin_frame=asset.name=='index.html')
            return
        # The native UI is always immediately available. With DevTools the
        # read-only Server tab shows startup; no splash replaces index.html.
        info = _CinterForgeSettings.STATIC_FILES.get(path)
        if not info:
            self._json(404, {'error': 'not found'})
            return
        name, ctype = info
        file = _CinterForgeSettings.ASSETS / name
        if not file.is_file():
            self._json(503, {'error': f'Incomplete shell package: {name}'})
            return
        data = file.read_bytes()
        if name == 'index.html':
            data = data.replace(b'__CPH_TOKEN__', self.server.token.encode('ascii'))
            data = data.replace(b'__CPH_PLATFORM__', self.server.system.encode('ascii'))
        self._send(200, data, ctype)


    def do_POST(self) -> None:
        from runtime import CinterForgeAuthentication as _CinterForgeAuthentication
        from runtime import CinterForgeDiscord as _CinterForgeDiscord
        from runtime import CinterForgeGuestShells as _CinterForgeGuestShells
        from runtime import CinterForgeIF as _CinterForgeIF
        from runtime import CinterForgeLaunchers as _CinterForgeLaunchers
        from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
        from runtime import CinterForgeService as _CinterForgeService
        from runtime import CinterForgeSessions as _CinterForgeSessions
        from runtime import CinterForgeSettings as _CinterForgeSettings
        from runtime import CinterForgeVMOperations as _CinterForgeVMOperations
        route = urlsplit(self.path).path
        if route == '/_native_orphan_shutdown':
            # An orphan can terminate ITSELF, never a PID supplied by a caller.
            # Only a loopback caller with the private grant/instance may ask.
            allowed = (self.client_address[0] in ('127.0.0.1', '::1') and
                       secrets.compare_digest(self.headers.get('X-Cinter-Owner', ''),
                                              self.server.native_grant) and
                       secrets.compare_digest(self.headers.get('X-Cinter-Instance', ''),
                                              self.server.lan_id) and
                       os.getppid() != self.server.native_parent_pid)
            if not allowed:
                self._json(403, {'error': 'Orphaned native owner verification required'})
                return
            self._json(200, {'stopping': True, 'instance': self.server.lan_id})
            def release_orphan() -> None:
                from runtime import CinterForgeService as _CinterForgeService
                pass
                try:
                    self.server.stop()
                finally:
                    if _CinterForgeService._server is self.server:
                        _CinterForgeService._server = None
            threading.Thread(target=release_orphan, name='cinter-http-orphan-recovery',
                             daemon=True).start()
            return
        if route == '/code/command' or route in (
                '/host/code-shell-open', '/host/code-shell-write',
                '/host/code-shell-close', '/host/code-shell-close-owner',
                '/host/code-shell-resize'):
            if not self._code_authorized():
                self._json(403, {'error': 'Code-OSS login required', 'error_code': 'permission_denied'})
                return
            body = {}
            try:
                n = int(self.headers.get('Content-Length', '0'))
                limit = 12 * 1024 * 1024 if route == '/code/command' else _CinterForgeSettings.MAX_BODY
                if not 0 < n <= limit:
                    raise ValueError('Invalid Code-OSS request length')
                body = json.loads(self.rfile.read(n))
                if not isinstance(body, dict):
                    raise ValueError('Expected a Code-OSS object')
                if route == '/code/command':
                    result = self.server.code_workspace.operation(body)
                    if body.get('operation') in ('open-folder', 'open-workspace'):
                        # A workspace change replaces the workbench and its PTYs.
                        # Release the old sessions before new tabs connect.
                        with self.server.code_sessions_lock:
                            old_sessions = list(self.server.code_sessions.values())
                            self.server.code_sessions.clear()
                        for item in old_sessions:
                            if isinstance(item, _CinterForgeSessions.CinterForgeTerminalSession) and not _CinterForgeGuestShells._is_owned_guest_shell(self.server, item):
                                item.close()
                else:
                    result = self._code_shell_action(route, body)
                self._json(200, {'ok': True, **result})
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, ImportError) as error:
                code = ('permission_denied' if isinstance(error, PermissionError) else
                        'not_directory' if isinstance(error, NotADirectoryError) else
                        'not_found' if isinstance(error, FileNotFoundError) else 'invalid_request')
                # VS Code probes optional paths continually. Report a missing
                # workspace file through the existing JSON error contract, but
                # not as an HTTP transport failure on every stat/read probe.
                expected_missing = (route == '/code/command' and code == 'not_found' and
                                    body.get('operation') in ('stat', 'read-file', 'read-directory'))
                if not expected_missing:
                    operation = str(body.get('operation', '') if route == '/code/command' else route)
                    # Retain the actual cause of a failed guest attachment;
                    # "RuntimeError" alone loses the diagnostic on user Macs.
                    detail = str(error).replace('\r', ' ').replace('\n', ' ')[:240]
                    self.server.log_event('Code-OSS request rejected ' + operation[:48] +
                                          ' (' + code + ', ' + type(error).__name__ +
                                          '): ' + detail)
                self._json(200 if expected_missing else 400,
                           {'ok': False, 'error': str(error), 'error_code': code})
            return
        if route in ('/api/lifecycle/reboot', '/api/lifecycle/guest-boot'):
            # The same private per-process token works for the normal Terminal
            # shell. The desktop Cinter CLI uses the native owner's private
            # 0600 receipt/grant and must be on loopback; remote LAN clients
            # cannot obtain either privilege merely by knowing the URL.
            native_owner = (self.client_address[0] in ('127.0.0.1', '::1') and
                            secrets.compare_digest(self.headers.get('X-Cinter-Owner', ''),
                                                   self.server.native_grant))
            if not (native_owner or self._authorized()):
                self._json(403, {'error': 'Native Terminal lifecycle authorization required'})
                return
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if n < 1 or n > _CinterForgeSettings.MAX_BODY:
                    raise ValueError('Invalid lifecycle request length')
                body = json.loads(self.rfile.read(n))
                if not isinstance(body, dict):
                    raise ValueError('Expected lifecycle object')
                if route == '/api/lifecycle/guest-boot':
                    if body:
                        raise ValueError('Guest boot takes no options')
                    result = self.server.guest_supervisor.schedule()
                    self._json(202 if result['state'] == 'starting' else 200, result)
                else:
                    component = body.get('component')
                    if component not in ('server', 'docker', 'colima', 'app'):
                        raise ValueError('Usage: reboot server|docker|colima|app')
                    if component in ('server', 'app'):
                        with self.server.debug_lock:
                            if self.server.lifecycle_actions:
                                raise RuntimeError('A lifecycle reboot is already pending')
                            service = ''
                            if component == 'server' and os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'developer':
                                # Development-only hot reload. The native Host
                                # retains its staged Python and worker; the new
                                # server module and web assets come from the
                                # *existing* sibling authored checkout. Never
                                # use a source checkout from an installed app.
                                mutable = self.server.mutable_builds.resolve(strict=True)
                                candidate = mutable.parent / 'CinterForgeTerminal/webapp/CinterForgeTerminal.py'
                                if (mutable.name == 'Builds' and candidate.is_file() and
                                        not candidate.is_symlink() and
                                        candidate.resolve().parent.parent == mutable.parent / 'CinterForgeTerminal'):
                                    service = str(candidate.resolve())
                            self.server.lifecycle_actions.append({'component': component, 'service': service})
                        self._json(202, {'accepted': component, 'status': 'queued'})
                    else:
                        # Reboot is an app-owned lifecycle transition, not a
                        # second invisible boot when a tab is opened.
                        with self.server.guest_start_lock:
                            if self.server.guest_start_state == 'starting':
                                raise RuntimeError('Managed guests are still starting; wait for readiness')
                            self.server.guest_start_state = 'starting'
                            self.server.guest_start_error = ''
                            self.server.guest_ready = []
                            self.server.guest_vnc_ready = []
                            self.server.guest_errors = {}
                        self.server.log_event('[DEVTOOLS] Rebooting ' + component +
                                              '; all guest menus disabled until reverified')
                        try:
                            _CinterForgeGuestShells._close_owned_guest_shells(self.server)
                            result = _CinterForgeVMOperations.reboot_runtime_guests(component)
                            sdk = _CinterForgeRuntimeBootstrap.sdk_root()
                            if sdk is None:
                                raise RuntimeError('DevTools disappeared during guest reboot')
                            with self.server.guest_start_lock:
                                # reboot_runtime_guests verified both RFB/HTTP
                                # endpoints independently of interactive PTYs.
                                self.server.guest_vnc_ready = list(result['guests'])
                            shell_errors = {}
                            for kind in result['guests']:
                                try:
                                    connected = _CinterForgeGuestShells._new_owned_guest_shell(self.server, kind, sdk)
                                    with self.server.guest_shells_lock:
                                        self.server.guest_shells[kind] = connected
                                    with self.server.guest_start_lock:
                                        self.server.guest_ready.append(kind)
                                except Exception as shell_error:
                                    shell_errors[kind] = str(shell_error)
                                    self.server.log_event('[DEVTOOLS] ' + kind +
                                                          ' Bash reconnect FAILED: ' + str(shell_error))
                            with self.server.guest_start_lock:
                                self.server.guest_errors = shell_errors
                                self.server.guest_start_state = 'failed' if shell_errors else 'ready'
                                self.server.guest_start_error = '; '.join(
                                    k + ': ' + v for k, v in shell_errors.items())
                            self.server.log_event('[DEVTOOLS] Reboot completed; VNC verified; ' +
                                                  str(len(self.server.guest_ready)) + ' Bash connections verified')
                        except Exception as error:
                            with self.server.guest_start_lock:
                                self.server.guest_start_state = 'failed'
                                self.server.guest_start_error = str(error)
                                self.server.guest_ready = []
                                self.server.guest_vnc_ready = []
                                self.server.guest_errors = {'linux': str(error), 'win': str(error)}
                            self.server.log_event('[DEVTOOLS] Reboot FAILED; guest menus remain disabled: ' + str(error))
                            raise
                        self._json(200, result)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                self.server.log_event('Lifecycle request FAILED: ' + str(error))
                self._json(400, {'error': str(error)})
            return
        if self.path == '/login' and self.server.lan and self._local() and self.server.lan_login is not None:
            # The only requirement for login is the user-configured credential.
            # A normal HTML form may be submitted from localhost, .local or a LAN IP.
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if n < 1 or n > 2048:
                    raise ValueError('invalid login length')
                fields = parse_qs(self.rfile.read(n).decode('utf-8'), keep_blank_values=True)
                valid = (len(fields.get('username', [])) == 1 and
                         len(fields.get('password', [])) == 1 and
                         _CinterForgeAuthentication._check_lan_login(self.server.lan_login,
                                          fields['username'][0], fields['password'][0]))
            except (ValueError, UnicodeDecodeError):
                valid = False
            if not valid:
                self._login_page(failed=True)
                return
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', 'cinter_lan=' + self.server.lan_cookie +
                             '; HttpOnly; SameSite=Strict; Path=/')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if not self._authorized():
            self._json(403, {'error': 'not authorized'})
            return
        try:
            n = int(self.headers.get('Content-Length', '0'))
            route = self.path.split('?', 1)[0]
            limit = 2 * 1024 * 1024 if route.startswith('/api/if/save/') else _CinterForgeSettings.MAX_BODY
            if not (0 < n <= limit):
                raise ValueError('Invalid request length')
            body = json.loads(self.rfile.read(n))
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object')
            if route == '/api/launcher/start':
                if self.server.system != 'mac':
                    raise ValueError('Cross-platform Launchers are only available on the Mac host')
                kind = body.get('platform')
                if kind not in ('mac', 'linux', 'win'):
                    raise ValueError('Unknown graphical launcher platform')
                result = {'platform': kind} if kind == 'mac' else _CinterForgeLaunchers._launcher_start(kind, self.server)
                self._json(200, {'ready': True, **result})
                return
            if route == '/api/launcher/launch':
                if self.server.system != 'mac':
                    raise ValueError('Cross-platform Launchers are only available on the Mac host')
                kind, app = body.get('platform'), body.get('app')
                if kind not in ('mac', 'linux', 'win') or not isinstance(app, str):
                    raise ValueError('Invalid graphical launcher request')
                self._json(200, _CinterForgeLaunchers._launcher_launch(kind, app, self.server))
                return
            if route == '/api/lan/setpass':
                # The command must originate inside a live, authenticated Terminal
                # session. Do not expose credentials through LAN info or logs.
                source = str(body.get('session_id', ''))
                with self.server.sessions_lock:
                    if source not in self.server.sessions:
                        raise ValueError('setpass requires an active Terminal shell')
                username = body.get('username')
                password = body.get('password')
                with self.server.lan_login_lock:
                    self.server.lan_login = _CinterForgeAuthentication._save_lan_login(
                        self.server.mutable_builds, username, password)
                    # Force already logged-in browsers to use the new credentials.
                    self.server.lan_cookie = secrets.token_urlsafe(36)
                # Existing anonymous WebSockets must not remain connected
                # indefinitely after setpass enables browser authentication.
                if self.server.sidecars is not None:
                    self.server.sidecars.disconnect_clients()
                self.server.log_event('LAN login updated through Terminal command')
                self._json(200, {'updated': True})
                return
            if route == '/api/discord/open':
                name = str(body.get('username', '')).strip()
                guild = str(body.get('guild', '')).strip()
                channel = str(body.get('channel', '')).strip()
                source_id = str(body.get('session_id', ''))
                with self.server.sessions_lock:
                    if source_id not in self.server.sessions:
                        raise ValueError('Discord request must come from an active terminal Session')
                resolved = _CinterForgeDiscord._discord_resolve(guild, channel)
                with self.server.debug_lock:
                    if len(self.server.discord_actions) >= 16:
                        raise RuntimeError('Discord tab queue full; retry after opening a Terminal')
                    self.server.discord_channels[resolved['channel_id']] = resolved
                    self.server.discord_actions.append({**resolved, 'requested_username': name,
                                                        'session_id': source_id})
                self._json(200, {'opening': True, 'bot': resolved['bot'],
                                 'guild': resolved['guild'], 'channel': resolved['channel']})
                return
            if route == '/api/discord/next':
                active = body.get('session_ids', [])
                if not isinstance(active, list) or len(active) > _CinterForgeSettings.MAX_SESSIONS:
                    raise ValueError('Invalid Discord session list')
                with self.server.debug_lock:
                    found = next((item for item in self.server.discord_actions
                                 if item['session_id'] in active), None)
                    if found is not None:
                        self.server.discord_actions.remove(found)
                self._json(200, found or {})
                return
            if route == '/api/discord/send':
                channel_id = str(body.get('channel_id', ''))
                message = body.get('content', '')
                if (channel_id not in self.server.discord_channels or
                        not isinstance(message, str) or not message.strip() or len(message) > 2000):
                    raise ValueError('Invalid Discord channel or message')
                _CinterForgeDiscord._discord_api('/channels/' + channel_id + '/messages', 'POST', {'content': message, 'allowed_mentions': {'parse': []}})
                self._json(200, {'sent': True})
                return
            if route.startswith('/api/if/save/'):
                _CinterForgeIF._if_store_save(self.server, unquote(route[len('/api/if/save/'):]), body)
                self._json(200, {'saved':True})
                return
            if route == '/api/bedrock/eula':
                with self.server.eula_lock:
                    self.server.eula_requested = True
                self._json(200, {'requested': True, 'message': 'Review EULA in the WebView'})
                return
            if route == '/api/bedrock/eula/ack':
                with self.server.eula_lock:
                    self.server.eula_requested = False
                self._json(200, {'ok': True})
                return
            if route.startswith('/api/bedrock/') and self.server.bedrock is None and route not in ('/api/bedrock/eula', '/api/bedrock/eula/ack'):
                raise RuntimeError('Bedrock requires a host Steam library: ' + self.server.bedrock_error)
            if route == '/api/bedrock/accept':
                # Only the trusted displayed WebView can accept the license.
                # Native terminal commands may request the dialog but cannot
                # silently record consent on the user's behalf.
                if self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
                    self._json(403, {'error': 'EULA acceptance requires the application WebView'})
                    return
                with self.server.eula_lock:
                    if not self.server.eula_requested:
                        self._json(403, {'error': 'Display the Bedrock EULA before accepting'})
                        return
                self.server.bedrock.accept(body.get('accepted'))
                # Consent is the user's install action. Begin fetching the
                # official verified server now, not on startup and not after a
                # second manual command. A worker keeps the WebView responsive.
                with self.server.eula_lock:
                    self.server.eula_requested = False
                self._json(200, self.server.bedrock.begin_install())
                return
            if route == '/api/bedrock/install':
                # Optional retry for an already accepted EULA, never a second
                # way to bypass consent or a request for package.json.
                self._json(200, self.server.bedrock.begin_install())
                return
            if route == '/api/bedrock/path':
                self._json(200, self.server.bedrock.set_world_path(body.get('path', '')))
                return
            if route == '/api/bedrock/command':
                self._json(200, self.server.bedrock.console_command(body.get('command')))
                return
            if route == '/api/bedrock/run':
                self._json(200, _CinterForgeSessions.open_bedrock_terminal(self.server,
                    str(body.get('name', '')), body.get('cols', 80), body.get('rows', 24)))
                return
            if route == '/api/bedrock/restart':
                selected = (self.server.bedrock._world_settings().get('selected') or {})
                if not selected.get('folder'):
                    raise RuntimeError('No selected Bedrock save to restart; type bedrock start')
                self._json(200, _CinterForgeSessions.open_bedrock_terminal(self.server,
                    str(selected['folder']), body.get('cols', 80), body.get('rows', 24), restart=True))
                return
            if route == '/api/bedrock/start':
                # `start` only lists saves. Never start an invisible background VM.
                self._json(200, self.server.bedrock.list_worlds())
                return
            if route == '/api/bedrock/stop':
                self._json(200, self.server.bedrock.stop())
                return
            if route == '/api/terminal/open':
                backend=body.get('backend', 'native')
                self.server.log_event('opening terminal session: backend=' + str(backend) +
                    ' platform=' + self.server.system)
                if backend not in ('native','mac','linux','win','minecraft'):
                    raise ValueError('Unknown Terminal backend')
                # On macOS these are Code-OSS development-only shells. Do not
                # create a guest Bash Session through direct API calls either.
                if self.server.system == 'mac' and backend in ('linux', 'win'):
                    raise ValueError(backend.title() + ' Bash is available only inside Code-OSS')
                command=None
                if backend=='minecraft':
                    if self.server.bedrock is None:
                        raise RuntimeError('Minecraft needs an installed Bedrock server')
                    state=self.server.bedrock.status()
                    if not (state.get('running') or state.get('starting')):
                        raise RuntimeError('Minecraft disabled: start Bedrock first with bedrock run')
                    if state.get('console_available') is False:
                        raise RuntimeError('Bedrock console attachment unavailable')
                    command=[sys.executable, str(_CinterForgeSettings.ASSETS/'CinterForgeTerminal.py'),'--terminal-cli','minecraft']
                elif backend in ('mac','linux','win') and backend!=self.server.system:
                    raise RuntimeError(backend+' Bash unavailable on '+self.server.system+'; refusing native-shell fallback')
                with self.server.sessions_lock:
                    if len(self.server.sessions) >= _CinterForgeSettings.MAX_SESSIONS:
                        raise ValueError('Maximum terminal sessions reached')
                    terminal = _CinterForgeSessions.CinterForgeTerminalSession(self.server.mutable_builds, self.server.system,
                                               body.get('cols', 80), body.get('rows', 24),
                                               url=self.server.origin, token=self.server.token,
                                               command=command,
                                               working_directory=Path(self.server.code_workspace.details()['cwd']),
                                               internal_token=self.server.cli_token)
                    self.server.sessions[terminal.id] = terminal
                self.server.log_event('terminal opened: id=' + terminal.id[:10] +
                    ' command=' + Path(terminal.shell[0]).name + ' output=' + str(terminal.output_log.name if terminal.output_log else 'none'))
                self._json(200, {'id': terminal.id, 'shell': 'Minecraft' if backend=='minecraft' else (backend.title()+' Bash' if self.server.system == 'mac' and backend in ('win','linux') else Path(terminal.shell[0]).name),
                                 'platform': backend if backend!='native' else self.server.system})
                return
            if route in ('/api/if/start','/api/if/stop'):
                command = str(body.get('command',''))
                if command not in _CinterForgeIF._if_catalog():
                    raise ValueError('Unknown story filename command')
                source = body.get('session_id')
                if source is not None:
                    if not isinstance(source,str) or not re.fullmatch(r'[0-9a-f]{32}', source):
                        raise ValueError('Invalid source terminal Session ID')
                    with self.server.sessions_lock:
                        if source not in self.server.sessions:
                            raise ValueError('Source terminal Session has closed')
                if route.endswith('/start'):
                    _CinterForgeIF._if_story(self.server,command)  # Verify installed story first.
                    with self.server.debug_lock:
                        if len(self.server.if_actions) >= 64:
                            raise RuntimeError('IF command queue full; retry after browser connects')
                        # Duplicate launches reselect the same story tab. A new
                        # generation is minted only after the previous stops.
                        launch = self.server.if_active.get(command)
                        if not launch:
                            launch = uuid.uuid4().hex
                            self.server.if_active[command] = launch
                        self.server.if_actions.append({'start':command,
                            'launch_id':launch, 'session_id':source})
                    self._json(200,{'requested':True,'launch_id':launch})
                else:
                    requested = body.get('launch_id')
                    if requested is not None and (not isinstance(requested,str) or
                            not re.fullmatch(r'[0-9a-f]{32}',requested)):
                        raise ValueError('Invalid IF launch ID')
                    with self.server.debug_lock:
                        launch = self.server.if_active.get(command)
                        stopped = bool(launch and (requested is None or requested == launch))
                        if stopped:
                            if len(self.server.if_actions) >= 64:
                                raise RuntimeError('IF command queue full; retry after browser connects')
                            del self.server.if_active[command]
                            self.server.if_actions.append({'stop':command,'launch_id':launch})
                    self._json(200,{'stopped':stopped})
                return
            if route == '/api/ui/html-menus':
                source = str(body.get('session_id', ''))
                with self.server.sessions_lock:
                    in_main = any(getattr(item, 'id', None) == source
                                  for item in self.server.sessions.values())
                with self.server.code_sessions_lock:
                    in_code = any(getattr(item, 'id', None) == source
                                  for item in self.server.code_sessions.values())
                if not (in_main or in_code):
                    raise ValueError('HTML menu override requires a live Terminal Session')
                if self.server.system != 'mac':
                    raise ValueError('HTML menus are already visible on this platform')
                action = body.get('action')
                if action not in ('show', 'hide', 'toggle'):
                    raise ValueError('Usage: htmlmenus show|hide|toggle')
                with self.server.debug_lock:
                    if action == 'toggle':
                        self.server.html_menus_override = not self.server.html_menus_override
                    else:
                        self.server.html_menus_override = action == 'show'
                    visible = self.server.html_menus_override
                self._json(200, {'show': visible})
                return
            if route == '/api/code/open':
                source_id = str(body.get('session_id', ''))
                with self.server.sessions_lock:
                    if source_id not in self.server.sessions:
                        raise ValueError('Code request requires an active Terminal Session')
                action = body.get('operation', 'run')
                if action == 'run':
                    result = self.server.code_workspace.details()
                elif action == 'mode':
                    result = self.server.code_workspace.operation({
                        'operation': 'set-theme', 'theme': body.get('theme')})
                elif action == 'open':
                    path = body.get('path')
                    if not isinstance(path, str) or not path:
                        raise ValueError('code open requires an existing absolute path')
                    kind = 'open-workspace' if path.endswith('.code-workspace') else 'open-folder'
                    result = self.server.code_workspace.operation({'operation': kind, 'path': path})
                else:
                    raise ValueError('Usage: code run | code mode light|dark | code open "<path>"')
                if action == 'open':
                    # Workspace switches close the old host-owned Code PTYs. The
                    # workbench recreates them with the new working directory.
                    with self.server.code_sessions_lock:
                        previous = list(self.server.code_sessions.values())
                        self.server.code_sessions.clear()
                    for item in previous:
                        if isinstance(item, _CinterForgeSessions.CinterForgeTerminalSession) and not _CinterForgeGuestShells._is_owned_guest_shell(self.server, item):
                            item.close()
                with self.server.debug_lock:
                    self.server.code_actions.append({'reload': action != 'run'})
                self._json(200, {'requested': True, **result})
                return
            if route == '/api/wargames/open':
                with self.server.debug_lock:
                    self.server.wargames_requests += 1
                self._json(200, {'requested': True})
                return
            if route == '/api/macgames/open':
                command = body.get('command')
                if not isinstance(command,str) or command not in _CinterForgeSessions._mac_catalog():
                    raise ValueError('Unknown Macintosh game command')
                with self.server.debug_lock:
                    self.server.macgame_request = command
                self._json(200,{'requested':True,'command':command})
                return
            if route == '/api/lagomorph/open':
                with self.server.debug_lock:
                    self.server.lagomorph_requests += 1
                self._json(200, {'requested': True})
                return
            if route == '/api/debugger':
                # Already authenticated, strictly loopback-only, and polled by
                # the native Host. A second, ambient environment-variable gate
                # blocked the actual installed application even after login.
                with self.server.debug_lock:
                    self.server.debug_requests += 1
                self._json(200, {'requested': True})
                return
            with self.server.sessions_lock:
                item = self.server.sessions.get(str(body.get('id', '')))
            if not item:
                self._json(404, {'error': 'Unknown terminal session'})
                return
            if route == '/api/terminal/input':
                item.write(body.get('data', ''))
                self._json(200, {'ok': True})
            elif route == '/api/terminal/poll':
                data, closed = item.poll(body.get('wait_ms', 100))
                with self.server.eula_lock:
                    eula_requested = self.server.eula_requested
                self._json(200, {'output': base64.b64encode(data).decode('ascii'),
                                 'closed': closed, 'bedrock_eula_requested': eula_requested})
            elif route == '/api/terminal/resize':
                item.resize(body.get('cols', 80), body.get('rows', 24))
                self._json(200, {'ok': True})
            elif route == '/api/terminal/close':
                with self.server.sessions_lock:
                    self.server.sessions.pop(item.id, None)
                item.close()
                self._json(200, {'ok': True})
            else:
                self._json(404, {'error': 'Not found'})
        except (ValueError, TypeError, RuntimeError, PermissionError, OSError, json.JSONDecodeError) as exc:
            route = urlsplit(self.path).path
            if route == '/api/terminal/open':
                self.server.log_event('SHELL START FAILURE: ' + repr(exc) +
                    '\n' + traceback.format_exc(limit=8))
                self._json(400, {'error': 'Shell start failed: ' + str(exc) +
                    ' (see Server tab: ' + str(self.server.log.path) + ')'})
            else:
                self.server.log_event('API failure on ' + route + ': ' + repr(exc))
                self._json(400, {'error': str(exc)})
        except Exception as exc:
            route = urlsplit(self.path).path
            self.server.log_event('UNEXPECTED API EXCEPTION at ' + route + ': ' + repr(exc) +
                                  '\n' + traceback.format_exc(limit=12))
            self._json(500, {'error': 'Internal Terminal failure; see Server log at ' +
                                  str(self.server.log.path)})

