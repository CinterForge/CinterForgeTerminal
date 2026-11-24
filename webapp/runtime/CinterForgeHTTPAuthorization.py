"""CinterForgeHTTPAuthorization — CinterForge Terminal HTTP request owner.

PURPOSE: Authorization, LAN login, response serialization, static assets, HTTP methods and audit logging.
METHODS: _authorized, _code_authorized, _json, _lan_logged_in, _local, _login_page, _send, _serve_artifact, do_HEAD, do_OPTIONS, log_message
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





class CinterForgeHTTPAuthorization:
    def _local(self) -> bool:
        """The listener accepts its actual IP, mDNS hostname, and Host aliases.

        Do not use Host-header spelling as an authentication boundary. The
        existing remote login and API authorization are handled separately.
        """
        return getattr(self.server, 'lan', False) or \
            getattr(self, 'client_address', ('127.0.0.1', 0))[0] in ('127.0.0.1', '::1')


    def _lan_logged_in(self) -> bool:
        """Require a login for every LAN HTTP client after ``setpass``.

        A browser connecting through localhost is still a browser. Its peer IP
        is not proof that it is the native WebView, so loopback must not bypass
        a user-configured login. The standalone loopback-only service retains
        its existing native token authentication.
        """
        if not getattr(self.server, 'lan', False) or getattr(self.server, 'lan_login', None) is None:
            return True
        # A native shell helper is a local process, not a browser page. It
        # needs a separate, non-HTML credential for commands such as bedrock
        # and setpass when browser access is password-protected.
        if self.client_address[0] in ('127.0.0.1', '::1'):
            internal = self.headers.get('X-Cinter-CLI-Token', '')
            if internal and secrets.compare_digest(internal, self.server.cli_token):
                return True
        import http.cookies
        try:
            cookie = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
            native = cookie['cinter_native'].value if 'cinter_native' in cookie else ''
            # A native WebView is explicitly bootstrapped from the loopback
            # launcher; a regular localhost browser is NOT implicitly trusted.
            if (native and self.client_address[0] in ('127.0.0.1', '::1') and
                    secrets.compare_digest(native, self.server.native_cookie)):
                return True
            value = cookie['cinter_lan'].value if 'cinter_lan' in cookie else ''
        except (KeyError, http.cookies.CookieError):
            return False
        return secrets.compare_digest(value, self.server.lan_cookie)


    def _code_authorized(self) -> bool:
        """Same-origin, logged-in Code-OSS iframe; no HTML-embedded API token.

        The workbench bootstrap filesystem provider issues ordinary same-origin
        fetch requests. Keep that provider usable without changing the token
        contract for the privileged Terminal APIs.
        """
        origin = self.headers.get('Origin')
        return (self._local() and self._lan_logged_in() and
                self.headers.get('Sec-Fetch-Site', 'same-origin') != 'cross-site' and
                (origin is None or origin == 'http://' + self.headers.get('Host', '')))


    def _login_page(self, failed=False) -> None:
        page = ('<!doctype html><html><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width,initial-scale=1"><title>CinterForge Terminal - LAN login</title>'
                '<style>body{font:18px system-ui;background:#101911;color:#d8eddb;'
                'display:grid;place-content:center;min-height:85vh}form{display:grid;gap:1rem;'
                'min-width:280px}input,button{font:inherit;padding:.5rem}</style>'
                '<h2>CinterForge Terminal</h2><p>Log into the remote development host.</p>' +
                ('<p>Incorrect login.</p>' if failed else
                 '<p>Login not configured. Run setpass &lt;username&gt; &lt;password&gt; on the host Terminal.</p>'
                 if not self.server.lan_login else '') +
                '<form method="post" action="/login">'
                '<label>Username<input name="username" autocomplete="username" required></label>'
                '<label>Access code<input name="password" type="password" autocomplete="current-password" required></label>'
                '<button type="submit">Connect</button></form></html>')
        self._send(401 if failed else 200, page.encode('utf-8'), 'text/html; charset=utf-8')


    def _authorized(self) -> bool:
        origin = self.headers.get('Origin')
        expected = 'http://' + self.headers.get('Host', '')
        host_ok = self._local()
        token_ok = secrets.compare_digest(self.headers.get('X-CPH-Token', ''), self.server.token)
        origin_ok = origin is None or origin == expected
        site = self.headers.get('Sec-Fetch-Site', 'same-origin')
        allowed = host_ok and self._lan_logged_in() and token_ok and origin_ok and site != 'cross-site'
        if not allowed and hasattr(self.server, 'log_event'):
            self.server.log_event('rejected Terminal API request (host/auth/origin)')
        return allowed


    def _send(self, code: int, body: bytes, ctype='application/json; charset=utf-8', *, same_origin_frame: bool = False) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN' if same_origin_frame else 'DENY')
        # The authored atlas embeds its logo as a PNG data URL. Permit data
        # images only for this map document, not scripts or unrelated pages.
        map_document = (same_origin_frame and ctype.startswith('text/html') and
                        urlsplit(self.path).path.startswith('/craftmaster-map/'))
        self.send_header('Referrer-Policy', 'same-origin' if map_document else 'no-referrer')
        images = "img-src 'self' data:; " if map_document else ''
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; style-src 'self' 'unsafe-inline'; " + images + "connect-src 'self' ws://*:20303; frame-src 'self' https://infinitemac.org; object-src 'none'; base-uri 'none'; frame-ancestors " + ("'self'" if same_origin_frame else "'none'"))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass


    def _serve_artifact(self, raw_path: str, *, head: bool = False) -> bool:
        """Serve only the built Code Web tree or one private Lagomorph CD image.

        The Code source tree, Node toolchain, other Builds artifacts and game
        source files are never visible over HTTP. The CD supports byte ranges
        and allows only Infinite Mac to request the mounted content image.
        """
        from runtime import CinterForgeSettings as _CinterForgeSettings
        path = unquote(raw_path)
        # Terminal's own Code-OSS bridge is an authored webapp script, not a
        # built Code-OSS workbench resource. Let the normal protected webapp
        # static resolver serve this exact file.
        if path == '/codeoss/CinterForgeCodeOSS.js':
            return False
        is_code = path.startswith('/codeoss/')
        is_lag = path == '/lagomorph/Lagomorph-Quadra650.iso'
        if not (is_code or is_lag):
            return False
        if is_code:
            rel = path[len('/codeoss/'):]
            if not rel or '\\' in rel or '..' in Path(rel).parts or Path(rel).is_absolute():
                self._json(400, {'error': 'unsafe Code path'})
                return True
            # Production has a single CodeOSS folder directly under the app's
            # Builds payload. The producer's shared Builds input stays in its
            # established location for local Setup and development.
            packaged = self.server.builds / 'CodeOSS'
            root = packaged if packaged.is_dir() else self.server.builds / 'InteractiveFiction/CodeOSS/Web'
            file = root / rel
        else:
            root = self.server.builds / 'Applications/CinterForgeCode/Emulators/Quadra650/Lagomorph'
            file = root / 'Lagomorph-Quadra650.iso'
        # Check the source path before resolving it: symlinks are never
        # accepted as public workbench/image assets, even if they point into
        # an otherwise allowed Builds directory.
        source_root = root
        source_file = file
        if source_root.is_symlink() or any(
                current.is_symlink() for current in
                [source_file, *source_file.parents]
                if current == source_root or current.is_relative_to(source_root)):
            self._json(403, {'error': 'Symlinked product assets cannot be served'})
            return True
        root = source_root.resolve()
        file = source_file.resolve()
        if not file.is_relative_to(root) or not file.is_file():
            self._json(503 if not root.is_dir() else 404,
                       {'error': 'Staged Code or Lagomorph asset unavailable; run ./Cinter setup all'})
            return True
        # Instrument only the staged workbench HTML. A native WebKit failure
        # otherwise produces a white iframe with no diagnostic in Terminal.
        code_workbench = is_code and rel == 'out/vs/code/browser/workbench/workbench.html'
        workbench_body = None
        if code_workbench:
            source = file.read_bytes()
            # The upstream page uses a nonce-bearing script bootstrap. Place
            # the observer before that first script so module startup failures
            # reach Terminal, while retaining the original CSP and assets.
            import re
            marker = re.search(rb'<script\b', source, flags=re.IGNORECASE)
            if marker:
                workbench_body = source[:marker.start()] + _CinterForgeSettings.CODEOSS_OBSERVER + source[marker.start():]
            # Other static fixtures and simple diagnostic HTML keep their
            # original bytes; producer.validate_codeoss_web checks real builds.
        total = len(workbench_body) if workbench_body is not None else file.stat().st_size
        start, end = 0, total - 1
        status = 200
        if is_lag and self.headers.get('Range'):
            value = self.headers['Range']
            if not value.startswith('bytes=') or ',' in value:
                self._json(416, {'error': 'unsupported byte range'})
                return True
            start_text, sep, end_text = value[6:].partition('-')
            try:
                if not sep:
                    raise ValueError()
                if not start_text:
                    suffix = int(end_text)
                    if suffix <= 0:
                        raise ValueError()
                    start = max(0, total - suffix)
                else:
                    start = int(start_text)
                    if end_text:
                        end = min(total - 1, int(end_text))
                if start < 0 or end < start or start >= total:
                    raise ValueError()
            except ValueError:
                self._json(416, {'error': 'invalid byte range'})
                return True
            status = 206
        ctype = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'private, max-age=3600')
        if is_code:
            # VS Code's Web worker needs a broader CSP than the control shell.
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            self.send_header('Content-Security-Policy', "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; connect-src 'self' https:; img-src 'self' data: blob:; font-src 'self' data:; frame-src 'self' blob: https://*.vscode-cdn.net; frame-ancestors 'self'")
        else:
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Access-Control-Allow-Origin', 'https://infinitemac.org')
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
        self.end_headers()
        if not head:
            try:
                if workbench_body is not None:
                    self.wfile.write(workbench_body)
                else:
                    with file.open('rb') as stream:
                        stream.seek(start)
                        remaining = end - start + 1
                        while remaining:
                            block = stream.read(min(1024 * 1024, remaining))
                            if not block:
                                break
                            self.wfile.write(block)
                            remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass
        return True


    def do_HEAD(self) -> None:
        if not self._local():
            self._json(403, {'error': 'invalid host'})
            return
        if not self._serve_artifact(urlsplit(self.path).path, head=True):
            self._json(404, {'error': 'not found'})


    def do_OPTIONS(self) -> None:
        if not self._local() or urlsplit(self.path).path != '/lagomorph/Lagomorph-Quadra650.iso':
            self._json(403, {'error': 'invalid origin or path'})
            return
        if self.headers.get('Origin') != 'https://infinitemac.org':
            self._json(403, {'error': 'unapproved emulator origin'})
            return
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', 'https://infinitemac.org')
        self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Range, Content-Type')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Content-Length', '0')
        self.end_headers()


    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data, separators=(',', ':')).encode('utf-8'))


    def log_message(self, fmt, *args):
        # Log useful errors/terminal lifecycle without recording private bodies,
        # bearer tokens, query strings or every Server tab poll.
        route = urlsplit(self.path).path
        if route in ('/api/server/log', '/api/terminal/poll'):
            return
        status = str(args[1]) if len(args) > 1 else ''
        if status.startswith(('4', '5')) or route in ('/api/terminal/open', '/api/terminal/close'):
            self.server.log_event('HTTP ' + self.command + ' ' + route + ' status=' + status)

