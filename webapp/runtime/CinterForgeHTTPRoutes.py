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
from html import escape as html_escape
from html.parser import HTMLParser
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



# One URL root maps to the one staged webapp directory. Dynamic API handlers
# run before this function; Python sources and hidden files are never static.
# This is a file-type safety policy, not a per-filename route registry.
_BROWSER_ASSET_EXTENSIONS = frozenset({
    '.avif', '.css', '.gif', '.html', '.ico', '.jpeg', '.jpg', '.js',
    '.json', '.map', '.mp3', '.mp4', '.ogg', '.pdf', '.png', '.svg',
    '.ttf', '.txt', '.wasm', '.wav', '.webm', '.webp', '.woff', '.woff2',
})


def _webapp_static_file(root: Path, url_path: str) -> Path | None:
    """Resolve /path directly into the staged webapp/, never outside it."""
    path = unquote(url_path)
    if not path.startswith('/') or path.startswith('//'):
        return None
    relative = 'index.html' if path == '/' else path[1:]
    members = relative.split('/')
    if ('\\' in relative or '\x00' in relative or
            any(not member or member in ('.', '..') or member.startswith('.') for member in members) or
            (members[0] == 'runtime' or (members[0] == 'codeoss' and relative != 'codeoss/CinterForgeCodeOSS.js')) or
            (Path(members[-1]).suffix.lower() not in _BROWSER_ASSET_EXTENSIONS and
             relative != 'ocr/eng.traineddata.gz')):
        return None
    if not root.is_dir() or root.is_symlink():
        return None
    asset = root.joinpath(*members)
    # Reject symlinked parents as well as final symlink targets. A generic
    # root-relative mapping must never expose host files through a link.
    if asset.is_symlink() or any(parent.is_symlink() for parent in asset.parents if parent == root or root in parent.parents):
        return None
    if not asset.is_file() or not asset.resolve().is_relative_to(root.resolve()):
        return None
    return asset


