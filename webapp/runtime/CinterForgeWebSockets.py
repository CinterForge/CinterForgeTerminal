"""Browser port owners: HTTP 20300/20301/20302, WS 20303/20304/20305.

20300 is the existing Terminal HTTP/API owner. Independent Linux/Windows
viewer pages (20301/20302) load the pinned guest noVNC JavaScript from the
already-forwarded 20601/20602 HTTP bridges. Browser WebSockets use 20303 for
Terminal PTYs and 20304/20305 for Linux/Windows RFB respectively. All six
listeners have the SAME lifecycle owner. No new VM or Docker container.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import mimetypes
import re
import http.cookies
import json
import secrets
import select
import socket
import socketserver
import struct
import threading
from urllib.parse import urlsplit

VIEWER_HTTP_PORTS = {'linux': 20301, 'win': 20302}
VNC_PORTS = {'linux': 20304, 'win': 20305}
VNC_BACKENDS = {'linux': 20601, 'win': 20602}
TERMINAL_PORT = 20303
_MAX_HANDSHAKE = 16384
_MAX_FRAME = 1024 * 1024


def _is_authorized(owner, header: dict[str, str], address: str, port: int) -> bool:
    """Allow the configured page origin on the same hostname; honor setpass."""
    origin = header.get('origin', '')
    host = header.get('host', '')
    try:
        src = urlsplit(origin)
        dst_host = host.rsplit(':', 1)[0].strip('[]').lower()
        kind = 'terminal' if port == TERMINAL_PORT else next(
            (item for item, value in VNC_PORTS.items() if value == port), None)
        expected_origin_port = owner.server_port if kind == 'terminal' else VIEWER_HTTP_PORTS.get(kind)
        if src.scheme != 'http' or src.port != expected_origin_port or src.hostname != dst_host:
            return False
        if int(host.rsplit(':', 1)[1]) != port:
            return False
    except (ValueError, IndexError, AttributeError):
        return False
    if not owner.lan_login:
        return True
    try:
        cookie = http.cookies.SimpleCookie(header.get('cookie', ''))
        lan_cookie = cookie['cinter_lan'].value if 'cinter_lan' in cookie else ''
        native = cookie['cinter_native'].value if 'cinter_native' in cookie else ''
        if secrets.compare_digest(lan_cookie, owner.lan_cookie):
            return True
        if address in ('127.0.0.1', '::1') and secrets.compare_digest(native, owner.native_cookie):
            return True
    except (http.cookies.CookieError, ValueError, KeyError):
        pass
    return False


def _read_http_upgrade(sock: socket.socket) -> tuple[bytes, dict[str, str], str, bytes]:
    sock.settimeout(8)
    request = bytearray()
    while b'\r\n\r\n' not in request:
        if len(request) > _MAX_HANDSHAKE:
            raise ValueError('WebSocket handshake exceeds limit')
        data = sock.recv(4096)
        if not data:
            raise ConnectionError('WebSocket handshake closed')
        request.extend(data)
    raw, remainder = bytes(request).split(b'\r\n\r\n', 1)
    lines = raw.decode('latin-1').split('\r\n')
    pieces = lines[0].split(' ')
    if len(pieces) != 3 or pieces[0] != 'GET' or pieces[2] != 'HTTP/1.1':
        raise ValueError('Not an HTTP/1.1 WebSocket upgrade')
    if not pieces[1].startswith('/'):
        raise ValueError('Invalid WebSocket path')
    header = {}
    for line in lines[1:]:
        key, sep, value = line.partition(':')
        if not sep or not key:
            raise ValueError('Malformed WebSocket request')
        key = key.strip().lower()
        if key in header:
            raise ValueError('Duplicate WebSocket header')
        header[key] = value.strip()
    if (header.get('upgrade', '').lower() != 'websocket' or
            'upgrade' not in {item.strip().lower() for item in header.get('connection', '').split(',')} or
            header.get('sec-websocket-version') != '13' or
            not header.get('sec-websocket-key')):
        raise ValueError('WebSocket upgrade headers missing')
    try:
        if len(base64.b64decode(header['sec-websocket-key'], validate=True)) != 16:
            raise ValueError('Invalid WebSocket key')
    except (ValueError, base64.binascii.Error):
        raise ValueError('Invalid WebSocket key') from None
    return raw + b'\r\n\r\n', header, pieces[1], remainder


def _reject(sock, status='403 Forbidden'):
    try:
        message = status.encode('ascii')
        sock.sendall(b'HTTP/1.1 ' + message + b'\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
    except OSError:
        pass


def _frame(data: bytes, opcode=1) -> bytes:
    n = len(data)
    prefix = bytes([0x80 | opcode])
    if n < 126:
        return prefix + bytes([n]) + data
    if n < 65536:
        return prefix + bytes([126]) + struct.pack('!H', n) + data
    return prefix + bytes([127]) + struct.pack('!Q', n) + data


def _recv_exact(stream, length: int) -> bytes:
    data = stream.read(length)
    if data is None or len(data) != length:
        raise ConnectionError('WebSocket disconnected')
    return data


def _receive_frame(stream) -> tuple[int, bytes]:
    first, second = _recv_exact(stream, 2)
    if not first & 0x80:  # Do not accept fragmented PTY commands.
        raise ValueError('Fragmented PTY WebSocket frame')
    if not second & 0x80:
        raise ValueError('Unmasked browser frame')
    n = second & 0x7f
    if n == 126:
        n = struct.unpack('!H', _recv_exact(stream, 2))[0]
    elif n == 127:
        n = struct.unpack('!Q', _recv_exact(stream, 8))[0]
    if n > _MAX_FRAME:
        raise ValueError('WebSocket PTY frame too large')
    mask = _recv_exact(stream, 4)
    data = _recv_exact(stream, n)
    return first & 0x0f, bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))


class _SidecarTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True  # Permit immediate restart; an active listener still owns its port.
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, owner, port, kind):
        self.owner = owner
        self.kind = kind
        self.connections = set()
        self.connections_lock = threading.Lock()
        super().__init__(('0.0.0.0', port), _SocketHandler)

    def register(self, sock):
        with self.connections_lock:
            self.connections.add(sock)

    def unregister(self, sock):
        with self.connections_lock:
            self.connections.discard(sock)

    def close_connections(self):
        with self.connections_lock:
            sockets = tuple(self.connections)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _SocketHandler(socketserver.BaseRequestHandler):
    def handle(self):
        owner = self.server.owner
        sock = self.request
        self.server.register(sock)
        try:
            raw, headers, path, remainder = _read_http_upgrade(sock)
            port = TERMINAL_PORT if self.server.kind == 'terminal' else VNC_PORTS[self.server.kind]
            if not _is_authorized(owner, headers, self.client_address[0], port):
                _reject(sock)
                return
            if self.server.kind == 'terminal':
                if path != '/session':
                    _reject(sock, '404 Not Found')
                    return
                self._terminal(sock, headers, remainder)
            else:
                if urlsplit(path).path not in ('/websockify', '/'):
                    _reject(sock, '404 Not Found')
                    return
                self._vnc(sock, raw, remainder)
        except (ConnectionError, OSError, TimeoutError, ValueError) as error:
            if not owner._stopping:
                owner.log_event(self.server.kind + ' WebSocket transport ended: ' + str(error)[:150])
        finally:
            self.server.unregister(sock)

    def _vnc(self, sock, raw, remainder):
        owner, kind = self.server.owner, self.server.kind
        with owner.guest_start_lock:
            ready = kind in owner.guest_vnc_ready
        if not ready:
            _reject(sock, '503 Service Unavailable')
            return
        try:
            upstream = socket.create_connection(('127.0.0.1', VNC_BACKENDS[kind]), timeout=5)
        except OSError as error:
            owner.log_event(kind + ' VNC forwarded port unavailable: ' + str(error))
            _reject(sock, '503 Service Unavailable')
            return
        with upstream:
            upstream.sendall(raw + remainder)
            sock.settimeout(None)
            upstream.settimeout(None)
            while not owner._stopping:
                readable, _, _ = select.select([sock, upstream], [], [], 0.5)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is sock else sock).sendall(data)

    def _terminal(self, sock, headers, remainder):
        owner = self.server.owner
        accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key'] +
                    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest())
        sock.settimeout(None)
        sock.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                     b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        # Use a single buffered stream so a WebSocket frame arriving in the
        # first TCP packet after Upgrade is not discarded.
        stream = sock.makefile('rb')
        send_lock = threading.Lock()
        stopped = threading.Event()

        def send(value):
            with send_lock:
                if not stopped.is_set():
                    sock.sendall(_frame(json.dumps(value, separators=(',', ':')).encode('utf-8')))

        try:
            # _read_http_upgrade normally consumes only the handshake. If a
            # client pipelines the attach frame, prepend it to stream reads.
            if remainder:
                import io
                class _Prefixed:
                    def __init__(self, prefix, underlying):
                        self.prefix = io.BytesIO(prefix)
                        self.underlying = underlying
                    def read(self, n):
                        initial = self.prefix.read(n)
                        return initial + (self.underlying.read(n - len(initial)) if len(initial) < n else b'')
                stream = _Prefixed(remainder, stream)
            opcode, data = _receive_frame(stream)
            attach = json.loads(data) if opcode == 1 else {}
            session_id = attach.get('id', '') if isinstance(attach, dict) else ''
            if (not isinstance(attach, dict) or attach.get('type') != 'attach' or
                    not secrets.compare_digest(str(attach.get('token', '')), owner.token)):
                send({'error': 'Terminal session authorization failed'})
                return
            with owner.sessions_lock:
                item = owner.sessions.get(session_id)
            if item is None:
                send({'error': 'Unknown terminal session'})
                return
            send({'type': 'attached', 'port': TERMINAL_PORT})

            def output_pump():
                try:
                    while not stopped.is_set() and not owner._stopping:
                        # Existing PTY reader is the sole output owner for
                        # this WebSocket, just as the previous polling tab was.
                        data, closed = item.poll(100)
                        with owner.eula_lock:
                            eula = owner.eula_requested
                        if data or closed or eula:
                            send({'type': 'output', 'output': base64.b64encode(data).decode('ascii'),
                                  'closed': closed, 'bedrock_eula_requested': eula})
                        if closed:
                            return
                except (OSError, RuntimeError, ValueError) as error:
                    if not owner._stopping:
                        try:
                            send({'error': 'Terminal session stream failed: ' + str(error)})
                        except OSError:
                            pass
                finally:
                    stopped.set()
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            threading.Thread(target=output_pump, name='cinter-pty-ws-' + session_id[:8], daemon=True).start()
            while not stopped.is_set() and not owner._stopping:
                opcode, payload = _receive_frame(stream)
                if opcode == 8:
                    break
                if opcode == 9:
                    with send_lock:
                        sock.sendall(_frame(payload, opcode=10))
                    continue
                if opcode != 1:
                    raise ValueError('Unsupported terminal frame type')
                command = json.loads(payload)
                if not isinstance(command, dict):
                    raise ValueError('Invalid terminal command')
                if command.get('type') == 'input':
                    item.write(command.get('data', ''))
                elif command.get('type') == 'resize':
                    item.resize(command.get('cols', 80), command.get('rows', 24))
                else:
                    raise ValueError('Unknown terminal WebSocket command')
        finally:
            stopped.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()


class _ViewerHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True  # Permit immediate restart; an active listener still owns its port.

    def __init__(self, owner, kind):
        self.owner = owner
        self.kind = kind
        super().__init__(('0.0.0.0', VIEWER_HTTP_PORTS[kind]), _ViewerHTTPHandler)


class _ViewerHTTPHandler(http.server.BaseHTTPRequestHandler):
    """A per-desktop webpage, NOT a second application backend or VM."""

    def log_message(self, format, *args):
        # Successful asset fetches are uninteresting; failures are logged by
        # the main service with their original cause.
        pass

    def _reply(self, status, body, content_type='text/plain; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        # no X-Frame-Options: SAMEORIGIN -- the parent on 20300 is another
        # origin by definition, despite being the same host.
        if content_type.startswith('text/html'):
            port = VNC_PORTS[self.server.kind]
            self.send_header('Content-Security-Policy',
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                f"connect-src 'self' ws://*:{port}; img-src 'self' data:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors http://*:20300")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        owner, kind = self.server.owner, self.server.kind
        port = VIEWER_HTTP_PORTS[kind]
        if not _viewer_authorized(owner, self.headers, self.client_address[0]):
            # Login is always performed on the existing Terminal HTTP owner.
            body = (f'Log into http://{self.headers.get("Host", "127.0.0.1").split(":")[0]}:'
                    f'{owner.server_port}/login to view the {kind} desktop.').encode()
            self._reply(401, body)
            return
        path = urlsplit(self.path).path
        if path in ('/', '/desktop.html'):
            with owner.guest_start_lock:
                ready = kind in owner.guest_vnc_ready
                reason = owner.guest_errors.get(kind, owner.guest_start_error or owner.guest_start_state)
            if not ready:
                self._reply(503, (kind + ' desktop not connected: ' + reason +
                                  '; see Terminal Server Log').encode())
                return
            from runtime.CinterForgeSettings import ASSETS
            asset = ASSETS / 'launcher' / 'desktop.html'
            # Staged, authored static page; use exactly this file, no path
            # interpolation or arbitrary filesystem access.
            page = asset.read_bytes().replace(b'__PLATFORM__', kind.encode())
            page = page.replace(b'__WS_PORT__', str(VNC_PORTS[kind]).encode())
            self._reply(200, page, 'text/html; charset=utf-8')
            return
        if path == '/desktop.js':
            # Executable viewer logic must be an external asset: the viewer's
            # strict script-src 'self' intentionally rejects inline scripts.
            from runtime.CinterForgeSettings import ASSETS
            script = ASSETS / 'launcher' / 'desktop.js'
            try:
                self._reply(200, script.read_bytes(), 'text/javascript; charset=utf-8')
            except OSError as error:
                owner.log_event(kind + ' VNC viewer script missing: ' + str(error))
                self._reply(503, b'VNC viewer script unavailable')
            return
        # The noVNC modules import other modules relative to /core. Proxy
        # only static assets through the existing private Colima forwarding,
        # never user paths or HTTP headers, and never the bridge's WS channel.
        if not re.fullmatch(r'/(?:core|vendor)/[A-Za-z0-9_./-]+', path) or \
                '/..' in path or '//' in path or not path.endswith(('.js', '.css', '.png', '.svg', '.json', '.woff', '.woff2', '.mjs', '.wasm')):
            self._reply(404, b'Not found')
            return
        try:
            connection = http.client.HTTPConnection('127.0.0.1', VNC_BACKENDS[kind], timeout=5)
            try:
                connection.request('GET', path)
                response = connection.getresponse()
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError('noVNC asset too large')
                ctype = response.getheader('Content-Type') or mimetypes.guess_type(path)[0] or 'application/octet-stream'
                status = response.status
            finally:
                connection.close()
            self._reply(status, body, ctype)
        except (OSError, TimeoutError, ValueError, http.client.HTTPException) as error:
            owner.log_event(kind + ' noVNC module unavailable: ' + str(error))
            self._reply(503, ('noVNC asset unavailable: ' + str(error)).encode())


def _viewer_authorized(owner, headers, address):
    """Browser asset auth matches the primary service's setpass cookies."""
    if not owner.lan_login:
        return True
    try:
        cookie = http.cookies.SimpleCookie(headers.get('Cookie', ''))
        remote = cookie['cinter_lan'].value if 'cinter_lan' in cookie else ''
        native = cookie['cinter_native'].value if 'cinter_native' in cookie else ''
        return (bool(remote) and secrets.compare_digest(remote, owner.lan_cookie) or
                address in ('127.0.0.1', '::1') and bool(native) and
                secrets.compare_digest(native, owner.native_cookie))
    except (http.cookies.CookieError, ValueError):
        return False


