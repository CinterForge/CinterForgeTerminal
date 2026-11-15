"""CinterForgeBedrock — CinterForge Terminal application runtime.

PURPOSE: Consent-gated Bedrock server installation, RakNet discovery, world preservation and Bedrock launcher watchdog.
METHODS: _sha256, _checked_url, _safe_extract, _curl_download, _user_steamapps, _parse_raknet_pong, discover_lan_bedrock, CinterForgeBedrockManager, _vm_watchdog_main
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


"""Optional, EULA-gated Bedrock dedicated-server lifecycle for Python Host.

The official binary is never bundled into Host source. Explicit EULA consent
in the local WebView installs into the host Steam library. Developer Setup
continues to use sibling Builds and never acquires user content implicitly.
No user-authored package.json or prior Bedrock archive is required.
"""


ASSET_LOCATION = _CinterForgeSettings.ASSETS  # compatibility within bundled Bedrock helpers


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _checked_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Bedrock downloads require an HTTPS official source URL')
    hostname = parsed.hostname.lower()
    if not (hostname == 'minecraft.net' or hostname.endswith('.minecraft.net')
            or hostname == 'minecraft.azureedge.net'):
        raise ValueError('Bedrock package URL must point to the official Minecraft distribution')
    return url


def _safe_extract(archive: Path, target: Path) -> None:
    from runtime import CinterForgeSettings as _CinterForgeSettings
    with zipfile.ZipFile(archive) as source:
        names = source.infolist()
        if len(names) > 25000:
            raise ValueError('Bedrock archive has too many entries')
        total = sum(item.file_size for item in names)
        if total > 4 * _CinterForgeSettings.MAX_DOWNLOAD_BYTES:
            raise ValueError('Bedrock archive uncompressed size exceeds limit')
        for item in names:
            name = item.filename.replace('\\', '/')
            relative = Path(name)
            if (relative.is_absolute() or '..' in relative.parts or not name or
                    (item.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Bedrock archive contains a traversal or symlink entry')
            destination = (target / relative).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise ValueError('Bedrock archive escapes installation root')
        source.extractall(target)


def _curl_download(url: str, archive: Path, report: Callable[[str], None]) -> None:
    """Fallback when Python's connection to Mojang stalls or is rejected.

    curl writes only into the selected installation staging directory. The caller
    still compares the complete archive against independently obtained SHA-256
    metadata before extracting anything.
    """
    from runtime import CinterForgeSettings as _CinterForgeSettings
    binary = shutil.which('curl')
    if not binary:
        raise RuntimeError('curl is unavailable; official download cannot use fallback transport')
    _checked_url(url)
    diagnostic = archive.with_suffix('.curl.log')
    initial_size = archive.stat().st_size if archive.exists() else 0
    report(f'Python download stalled; trying curl from {initial_size / 1048576:.2f} MiB...')
    # Partial responses are handled by curl's -C -, and --fail ensures HTTP
    # errors (including a 403) are treated as failures, not ZIP content.
    command = [binary, '--fail', '--location', '--proto', '=https',
               '--proto-redir', '=https', '--max-redirs', '4',
               '--connect-timeout', '15', '--max-time', '300',
               '--speed-limit', '1024', '--speed-time', '25',
               '--retry', '2', '--retry-delay', '2', '--retry-all-errors',
               '--user-agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15',
               '--referer', _CinterForgeSettings.DOWNLOAD_URL,
               '--silent', '--show-error', '--continue-at', '-',
               '--output', str(archive), '--write-out', '\nHTTP %{http_code} final=%{url_effective} bytes=%{size_download}\n',
               url]
    with diagnostic.open('wb') as errors:
        process = subprocess.Popen(command, stdout=errors, stderr=subprocess.STDOUT)
        previous = -1
        deadline = time.monotonic() + 320
        try:
            while process.poll() is None:
                count = archive.stat().st_size if archive.exists() else 0
                if count > _CinterForgeSettings.MAX_DOWNLOAD_BYTES:
                    process.kill()
                    raise RuntimeError('Bedrock download exceeds size limit')
                if count != previous:
                    report(f'curl download: {count / 1048576:.2f} MiB received')
                    previous = count
                if time.monotonic() > deadline:
                    process.kill()
                    raise RuntimeError('curl exceeded its bounded download time')
                time.sleep(1)
            result = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    tail = diagnostic.read_text('utf-8', errors='replace')[-2048:].strip()
    if result:
        raise RuntimeError(f'curl failed with exit {result}: {tail or "no HTTP response"}')
    if not archive.is_file() or archive.stat().st_size == 0:
        raise RuntimeError(f'curl returned no archive bytes: {tail}')
    report(f'curl completed ({archive.stat().st_size / 1048576:.2f} MiB): {tail}')


def _user_steamapps(builds: Path) -> Path:
    """Find the real host Steam library, never the managed PTY's HOME.

    The installed app's location wins over any inherited environment value.
    An explicit library wins over discovery for development launches.  On
    POSIX, the login account home comes from the OS user database because
    HOME may deliberately point inside Builds/CFTVM during native acceptance.
    """
    from runtime import CinterForgeSessions as _CinterForgeSessions
    builds = builds.resolve(strict=True)
    chosen = _CinterForgeSessions._steam_library_for_installed_root(builds)
    if chosen is not None:
        return chosen
    from_install = (builds, ASSET_LOCATION, *builds.parents, *ASSET_LOCATION.parents)
    for location in from_install:
        if location.name.lower() != 'steamapps':
            continue
        for candidate in (builds, ASSET_LOCATION):
            relative = candidate.relative_to(location) if candidate.is_relative_to(location) else None
            if relative and relative.parts[:3] == ('common', 'CinterForge', 'Applications'):
                return location.resolve(strict=True)

    configured = os.environ.get('CINTER_STEAMAPPS') or os.environ.get('CFOS_STEAMAPPS')
    if configured:
        candidates = [Path(configured).expanduser()]
    elif sys.platform == 'darwin':
        try:
            import pwd
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            home = Path.home()
        candidates = [home / 'Library/Application Support/Steam/steamapps']
    elif os.name == 'nt':
        candidates = [Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Steam/steamapps']
    else:
        try:
            import pwd
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            home = Path.home()
        candidates = [home / '.local/share/Steam/steamapps',
                      home / '.steam/steam/steamapps']
    for steam in candidates:
        if steam.name.lower() == 'steamapps' and steam.is_dir():
            return steam.resolve(strict=True)
    raise RuntimeError('Host steamapps library not found; set CINTER_STEAMAPPS '
                       'to an existing Steam library: ' + ', '.join(map(str, candidates)))


_RAKNET_MAGIC = bytes.fromhex('00ffff00fefefefefdfdfdfd12345678')


def _parse_raknet_pong(packet: bytes, address: str, port: int) -> dict | None:
    if len(packet) < 35 or packet[0] != 0x1c or packet[17:33] != _RAKNET_MAGIC:
        return None
    name_len = int.from_bytes(packet[33:35], 'big')
    if name_len > 2048 or 35 + name_len > len(packet):
        return None
    motd = packet[35:35 + name_len].decode('utf-8', errors='replace')
    fields = motd.split(';')
    return {'address': address, 'port': port,
            'name': (fields[1] if len(fields) > 1 else motd)[:160],
            'protocol': 'RakNet unconnected pong'}


def discover_lan_bedrock(*, timeout: float = 1.5) -> list[dict]:
    """Bounded read-only discovery on the host LAN, without touching worlds.

    Results establish host-side UDP reception only. A separate guest-relay
    marker is required to establish a packet crossed the LinuxKit boundary.
    """
    timeout = max(0.1, min(float(timeout), 3.0))
    sockets = []
    seen = {}
    try:
        for port in (19132, 19133):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(('0.0.0.0', 0))
            sockets.append((sock, port))
            # RakNet Unconnected Ping: packet ID, timestamp, magic, GUID.
            ping = b'\x01' + struct.pack('>Q', int(time.time() * 1000)) + _RAKNET_MAGIC + b'CINTERFG'
            try:
                sock.sendto(ping, ('255.255.255.255', port))
            except OSError:
                pass  # A restricted host may still receive other LAN traffic.
        deadline = time.monotonic() + timeout
        while sockets and time.monotonic() < deadline:
            ready, _, _ = select.select([s for s, _ in sockets], [], [], max(0, deadline - time.monotonic()))
            for sock in ready:
                try:
                    packet, (address, port) = sock.recvfrom(4096)
                except OSError:
                    continue
                parsed = _parse_raknet_pong(packet, address, port)
                if parsed:
                    seen[(address, port)] = parsed
    finally:
        for sock, _ in sockets:
            sock.close()
    return sorted(seen.values(), key=lambda item: (item['name'].casefold(), item['address'], item['port']))


class CinterForgeBedrockManager:
    def __init__(self, builds: Path, system: str, *, user_install: bool = False):
        from runtime import CinterForgeSettings as _CinterForgeSettings
        self.builds = builds.resolve(strict=True)
        self.platform = system
        self.target = 'linux' if system == 'mac' else system
        self.app = os.environ.get('CPH_APP_ID', 'CinterForgeTerminal')
        if not _CinterForgeSettings.APP_PATTERN.fullmatch(self.app):
            raise ValueError('Invalid application instance identifier')
        self.user_install = user_install
        self.steamapps = _user_steamapps(self.builds) if user_install else None
        self.root = (self.steamapps / 'common/CinterForge/Servers/Bedrock'
                     if user_install else self.builds / 'Servers/Bedrock')
        self.package_dir = self.root / 'Packages' / self.target
        self.package = self.package_dir / 'current'
        self.run_root = (self.root / 'Run' / self.app if user_install
                         else self.builds / 'Run' / 'Bedrock' / self.app)
        self.receipt = self.run_root / 'eula-acceptance.json'
        self.install_log = (self.root / 'Logs/bedrock-install.log' if user_install
                            else self.builds / 'Logs/CinterForgeTerminal/bedrock-install.log')
        self._install_thread: threading.Thread | None = None
        self._install_progress = ''
        self._install_error = ''
        self._events = deque(maxlen=2048)
        self._event_id = 0
        self._log_error = ''
        self._process: subprocess.Popen | None = None
        self._terminal_session = None  # Direct, visible Session PTY owns the Bedrock process.
        self._watchdog: subprocess.Popen | None = None
        self._ready = False
        self._launch_offset = 0
        self._startup_error = ''
        self._log_stream = None
        # The VM can outlive its HTTP host. Its identity and console endpoint
        # must not depend on a CinterForgeBedrockManager/Popen instance remaining in memory.
        self._session_file = self.run_root / '.cinter/console-session.json'
        self._console_fifo = self.run_root / '.cinter/console-input.fifo'
        self._lock = threading.RLock()

    def _watch_vm(self, pid: int) -> None:
        """Guard VM lifetime if the HTTP/Terminal owner is force-quit.

        The watchdog's stdin is an exclusive pipe owned by this Python service.
        EOF is reliable even on SIGKILL; parent-PID checks also cover a native
        Terminal crash that leaves its Python service alive. A watchdog may
        stop only the *exact* verified VM PID created by this service.
        """
        from runtime import CinterForgeSettings as _CinterForgeSettings
        if self.platform != 'mac':
            return
        native_owner = os.getppid()
        self._watchdog = subprocess.Popen(
            [sys.executable, '-B', str((_CinterForgeSettings.ASSETS / 'CinterForgeTerminal.py')),
             '--vm-watchdog', str(self.builds), self.platform,
             '1' if self.user_install else '0', str(pid), str(native_owner)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=self._log_stream, close_fds=True, start_new_session=True)

    def _close_watchdog(self) -> None:
        watcher = self._watchdog
        self._watchdog = None
        if watcher is None:
            return
        if watcher.stdin is not None:
            watcher.stdin.close()
        try:
            watcher.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # The watchdog has its own bounded VM shutdown; do not kill it
            # while it is stopping an actually running guest.
            pass

    def accepted(self) -> bool:
        from runtime import CinterForgeSettings as _CinterForgeSettings
        try:
            receipt = json.loads(self.receipt.read_text('utf-8'))
            return receipt.get('eula') == _CinterForgeSettings.EULA_URL and receipt.get('accepted') is True
        except (OSError, ValueError, TypeError):
            return False

    def accept(self, confirmed: bool) -> dict:
        from runtime import CinterForgeSettings as _CinterForgeSettings
        if confirmed is not True:
            raise ValueError('Explicit EULA acceptance is required')
        self.run_root.mkdir(parents=True, exist_ok=True)
        receipt = {'schema': 'cinterforge.bedrock.acceptance.v1', 'accepted': True,
                   'eula': _CinterForgeSettings.EULA_URL, 'privacy': _CinterForgeSettings.PRIVACY_URL, 'timestamp_utc': time.time(),
                   'application': self.app}
        self.receipt.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        return {'eula_accepted': True}

    @staticmethod
    def _registry_json(path: str) -> dict:
        """Fetch bounded, HTTPS metadata, not unverified executable content."""
        from runtime import CinterForgeSettings as _CinterForgeSettings
        url = _CinterForgeSettings.METADATA_ROOT + path
        with urlopen(url, timeout=25) as response:
            if response.geturl() != url:
                raise RuntimeError('Unexpected Bedrock metadata redirect')
            contents = response.read(256 * 1024 + 1)
        if len(contents) > 256 * 1024:
            raise RuntimeError('Bedrock metadata is unreasonably large')
        data = json.loads(contents)
        if not isinstance(data, dict):
            raise ValueError('Invalid Bedrock metadata document')
        return data

    def _manifest(self) -> dict:
        """Resolve the current official binary plus its independently recorded SHA-256.

        Nothing is fetched here until the consent-gated installation worker runs.
        This is *not* a user-supplied or generated package.json dependency.
        """
        from runtime import CinterForgeSettings as _CinterForgeSettings
        try:
            versions = self._registry_json('versions.json')['release']
            version = versions['latest']
            if (not isinstance(version, str) or not _CinterForgeSettings.VERSION_PATTERN.fullmatch(version)
                    or version not in versions['versions']):
                raise ValueError('Bedrock stable release registry is invalid')
            metadata = self._registry_json('release/' + version + '/metadata.json')
            if metadata.get('version') != version:
                raise ValueError('Bedrock version metadata does not match the registry')
            binary = metadata['binary']['windows' if self.target == 'win' else 'linux']
            entry = {'url': binary['url'], 'sha256': binary['sha256'], 'version': version}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError('Unable to resolve the official Bedrock server and SHA-256. '
                               f'Official download information: {_CinterForgeSettings.DOWNLOAD_URL}. Detail: {exc}') from exc
        checksum = entry.get('sha256', '')
        if not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', checksum):
            raise ValueError('Bedrock release metadata requires a SHA-256 checksum')
        source = entry.get('url', '')
        if not isinstance(source, str):
            raise ValueError('Invalid Bedrock download URL')
        _checked_url(source)
        kind = 'bin-win' if self.target == 'win' else 'bin-linux'
        prefix = f'https://www.minecraft.net/bedrockdedicatedserver/{kind}/bedrock-server-'
        if not source.startswith(prefix) or not re.fullmatch(r'[0-9.]+\.zip', source[len(prefix):]):
            raise ValueError('Resolved Bedrock URL does not match the official platform package')
        return entry

    def begin_install(self) -> dict:
        """Start one download only after user consent, without blocking the UI.

        The single background worker reports progress via authenticated status
        requests *only while installation is in progress*, not on startup.
        """
        with self._lock:
            if not self.accepted():
                raise PermissionError('Bedrock EULA acceptance is required before downloading')
            if self.installed():
                return self.status()
            if self._install_thread and self._install_thread.is_alive():
                return self.status()
            # Fail before creating a download thread if the target host location
            # cannot persist diagnostics. Do not silently run an unlogged installer.
            self._verify_log_write()
            self._install_error = ''
            self._install_progress = 'Preparing official Bedrock server download…'
            self._append_log(self._install_progress)
            self._install_thread = threading.Thread(target=self._install_worker,
                                                     name='cinter-bedrock-install', daemon=True)
            self._install_thread.start()
            return self.status()

    def _install_worker(self) -> None:
        try:
            self.install(progress=self._set_install_progress)
        except Exception as exc:
            with self._lock:
                self._install_error = str(exc)
                self._install_progress = 'Bedrock installation failed: ' + str(exc)
                try:
                    self._append_log(self._install_progress)
                except OSError as log_exc:
                    self._log_error = f'{self.install_log}: {log_exc}'
                    print('[bedrock] logging failed: ' + self._log_error, file=sys.stderr, flush=True)

    def _verify_log_write(self) -> None:
        try:
            if self.user_install:
                # Recheck after service startup; never redirect user data to Builds.
                if not self.steamapps.is_dir() or self.steamapps.name.lower() != 'steamapps':
                    raise OSError('Host Steam library disappeared during session: ' + str(self.steamapps))
                if self.root.is_symlink() or self.install_log.is_symlink():
                    raise OSError('Refusing linked Bedrock installation/log directory')
            elif self.builds.name != 'Builds':
                raise OSError(f'Invalid host Builds root: {self.builds}')
            self.install_log.parent.mkdir(parents=True, exist_ok=True)
            # Open and fsync the actual log file rather than checking access bits,
            # which do not prove a mounted host filesystem can be written.
            with self.install_log.open('a', encoding='utf-8') as stream:
                stream.write('')
                stream.flush()
                os.fsync(stream.fileno())
            self._log_error = ''
        except OSError as exc:
            self._log_error = f'{self.install_log}: {exc}'
            print('[bedrock] log write verification failed: ' + self._log_error, file=sys.stderr, flush=True)
            raise RuntimeError('Cannot write Bedrock diagnostics to host installation: ' + self._log_error) from exc

    def events_since(self, cursor: int = 0) -> dict:
        with self._lock:
            cursor = max(0, int(cursor))
            return {'cursor': self._event_id, 'events': [dict(item) for item in self._events
                    if item['id'] > cursor], 'path': str(self.install_log),
                    'log_error': self._log_error}

    def _append_log(self, message: str) -> None:
        # Memory events are independent of disk IO and cannot be overwritten by
        # a newer progress update before an authenticated client polls.
        with self._lock:
            self._event_id += 1
            self._events.append({'id': self._event_id, 'message': str(message)[:2048]})
            try:
                self.install_log.parent.mkdir(parents=True, exist_ok=True)
                with self.install_log.open('a', encoding='utf-8') as stream:
                    stream.write(time.strftime('%Y-%m-%d %H:%M:%S ') + message.replace('\n', ' ') + '\n')
                    stream.flush()
                self._log_error = ''
            except OSError as exc:
                self._log_error = f'{self.install_log}: {exc}'
                print('[bedrock] log write failed: ' + self._log_error, file=sys.stderr, flush=True)
                raise

    def _set_install_progress(self, message: str) -> None:
        with self._lock:
            if message != self._install_progress:
                self._install_progress = message
                self._append_log(message)

    def log_tail(self, limit: int = 60) -> dict:
        # Read only the managed log, bounded to keep the authenticated API
        # and terminal command responsive even after lengthy installations.
        if not self.install_log.is_file():
            return {'path': str(self.install_log), 'lines': []}
        with self.install_log.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 65536))
            data = stream.read(65536)
        lines = data.decode('utf-8', 'replace').splitlines()
        return {'path': str(self.install_log), 'lines': lines[-max(1, min(limit, 100)): ]}

    # Discovery behavior adapted from Converter/CraftMaster/FormatSupport/minecraft/MCWorlds.cpp.
    # This is a local Python implementation, not an import from a sibling checkout.
    def _world_settings_path(self) -> Path:
        return self.run_root / 'world-source.json'

    def _world_settings(self) -> dict:
        path = self._world_settings_path()
        if not path.is_file() or path.is_symlink():
            return {}
        try:
            data = json.loads(path.read_text('utf-8'))
            return data if isinstance(data, dict) and data.get('schema') == 'cinterforge.bedrock.worlds.v1' else {}
        except (OSError, ValueError):
            return {}

    def _save_world_settings(self, data: dict) -> None:
        self.run_root.mkdir(parents=True, exist_ok=True)
        target = self._world_settings_path()
        if target.is_symlink():
            raise RuntimeError('Refusing symlinked world settings')
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.run_root,
                                         prefix='world-settings-', delete=False) as output:
            temp = Path(output.name)
            json.dump({'schema': 'cinterforge.bedrock.worlds.v1', **data}, output, indent=2)
            output.write('\n')
            output.flush(); os.fsync(output.fileno())
        try:
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _login_home() -> Path:
        if os.name == 'nt':
            return Path(os.environ.get('USERPROFILE') or Path.home())
        try:
            import pwd
            return Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            return Path.home()

    def _default_world_roots(self) -> list[Path]:
        home = self._login_home()
        roots: list[Path] = []
        if self.platform == 'win':
            roaming = Path(os.environ.get('APPDATA', str(home / 'AppData/Roaming')))
            local = Path(os.environ.get('LOCALAPPDATA', str(home / 'AppData/Local')))
            base = roaming / 'Minecraft Bedrock'
            users = base / 'Users'
            if users.is_dir():
                roots.extend(u / 'games/com.mojang/minecraftWorlds' for u in users.iterdir() if u.is_dir())
            roots.extend([base / 'Shared/games/com.mojang/minecraftWorlds',
                          roaming / 'Minecraft Education Edition/games/com.mojang/minecraftWorlds'])
            packages = local / 'Packages'
            if packages.is_dir():
                roots.extend(folder / 'LocalState/games/com.mojang/minecraftWorlds'
                             for folder in packages.iterdir() if folder.is_dir() and
                             (folder.name.startswith('Microsoft.MinecraftUWP_') or
                              folder.name.startswith('Microsoft.MinecraftEducationEdition')))
        elif self.platform == 'mac':
            base = home / 'Library/Application Support/minecraftpe'
            users = base / 'Users'
            if users.is_dir():
                roots.extend(u / 'games/com.mojang/minecraftWorlds' for u in users.iterdir() if u.is_dir())
            roots.extend([base / 'Shared/games/com.mojang/minecraftWorlds',
                          base / 'games/com.mojang/minecraftWorlds',
                          home / 'Library/Application Support/com.mojang/minecraftWorlds'])
        else:
            # Linux has no universal official Bedrock Edition save directory.
            # These are discovery candidates only; `bedrock path` is authoritative.
            roots.extend([home / '.local/share/minecraftpe/games/com.mojang/minecraftWorlds',
                          home / '.var/app/io.mrarm.mcpelauncher/data/mcpelauncher/games/com.mojang/minecraftWorlds',
                          home / 'minecraftWorlds'])
        return [r.resolve() for r in roots if r.is_dir() and not r.is_symlink()]

    def world_roots(self) -> list[Path]:
        settings = self._world_settings()
        custom = settings.get('root')
        if custom:
            root = Path(custom)
            return [root.resolve()] if root.is_dir() and not root.is_symlink() else []
        return list(dict.fromkeys(self._default_world_roots()))

    def set_world_path(self, raw: str) -> dict:
        with self._lock:
            if self._live_session_locked()[0] is not None:
                raise RuntimeError('Stop the Bedrock server before changing the world search path')
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError('Expected a path to an existing minecraftWorlds directory')
            path = Path(raw).expanduser()
            if not path.is_dir() or path.is_symlink():
                raise ValueError('World search path must be an existing non-symlink directory: ' + str(path))
            path = path.resolve(strict=True)
            self._save_world_settings({'root': str(path)})
            return self.list_worlds()

    def list_worlds(self) -> dict:
        entries: list[dict] = []
        for root in self.world_roots():
            try:
                folders = sorted(root.iterdir(), key=lambda v: v.name.casefold())
            except OSError:
                continue
            for folder in folders:
                if folder.is_symlink() or not folder.is_dir() or not (folder / 'level.dat').is_file() or not (folder / 'db').is_dir():
                    continue
                name_file = folder / 'levelname.txt'
                try:
                    name = name_file.read_text('utf-8-sig', errors='replace')[:2048].strip() if name_file.is_file() else folder.name
                except OSError:
                    name = folder.name
                entries.append({'name': name or folder.name, 'folder': folder.name,
                                'path': str(folder.resolve()), 'root': str(root)})
        settings = self._world_settings()
        return {'roots': [str(r) for r in self.world_roots()], 'configured_root': settings.get('root'),
                'selected': settings.get('selected'), 'worlds': entries}

    @staticmethod
    def _safe_world_copy(source: Path, destination: Path) -> None:
        # Never follow Minecraft-world symlinks or overwrite an existing server save.
        if source.is_symlink() or not source.is_dir():
            raise RuntimeError('World source is not a safe directory')
        for parent, dirs, files in os.walk(source, followlinks=False):
            for name in dirs + files:
                if (Path(parent) / name).is_symlink():
                    raise RuntimeError('World contains a symlink: ' + name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.parent / ('world-import-' + uuid.uuid4().hex)
        try:
            shutil.copytree(source, temp, symlinks=False)
            if destination.exists():
                raise RuntimeError('Selected server world already exists unexpectedly')
            temp.rename(destination)
        finally:
            if temp.exists():
                shutil.rmtree(temp)

    def run_world(self, name: str, *, launch: bool = True) -> dict:
        with self._lock:
            # `bedrock run ""` explicitly reuses the previously chosen save;
            # never guess a world when no selection has been made.
            if not name.strip():
                selected = self._world_settings().get('selected') or {}
                name = str(selected.get('folder') or '')
                if not name:
                    raise ValueError('No Bedrock save selected; type bedrock start to list saves')
            matches = [world for world in self.list_worlds()['worlds']
                       if world['name'].casefold() == name.strip().casefold() or world['folder'].casefold() == name.strip().casefold()]
            if not matches:
                raise ValueError('No Bedrock save named ' + repr(name) + '; type bedrock start for the list')
            if len(matches) != 1:
                raise ValueError('Ambiguous Bedrock save name; use its unique folder name or set bedrock path')
            world = matches[0]
            source = Path(world['path'])
            if not (source / 'level.dat').is_file() or not (source / 'db').is_dir():
                raise RuntimeError('World disappeared before launch: ' + str(source))
            key = hashlib.sha256(str(source).encode('utf-8')).hexdigest()[:12]
            managed_name = 'CinterForge-' + key
            target = self.run_root / 'worlds' / managed_name
            if not self.accepted() or not self.installed():
                raise RuntimeError('Install the official Bedrock server and accept its EULA before running a saved world')
            existing_pid, _, _ = self._live_session_locked()
            if existing_pid is not None:
                current = self._server_configuration().get('level-name', '')
                if current == managed_name:
                    return {**self.status(), 'source_world': str(source),
                            'active_world': str(self.run_root / 'worlds' / managed_name),
                            'world_name': world['name']}
                raise RuntimeError('A Bedrock VM is already running. Stop it explicitly before changing worlds.')
            self.run_root.mkdir(parents=True, exist_ok=True)
            # Install normally before the world copy, so default server.properties exists.
            self._prepare_run_tree()
            if target.is_symlink():
                raise RuntimeError('Refusing symlinked managed Bedrock world: ' + str(target))
            if not target.exists():
                self._safe_world_copy(source, target)
            elif not (target / 'level.dat').is_file() or not (target / 'db').is_dir():
                raise RuntimeError('Managed Bedrock world is incomplete: ' + str(target))
            config = self.run_root / 'server.properties'
            original = config.read_text('utf-8') if config.is_file() else ''
            lines = [line for line in original.splitlines() if not line.strip().startswith('level-name=')]
            lines.append('level-name=' + managed_name)
            temp = config.with_name('server.properties.tmp-' + uuid.uuid4().hex)
            try:
                temp.write_text('\n'.join(lines) + '\n', 'utf-8')
                os.replace(temp, config)
            finally:
                temp.unlink(missing_ok=True)
            self._save_world_settings({'root': self._world_settings().get('root'),
                                       'selected': world})
            # The desktop API prepares the managed copy first, then starts the
            # EXACT Setup-tested launcher in its own visible PTY Session.
            # Keep launch=True for existing non-UI callers of run_world().
            result = self.start() if launch else self.status()
            return {**result, 'source_world': str(source), 'active_world': str(target),
                    'world_name': world['name']}

    def _prepare_run_tree(self) -> None:
        self.run_root.mkdir(parents=True, exist_ok=True)
        for source in self.package.rglob('*'):
            relative = source.relative_to(self.package)
            if relative == Path('receipt.json'):
                continue
            destination = self.run_root / relative
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif source.is_file() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

    def installed(self) -> bool:
        executable = 'bedrock_server.exe' if self.target == 'win' else 'bedrock_server'
        return (self.package / 'receipt.json').is_file() and (self.package / executable).is_file()

    def _server_configuration(self) -> dict:
        """Report the user's actual world/port, never imply network reachability."""
        settings = {}
        properties = self.run_root / 'server.properties'
        if properties.is_file() and not properties.is_symlink():
            try:
                for line in properties.read_text(encoding='utf-8', errors='replace').splitlines():
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        if key.strip() in ('server-name', 'level-name', 'server-port',
                                           'server-portv6', 'enable-lan-visibility'):
                            settings[key.strip()] = value.strip()
            except OSError:
                pass
        return settings

    def lan_discovery(self, *, timeout: float = 1.5) -> dict:
        # The host's LAN scan does not prove reachability of the bridged guest.
        # Old NAT relay markers in persistent logs are historical and must not
        # be shown as evidence about a current native-bridged server.
        return {'servers': discover_lan_bedrock(timeout=timeout),
                'relay_events': [], 'scope': 'host UDP scan, not a guest readiness test'}

    def _log_path(self) -> Path:
        base = self.root / 'Logs' if self.user_install else self.builds / 'Logs/Bedrock'
        return base / f'{self.app}.log'

    def _session_record(self) -> dict:
        try:
            if self._session_file.is_symlink():
                return {}
            value = json.loads(self._session_file.read_text('utf-8'))
            if isinstance(value, dict) and value.get('schema') == 'cinterforge.bedrock.console.v1':
                return value
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _launcher_pid_matches(self, pid: int) -> bool:
        """Verify both process identity and the exact world directory.

        Some /bin/sh implementations exec the last command in the launcher,
        so the original script name may disappear from process arguments.
        In that case confirm the CinterBDSVM executable AND its inherited cwd.
        """
        if self.platform != 'mac' or not isinstance(pid, int) or pid <= 1:
            return False
        try:
            result = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'command='],
                                    capture_output=True, text=True, timeout=2, check=False)
            command = result.stdout.strip() if result.returncode == 0 else ''
            if 'run-mac-bds.sh' in command and str(self.run_root) in command:
                return True
            launcher = str(self.builds / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64/CinterBDSVM')
            if launcher not in command:
                return False
            # lsof is built into macOS. Verify process cwd rather than assuming
            # an unrelated VM with the same executable serves this world.
            cwd = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                                 capture_output=True, text=True, timeout=2, check=False)
            return cwd.returncode == 0 and str(self.run_root) in [
                line[1:] for line in cwd.stdout.splitlines() if line.startswith('n')]
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _external_vm_pid(self) -> int | None:
        """Find the existing VM without launching, stopping, or probing the LAN."""
        if self.platform != 'mac':
            return None
        record = self._session_record()
        try:
            pid = int(record.get('pid', 0))
        except (TypeError, ValueError):
            pid = 0
        if self._launcher_pid_matches(pid):
            return pid
        # The previous release had no session record. Discover its VM by its
        # launcher args or, when /bin/sh exec'd Swift, by the VM cwd.
        try:
            result = subprocess.run(['ps', '-ww', '-axo', 'pid=,command='],
                                    capture_output=True, text=True, timeout=3, check=False)
            if result.returncode:
                return None
            for line in result.stdout.splitlines():
                bits = line.strip().split(None, 1)
                if len(bits) != 2:
                    continue
                command = bits[1]
                if ('run-mac-bds.sh' not in command and 'CinterBDSVM' not in command):
                    continue
                candidate = int(bits[0])
                if self._launcher_pid_matches(candidate):
                    return candidate
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        return None

    def _live_session_locked(self) -> tuple[int | None, bool, bool]:
        """(pid, owned by this manager, reconnectable console)."""
        process = self._process
        if process is not None and process.poll() is None:
            if (self._terminal_session is not None and
                    self._terminal_session.process is process and
                    not self._terminal_session.closed):
                return process.pid, True, True
            if self.platform != 'mac':
                return process.pid, True, process.stdin is not None
            return process.pid, True, self._fifo_is_live() or getattr(process, 'stdin', None) is not None
        pid = self._external_vm_pid()
        record = self._session_record()
        return pid, False, bool(pid and record.get('pid') == pid and self._fifo_is_live())

    def _fifo_is_live(self) -> bool:
        try:
            return not self._console_fifo.is_symlink() and stat.S_ISFIFO(self._console_fifo.stat().st_mode)
        except OSError:
            return False

    def _send_console_locked(self, command: str, *, pid: int, own: bool, attachable: bool) -> None:
        # Direct PTY: the visible Bedrock tab is the console. There is no
        # detached FIFO or hidden Minecraft command channel for this launch.
        terminal = self._terminal_session
        if terminal is not None and own and terminal.process.pid == pid and not terminal.closed:
            terminal.write(command + '\n')
            return
        if attachable and self.platform == 'mac' and self._fifo_is_live():
            # O_NONBLOCK prevents a dead reader or orphaned FIFO from hanging
            # the HTTP request. Each Minecraft command fits in one atomic write.
            fd = os.open(self._console_fifo, os.O_WRONLY | os.O_NONBLOCK)
            try:
                payload = (command + '\n').encode('utf-8')
                if os.write(fd, payload) != len(payload):
                    raise OSError('Partial Bedrock console command write')
            finally:
                os.close(fd)
            return
        if own and self._process is not None and self._process.stdin is not None:
            self._process.stdin.write((command + '\n').encode('utf-8'))
            self._process.stdin.flush()
            return
        raise RuntimeError('Bedrock VM is running, but this older launch has no reconnectable '
                           'console endpoint. Keep playing; the VM will not be restarted. '
                           'Console control requires one later, deliberate restart.')

    def _record_launch(self, pid: int) -> None:
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        data = {'schema': 'cinterforge.bedrock.console.v1', 'pid': pid,
                'log_offset': self._launch_offset, 'created': time.time(),
                'world_root': str(self.run_root)}
        temp = self._session_file.with_name('console-session-' + uuid.uuid4().hex + '.tmp')
        try:
            temp.write_text(json.dumps(data) + '\n', encoding='utf-8')
            temp.chmod(0o600)
            os.replace(temp, self._session_file)
        finally:
            temp.unlink(missing_ok=True)

    def _refresh_readiness_locked(self, pid: int | None = None, owned: bool | None = None) -> None:
        """Observe output only; absence of a marker never means a live VM stopped."""
        if owned is None:
            pid, owned, _ = self._live_session_locked()
        if pid is None or self._ready:
            return
        record = self._session_record()
        # Recover the same launch's log offset after the HTTP service restarts.
        offset = self._launch_offset if owned else int(record.get('log_offset', 0) or 0)
        path = self._log_path()
        try:
            with path.open('rb') as stream:
                stream.seek(0, 2)
                end = stream.tell()
                stream.seek(max(0, min(offset, end), end - 262144))
                output = stream.read()
        except OSError:
            output = b''
        if b'Server started.' in output:
            self._ready = True
        elif owned and self._process is not None and self._process.poll() is not None:
            errors = [line for line in output.decode('utf-8', errors='replace').splitlines()
                      if ('CINTER_VM_ERROR' in line or 'CINTER_BDS_ERROR' in line)]
            self._startup_error = ' | '.join(errors[-4:]) or f'VM exited; see {path}'

    def status(self) -> dict:
        with self._lock:
            pid, owned, attachable = self._live_session_locked()
            self._refresh_readiness_locked(pid=pid, owned=owned)
            if pid is None and self._process is not None and self._process.poll() is not None and not self._startup_error:
                self._startup_error = f'Bedrock VM launcher exited (code {self._process.returncode}); see {self._log_path()}'
            live = pid is not None
            return {'platform': self.target, 'host': self.platform, 'application': self.app,
                    'server_configuration': self._server_configuration(),
                    'world_selection': self._world_settings(), 'world_search_roots': [str(p) for p in self.world_roots()],
                    'run_root': str(self.run_root),
                    'network_verified': False,  # Guest LAN connections are verified by players, not the VM PID.
                    'guest_nat': False,
                    'eula_accepted': self.accepted(), 'installed': self.installed(),
                    'running': live, 'starting': live and not self._ready,
                    'server_ready': self._ready and live,
                    'console_available': live and attachable,
                    'externally_managed': live and not owned,
                    'startup_error': self._startup_error,
                    'pid': pid,
                    'installing': bool(self._install_thread and self._install_thread.is_alive()),
                    'install_progress': self._install_progress, 'install_error': self._install_error,
                    'log_path': str(self.install_log), 'log_error': self._log_error,
                    'event_cursor': self._event_id,
                    'install_root': str(self.root), 'steamapps': str(self.steamapps) if self.steamapps else None}

    def install(self, progress: Callable[[str], None] | None = None) -> dict:
        from runtime import CinterForgeSettings as _CinterForgeSettings
        def report(message: str) -> None:
            if progress is not None:
                progress(message)

        if not self.accepted():
            raise PermissionError('Review and accept the Bedrock EULA in the WebView before downloading')
        if self.installed():
            return self.status()
        report('Resolving latest stable Bedrock server and verified SHA-256...')
        entry = self._manifest()
        self.package_dir.mkdir(parents=True, exist_ok=True)
        self.run_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='bedrock-stage-', dir=self.package_dir) as temp:
            root = Path(temp)
            archive = root / 'server.zip'
            source_url = _checked_url(entry['url'])
            # Bedrock is an on-demand official download, not an IF/Builds archive lookup.
            # Reuse only a fully installed server with its own receipt.
            report('Downloading official Minecraft Bedrock server: ' + source_url)
            # Allow redirects only to official Minecraft CDN endpoints.
            from urllib.request import HTTPRedirectHandler, build_opener
            class OfficialRedirect(HTTPRedirectHandler):
                def redirect_request(self, request, fp, code, msg, headers, newurl):
                    _checked_url(newurl)
                    return super().redirect_request(request, fp, code, msg, headers, newurl)
            opener = build_opener(OfficialRedirect())
            # Mojang downloads can stall without sending a single full MiB.
            # Use smaller reads, bounded retries and HTTP byte-range resume,
            # preserving the verified digest requirement before extraction.
            import socket
            copied = 0
            total = None
            last_report = time.monotonic()
            last_notice = 0
            for attempt in range(1, 5):
                try:
                    headers = {'Range': f'bytes={copied}-'} if copied else {}
                    with opener.open(Request(source_url, headers=headers) if headers else source_url, timeout=20) as response:
                        _checked_url(response.geturl())
                        code = getattr(response, 'status', None)
                        if code is None:
                            code = response.getcode() if hasattr(response, 'getcode') else 200
                        if copied:
                            content_range = getattr(response, 'headers', {}).get('Content-Range', '')
                            if code != 206 or not content_range.startswith(f'bytes {copied}-'):
                                # A server that ignores Range cannot append to
                                # an existing partial ZIP. Restart it safely.
                                copied = 0
                                archive.unlink(missing_ok=True)
                                raise RuntimeError('Official download server did not honor resume range')
                        if getattr(response, 'headers', {}).get('Content-Length', '').isdigit():
                            total = copied + int(response.headers['Content-Length'])
                            if total > _CinterForgeSettings.MAX_DOWNLOAD_BYTES:
                                raise RuntimeError('Bedrock download exceeds size limit')
                        with archive.open('ab') as output:
                            while True:
                                try:
                                    chunk = response.read(64 * 1024)
                                except (TimeoutError, socket.timeout) as exc:
                                    raise TimeoutError(f'No response data for 20 seconds after {copied / 1048576:.2f} MiB') from exc
                                if not chunk:
                                    break
                                copied += len(chunk)
                                if copied > _CinterForgeSettings.MAX_DOWNLOAD_BYTES:
                                    raise RuntimeError('Bedrock download exceeds size limit')
                                output.write(chunk)
                                now = time.monotonic()
                                if copied - last_notice >= 1024 * 1024 or now - last_report >= 4:
                                    size = f'/{total / 1048576:.1f}' if total else ''
                                    report(f'Downloaded {copied / 1048576:.1f}{size} MiB (attempt {attempt}/4)')
                                    last_notice = copied
                                    last_report = now
                        if total is not None and copied != total:
                            raise RuntimeError(f'Incomplete download: {copied}/{total} bytes')
                    break
                except (OSError, RuntimeError) as exc:
                    report(f'Python HTTP attempt {attempt}/4 failed at {copied / 1048576:.2f} MiB: '
                           f'{type(exc).__name__}: {exc}')
                    # Repeating four identical zero-byte urllib requests only
                    # burns a minute. Try the native HTTP stack immediately.
                    if copied == 0 and shutil.which('curl'):
                        _curl_download(source_url, archive, report)
                        copied = archive.stat().st_size
                        break
                    if attempt == 4:
                        if shutil.which('curl'):
                            _curl_download(source_url, archive, report)
                            copied = archive.stat().st_size
                            break
                        raise RuntimeError(f'Official Bedrock download failed after 4 attempts: {exc}') from exc
                    report(f'Bedrock download interrupted; retrying {attempt + 1}/4 at {copied / 1048576:.2f} MiB')
                    time.sleep(min(2 * attempt, 6))
            copied = archive.stat().st_size
            report(f'Archive ready ({copied / (1024 * 1024):.1f} MiB); verifying SHA-256...')
            checksum = _sha256(archive)
            if checksum != entry['sha256'].lower():
                raise RuntimeError('Bedrock archive checksum mismatch; installation aborted')
            # No redundant archive copy: the verified extracted installation and
            # its receipt are the durable package; every fresh install fetches Mojang.
            report('SHA-256 verified; extracting official package...')
            payload = root / 'payload'
            payload.mkdir()
            _safe_extract(archive, payload)
            executable = payload / ('bedrock_server.exe' if self.target == 'win' else 'bedrock_server')
            if not executable.is_file():
                raise RuntimeError('Official Bedrock binary missing from verified archive')
            if self.target == 'linux':
                executable.chmod(executable.stat().st_mode | 0o111)
            if self.platform == 'mac':
                # LinuxKit runs Bedrock under Rosetta using the same guest
                # runtime closure already staged by `./Cinter stage mac`.
                # Do not copy LinuxKit source or invent another guest installer.
                guest = self.builds / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64'
                tools = guest / 'GuestRuntime'
                for required in ('CinterBDSVM', 'cinter-bds-kernel',
                                 'cinter-bds-initrd.img', 'cinter-bds-cmdline'):
                    if not (guest / required).is_file():
                        raise RuntimeError('LinuxKit/Rosetta guest not staged: ' + str(guest / required))
                for required in ('glibc-x86_64/ld-linux-x86-64.so.2',
                                 'cinter-nethernet-probe'):
                    if not (tools / required).is_file():
                        raise RuntimeError('LinuxKit guest dependency missing: ' + str(tools / required))
                shutil.copytree(tools, payload / '.cinter')
            (payload / 'receipt.json').write_text(json.dumps({
                'schema': 'cinterforge.bedrock.package.v1', 'sha256': checksum,
                'source': entry['url'], 'platform': self.target,
                'version': entry.get('version')}, indent=2) + '\n')
            # A damaged prior application download can be replaced only after
            # the new official ZIP is fully verified. Preserve rollback until
            # the new payload has been promoted; no user worlds live here.
            previous = self.package_dir / 'previous-incomplete'
            if self.package.exists():
                if previous.exists():
                    raise RuntimeError('Prior incomplete Bedrock package needs inspection: ' + str(previous))
                self.package.rename(previous)
            try:
                payload.rename(self.package)
            except Exception:
                if previous.exists(): previous.rename(self.package)
                raise
            if previous.exists(): shutil.rmtree(previous)
        # The immutable receipt inside the installed tree pins these bytes;
        # there is no separate package manifest for users to create or maintain.
        report('Bedrock installed at ' + str(self.package))
        return self.status()

    def _run_command(self) -> tuple[list[str], dict[str,str]]:
        if self.platform == 'mac':
            # The original locally signed Mac VM uses a host-side LAN relay.
            guest = self.builds / 'CinterForgeTerminal/Dependencies/LinuxKit/mac/arm64'
            launcher = guest / 'run-mac-bds.sh'
            required = ('CinterBDSVM', 'cinter-bds-kernel',
                        'cinter-bds-initrd.img', 'cinter-bds-cmdline',
                        'GuestRuntime/glibc-x86_64/ld-linux-x86-64.so.2',
                        'GuestRuntime/cinter-nethernet-probe',
                        'CinterBDSNetRelay')
            for name in ('run-mac-bds.sh', *required):
                path = guest / name
                if not path.is_file():
                    raise RuntimeError('LinuxKit networking dependency missing: ' + str(path))
            # Keep the locally signed VM and its LAN relay together.
            script = launcher.read_text(encoding='utf-8', errors='replace')
            if ('CinterBDSNetRelay' not in script or
                    'CINTER_BDS_NETWORK host-relay' not in script):
                raise RuntimeError('Installed LinuxKit launcher is stale (missing LAN relay). '
                                   'Run ./Cinter build all followed by ./Cinter stage all.')
            for name in ('bedrock_server', 'server.properties'):
                if not (self.run_root / name).is_file():
                    raise RuntimeError('LinuxKit server runtime incomplete: ' + str(self.run_root / name))
            return ['/bin/sh', str(launcher), str(self.run_root)], {}
        executable = self.run_root / ('bedrock_server.exe' if self.platform == 'win' else 'bedrock_server')
        return [str(executable)], {}

    def start(self) -> dict:
        with self._lock:
            if not self.accepted():
                raise PermissionError('Bedrock EULA acceptance required')
            if not self.installed():
                raise RuntimeError('Bedrock package not installed')
            # Never launch a second VM just because the Python API restarted.
            if self._live_session_locked()[0] is not None:
                return self.status()
            self._prepare_run_tree()
            command, extra = self._run_command()
            log_path = self._log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._launch_offset = log_path.stat().st_size if log_path.exists() else 0
            self._ready = False
            self._startup_error = ''
            self._log_stream = log_path.open('ab')
            input_fd = None
            input_arg = subprocess.PIPE
            try:
                if self.platform == 'mac':
                    # The VM owns this FIFO's read descriptor. The *next* API
                    # process can reopen the write side while the VM keeps running.
                    self._console_fifo.parent.mkdir(parents=True, exist_ok=True)
                    if self._console_fifo.is_symlink():
                        raise RuntimeError('Refusing symlinked Bedrock console path')
                    if self._console_fifo.exists():
                        if not self._fifo_is_live():
                            raise RuntimeError('Bedrock console path exists and is not a FIFO')
                        self._console_fifo.unlink()  # no live VM owns it (checked above)
                    os.mkfifo(self._console_fifo, 0o600)
                    input_fd = os.open(self._console_fifo, os.O_RDWR)
                    input_arg = input_fd
                self._process = subprocess.Popen(command, cwd=self.run_root,
                    stdin=input_arg, stdout=self._log_stream, stderr=subprocess.STDOUT,
                    env=dict(os.environ, **extra), start_new_session=(os.name != 'nt'))
                if self.platform == 'mac':
                    self._record_launch(self._process.pid)
                    self._watch_vm(self._process.pid)
            except Exception:
                # If console setup or the watchdog fails after Popen, do not
                # abandon a live VM without an owner. Only this Popen child and
                # its new process group are eligible for emergency cleanup.
                launched = self._process
                if launched is not None and launched.poll() is None:
                    if os.name != 'nt':
                        os.killpg(launched.pid, signal.SIGTERM)
                    else:
                        launched.terminate()
                    try:
                        launched.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        if os.name != 'nt':
                            os.killpg(launched.pid, signal.SIGKILL)
                        else:
                            launched.kill()
                        launched.wait(timeout=4)
                self._close_watchdog()
                self._process = None
                self._log_stream.close(); self._log_stream = None
                raise
            finally:
                if input_fd is not None:
                    os.close(input_fd)
            # Detect failed launcher/VM setup before returning a successful
            # launch acknowledgement. Do not wait for Bedrock readiness here.
            if self._process is not None:
                try:
                    self._process.wait(timeout=0.8)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    code = self._process.returncode
                    try:
                        with log_path.open('rb') as stream:
                            stream.seek(max(self._launch_offset, log_path.stat().st_size - 8192))
                            tail = stream.read().decode('utf-8', errors='replace').strip()
                    except OSError:
                        tail = ''
                    self._startup_error = f'VM launcher exited immediately (code {code}). ' + tail[-1600:]
                    self._close_watchdog()
                    raise RuntimeError(self._startup_error)
            return self.status()

    def console_output(self, offset: int = -1) -> dict:
        """Follow VM logs even after the HTTP service was replaced."""
        with self._lock:
            state = self.status()
            path = self._log_path()
            try:
                with path.open('rb') as stream:
                    stream.seek(0, 2)
                    end = stream.tell()
                    start = end if offset < 0 else max(0, min(int(offset), end))
                    start = max(start, end - 32768)
                    stream.seek(start)
                    raw = stream.read(32768)
                return {'running': state['running'], 'starting': state['starting'],
                        'console_available': state['console_available'],
                        'output': raw.decode('utf-8', errors='replace'), 'offset': start + len(raw)}
            except FileNotFoundError:
                return {'running': state['running'], 'starting': state['starting'],
                        'console_available': state['console_available'], 'output': '', 'offset': 0}

    def console_command(self, command: str) -> dict:
        """Send one command to the active BDS console through its stdin/VM serial path.

        This operates on the already-running managed server. It must not
        inadvertently launch a second instance or write into a shell.
        """
        if not isinstance(command, str):
            raise ValueError('Minecraft console command must be text')
        command = command.strip()
        if command.startswith('/'):
            command = command[1:].lstrip()
        if not command or len(command.encode('utf-8')) > 2048 or any(c in command for c in '\r\n\x00'):
            raise ValueError('Provide one Minecraft console command (maximum 2048 bytes, no newlines)')
        if command.split(None, 1)[0].lower() in ('stop', 'quit'):
            raise ValueError('Use bedrock stop for a controlled server shutdown')
        with self._lock:
            pid, owned, attachable = self._live_session_locked()
            if pid is None:
                raise RuntimeError('Bedrock server is not running; use bedrock run <save name>')
            try:
                self._send_console_locked(command, pid=pid, own=owned, attachable=attachable)
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise RuntimeError('Bedrock console input is no longer connected: ' + str(exc)) from exc
            return {'sent': True, 'command': command, 'pid': pid,
                    'note': 'Submitted to server console; execution is not confirmed until BDS responds.'}

    def _wait_for_mac_vm_exit_locked(self, pid: int, seconds: float) -> bool:
        """Check the *actual* managed VM, not just a possibly dead shell parent."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self._launcher_pid_matches(pid):
                return self._external_vm_pid() is None
            time.sleep(0.2)
        return not self._launcher_pid_matches(pid) and self._external_vm_pid() is None

    def _stop_mac_vm_without_console_locked(self, pid: int) -> None:
        """Explicit operator stop/restart of an orphaned old LinuxKit VM.

        The Swift VM already watches .cinter/vm-stop-request. That is a VM
        power-off, NOT a graceful BDS 'stop'; reserve it for an explicit operator
        action, never for status, run_world, or minecraft. If the older VM does
        not honor the request, signal only an identity-verified managed process.
        """
        if not self._launcher_pid_matches(pid):
            if self._external_vm_pid() is None:
                return  # It exited independently during the request.
            raise RuntimeError('Bedrock VM identity changed; refusing to stop an unverified process')
        directory = self.run_root / '.cinter'
        request = directory / 'vm-stop-request'
        if directory.is_symlink() or request.is_symlink():
            raise RuntimeError('Refusing symlinked Bedrock VM stop-request path')
        directory.mkdir(parents=True, exist_ok=True)
        temp = request.with_name('vm-stop-request-' + uuid.uuid4().hex + '.tmp')
        try:
            temp.write_text('0\n', encoding='ascii')
            os.replace(temp, request)
        finally:
            temp.unlink(missing_ok=True)
        if self._wait_for_mac_vm_exit_locked(pid, 12):
            return
        # The older launcher may predate the host-side VM stop watcher. The
        # operator explicitly requested shutdown: terminate only the verified
        # VM's isolated process session, not unrelated macOS or Minecraft jobs.
        if not self._launcher_pid_matches(pid):
            raise RuntimeError('Managed VM identity changed while stopping; not signaling a new PID')
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            if self._external_vm_pid() is None:
                return
            raise RuntimeError('Bedrock VM PID disappeared but another managed VM is still running')
        if group != pid and not self._launcher_pid_matches(group):
            raise RuntimeError('Managed VM process group is not owned by its verified launcher; refusing forced shutdown')
        os.killpg(group, signal.SIGTERM)
        if self._wait_for_mac_vm_exit_locked(pid, 5):
            return
        if not self._launcher_pid_matches(pid) or os.getpgid(pid) != group:
            raise RuntimeError('VM identity changed after termination request; refusing further signals')
        os.killpg(group, signal.SIGKILL)
        if not self._wait_for_mac_vm_exit_locked(pid, 4):
            raise RuntimeError('Bedrock VM still running after explicit stop; refusing to start another VM')

    def stop(self) -> dict:
        with self._lock:
            pid, owned, attachable = self._live_session_locked()
            if pid is not None:
                if self.platform == 'mac':
                    if attachable:
                        # Prefer the Bedrock 'stop' command, allowing the world
                        # to be saved before the guest shuts down its own VM.
                        try:
                            self._send_console_locked('stop', pid=pid, own=owned, attachable=True)
                        except OSError:
                            pass  # Explicit stop can fall back to VM shutdown.
                        if not self._wait_for_mac_vm_exit_locked(pid, 12):
                            current = self._external_vm_pid()
                            if current is None and self._launcher_pid_matches(pid):
                                current = pid
                            if current is not None:
                                self._stop_mac_vm_without_console_locked(current)
                    else:
                        # The old launch cannot receive stdin; the operator's
                        # explicit 'bedrock stop' is authority to shut it down.
                        self._stop_mac_vm_without_console_locked(pid)
                    if self._external_vm_pid() is not None:
                        raise RuntimeError('Managed VM remains alive; not claiming it stopped')
                else:
                    self._send_console_locked('stop', pid=pid, own=owned, attachable=attachable)
                    if owned and self._process:
                        try:
                            self._process.wait(timeout=12)
                        except subprocess.TimeoutExpired:
                            self._process.terminate()
                            try: self._process.wait(timeout=4)
                            except subprocess.TimeoutExpired:
                                self._process.kill()
                                self._process.wait(timeout=4)
            self._close_watchdog()
            self._process = None
            self._terminal_session = None
            self._ready = False
            self._startup_error = ''
            if self._log_stream is not None:
                self._log_stream.close()
                self._log_stream = None
            return self.status()

    def restart(self) -> dict:
        """Operator-requested recovery of the same managed world and console."""
        with self._lock:
            if not self.accepted() or not self.installed():
                raise RuntimeError('Bedrock must be installed and EULA accepted before restart')
            # Never overwrite a live world. Stop precisely this managed VM and
            # wait for it to disappear before allowing start() to reuse Run/.
            self.stop()
            if self._live_session_locked()[0] is not None:
                raise RuntimeError('Previous Bedrock VM is still alive; restart aborted')
            state = self.start()
            if not state.get('running') or not state.get('console_available'):
                raise RuntimeError('Bedrock replacement did not establish a live console input')
            return state


def _vm_watchdog_main(argv: list[str]) -> int:
    """Short-lived Bedrock VM watchdog, never an independent server daemon."""
    if len(argv) != 5:
        raise ValueError('Invalid Bedrock VM watchdog arguments')
    builds, system, install, pid_text, owner_text = argv
    pid, native_owner = int(pid_text), int(owner_text)
    if pid <= 1 or native_owner <= 1 or system != 'mac':
        raise ValueError('Invalid Bedrock watchdog identity')
    manager = CinterForgeBedrockManager(Path(builds), system, user_install=(install == '1'))
    while True:
        if not manager._launcher_pid_matches(pid):
            return 0
        readable, _, _ = select.select([sys.stdin.buffer], [], [], 1.0)
        if readable and not os.read(sys.stdin.fileno(), 1):
            break
        try:
            os.kill(native_owner, 0)
        except ProcessLookupError:
            break
    # A new Terminal process/session must not be stopped accidentally.
    if manager._launcher_pid_matches(pid):
        try:
            manager.stop()
        except Exception as error:
            print('[BEDROCK WATCHDOG] VM cleanup failed: ' + str(error),
                  file=sys.stderr, flush=True)
            return 1
    return 0


"""CinterForgeTerminal's application commands, invoked by the real native shell.

Help text is pure/local. Developer and Bedrock actions use the authenticated
loopback API owned by the running CinterForgeTerminal Python HTTP service.
"""