def _mac_games_report_icons(server, catalog: dict) -> dict[str, str]:
    """Only serve GameIcons explicitly matched to the canonical 61-game catalog.

    This is a read-only shared Builds/Assets library, not a copy in the app
    bundle. The same manifest and PNG bytes are published/restored by Assets.
    """
    from runtime.assets.CinterForgeAssetViews import root_for
    library = root_for(server) / 'GameIcons'
    manifest = library / 'manifest.json'
    if (library.is_symlink() or manifest.is_symlink() or not manifest.is_file()
            or manifest.stat().st_size > 1024 * 1024):
        return {}
    document = json.loads(manifest.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or not isinstance(document.get('games'), list):
        raise ValueError('Invalid GameIcons manifest')
    matched = {}
    for record in document['games']:
        if not isinstance(record, dict):
            continue
        command = record.get('host_command')
        game = catalog.get(command) if isinstance(command, str) else None
        if not game or record.get('os') != game.get('guest_os') or record.get('status') != 'extracted':
            continue
        relative = record.get('file')
        folder = 'System71' if game['guest_os'] == '7.1' else 'MacOS76'
        if (not isinstance(relative, str) or not re.fullmatch(
                re.escape(folder) + r'/[a-z0-9_-]+\.png', relative)):
            continue
        candidate = library.joinpath(*relative.split('/'))
        if (candidate.is_symlink() or candidate.parent.is_symlink() or not candidate.is_file()
                or candidate.stat().st_size > 1024 * 1024):
            continue
        matched[command] = relative
    return matched


class _IndexScripts(HTMLParser):
    """The authored index.html is the single source for classic script order."""
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != 'script':
            return
        src = dict(attrs).get('src')
        if src is None:
            return
        # Only scripts in our own staged webapp may be loaded into the player.
        if (not src.startswith('/') or src.startswith('//') or '\\' in src or
                '\x00' in src or '?' in src or '#' in src or
                any(part in ('', '.', '..') for part in src[1:].split('/'))):
            raise RuntimeError('Invalid script reference in bundled index.html: ' + src)
        self.sources.append(src)


def _player_script_tags(index: Path) -> str:
    parser = _IndexScripts()
    parser.feed(index.read_text(encoding='utf-8'))
    parser.close()
    if not parser.sources:
        raise RuntimeError('Bundled index.html has no client scripts')
    return ''.join('<script defer src="' + html_escape(src, quote=True) + '"></script>'
                   for src in parser.sources)


class CinterForgeHTTPRoutes:
    def _native_if_display(self):
        """Only the native host WebView may drive an in-world interpreter."""
        import http.cookies
        try:
            cookies = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
            native = cookies['cinter_native'].value if 'cinter_native' in cookies else ''
        except (KeyError, http.cookies.CookieError):
            native = ''
        return (self.client_address[0] in ('127.0.0.1', '::1') and
                bool(native) and secrets.compare_digest(native, self.server.native_cookie))

    def _display_fiction(self):
        manager = getattr(self.server, 'bedrock', None)
        session = getattr(manager, '_terminal_session', None)
        return getattr(session, 'display_fiction', None) if session and not session.closed else None

    def _if_save_screen(self):
        screen = parse_qs(urlsplit(self.path).query).get('screen')
        if screen is None:
            return None
        if (not self._native_if_display() or len(screen) != 1 or
                not re.fullmatch(r'cinter_[0-9a-f]{32}', screen[0])):
            raise ValueError('Invalid in-world story save scope')
        return screen[0]

    def do_GET(self) -> None:
        from runtime import CinterForgeAuthentication as _CinterForgeAuthentication
        from runtime import CinterForgeCraftMasterMap as _CinterForgeCraftMasterMap
        from runtime.assets import CinterForgeAssetViews as _CinterForgeAssetViews
        from runtime import CinterForgeDiscord as _CinterForgeDiscord
        from runtime import CinterForgeState as _CinterForgeIF
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
        if path == '/runtime-identity':
            if not self.server.capabilities.get('assets'):
                self._json(404, {'error': 'Assets unavailable'})
            else:
                self._json(200, {'ok': True, 'service': 'cinterforge-ifplayer'})
            return
        if path.startswith('/api/assets/blockify/job/'):
            # Original IFPlayer's saved Asset Browser makes normal same-origin
            # fetches; it does not possess Terminal's privileged native API token.
            # The job READ endpoint is restricted to that same-origin viewer or
            # to a genuinely token-authorized native client.
            browser_origin = (self._code_authorized() and
                              self.headers.get('Sec-Fetch-Site') == 'same-origin')
            if not self.server.capabilities.get('assets') or not (browser_origin or self._authorized()):
                self._json(403, {'error': 'not authorized'})
                return
            from runtime.assets import CinterForgeAssetOriginal as original
            try:
                self._json(200, original.blockify_job(self.server, unquote(path.split('/')[-1]), include_rows=parse_qs(urlsplit(self.path).query).get('summary') != ['1']))
            except (ValueError, FileNotFoundError) as exc:
                self._json(404, {'error': str(exc)})
            return
        if path.startswith('/blockbench/'):
            if not self.server.capabilities.get('assets'):
                self._json(404, {'error': 'Not found'})
                return
            if _CinterForgeAssetViews.serve_voxel(self, path):
                return
        if path.startswith('/assets-library/'):
            if not self.server.capabilities.get('assets') and not path.startswith('/assets-library/packs/'):
                self._json(404, {'error': 'Not found'})
                return
            if _CinterForgeAssetViews.serve(self, path):
                return
        if path == '/bedrock-report/index.html':
            page = _CinterForgeSettings.ASSETS / 'views/BedrockReport.html'
            if not self._code_authorized() or not page.is_file() or page.is_symlink():
                self._json(403, {'error': 'Bedrock report unavailable'})
            else:
                # The report is supplied by CraftMaster; only the desktop console adapter is ours.
                body = page.read_bytes().replace(b'</body>', b'<script src="/client/minecraft/CinterForgeJavaReport.js" defer></script></body>', 1)
                self._send(200, body, 'text/html; charset=utf-8', same_origin_frame=True)
            return
        if path == '/api/bedrock/report/java':
            if not self._code_authorized():
                self._json(403, {'error': 'Not authorized'})
                return
            from runtime.CinterForgeJava import report_status
            self._json(200, report_status(self.server))
            return
        if path == '/api/bedrock/report':
            if not self._code_authorized() or self.server.bedrock is None:
                self._json(403, {'error': 'Bedrock report unavailable'})
                return
            try:
                inventory = self.server.bedrock.report_worlds()
                def browser_world(world):
                    return {key: world.get(key) for key in
                            ('id', 'name', 'command_name', 'edition', 'current',
                             'conversion', 'storage_error', 'pack')} | {
                                'thumbnail': self.server.bedrock.world_thumbnail(world) is not None,
                                'downloadable': self.server.bedrock.is_builtin_candlewood(world)}
                self._json(200, {'worlds': [browser_world(w) for w in inventory['worlds']],
                                 'java_worlds': [browser_world(w) for w in inventory['java_worlds']],
                                 'current_world': inventory['current_world'],
                                 'current_pack': inventory['current_pack'],
                                 'running': inventory['running'],
                                 'java_error': inventory['java_error']})
            except (RuntimeError, OSError, ValueError) as exc:
                self._json(400, {'error': str(exc)})
            return
        if path in ('/api/bedrock/world/icon', '/api/bedrock/world/download'):
            if not self._code_authorized() or self.server.bedrock is None:
                self._json(403, {'error': 'Not authorized'})
                return
            try:
                identifier = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                world = self.server.bedrock.world_by_id(identifier)
                if path.endswith('/icon'):
                    icon = self.server.bedrock.world_thumbnail(world)
                    if icon is None:
                        self._json(404, {'error': 'Minecraft has no world thumbnail'})
                        return
                    mime = 'image/png' if icon.suffix.lower() == '.png' else 'image/jpeg'
                    self._send(200, icon.read_bytes(), mime)
                    return
                if not self.server.bedrock.is_builtin_candlewood(world):
                    self._json(404, {'error': 'Only built-in Candlewood supports Download'})
                    return
                archive, disposable = self.server.bedrock.export_world_file(world, self.server.mutable_builds)
                try:
                    from urllib.parse import quote
                    name = str(world['command_name']).replace('/', '_').replace('\\', '_') + '.mcworld'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(name, safe=''))
                    self.send_header('Content-Length', str(archive.stat().st_size))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    with archive.open('rb') as stream:
                        for chunk in iter(lambda: stream.read(1024*1024), b''):
                            self.wfile.write(chunk)
                finally:
                    if disposable:
                        archive.unlink(missing_ok=True)
            except (RuntimeError, OSError, ValueError, FileNotFoundError, zipfile.BadZipFile) as exc:
                try:
                    self._json(400, {'error': str(exc)})
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return
        if path == '/api/craftmaster/map/status':
            if not self._code_authorized():
                self._json(403, {'error': 'not authorized'})
                return
            try:
                mid = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                self._json(200, _CinterForgeCraftMasterMap.map_progress(self.server, mid))
            except ValueError as exc:
                self._json(404, {'error': str(exc)})
            return
        if _CinterForgeCraftMasterMap.serve_map_asset(self, path):
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
                             'devtools_vnc': guests['vnc'],
                             'capabilities': dict(self.server.capabilities)})
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
                      + _player_script_tags(_CinterForgeSettings.ASSETS / 'index.html')
                      + '</html>')
            # Only the dedicated player document may be embedded by this same-origin Terminal.
            self._send(200, markup.encode(), 'text/html; charset=utf-8', same_origin_frame=True)
            return
        if path == '/api/if/display/next':
            if not self._authorized() or not self._native_if_display():
                self._json(403, {'error': 'Native Terminal WebView required'})
                return
            fiction = self._display_fiction()
            self._json(200, fiction.next() if fiction else {'action': None, 'active': []})
            return
        if path.startswith('/api/if/save/'):
            if not self._authorized():
                self._json(403, {'error':'not authorized'})
                return
            try:
                self._json(200, _CinterForgeIF._if_load_save(self.server, unquote(path[len('/api/if/save/'):]), screen=self._if_save_screen()))
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
        if path == '/api/infinitemac/media':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            self._json(200, _CinterForgeIF._infinitemac_media_url())
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
        if path == '/api/games/catalog':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            catalog = _CinterForgeSessions._mac_catalog()
            try:
                icons = _mac_games_report_icons(self.server, catalog)
            except (OSError, RuntimeError, ValueError):
                icons = {}  # Games remain launchable if Assets have not been set up.
            self._json(200, {'games': [
                {'command': command, 'title': game['title'], 'os': game['guest_os'],
                 'icon': ('/game-icons/' + icons[command]) if command in icons else None}
                for command, game in catalog.items()], 'count': len(catalog), 'icons': len(icons)})
            return
        if path.startswith('/game-icons/'):
            # Only known PNGs from the trusted manifest; never expose arbitrary Assets.
            if not self._code_authorized():
                self._json(403, {'error': 'not authorized'})
                return
            relative = path[len('/game-icons/'):]
            try:
                from runtime.assets.CinterForgeAssetViews import root_for
                allowed = _mac_games_report_icons(self.server, _CinterForgeSessions._mac_catalog())
                if relative not in allowed.values():
                    raise FileNotFoundError('Unknown game icon')
                root = root_for(self.server) / 'GameIcons'
                image = root.joinpath(*relative.split('/'))
                if (root.is_symlink() or image.is_symlink() or image.parent.is_symlink()
                        or not image.is_file()):
                    raise FileNotFoundError('Game icon missing')
                self._send(200, image.read_bytes(), 'image/png')
            except (OSError, RuntimeError, ValueError):
                self._json(404, {'error': 'Game icon unavailable'})
            return
        if path == '/api/games/verification':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            # Return copies, not the live mutable host-owned records.
            with self.server.debug_lock:
                inventory = json.loads(json.dumps(self.server.game_verification))
            self._json(200, {'verification': inventory})
            return
        if path == '/api/macgames/next':
            if not self._authorized():
                self._json(403, {'error':'not authorized'})
                return
            with self.server.debug_lock:
                request = self.server.macgame_request
                self.server.macgame_request = None
            self._json(200, request or {'game': None})
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
        if path in ('/api/bedrock/log', '/api/bedrock/events', '/api/bedrock/status', '/api/bedrock/worlds', '/api/bedrock/lan', '/api/bedrock/access', '/api/bedrock/properties') and self.server.bedrock is None:
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
            else:
                self._json(503, {'error': 'Bedrock requires a host Steam library: '
                                 + self.server.bedrock_error})
            return
        if path == '/api/assets/packs/current':
            if not self._local() or not self._lan_logged_in():
                self._json(403, {'error': 'not authorized'})
                return
            try:
                self._json(200, _CinterForgeAssetViews.pack_current(self.server))
            except (ValueError, OSError, RuntimeError) as exc:
                self._json(400, {'error': str(exc)})
            return
        if path == '/api/assets/next':
            if not self._authorized():
                self._json(404, {'error': 'Not found'})
                return
            with self.server.asset_actions_lock:
                action = self.server.asset_actions.popleft() if self.server.asset_actions else None
            self._json(200, action or {'kind': None})
            return
        if path == '/api/craftmaster/map/next':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            with self.server.craftmaster_map_lock:
                pending = (self.server.craftmaster_map_requests.popleft()
                           if self.server.craftmaster_map_requests else None)
            self._json(200, pending or {})
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
        if path == '/api/bedrock/properties':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            try:
                self._json(200, self.server.bedrock.property_snapshot())
            except (OSError, ValueError, RuntimeError) as error:
                self._json(400, {'error': str(error)})
            return
        if path == '/api/bedrock/access':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            self._json(200, self.server.bedrock.access.snapshot())
            return
        if path == '/api/bedrock/worlds':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            try:
                worlds = self.server.bedrock.list_worlds()
            except Exception as error:
                # Never drop the HTTP connection on world-discovery failures;
                # preserve the full traceback in the persistent Server log.
                self.server.log_event('[BEDROCK] World discovery failed: '
                                      + traceback.format_exc())
                self._json(500, {'error': 'Bedrock world discovery failed: ' + str(error)})
                return
            self._json(200, worlds)
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
        file = _webapp_static_file(_CinterForgeSettings.ASSETS, path)
        if file is None:
            self._json(404, {'error': 'not found'})
            return
        mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
        if file.suffix.lower() in ('.js', '.css', '.html', '.json', '.txt', '.svg'):
            mime += '; charset=utf-8'
        data = file.read_bytes()
        if file.name == 'index.html' and file.parent == _CinterForgeSettings.ASSETS:
            data = data.replace(b'__CPH_TOKEN__', self.server.token.encode('ascii'))
            data = data.replace(b'__CPH_PLATFORM__', self.server.system.encode('ascii'))
        self._send(200, data, mime)


    def do_POST(self) -> None:
        from runtime import CinterForgeAuthentication as _CinterForgeAuthentication
        from runtime import CinterForgeCraftMasterMap as _CinterForgeCraftMasterMap
        from runtime.assets import CinterForgeAssetViews as _CinterForgeAssetViews
        from runtime import CinterForgeDiscord as _CinterForgeDiscord
        from runtime import CinterForgeGuestShells as _CinterForgeGuestShells
        from runtime import CinterForgeState as _CinterForgeIF
        from runtime import CinterForgeLaunchers as _CinterForgeLaunchers
        from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
        from runtime import CinterForgeService as _CinterForgeService
        from runtime import CinterForgeSessions as _CinterForgeSessions
        from runtime import CinterForgeSettings as _CinterForgeSettings
        from runtime import CinterForgeVMOperations as _CinterForgeVMOperations
        route = urlsplit(self.path).path
        if _CinterForgeCraftMasterMap.write_map_annotation(self, route):
            return
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
        # Reuse original IFPlayer's same-origin JSON command transport without
        # publishing the privileged CPH token into WebView JavaScript. Only the
        # Assets command plane and its job progress receiver accept this browser
        # route; import-from-host, CLI, and every other Terminal API retain their
        # existing native token requirement. An exact Origin and fetch-site are
        # mandatory for browser writes (no permissive missing-Origin fallback).
        route = self.path.split('?', 1)[0]
        browser_world_actions = ('/api/bedrock/world/select', '/api/bedrock/report/open',
                                 '/api/bedrock/run', '/api/bedrock/start', '/api/bedrock/stop',
                                 '/api/bedrock/eula', '/api/bedrock/pack/set', '/api/assets/packs/open',
                                 '/api/bedrock/report/java',
                                 '/api/craftmaster/map/open', '/api/craftmaster/map/run',
                                 '/api/assets/packs/download')
        browser_asset_write = (route in (('/assets/command', '/api/assets/view',
                                         '/api/assets/blockify/progress', '/api/assets/packs/set',
                                         '/api/assets/packs/download') + browser_world_actions)
                               and (self.server.capabilities.get('assets') or route in
                                    (('/api/assets/packs/set', '/api/assets/packs/download') + browser_world_actions))
                               and self._code_authorized()
                               and self.headers.get('Sec-Fetch-Site') == 'same-origin'
                               and self.headers.get('Origin') == 'http://' + self.headers.get('Host', ''))
        if not browser_asset_write and not self._authorized():
            self._json(403, {'error': 'not authorized'})
            return
        try:
            n = int(self.headers.get('Content-Length', '0'))
            # The ORIGINAL IFPlayer asset-migration-save command transmits
            # complete Babylon GLTF exports (model JSON, buffers and textures)
            # as base64 JSON. The general 64 KiB Terminal control limit rejects
            # practically every 3D asset before original Server sees it.
            # This larger bound is limited to the one asset-authoring endpoint.
            limit = (256 * 1024 * 1024 if route == '/assets/command' else
                     2 * 1024 * 1024 if (route.startswith('/api/if/save/') or route == '/api/assets/upload/chunk')
                     else _CinterForgeSettings.MAX_BODY)
            if not (0 < n <= limit):
                raise ValueError('Invalid request length')
            body = json.loads(self.rfile.read(n))
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object')
            if route in ('/api/infinitemac/game-icons', '/api/infinitemac/game-icons/finish'):
                # Accept only authenticated native-loopback requests. Unlike
                # optional Asset/DevTools authoring, the raw framebuffer is
                # supplied by the existing embedded Infinite Mac WebView.
                if self.client_address[0] not in ('127.0.0.1', '::1'):
                    self._json(403, {'error': 'Icon capture requires the local Terminal host'})
                    return
                os_version = body.get('os')
                versions = {'7.1': ('System-7.1', 50), '7.6': ('Mac-OS-7.6', 32)}
                if not isinstance(os_version, str) or os_version not in versions:
                    raise ValueError('Unknown Games Launcher operating system')
                folder, expected = versions[os_version]
                from runtime.assets import CinterForgeAssetViews as _GameIconAssets
                library = _GameIconAssets.root_for(self.server)
                # No guessed ~/Source or installed-app Builds fallback. The
                # user-selected shared Assets root is the only destination.
                destination = library / 'Games' / 'Icons' / folder
                for directory in (library / 'Games', library / 'Games' / 'Icons', destination):
                    if directory.is_symlink():
                        raise ValueError('Games icon asset destination is a symlink')
                destination.mkdir(parents=True, exist_ok=True)
                if route == '/api/infinitemac/game-icons':
                    slot = body.get('slot')
                    encoded = body.get('png_base64')
                    if (type(slot) is not int or slot < 1 or slot > expected or
                            not isinstance(encoded, str) or len(encoded) > 48 * 1024):
                        raise ValueError('Invalid Games Launcher icon slot or PNG data')
                    raw = base64.b64decode(encoded, validate=True)
                    # A screenshot crop is precisely 32x32 in the original
                    # guest resolution, not a resized CSS/browser screenshot.
                    if (len(raw) < 45 or len(raw) > 32 * 1024 or
                            raw[:8] != b'\x89PNG\r\n\x1a\n' or
                            raw[12:16] != b'IHDR' or
                            struct.unpack('>II', raw[16:24]) != (32, 32) or
                            raw[-12:-8] != b'\x00\x00\x00\x00' or raw[-8:-4] != b'IEND'):
                        raise ValueError('Games Launcher icon must be a 32x32 PNG')
                    output = destination / f'slot-{slot:02d}.png'
                    if output.is_symlink():
                        raise ValueError('Games Launcher icon path is a symlink')
                    changed = not output.is_file() or output.read_bytes() != raw
                    if changed:
                        temporary = destination / (f'.slot-{slot:02d}-{uuid.uuid4().hex}.tmp')
                        try:
                            temporary.write_bytes(raw)
                            temporary.replace(output)
                        finally:
                            temporary.unlink(missing_ok=True)
                    self._json(200, {'saved': True, 'changed': changed, 'slot': slot})
                    return
                captured = body.get('captured')
                missing = body.get('missing')
                blank_slots = body.get('blank_slots', [])
                if (not isinstance(captured, list) or not isinstance(missing, list) or
                        not isinstance(blank_slots, list) or
                        any(type(n) is not int or n < 1 or n > expected for n in blank_slots) or
                        not set(blank_slots).issubset(captured) or
                        any(type(n) is not int or n < 1 or n > expected for n in captured + missing) or
                        len(set(captured + missing)) != expected or
                        set(captured).intersection(missing)):
                    raise ValueError('Incomplete or inconsistent Games Launcher grid inventory')
                if any(not (destination / f'slot-{slot:02d}.png').is_file() for slot in captured):
                    raise ValueError('A Games Launcher PNG is missing from the asset library')
                manifest = {
                    'source': 'Infinite Mac native 68K framebuffer; OCR-detected Games Launcher',
                    'os': os_version, 'columns': 8 if os_version == '7.1' else 6,
                    'rows': 8 if os_version == '7.1' else 6,
                    'expected': expected, 'captured': len(captured), 'missing_slots': sorted(missing),
                    'possible_blank_slots': sorted(set(blank_slots)),
                    'image_size': [32, 32],
                    # Slots are deliberately positional. The guest's dynamic
                    # Finder-name ordering must not be guessed from the older
                    # webapp game aliases or filename lists.
                    'icons': [{'slot': slot, 'file': f'slot-{slot:02d}.png'}
                              for slot in sorted(captured)]}
                output = destination / 'manifest.json'
                if output.is_symlink():
                    raise ValueError('Games Launcher manifest path is a symlink')
                data = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode('utf-8')
                if not output.is_file() or output.read_bytes() != data:
                    temporary = destination / ('.manifest-' + uuid.uuid4().hex + '.tmp')
                    try:
                        temporary.write_bytes(data)
                        temporary.replace(output)
                    finally:
                        temporary.unlink(missing_ok=True)
                self.server.log_event('Games Launcher icons captured: os=' + os_version +
                                      ' icons=' + str(len(captured)) + '/' + str(expected) +
                                      ' path=' + str(destination))
                self._json(200, {'saved': True, 'count': len(captured),
                                 'directory': str(destination)})
                return
            if route == '/assets/command':
                if not self.server.capabilities.get('assets') or not self._local() or not self._lan_logged_in():
                    self._json(403, {'ok': False, 'error': 'not authorized'})
                    return
                from runtime.assets import CinterForgeAssetOriginal as original
                result = original.authoring_command(self.server, body)
                self._json(200, result)
                return
            if route.startswith('/api/assets/'):
                if not self.server.capabilities.get('assets') and route not in ('/api/assets/packs/open', '/api/assets/packs/set', '/api/assets/packs/download'):
                    self._json(404, {'error': 'Not found'})
                    return
                # Only a native Terminal session may request arbitrary host
                # file paths; browser-origin uploads use an opaque upload token.
                if route in ('/api/assets/import', '/api/assets/picker', '/api/assets/blockify/start', '/api/assets/delete'):
                    sid = body.get('session_id')
                    with self.server.sessions_lock:
                        session_live = isinstance(sid, str) and sid in self.server.sessions
                    if not session_live or self.client_address[0] not in ('127.0.0.1', '::1') or not self.headers.get('X-Cinter-CLI-Token') or not secrets.compare_digest(self.headers.get('X-Cinter-CLI-Token', ''), self.server.cli_token):
                        self._json(403, {'error': 'Assets file commands require a native Terminal session'})
                        return
                if route == '/api/assets/packs/open':
                    result = _CinterForgeAssetViews.pack_open(self.server)
                elif route == '/api/assets/packs/set':
                    result = _CinterForgeAssetViews.pack_set_current(self.server, body.get('name'))
                elif route == '/api/assets/packs/download':
                    result = _CinterForgeAssetViews.pack_download(self.server, body.get('name'))
                elif route == '/api/assets/import':
                    result = _CinterForgeAssetViews.import_path(self.server, body.get('path'))
                elif route == '/api/assets/delete':
                    from runtime.assets import CinterForgeAssetOriginal as original
                    result = original.delete_uci(self.server, str(body.get('uci') or ''))
                elif route == '/api/assets/picker':
                    result = _CinterForgeAssetViews.queue_tab(self.server, 'picker')
                elif route == '/api/assets/report':
                    result = (_CinterForgeAssetViews.report(self.server) if body.get('refresh_only') is True
                              else _CinterForgeAssetViews.queue_tab(self.server, 'report'))
                elif route == '/api/assets/view':
                    result = _CinterForgeAssetViews.queue_tab(self.server, 'view', uci=body.get('uci'))
                elif route == '/api/assets/blockify/start':
                    from runtime.assets import CinterForgeAssetOriginal as original
                    result = original.start_blockify(self.server, body.get('path'))
                elif route == '/api/assets/blockify/progress':
                    from runtime.assets import CinterForgeAssetOriginal as original
                    result = original.blockify_progress(self.server, body)
                elif route == '/api/assets/upload/start':
                    result = _CinterForgeAssetViews.upload_start(self.server, body.get('name'), body.get('size'))
                elif route == '/api/assets/upload/chunk':
                    result = _CinterForgeAssetViews.upload_chunk(self.server, body.get('id'), body.get('offset'), body.get('data'))
                elif route == '/api/assets/upload/finish':
                    result = _CinterForgeAssetViews.upload_finish(self.server, body.get('id'), body.get('sha256'))
                elif route == '/api/assets/upload/cancel':
                    result = _CinterForgeAssetViews.upload_cancel(self.server, body.get('id'))
                else:
                    self._json(404, {'error': 'Not found'})
                    return
                self._json(200, result)
                return
            if route == '/api/startup/speech':
                # WebKit's Web Speech voice list can omit an installed Zarvox;
                # macOS must use the native voice, never a silent substitute.
                # Only the native loopback WebView may play host audio.
                import http.cookies
                try:
                    cookies = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
                    native = cookies['cinter_native'].value if 'cinter_native' in cookies else ''
                except (KeyError, http.cookies.CookieError):
                    native = ''
                if (self.server.system != 'mac' or
                        self.client_address[0] not in ('127.0.0.1', '::1') or
                        not native or not secrets.compare_digest(native, self.server.native_cookie)):
                    self._json(403, {'error': 'Native Mac startup voice unavailable'})
                    return
                phrases = {'welcome': 'Welcome to CinterForge.',
                           'question': 'Shall... we... play... eh... game?'}
                if set(body) != {'part'} or body['part'] not in phrases:
                    raise ValueError('Unknown startup speech segment')
                speaker = Path('/usr/bin/say')
                error = ''
                try:
                    if not speaker.is_file():
                        error = 'macOS say binary unavailable'
                    else:
                        # Invoke the same native command that users can test
                        # with `say -v Zarvox`; WebKit's voice list is irrelevant.
                        spoken = subprocess.run([str(speaker), '-v', 'Zarvox',
                                                 phrases[body['part']]],
                                                capture_output=True, text=True,
                                                stdin=subprocess.DEVNULL, timeout=15, check=False)
                        if spoken.returncode != 0:
                            detail = (spoken.stderr or '').strip().splitlines()
                            error = 'macOS Zarvox speech failed (exit ' + str(spoken.returncode) + ')'
                            if detail:
                                error += ': ' + detail[0][:200]
                except (OSError, subprocess.TimeoutExpired) as exc:
                    error = 'macOS Zarvox speech unavailable: ' + type(exc).__name__
                if error:
                    self.server.log_event('[VOICE] ' + error)
                self._json(200, {'spoken': not bool(error), 'voice': 'Zarvox',
                                 **({'error': error} if error else {})})
                return
            if route == '/api/infinitemac/clipboard-event':
                # Same-origin, authenticated host observation only. The browser
                # reports a matched clipboard reply; it does not authorize a
                # guest command or claim the emulator booted merely by loading.
                os_version = body.get('os')
                kind = body.get('kind')
                if os_version not in ('7.1', '7.6', '8.1') or kind not in ('ready', 'ack'):
                    raise ValueError('Invalid Infinite Mac clipboard event')
                if kind == 'ready':
                    self.server.log_event('[INFINITE MAC] CINTERFORGE_READY ' + os_version +
                                          ' (matching clipboard HELLO token returned)')
                else:
                    self.server.log_event('[INFINITE MAC] cinter:' + os_version +
                                          ':ack received from 68K clipboard')
                self._json(200, {'logged': True})
                return
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
                if not self.server.capabilities.get('discord'):
                    raise ValueError('Unknown command')
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
                if not self.server.capabilities.get('discord'):
                    raise ValueError('Unknown command')
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
                if not self.server.capabilities.get('discord'):
                    raise ValueError('Unknown command')
                channel_id = str(body.get('channel_id', ''))
                message = body.get('content', '')
                if (channel_id not in self.server.discord_channels or
                        not isinstance(message, str) or not message.strip() or len(message) > 2000):
                    raise ValueError('Invalid Discord channel or message')
                _CinterForgeDiscord._discord_api('/channels/' + channel_id + '/messages', 'POST', {'content': message, 'allowed_mentions': {'parse': []}})
                self._json(200, {'sent': True})
                return
            if route == '/api/if/display/result':
                if not self._native_if_display():
                    self._json(403, {'error': 'Native Terminal WebView required'})
                    return
                fiction = self._display_fiction()
                self._json(200, {'accepted': bool(fiction and fiction.complete(body))})
                return
            if route.startswith('/api/if/save/'):
                _CinterForgeIF._if_store_save(self.server, unquote(route[len('/api/if/save/'):]), body, screen=self._if_save_screen())
                self._json(200, {'saved':True})
                return
            if route == '/api/java':
                from runtime.CinterForgeJava import session_action
                self._json(200, session_action(self.server, body))
                return
            if route == '/api/bedrock/report/java':
                from runtime.CinterForgeJava import report_action
                self._json(200, report_action(self.server, body))
                return
            if route == '/api/bedrock/report/open':
                self._json(200, _CinterForgeCraftMasterMap.open_world_report(self.server))
                return
            if route == '/api/bedrock/world/select':
                if self.server.bedrock is None:
                    raise RuntimeError('Bedrock is unavailable')
                with self.server.bedrock_terminal_lock:
                    if body.get('published') is True:
                        self._json(200, self.server.bedrock.select_named_published_world(body.get('name', '')))
                    else:
                        self._json(200, self.server.bedrock.select_world(body.get('name', '')))
                return
            if route == '/api/bedrock/pack/set':
                self._json(200, _CinterForgeAssetViews.pack_set_current(self.server, body.get('name')))
                return
            if route == '/api/craftmaster/map/open':
                self._json(200, _CinterForgeCraftMasterMap.open_existing_map(
                    self.server, str(body.get('name', ''))))
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
            if route == '/api/bedrock/properties':
                self._json(200, self.server.bedrock.set_property(body.get('name'), body.get('value')))
                return
            if route == '/api/bedrock/access':
                self._json(200, self.server.bedrock.access.change(
                    body.get('kind'), body.get('action'), body.get('name')))
                return
            if route == '/api/bedrock/command':
                self._json(200, self.server.bedrock.console_command(body.get('command')))
                return
            if route == '/api/craftmaster/map/run':
                self._json(200, _CinterForgeCraftMasterMap.run_map_conversion(
                    self.server, str(body.get('name', ''))))
                return
            if route == '/api/craftmaster/map/close':
                map_id = str(body.get('id', ''))
                with self.server.craftmaster_map_lock:
                    state = self.server.craftmaster_maps.get(map_id)
                    if state is not None:
                        state['tab_open'] = False
                        if not state.get('remote') and not state.get('working'):
                            self.server.craftmaster_maps.pop(map_id, None)
                    self.server.craftmaster_map_requests = deque(
                        request for request in self.server.craftmaster_map_requests
                        if request['id'] != map_id
                    )
                self._json(200, {'closed': True})
                return
            if route in ('/api/bedrock/run', '/api/bedrock/start'):
                chosen = self.server.bedrock.current_world()
                if chosen is None:
                    raise ValueError('Select a Current World before running Bedrock')
                self._json(200, _CinterForgeSessions.open_bedrock_terminal(self.server,
                    '', body.get('cols', 80), body.get('rows', 24),
                    testgrounds=body.get('testgrounds') is True,
                    testgrounds_world_id=body.get('world_id')))
                return
            if route == '/api/bedrock/restart':
                selected = (self.server.bedrock._world_settings().get('selected') or {})
                if not selected.get('folder'):
                    raise RuntimeError('No selected Bedrock save to restart; type bedrock start')
                self._json(200, _CinterForgeSessions.open_bedrock_terminal(self.server,
                    str(selected['folder']), body.get('cols', 80), body.get('rows', 24), restart=True))
                return
            if route == '/api/bedrock/stop':
                with self.server.bedrock_terminal_lock:
                    self._json(200, self.server.bedrock.stop())
                return
            if route == '/api/terminal/open':
                backend=body.get('backend', 'native')
                self.server.log_event('opening terminal session: backend=' + str(backend) +
                    ' platform=' + self.server.system)
                if backend not in ('native','mac','linux','win'):
                    raise ValueError('Unknown Terminal backend')
                # On macOS these are Code-OSS development-only shells. Do not
                # create a guest Bash Session through direct API calls either.
                if self.server.system == 'mac' and backend in ('linux', 'win'):
                    raise ValueError(backend.title() + ' Bash is available only inside Code-OSS')
                command=None
                if backend in ('mac','linux','win') and backend != self.server.system:
                    raise RuntimeError(backend + ' Bash unavailable on ' + self.server.system)
                with self.server.sessions_lock:
                    if len(self.server.sessions) >= _CinterForgeSettings.MAX_SESSIONS:
                        raise ValueError('Maximum terminal sessions reached')
                    terminal = _CinterForgeSessions.CinterForgeTerminalSession(self.server.mutable_builds, self.server.system,
                                               body.get('cols', 80), body.get('rows', 24),
                                               url=self.server.origin, token=self.server.token,
                                               command=command,
                                               working_directory=Path(self.server.code_workspace.details()['cwd']),
                                               internal_token=self.server.cli_token,
                                               capabilities=self.server.capabilities)
                    self.server.sessions[terminal.id] = terminal
                self.server.log_event('terminal opened: id=' + terminal.id[:10] +
                    ' command=' + Path(terminal.shell[0]).name + ' output=' + str(terminal.output_log.name if terminal.output_log else 'none'))
                self._json(200, {'id': terminal.id, 'shell': backend.title()+' Bash' if self.server.system == 'mac' and backend in ('win','linux') else Path(terminal.shell[0]).name,
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
            if route == '/api/emulators/open':
                if body:
                    raise ValueError('Emulator gallery command takes no arguments')
                # The browser paints the SAME staged machine profiles as the
                # Macintosh, BeOS and NeXTSTEP menus and launches them through
                # the existing openMuseumProfile/openSession path.
                with self.server.asset_actions_lock:
                    self.server.asset_actions = type(self.server.asset_actions)(
                        request for request in self.server.asset_actions
                        if request.get('kind') != 'emulators')
                    self.server.asset_actions.append({'kind': 'emulators'})
                self._json(200, {'requested': True})
                return
            if route == '/api/games/report/open':
                if body:
                    raise ValueError('Games Report takes no arguments')
                # Existing Assets tab-action queue also owns Machines Report.
                with self.server.asset_actions_lock:
                    self.server.asset_actions = type(self.server.asset_actions)(
                        item for item in self.server.asset_actions
                        if item.get('kind') != 'games-report')
                    self.server.asset_actions.append({'kind': 'games-report'})
                self._json(200, {'requested': True})
                return
            if route == '/api/macgames/open':
                command = body.get('command')
                catalog = _CinterForgeSessions._mac_catalog()
                if not isinstance(command, str) or command not in catalog:
                    raise ValueError('Unknown Macintosh game command')
                entry = catalog[command]
                if not entry.get('guest_command'):
                    raise ValueError('No verified 68K game launch mapping for ' + entry['title'])
                if entry.get('guest_os') not in ('7.1', '7.6') or not re.fullmatch(
                        r'[a-z0-9]{1,64}', entry['guest_command']):
                    raise ValueError('Invalid installed 68K game launch mapping')
                with self.server.debug_lock:
                    self.server.macgame_request = {'game': command}
                self._json(200, {'requested': True, 'command': command})
                return
            if route == '/api/games/feedback':
                command = body.get('command')
                succeeded = body.get('succeeded')
                if (not isinstance(command, str) or
                        command not in _CinterForgeSessions._mac_catalog() or
                        not isinstance(succeeded, bool)):
                    raise ValueError('Invalid game feedback')
                with self.server.debug_lock:
                    state = self.server.game_verification.get(command)
                    if state is None:
                        raise ValueError('Game was not played in this Terminal process')
                    attempted = state.get('last_attempt_url')
                    if attempted and succeeded:
                        state['verified_url'] = attempted
                        state['status'] = 'verified'
                    elif attempted:
                        if attempted not in state['failed_urls']:
                            state['failed_urls'].append(attempted)
                        if state.get('verified_url') == attempted:
                            state['verified_url'] = None
                        state['status'] = 'failed'
                    else:
                        # No real downloadable candidate was supplied; never
                        # promote a made-up URL just because an emulator booted.
                        state['status'] = 'unverified'
                    state['last_outcome'] = 'success' if succeeded else 'failure'
                    updated = dict(state)
                self._json(200, {'ok': True, 'verification': updated})
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