class CinterForgeSidecarPorts:
    """Two HTTP views + three sockets, owned by the existing Terminal server."""

    def __init__(self, owner):
        self.owner = owner
        self.servers = []

    def start(self):
        try:
            for kind in ('linux', 'win'):
                sidecar = _ViewerHTTPServer(self.owner, kind)
                self.servers.append(sidecar)
                threading.Thread(target=sidecar.serve_forever, name='cinter-' + kind + '-viewer', daemon=True).start()
                self.owner.log_event(kind + ' desktop HTTP listening on 0.0.0.0:' + str(VIEWER_HTTP_PORTS[kind]))
            for kind, port in (('terminal', TERMINAL_PORT), ('linux', VNC_PORTS['linux']), ('win', VNC_PORTS['win'])):
                sidecar = _SidecarTCPServer(self.owner, port, kind)
                self.servers.append(sidecar)
                threading.Thread(target=sidecar.serve_forever, name='cinter-' + kind + '-ws', daemon=True).start()
                self.owner.log_event(kind + ' WebSocket listening on 0.0.0.0:' + str(port))
        except Exception:
            self.stop()
            raise

    def disconnect_clients(self):
        for sidecar in self.servers:
            if hasattr(sidecar, 'close_connections'):
                sidecar.close_connections()

    def stop(self):
        self.disconnect_clients()
        for sidecar in self.servers:
            sidecar.shutdown()
            sidecar.server_close()
        self.servers.clear()
