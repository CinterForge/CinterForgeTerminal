"""Bedrock Dedicated Server and CraftMaster integration for CinterForgeTerminal.

Responsibilities
----------------
- Discover local Java and Bedrock worlds without changing their source data.
- Convert selected Java worlds with the bundled, receipt-verified CraftMaster CLI.
- Import independently published CraftMaster worlds from public Cloudflare URLs.
- Install Mojang's official Bedrock Dedicated Server only with EULA consent.
- Supervise the visible server console and its exact macOS VM process.

Locations
---------
Developer Bedrock state:  <workspace>/Builds/Servers/Bedrock/
Installed Bedrock state:  <steamapps>/common/CinterForge/Servers/Bedrock/
Java saves on macOS:     ~/Library/Application Support/minecraft/saves/

The native Terminal runtime never invokes Setup. Guest dependencies belong to
Setup and installed VM transfer belongs to Stage, not to this module.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import importlib.util
import ipaddress
import json
import mimetypes
import os
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
import zipfile
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit
from urllib.request import Request, urlopen

if os.name != 'nt':
    import fcntl
    import pty
    import termios

from runtime import CinterForgeSettings as _CinterForgeSettings
from runtime.CinterForgeBedrockAccess import BedrockServerAccess
from runtime.CinterForgeCode import CinterForgeCodeWorkspace
from runtime.CinterForgeGuest import CinterForgeGuestSupervisor
from runtime.CinterForgeLogging import CinterForgeRuntimeLog
from runtime.CinterForgeVM import CinterForgeVM


def _owned_bedrock_exit_code(process) -> int | None:
    """Check either subprocess.Popen or Windows pywinpty without blocking."""
    poll = getattr(process, 'poll', None)
    if callable(poll):
        return poll()
    if process.isalive():
        return None
    return process.wait()  # pywinpty.wait() has no timeout; now safely exited.


def _wait_owned_bedrock(process, timeout: float) -> int | None:
    """Bounded wait for the actual launch owner, not an unrelated PID."""
    if callable(getattr(process, 'poll', None)):
        return process.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        code = _owned_bedrock_exit_code(process)
        if code is not None:
            return code
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(getattr(process, 'pid', 'Bedrock PTY'), timeout)
        time.sleep(min(0.05, remaining))


def _terminate_owned_bedrock(process, *, force: bool) -> None:
    """Use pywinpty's force argument only for Windows PTYs."""
    if callable(getattr(process, 'poll', None)):
        if force:
            process.kill()
        else:
            process.terminate()
    else:
        process.terminate(force=force)


# Official Bedrock binary download metadata remains restricted to Minecraft.
# Published converted worlds are separate, public Cloudflare R2 objects.
PUBLIC_CRAFTMASTER_WORLDS_URL = (
    'https://cinterforge.ucreg.io/CinterForgeCraftMaster/Worlds/'
)
PUBLIC_CRAFTMASTER_CHANNELS = ('release', 'beta')
PUBLIC_MANIFEST_MAX_BYTES = 1024 * 1024
WORLD_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
WORLD_DOWNLOAD_PROGRESS_BYTES = 16 * 1024 * 1024
WORLD_ARCHIVE_MAX_BYTES = 32 * 1024**3

ASSET_LOCATION = _CinterForgeSettings.ASSETS  # Existing caller compatibility.


def _world_command_name(world: dict) -> str:
    """A single, human-facing name suitable for `bedrock <name>`.

    Java world exports follow CraftMaster's final parenthesized folder suffix.
    Bedrock level names are authoritative; their opaque folder IDs are not
    commands unless the caller explicitly supplies an existing ID.
    """
    raw = str(world.get('folder') if world.get('edition') == 'java' else world.get('name') or '')
    suffix = re.search(r'\(([^()]*)\)\s*$', raw)
    if suffix and suffix.group(1).strip():
        return suffix.group(1).strip()
    return str(world.get('name') or world.get('folder') or '').strip()


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


def _safe_extract(
    archive: Path,
    target: Path,
    *,
    max_uncompressed_bytes: int | None = None,
) -> None:
    """Extract a Bedrock ZIP only after validating every entry and total size.

    ZIP extraction is kept inside `target`. Never extract a traversal path,
    absolute path, or symlink supplied by a downloaded archive.
    """
    from runtime import CinterForgeSettings as _CinterForgeSettings

    with zipfile.ZipFile(archive) as source_archive:
        archive_entries = source_archive.infolist()
        if len(archive_entries) > 25000:
            raise ValueError('Bedrock archive has too many entries')

        total_uncompressed_bytes = sum(entry.file_size for entry in archive_entries)
        maximum_allowed_bytes = (
            4 * _CinterForgeSettings.MAX_DOWNLOAD_BYTES
            if max_uncompressed_bytes is None else max_uncompressed_bytes
        )
        if total_uncompressed_bytes > maximum_allowed_bytes:
            raise ValueError('Bedrock archive uncompressed size exceeds limit')

        resolved_destination_root = target.resolve()
        for archive_entry in archive_entries:
            entry_name = archive_entry.filename.replace('\\', '/')
            relative_entry_path = Path(entry_name)
            is_zip_symlink = (
                (archive_entry.external_attr >> 16) & 0o170000
            ) == 0o120000
            if (
                relative_entry_path.is_absolute()
                or '..' in relative_entry_path.parts
                or not entry_name
                or is_zip_symlink
            ):
                raise ValueError('Bedrock archive contains a traversal or symlink entry')

            extracted_path = (target / relative_entry_path).resolve()
            if not extracted_path.is_relative_to(resolved_destination_root):
                raise ValueError('Bedrock archive escapes installation root')

        source_archive.extractall(target)


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
    """Read a RakNet unconnected-pong MOTD without trusting arbitrary UDP data."""
    if len(packet) < 35 or packet[0] != 0x1C or packet[17:33] != _RAKNET_MAGIC:
        return None

    motd_length_bytes = int.from_bytes(packet[33:35], 'big')
    if motd_length_bytes > 2048 or 35 + motd_length_bytes > len(packet):
        return None

    motd = packet[35:35 + motd_length_bytes].decode('utf-8', errors='replace')
    motd_fields = motd.split(';')
    return {
        'address': address,
        'port': port,
        'name': (motd_fields[1] if len(motd_fields) > 1 else motd)[:160],
        'protocol': 'RakNet unconnected pong',
    }


def discover_lan_bedrock(*, timeout: float = 1.5) -> list[dict]:
    """Discover nearby Bedrock servers with a bounded host-side UDP scan.

    This proves only host-side UDP reception. The separate guest-relay marker
    is required to establish whether a packet crossed LinuxKit's boundary.
    """
    timeout = max(0.1, min(float(timeout), 3.0))
    discovery_sockets = []
    discovered_servers = {}

    try:
        for bedrock_port in (19132, 19133):
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp_socket.bind(('0.0.0.0', 0))
            discovery_sockets.append((udp_socket, bedrock_port))

            # RakNet Unconnected Ping: packet ID + timestamp + magic + GUID.
            ping = (
                b'\x01'
                + struct.pack('>Q', int(time.time() * 1000))
                + _RAKNET_MAGIC
                + b'CINTERFG'
            )
            try:
                udp_socket.sendto(ping, ('255.255.255.255', bedrock_port))
            except OSError:
                pass  # Restricted hosts may still receive other LAN traffic.

        deadline = time.monotonic() + timeout
        while discovery_sockets and time.monotonic() < deadline:
            remaining_seconds = max(0, deadline - time.monotonic())
            ready_sockets, _, _ = select.select(
                [udp_socket for udp_socket, _ in discovery_sockets],
                [], [], remaining_seconds,
            )
            for udp_socket in ready_sockets:
                try:
                    packet, (address, port) = udp_socket.recvfrom(4096)
                except OSError:
                    continue

                discovered_server = _parse_raknet_pong(packet, address, port)
                if discovered_server:
                    discovered_servers[(address, port)] = discovered_server
    finally:
        for udp_socket, _ in discovery_sockets:
            udp_socket.close()

    return sorted(
        discovered_servers.values(),
        key=lambda server: (
            server['name'].casefold(), server['address'], server['port']
        ),
    )


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
        # One server data root per installation mode. Never place installed
        # worlds, logs or runtime state under the developer Builds directory.
        installed_server_directory = 'common/CinterForge/Servers/Bedrock'
        developer_server_directory = 'Servers/Bedrock'
        self.root = (
            self.steamapps / installed_server_directory
            if user_install else self.builds / developer_server_directory
        )
        # Curated worlds are user-requested content, not developer Builds or a second Bedrock server.
        self.curated_worlds = self.root / 'CuratedWorlds'
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
        self._craftmaster_process: subprocess.Popen | None = None
        self._craftmaster_conversion_lock = threading.Lock()
        # Server access rules belong to this installation, not any selected
        # level. All world launches reuse this one policy and Run directory.
        self.access = BedrockServerAccess(
            self.root, self.run_root,
            live=lambda: self._live_session_locked()[0] is not None,
            send=self._access_send,
            event=self._access_event,
            command=self._bridge_host_command,
        )

    def _access_send(self, command: str) -> None:
        self.console_command(command)

    def _access_event(self, message: str, *, kind: str = 'server-access') -> None:
        with self._lock:
            self._event_id += 1
            self._events.append({'id': self._event_id, 'kind': kind,
                                 'message': str(message)[:512]})

    def _bridge_host_command(self, sender: str, command: str) -> None:
        # The Bedrock PTY reader is the ONLY event source. Its attached tab
        # owns the existing host Bash command dispatcher and all visible IO.
        session = self._terminal_session
        if session is not None and not session.closed:
            try:
                session.forward_host_command(sender, command)
            except (OSError, RuntimeError) as error:
                session._append_host_output(
                    ('[Bedrock] Host Bash input failed: ' + str(error) + '\r\n').encode('utf-8'))
        else:
            self._access_event('Minecraft host Bash is not connected', kind='server-access-error')

    def observe_console_output(self, data: bytes) -> None:
        # PTY reader calls this only for the owning Bedrock Session. This is
        # not a second console and does not launch a VM.
        with self._lock:
            try:
                self.access.observe(data)
            except (OSError, RuntimeError, ValueError, TypeError) as error:
                # A malformed policy or disk failure must not terminate the
                # sole PTY reader or kill the active Minecraft console.
                message = 'Bedrock access policy observer: ' + str(error)
                if getattr(self, '_access_observer_error', None) != message:
                    self._access_observer_error = message
                    self._access_event(message, kind='server-access-error')


    # --- Runtime process ownership and EULA ----------------------------------
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
            output.flush()
            os.fsync(output.fileno())
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

    # --- Local Bedrock / Java save discovery ---------------------------------
    def _default_world_roots(self) -> list[Path]:
        """Discover existing Bedrock saves in known platform-specific locations.

        macOS:   ~/Library/Application Support/minecraftpe/
        Windows: %APPDATA%/Minecraft Bedrock/ and Microsoft Store Packages/
        Linux:   common `minecraftpe` / launcher locations (discovery only)
        """
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
            roots = [root.resolve()] if root.is_dir() and not root.is_symlink() else []
            if self.curated_worlds.is_dir() and not self.curated_worlds.is_symlink():
                roots.append(self.curated_worlds.resolve())
            return roots
        roots = list(dict.fromkeys(self._default_world_roots()))
        if self.curated_worlds.is_dir() and not self.curated_worlds.is_symlink():
            roots.append(self.curated_worlds.resolve())
        return roots

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
            previous = self._world_settings()
            self._save_world_settings({'root': str(path), 'selected': previous.get('selected')})
            return self.list_worlds()

    def _java_world_roots(self) -> list[Path]:
        """Use each OS's real Java saves root; never scan arbitrary directories."""
        home = self._login_home()
        if self.platform == 'mac':
            paths = [home / 'Library/Application Support/minecraft/saves']
        elif self.platform == 'win':
            roaming = Path(os.environ.get('APPDATA', str(home / 'AppData/Roaming')))
            paths = [roaming / '.minecraft/saves']
        else:
            paths = [home / '.minecraft/saves']
        return [p.resolve() for p in paths if p.is_dir() and not p.is_symlink()]

    # --- Bundled native CraftMaster conversion -------------------------------
    def _craftmaster_cli(self) -> tuple[Path, Path]:
        """Use exactly the verified native executable shipped with this app."""
        from runtime.CinterForgeCommands import _packaged_craftmaster
        binary = _packaged_craftmaster()
        package = binary.parent.parent
        manifest = json.loads((package / 'manifest.json').read_text('utf-8'))
        for name in ('Resources.json', 'Contours.json', 'MCTemplate.mcworld',
                     'world-template.htm', 'zoomify-viewer.min.js', 'preview.py'):
            asset = package / 'data' / name
            expected = manifest.get('files', {}).get('data/' + name)
            if (not asset.is_file() or asset.is_symlink() or
                    not isinstance(expected, str) or _sha256(asset) != expected):
                raise RuntimeError('Packaged CraftMaster data failed verification: ' + name)
        return binary, package / 'data'

    def list_java_worlds(self) -> list[dict]:
        """Native CraftMaster is authoritative for Java world inventory."""
        roots = self._java_world_roots()
        if not roots:
            return []
        binary, _ = self._craftmaster_cli()
        worlds = []
        for root in roots:
            scan = subprocess.run([str(binary), 'worlds', '--json', '--root', str(root)],
                                  capture_output=True, text=True, timeout=90, check=False)
            if scan.returncode:
                raise RuntimeError('CraftMaster Java world scan failed: ' + scan.stderr[-1000:])
            data = json.loads(scan.stdout)
            if data.get('schema') != 'cinterforge.craftmaster.worlds.v1':
                raise RuntimeError('CraftMaster returned unexpected worlds inventory schema')
            for item in data.get('worlds', []):
                path = Path(item.get('path', ''))
                if (not path.is_dir() or path.is_symlink() or
                        not path.resolve().parent == root or
                        not (path / 'level.dat').is_file() or (path / 'db').is_dir()):
                    continue
                world = {'name': item['name'], 'folder': path.name,
                         'path': str(path.resolve()), 'root': str(root),
                         'edition': 'java', 'dimensions': item.get('dimensions', {})}
                world['command_name'] = _world_command_name(world)
                world['id'] = self._world_id(world)
                # User-owned world state lives beside the Java save, never in Builds.
                local = path / 'CinterForge'
                try:
                    if local.is_symlink():
                        raise OSError('CinterForge is a symlink')
                    local.mkdir(exist_ok=True)
                except OSError as error:
                    world['storage_error'] = str(error)
                worlds.append(world)
        return worlds

    def java_world_export_name(self, world: dict) -> str:
        """Ask the packaged converter for the published/Bedrock folder name."""
        binary, _ = self._craftmaster_cli()
        source = Path(world['path'])
        result = subprocess.run(
            [str(binary), 'export-name', '--world-folder', source.name],
            capture_output=True, text=True, timeout=20, check=True,
        )
        export_name = result.stdout.strip()
        # CraftMaster owns Unicode naming. Reject unsafe destination paths.
        if (
            not export_name or len(export_name.encode('utf-8')) > 240
            or export_name != export_name.strip()
            or export_name in ('.', '..') or Path(export_name).name != export_name
            or any(character in export_name for character in '\\/:*?"<>|')
            or any(ord(character) < 32 or ord(character) == 127 for character in export_name)
        ):
            raise RuntimeError('CraftMaster returned unsafe converted world name: ' + repr(export_name))
        return export_name

    def find_java_world(self, requested_name: str) -> dict | None:
        """Match an exact Java folder, display name or CraftMaster export name.

        `Candlewood (Candlewood)` must be selectable as `Candlewood`, without
        incorrectly falling back to a published world on a local command.
        """
        requested = requested_name.strip().casefold()
        matches = []
        for world in self.list_java_worlds():
            names = {world['name'].casefold(), world['folder'].casefold(),
                     _world_command_name(world).casefold()}
            if requested in names:
                matches.append(world)
        if len(matches) > 1:
            raise RuntimeError('Ambiguous Java world; select its unique folder name')
        return matches[0] if matches else None

    @staticmethod
    def _world_id(world: dict) -> str:
        return hashlib.sha256((str(world['edition']) + ':' +
                               str(Path(world['path']).resolve())).encode('utf-8')).hexdigest()[:24]

    @staticmethod
    def world_data_root(world: dict) -> Path:
        """Persistent, world-local CinterForge data (never mixed into Java input)."""
        source = Path(world['path'])
        if source.is_symlink() or not source.is_dir():
            raise RuntimeError('World is unavailable or symlinked: ' + str(source))
        result = source / 'CinterForge'
        if result.is_symlink() or (result.exists() and not result.is_dir()):
            raise RuntimeError('World CinterForge directory is unsafe: ' + str(result))
        return result

    def craftmaster_output_root(self, world: dict) -> Path:
        if world.get('edition') != 'java':
            raise ValueError('CraftMaster output belongs inside an original Java save')
        return self.world_data_root(world)

    def _migrate_craftmaster_output(self, world: dict) -> None:
        """Copy only known previous outputs; leave the working Builds originals intact."""
        old = self.root / 'Conversions' / 'CraftMaster'
        if old.is_symlink() or not old.is_dir():
            return
        source = Path(world['path'])
        export = self.java_world_export_name(world)
        destination = self.craftmaster_output_root(world)
        for folder, name in (('Maps', source.name), ('Worlds', export)):
            original = old / folder / name
            target = destination / folder / name
            if not original.is_dir() or target.exists():
                continue
            if any(path.is_symlink() for path in original.rglob('*')):
                raise RuntimeError('Refusing to migrate linked CraftMaster output: ' + str(original))
            # Prior shared Builds data cannot be attributed to this Java world
            # solely by matching its name. Its receipt binds the source path.
            if folder == 'Maps':
                receipt = original / 'Working' / 'BedrockState.txt'
                if not receipt.is_file():
                    continue
            else:
                marker = self.curated_worlds / export / 'cinter-craftmaster-world.json'
                if not marker.is_file() or marker.is_symlink():
                    continue
                try:
                    if json.loads(marker.read_text('utf-8')).get('source') != str(source.resolve()):
                        continue
                except (ValueError, OSError):
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(original, target, symlinks=False)
            self._append_log('Migrated previously generated CraftMaster ' + folder + '/' + name)

    def current_world(self) -> dict | None:
        selected = self._world_settings().get('selected') or {}
        path = selected.get('path')
        if not path:
            return None
        inventory = self.list_worlds()
        matches = [world for world in inventory['worlds'] + inventory['java_worlds']
                   if world['path'] == path and world['edition'] == selected.get('edition', 'bedrock')]
        return matches[0] if len(matches) == 1 else None

    def select_world(self, requested: str) -> dict:
        """Exactly one global selection, stored by verified source path and edition."""
        inventory = self.list_worlds()
        worlds = inventory['worlds'] + inventory['java_worlds']
        key = str(requested or '').strip().casefold()
        if not key:
            raise ValueError('Select a Java or Bedrock world by its displayed name or ID')
        matched = [world for world in worlds if key in
                   (world['id'], world['folder'].casefold(), world['name'].casefold(),
                    world['command_name'].casefold())]
        # The exact ID disambiguates same-named Java/Bedrock worlds.
        exact_id = [world for world in matched if world['id'] == key]
        if exact_id:
            matched = exact_id
        if len(matched) != 1:
            raise ValueError('World not found or ambiguous; use the unique ID from bedrock report')
        world = matched[0]
        with self._lock:
            previous = self._world_settings()
            self._save_world_settings({'root': previous.get('root'),
                'selected': {key: world[key] for key in ('id','path','edition','folder','name','command_name')}})
        return {'current_world': world['command_name'], 'world': world}

    def selected_pack_record(self, world: dict) -> dict | None:
        file = self.world_data_root(world) / 'Packs/current.json'
        if not file.exists():
            return None
        if file.is_symlink() or not file.is_file():
            raise RuntimeError('Unsafe per-world pack selection')
        record = json.loads(file.read_text('utf-8'))
        if (record.get('schema') != 'cinterforge.terminal.current-pack.v1' or
                not re.fullmatch(r'[0-9a-f]{64}', str(record.get('sha256', '')))):
            raise RuntimeError('Invalid per-world pack selection')
        return record

    @staticmethod
    def _java_source_files(source: Path) -> tuple[dict, float | None]:
        """Inventory real Java save files, excluding our generated CinterForge tree.

        Retain nanosecond modification times and sizes so the same inventory
        can later identify individual files/regions for incremental map updates.
        Minecraft's session.lock is recorded but does not mark gameplay stale.
        """
        files = {}
        newest = None
        for base, directories, names in os.walk(source, followlinks=False):
            base_path = Path(base)
            directories[:] = sorted(directory for directory in directories
                                    if not (base_path == source and directory == 'CinterForge'))
            for directory in directories:
                if (base_path / directory).is_symlink():
                    raise RuntimeError('Linked directory in Java save: ' + str(base_path / directory))
            for name in sorted(names):
                file = base_path / name
                if file.is_symlink():
                    raise RuntimeError('Linked file in Java save: ' + str(file))
                info = file.stat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                relative = file.relative_to(source).as_posix()
                files[relative] = {'size': info.st_size, 'mtime_ns': info.st_mtime_ns}
                if relative != 'session.lock':
                    newest = max(newest or 0, info.st_mtime_ns)
        return files, newest / 1_000_000_000 if newest is not None else None

    @staticmethod
    def _java_gameplay_files(files: dict) -> dict:
        """session.lock is touched by Minecraft without any world content edit."""
        return {name: info for name, info in files.items() if name != 'session.lock'}

    def _java_source_receipt(self, world: dict) -> Path:
        return self.craftmaster_output_root(world) / 'JavaSourceFiles.json'

    def _write_java_source_receipt(self, world: dict, files: dict, archive: Path) -> None:
        """Commit the source inventory only after a successful native conversion."""
        destination = self._java_source_receipt(world)
        if destination.is_symlink():
            raise RuntimeError('Unsafe Java source inventory path')
        destination.parent.mkdir(parents=True, exist_ok=True)
        record = {'schema': 'cinterforge.craftmaster.java-source-files.v1',
                  'source': str(Path(world['path']).resolve()),
                  'archive': archive.name, 'files': files}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=destination.parent,
                                             prefix='java-source-', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(record, handle, ensure_ascii=False, sort_keys=True)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def conversion_status(self, world: dict) -> dict:
        if world.get('edition') != 'java':
            return {'ready': True, 'needs_update': False, 'has_conversion': True}
        source = Path(world['path'])
        output = self.craftmaster_output_root(world)
        result = {'ready': False, 'needs_update': False, 'has_conversion': False,
                  'verification_unavailable': True, 'last_played': None, 'last_updated': None}
        try:
            files, last_modified = self._java_source_files(source)
            result['last_played'] = last_modified
        except (OSError, RuntimeError, ValueError) as error:
            result['status_error'] = 'Cannot inspect Java save: ' + str(error)
            return result
        # The native export-name is authoritative, but an older or unavailable
        # packaged binary must not hide dates or turn a conversion into 'stale'.
        try:
            export = self.java_world_export_name(world)
        except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired) as error:
            export = world['command_name']
            result['status_error'] = 'CraftMaster export-name unavailable: ' + str(error)
        archive = output / 'Worlds' / export / (export + '.mcworld')
        try:
            if archive.is_symlink():
                raise RuntimeError('Linked Bedrock archive')
            if archive.is_file():
                result['last_updated'] = archive.stat().st_mtime
                result['has_conversion'] = True
            if not result['has_conversion']:
                # No archive is not an outdated conversion: no conversion exists.
                result.update(verification_unavailable=False, needs_update=True, state='not_converted')
                return result
            receipt = self._java_source_receipt(world)
            if receipt.is_symlink():
                raise RuntimeError('Linked Java source inventory')
            if receipt.is_file():
                stored = json.loads(receipt.read_text(encoding='utf-8'))
                if (stored.get('schema') != 'cinterforge.craftmaster.java-source-files.v1'
                        or stored.get('source') != str(source.resolve())
                        or stored.get('archive') != archive.name
                        or not isinstance(stored.get('files'), dict)):
                    raise ValueError('Invalid Java source inventory')
                changed = (self._java_gameplay_files(files) !=
                           self._java_gameplay_files(stored['files']))
                result.update(ready=not changed, needs_update=changed,
                              verification_unavailable=False,
                              state='stale' if changed else 'ready')
                return result
            # Legacy conversion: no full save inventory was recorded at export.
            # A file modified after the export proves the archive may be old;
            # native status may establish readiness for older conversions. Do
            # not equate a missing/invalid native receipt with changed Java data.
            newer = (last_modified is not None and
                     last_modified > archive.stat().st_mtime + 0.000001)
            if newer:
                result.update(ready=False, needs_update=True,
                              verification_unavailable=False, state='stale')
                return result
            try:
                binary, assets = self._craftmaster_cli()
                response = subprocess.run([str(binary), 'status', '--world', str(source),
                                           '--data-dir', str(assets), '--output-root', str(output), '--json'],
                                          capture_output=True, text=True, timeout=180, check=False)
                if response.returncode:
                    raise RuntimeError(response.stderr.strip()[-600:] or 'CraftMaster status failed')
                native = json.loads(response.stdout)
                if native.get('schema') != 'cinterforge.craftmaster.bedrock-status.v1':
                    raise RuntimeError('CraftMaster status returned an unexpected schema')
                if native.get('ready') is True:
                    result.update(ready=True, needs_update=False,
                                  verification_unavailable=False, state='ready')
                    return result
                if native.get('source_changed') is True and native.get('source_readable') is True:
                    result.update(ready=False, needs_update=True,
                                  verification_unavailable=False, state='stale')
                    return result
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                result['status_error'] = str(error)
            # Older conversions lack our complete-file snapshot. When every
            # Java source file predates a valid local .mcworld, the file dates
            # show no evidence of changes since the conversion. A missing or
            # incompatible native receipt must never be interpreted as stale.
            try:
                with zipfile.ZipFile(archive) as bundle:
                    names = set(bundle.namelist())
                    if not {'level.dat', 'db/CURRENT'}.issubset(names) or not any(
                            name.startswith('db/MANIFEST-') for name in names):
                        raise ValueError('Bedrock archive has incomplete world data')
                result.update(ready=True, needs_update=False,
                              verification_unavailable=False,
                              state='ready', verification_method='file_modification_dates')
                result.pop('status_error', None)
            except (OSError, ValueError, zipfile.BadZipFile) as error:
                result['state'] = 'unverified'
                result['status_error'] = str(error)
            return result
        except (OSError, RuntimeError, ValueError) as error:
            result['status_error'] = str(error)
            result['state'] = 'unverified'
            return result

    def world_by_id(self, id: str) -> dict:
        if not re.fullmatch(r'[0-9a-f]{24}', str(id or '')):
            raise ValueError('Invalid world ID')
        inventory = self.list_worlds()
        match = [w for w in inventory['worlds'] + inventory['java_worlds'] if w['id'] == id]
        if len(match) != 1:
            raise FileNotFoundError('World is no longer available')
        return match[0]

    def world_thumbnail(self, world: dict) -> Path | None:
        source = Path(world['path'])
        candidates = (('icon.png',) if world['edition'] == 'java'
                      else ('world_icon.jpeg', 'world_icon.jpg', 'world_icon.png'))
        for name in candidates:
            image = source / name
            if image.is_file() and not image.is_symlink() and image.stat().st_size <= 4*1024*1024:
                return image
        return None

    def is_builtin_candlewood(self, world: dict) -> bool:
        """Only the original Candlewood or its managed curated Bedrock export.

        Download is a limited built-in feature for now. Matching a user-supplied
        display name alone cannot make arbitrary world saves downloadable.
        """
        source = Path(world.get('path', ''))
        if world.get('command_name') != 'Candlewood' or source.is_symlink():
            return False
        if world.get('edition') == 'java':
            return source.name == 'Candlewood (Candlewood)' and any(
                source.parent == saves for saves in self._java_world_roots())
        if world.get('edition') == 'bedrock':
            return (source.name == 'Candlewood' and
                    source.parent == self.curated_worlds and source.is_dir())
        return False

    def export_world_file(self, world: dict, temporary_root: Path) -> tuple[Path, bool]:
        """Return (validated archive, temporary); never expose private state."""
        if not self.is_builtin_candlewood(world):
            raise PermissionError('Only the built-in Candlewood world can be downloaded')
        if world['edition'] == 'java':
            source = Path(world['path'])
            export = self.java_world_export_name(world)
            archive = self.craftmaster_output_root(world) / 'Worlds' / export / (export + '.mcworld')
            if not archive.is_file() or archive.is_symlink():
                raise FileNotFoundError('Java world needs Update for Bedrock before download')
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
                if not {'level.dat', 'db/CURRENT'}.issubset(names) or not any(
                        name.startswith('db/MANIFEST-') for name in names):
                    raise RuntimeError('CraftMaster archive is not a complete Bedrock world')
            return archive, False
        source = Path(world['path'])
        if not all((source / name).is_file() for name in ('level.dat', 'db/CURRENT')) or not list((source / 'db').glob('MANIFEST-*')):
            raise RuntimeError('Bedrock world is missing level.dat or LevelDB')
        if source.is_symlink():
            raise RuntimeError('Unsafe linked world')
        temporary_root.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix='bedrock-export-', suffix='.mcworld', dir=temporary_root)
        os.close(fd)
        output = Path(raw)
        try:
            total = 0
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
                for base, dirs, files in os.walk(source, followlinks=False):
                    folder = Path(base)
                    dirs[:] = [name for name in dirs if name != 'CinterForge' and
                               not (folder / name).is_symlink()]
                    for name in files:
                        file = folder / name
                        if file.is_symlink() or not file.is_file():
                            raise RuntimeError('Unsafe linked Bedrock world member')
                        rel = file.relative_to(source)
                        if rel.name.startswith('cinter-') or rel.name == 'cinterforge-selected-pack.json':
                            continue
                        total += file.stat().st_size
                        if total > 64 * 1024**3:
                            raise RuntimeError('World exceeds 64 GiB export cap')
                        bundle.write(file, rel.as_posix())
            with zipfile.ZipFile(output) as verify:
                names = set(verify.namelist())
                if not {'level.dat', 'db/CURRENT'}.issubset(names) or not any(
                        name.startswith('db/MANIFEST-') for name in names):
                    raise RuntimeError('Export is missing required Bedrock content')
            return output, True
        except Exception:
            output.unlink(missing_ok=True)
            raise

    def report_worlds(self) -> dict:
        inventory = self.list_worlds()
        selected = self._world_settings().get('selected') or {}
        for world in inventory['worlds'] + inventory['java_worlds']:
            world['current'] = world['path'] == selected.get('path') and world['edition'] == selected.get('edition', 'bedrock')
            record = self.selected_pack_record(world)
            world['pack'] = record['name'] if record else None
            if world['edition'] == 'java':
                world['conversion'] = self.conversion_status(world)
            else:
                world['conversion'] = {'ready': True, 'needs_update': False}
        current = next((w for w in inventory['worlds'] + inventory['java_worlds'] if w['current']), None)
        inventory['current_world'] = current['command_name'] if current else None
        inventory['current_pack'] = ((self.selected_pack_record(current) or {}).get('name') if current else None)
        inventory['running'] = bool(self.status().get('running'))
        return inventory

    def run_native_craftmaster(
        self, world: dict, progress: Callable[[str], None] | None = None,
    ) -> Path:
        """Incrementally convert a Java world and stream the native CLI output.

        This is also the standalone `craftmaster <world>` operation: it does not
        import the world or start a Bedrock server.
        """
        binary, assets = self._craftmaster_cli()
        source = Path(world['path'])
        if source.is_symlink() or not (source / 'level.dat').is_file():
            raise RuntimeError('Java world disappeared or became unsafe: ' + str(source))
        export_name = self.java_world_export_name(world)
        self._migrate_craftmaster_output(world)
        output_root = self.craftmaster_output_root(world)
        output_root.mkdir(parents=True, exist_ok=True)
        command = [
            str(binary), 'convert',
            '--world', str(source.resolve()),
            '--data-dir', str(assets),
            '--output-root', str(output_root),
            '--dimensions', 'all', '--no-open',
        ]
        report = progress or self._append_log
        with self._craftmaster_conversion_lock:
            source_before, _ = self._java_source_files(source)
            report(f'{export_name}: starting native CraftMaster conversion')
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            with self._lock:
                self._craftmaster_process = process
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    if line.strip():
                        report('[CraftMaster] ' + line.rstrip()[:2000])
                exit_code = process.wait()
                if exit_code:
                    raise RuntimeError(
                        f'CraftMaster conversion failed (exit {exit_code}); see Bedrock log'
                    )
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                with self._lock:
                    if self._craftmaster_process is process:
                        self._craftmaster_process = None
                if process.stdout:
                    process.stdout.close()

        archive = output_root / 'Worlds' / export_name / (export_name + '.mcworld')
        if not archive.is_file() or archive.is_symlink() or archive.stat().st_size < 1024:
            raise RuntimeError('CraftMaster did not create a complete .mcworld: ' + str(archive))
        live_map = output_root / 'Maps' / source.name / 'Overworld' / 'Live Map.htm'
        if not live_map.is_file() or live_map.is_symlink():
            raise RuntimeError('CraftMaster did not create the live Zoomify map: ' + str(live_map))
        source_after, _ = self._java_source_files(source)
        if self._java_gameplay_files(source_before) != self._java_gameplay_files(source_after):
            raise RuntimeError('Java save changed during conversion; Bedrock freshness cannot be certified')
        self._write_java_source_receipt(world, source_after, archive)
        report(export_name + ': Zoomify and Bedrock export ready')
        return archive

    def _convert_java_world(
        self, world: dict, progress: Callable[[str], None] | None = None,
        *, force: bool = False,
    ) -> Path:
        """Reuse an installed save, or atomically import a converted update."""
        source = Path(world['path'])
        export_name = self.java_world_export_name(world)
        destination = self.curated_worlds / export_name
        if destination.is_symlink() or self.curated_worlds.is_symlink():
            raise RuntimeError('Refusing linked CraftMaster world destination')
        if destination.is_dir() and not any(path.is_symlink() for path in destination.rglob('*')):
            if not force and (
                (destination / 'level.dat').is_file()
                and (destination / 'db/CURRENT').is_file()
                and any((destination / 'db').glob('MANIFEST-*'))
            ):
                if progress:
                    progress(export_name + ': reusing validated Bedrock world')
                return destination
        if destination.exists() and not force:
            raise RuntimeError('Refusing to overwrite Bedrock world: ' + str(destination))
        if force and destination.exists() and (not destination.is_dir() or any(
                entry.is_symlink() for entry in destination.rglob('*'))):
            raise RuntimeError('Refusing unsafe existing CraftMaster world: ' + str(destination))

        archive = self.run_native_craftmaster(world, progress=progress)
        self.curated_worlds.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix='craftmaster-import-', dir=self.curated_worlds,
        ) as temporary_directory:
            extracted = Path(temporary_directory) / 'world'
            extracted.mkdir()
            _safe_extract(archive, extracted, max_uncompressed_bytes=64 * 1024**3)
            if not all((extracted / path).is_file() for path in ('level.dat', 'levelname.txt', 'db/CURRENT')):
                raise RuntimeError('CraftMaster archive missing Bedrock level or LevelDB')
            if not list((extracted / 'db').glob('MANIFEST-*')):
                raise RuntimeError('CraftMaster world LevelDB missing MANIFEST')
            (extracted / 'levelname.txt').write_text(export_name + '\n', encoding='utf-8')
            (extracted / 'cinter-craftmaster-world.json').write_text(
                json.dumps({
                    'schema': 'cinterforge.terminal.craftmaster-world.v1',
                    'source': str(source.resolve()),
                    'world': export_name,
                    'archive_sha256': _sha256(archive),
                }, indent=2) + '\n', encoding='utf-8',
            )
            if destination.exists():
                if not force:
                    raise RuntimeError('CraftMaster destination changed during conversion: ' + str(destination))
                backup = self.curated_worlds / ('craftmaster-previous-' + uuid.uuid4().hex)
                os.replace(destination, backup)
                try:
                    os.replace(extracted, destination)
                except Exception:
                    os.replace(backup, destination)
                    raise
                else:
                    shutil.rmtree(backup)  # Converted and validated before replacing the old save.
            else:
                os.replace(extracted, destination)
        (progress or self._append_log)(export_name + ': world imported for Bedrock')
        return destination

    def refresh_managed_world(self, source: Path) -> None:
        """Replace an *offline* managed server copy after explicit CraftMaster update.

        The Bedrock runtime copies curated saves to a fixed path keyed by their
        source folder. Reusing an old copy would silently restart old blocks.
        """
        source = source.resolve(strict=True)
        key = hashlib.sha256(str(source).encode('utf-8')).hexdigest()[:12]
        destination = self.run_root / 'worlds' / ('CinterForge-' + key)
        if destination.is_symlink():
            raise RuntimeError('Refusing linked managed Bedrock world: ' + str(destination))
        if not destination.exists():
            return  # First launch creates it from the newly converted source.
        # Never replace a world that Bedrock is still using.
        with self._lock:
            pid, _, _ = self._live_session_locked()
            current = self._server_configuration().get('level-name', '')
            if pid is not None and current == destination.name:
                raise RuntimeError('Stop the active Bedrock world before replacing its save')
        fresh = destination.parent / ('craftmaster-updated-' + uuid.uuid4().hex)
        backup = destination.parent / ('craftmaster-backup-' + uuid.uuid4().hex)
        try:
            self._safe_world_copy(source, fresh)
            os.replace(destination, backup)
            try:
                os.replace(fresh, destination)
            except Exception:
                os.replace(backup, destination)
                raise
            else:
                shutil.rmtree(backup)
        finally:
            if fresh.exists():
                shutil.rmtree(fresh)

    # --- Public Cloudflare published-world installation ----------------------
    def install_published_world(
        self,
        name: str,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        """Download and verify an explicitly selected public CraftMaster world.

        Cloudflare public channels (no private developer bucket):
          release: https://cinterforge.ucreg.io/CinterForgeCraftMaster/Worlds/<world>/
          beta:    https://cinterforge.ucreg.io/CinterForgeCraftMaster/Worlds/beta/<world>/

        Download the .mcworld directly without contacting any manifest URL.
        Verify ZIP CRC and Bedrock structure; record the observed SHA-256
        without falsely claiming independent publisher authentication.
        run_world() creates the separately writable server copy.
        """
        if (
            not isinstance(name, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _().-]{0,120}', name)
            or name.endswith((' ', '.'))
        ):
            raise ValueError('Invalid curated world name')

        world_directory = self.curated_worlds / name
        installed_receipt_path = world_directory / 'cinter-published-world.json'
        if world_directory.is_symlink() or self.curated_worlds.is_symlink():
            raise RuntimeError('Refusing linked published-world destination')

        if installed_receipt_path.is_file() and not installed_receipt_path.is_symlink():
            installed_receipt = json.loads(installed_receipt_path.read_text(encoding='utf-8'))
            if (
                installed_receipt.get('schema') == 'cinterforge.terminal.curated-world.v1'
                and installed_receipt.get('world') == name
                and re.fullmatch(r'[0-9a-f]{64}', str(installed_receipt.get('sha256', '')))
                and (world_directory / 'level.dat').is_file()
                and (world_directory / 'db/CURRENT').is_file()
                and (world_directory / 'levelname.txt').is_file()
                and not any(item.is_symlink() for item in world_directory.rglob('*'))
            ):
                if progress:
                    progress(
                        name + ': already installed from verified published world ('
                        + str(world_directory) + ')'
                    )
                return world_directory

        if world_directory.exists():
            raise RuntimeError(
                'Existing Candlewood content is incomplete or unverified: '
                + str(world_directory)
            )

        # Public R2 URLs are stable. Do not require a separate manifest to
        # authorize the archive: Cloudflare may expose the .mcworld even when
        # manifest.json is blocked or not propagated. A manifest, if available,
        # provides additional checksum verification, never a download gate.
        encoded_name = quote(name, safe='')
        archive_name = name + '.mcworld'
        missing_channels = []
        self.curated_worlds.mkdir(parents=True, exist_ok=True)

        for channel in PUBLIC_CRAFTMASTER_CHANNELS:
            channel_prefix = '' if channel == 'release' else channel + '/'
            world_url = PUBLIC_CRAFTMASTER_WORLDS_URL + channel_prefix + encoded_name
            download_url = world_url + '/' + quote(archive_name, safe='')
            if progress:
                progress(f'{name}: downloading public {channel} world directly: {download_url}')

            with tempfile.TemporaryDirectory(
                prefix='published-world-download-', dir=self.curated_worlds,
            ) as temporary_directory:
                temporary_root = Path(temporary_directory)
                downloaded_archive = temporary_root / archive_name
                digest = hashlib.sha256()
                downloaded_bytes = 0
                last_progress_bytes = 0
                try:
                    # Match an ordinary public browser GET.  In particular,
                    # do NOT send Cache-Control: no-cache: the public Cloudflare
                    # edge may serve a cached object while rejecting an origin
                    # revalidation that a normal browser never asks it to do.
                    request = Request(download_url, headers={
                        'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                       'AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15'),
                        'Accept': 'application/octet-stream, application/zip, */*',
                    })
                    with urlopen(request, timeout=60) as response:
                        if response.geturl() != download_url or response.status != 200:
                            raise RuntimeError('Unexpected published world download response')
                        declared_size = response.headers.get('Content-Length')
                        if declared_size and int(declared_size) > WORLD_ARCHIVE_MAX_BYTES:
                            raise RuntimeError('Published world exceeds maximum archive size')
                        with downloaded_archive.open('wb') as archive_stream:
                            for block in iter(
                                lambda: response.read(WORLD_DOWNLOAD_CHUNK_BYTES), b''
                            ):
                                downloaded_bytes += len(block)
                                if downloaded_bytes > WORLD_ARCHIVE_MAX_BYTES:
                                    raise RuntimeError('Published world exceeds maximum archive size')
                                archive_stream.write(block)
                                digest.update(block)
                                if (
                                    progress
                                    and downloaded_bytes - last_progress_bytes >= WORLD_DOWNLOAD_PROGRESS_BYTES
                                ):
                                    progress(f'{name}: downloaded {downloaded_bytes / 1048576:.1f} MiB')
                                    last_progress_bytes = downloaded_bytes
                except HTTPError as error:
                    if error.code in (403, 404):
                        # A 404 means no published object on this channel; a 403
                        # means this HTTP client was denied.  Neither establishes
                        # anything about other channels or a user's browser.
                        status = 'not published' if error.code == 404 else 'request denied'
                        ray = error.headers.get('CF-Ray', '') if error.headers else ''
                        detail = (f'{channel}: HTTP {error.code} ({status}) '
                                  f'for GET {download_url}')
                        if ray:
                            detail += f' (Cloudflare Ray {ray})'
                        missing_channels.append(detail)
                        if progress:
                            progress(name + ': ' + detail)
                        continue
                    raise

                if downloaded_bytes < 1024:
                    raise RuntimeError('Published world download is empty: ' + download_url)
                actual_sha256 = digest.hexdigest()
                if progress:
                    progress(f'{name}: downloaded {downloaded_bytes / 1048576:.1f} MiB; validating Bedrock ZIP')

                # No public manifest request. The verified HTTPS archive URL is
                # the only network dependency. ZIP entries are CRC-checked by
                # ZipFile.extractall; avoid decompressing this large world twice.
                # Without independent publisher metadata, the checksum proves
                # local integrity only, not authenticated publisher provenance.
                extracted_world = temporary_root / name
                extracted_world.mkdir()
                if progress:
                    progress(name + ': extracting and CRC-checking Bedrock world')
                try:
                    _safe_extract(
                        downloaded_archive, extracted_world,
                        max_uncompressed_bytes=64 * 1024**3,
                    )
                except zipfile.BadZipFile as error:
                    raise RuntimeError('Published world ZIP failed integrity check') from error
                if not all(
                    (extracted_world / filename).is_file()
                    for filename in ('level.dat', 'levelname.txt', 'db/CURRENT')
                ):
                    raise RuntimeError('Published world archive missing Bedrock files')
                if not list((extracted_world / 'db').glob('MANIFEST-*')):
                    raise RuntimeError('Published world LevelDB missing MANIFEST')

                (extracted_world / 'levelname.txt').write_text(name + '\n', encoding='utf-8')
                receipt = {
                    'schema': 'cinterforge.terminal.curated-world.v1',
                    'world': name,
                    'sha256': actual_sha256,
                    'bytes': downloaded_bytes,
                    'publication_version': None,
                    'checksum_source': 'downloaded-content-only',
                    'source': download_url,
                }
                (extracted_world / 'cinter-published-world.json').write_text(
                    json.dumps(receipt, indent=2) + '\n', encoding='utf-8',
                )
                os.replace(extracted_world, world_directory)
                if progress:
                    progress(f'{name}: installed validated Bedrock archive at {world_directory}')
                return world_directory

        raise RuntimeError(
            'Public CraftMaster download failed for ' + name + ': '
            + '; '.join(missing_channels)
        )

    def prepare_selected_java_world(self, world: dict, progress: Callable[[str], None] | None = None) -> str:
        """Only reuse a curated conversion identified as this Java save's output."""
        source = Path(world['path']).resolve(strict=True)
        export = self.java_world_export_name(world)
        curated = self.curated_worlds / export
        receipt = curated / 'cinter-craftmaster-world.json'
        if (curated.is_dir() and not curated.is_symlink() and receipt.is_file()
                and not receipt.is_symlink()):
            data = json.loads(receipt.read_text('utf-8'))
            if (data.get('schema') == 'cinterforge.terminal.craftmaster-world.v1' and
                    data.get('source') == str(source) and
                    (curated / 'level.dat').is_file() and (curated / 'db/CURRENT').is_file()):
                return export
        if curated.exists():
            raise RuntimeError('Cannot replace a Bedrock world with an unverified Java association: ' + export)
        return self._convert_java_world(world, progress=progress).name

    def prepare_world(self, name: str, progress: Callable[[str], None] | None = None) -> str:
        """Resolve the requested world: Bedrock save, Java save, then public CDN.

        A Java save is matched by its displayed name or folder; CraftMaster
        determines the export name (for example, `Candlewood (Candlewood)`
        exports as `Candlewood`). No fuzzy or partial match is assumed.
        """
        if not name.strip():
            return name
        available_bedrock_worlds = self.list_worlds()['worlds']
        matching_bedrock_worlds = [
            world for world in available_bedrock_worlds
            if name.casefold() in (world['name'].casefold(), world['folder'].casefold(),
                                   _world_command_name(world).casefold())
        ]
        if len(matching_bedrock_worlds) > 1:
            raise RuntimeError('Ambiguous Bedrock world: use its unique folder name')
        if matching_bedrock_worlds:
            return matching_bedrock_worlds[0]['folder']

        java_world = self.find_java_world(name)
        if java_world:
            return self._convert_java_world(java_world, progress=progress).name

        # Only explicitly missing local worlds attempt the public CDN.
        return self.install_published_world(name, progress=progress).name

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
                world = {'name': name or folder.name, 'folder': folder.name,
                         'path': str(folder.resolve()), 'root': str(root),
                         'edition': 'bedrock'}
                world['command_name'] = _world_command_name(world)
                world['id'] = self._world_id(world)
                entries.append(world)
        settings = self._world_settings()
        java_error = None
        try:
            java = self.list_java_worlds()
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            java = []
            java_error = str(error)
        return {'roots': [str(r) for r in self.world_roots()], 'configured_root': settings.get('root'),
                'selected': settings.get('selected'), 'worlds': entries,
                'java_worlds': java, 'java_error': java_error}

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

    def _install_world_status_bridge(self, target: Path) -> None:
        """Attach the packaged, console-only chat relay to the managed COPY.

        Other world packs and the source save are never modified. The bridge
        only prints prefixed structured text into the existing PTY; it opens
        no network listener and executes no JavaScript received from players.
        """
        authored = ASSET_LOCATION / 'bedrock_status_bridge'
        manifest_file = authored / 'manifest.json'
        script_file = authored / 'scripts' / 'main.js'
        if any(not f.is_file() or f.is_symlink() for f in (manifest_file, script_file)):
            raise RuntimeError('Packaged CinterForge Minecraft status bridge is missing')
        manifest = json.loads(manifest_file.read_text('utf-8'))
        pack_id = manifest['header']['uuid']
        version = manifest['header']['version']
        destination = self.run_root / 'behavior_packs' / 'cinterforge-status-bridge'
        if destination.parent.is_symlink():
            raise RuntimeError('Refusing symlinked Bedrock behavior-packs root')
        for folder in (destination, destination / 'scripts'):
            if folder.is_symlink():
                raise RuntimeError('Refusing symlinked Bedrock status bridge directory')
            folder.mkdir(parents=True, exist_ok=True)
        for source, output in ((manifest_file, destination / 'manifest.json'),
                               (script_file, destination / 'scripts' / 'main.js')):
            if output.is_symlink():
                raise RuntimeError('Refusing symlinked Bedrock status bridge asset')
            if not output.is_file() or output.read_bytes() != source.read_bytes():
                output.write_bytes(source.read_bytes())

        registry_path = target / 'world_behavior_packs.json'
        if registry_path.is_symlink():
            raise RuntimeError('Refusing symlinked world behavior-pack registry')
        entries = json.loads(registry_path.read_text('utf-8')) if registry_path.is_file() else []
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise RuntimeError('Invalid existing Bedrock world behavior-pack registry: ' + str(registry_path))
        entry = {'pack_id': pack_id, 'version': version}
        current = [item for item in entries if item.get('pack_id') != pack_id]
        current.append(entry)
        if entries != current:
            temporary = registry_path.with_name(registry_path.name + '-' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_text(json.dumps(current, indent=2) + '\n', encoding='utf-8')
                os.replace(temporary, registry_path)
            finally:
                temporary.unlink(missing_ok=True)

    def _install_selected_world_pack(self, target: Path, world: dict) -> None:
        """Install verified selected .mcaddon modules on the managed Bedrock COPY.

        World-native packs and the built-in status bridge are left untouched;
        only entries recorded by this installer are replaced or removed.
        """
        record = self.selected_pack_record(world)
        previous_receipt = target / 'cinterforge-selected-pack.json'
        if previous_receipt.is_symlink():
            raise RuntimeError('Unsafe Bedrock pack receipt')
        previous = (json.loads(previous_receipt.read_text('utf-8'))
                    if previous_receipt.is_file() else {})
        old = previous.get('entries', [])
        if not isinstance(old, list):
            raise RuntimeError('Invalid previously applied pack state')
        additions: list[tuple[str, dict]] = []
        if record:
            from io import BytesIO
            name = record['name']
            source = self.world_data_root(world) / 'Packs' / 'Downloaded' / name / (name + '.mcaddon')
            if (source.is_symlink() or not source.is_file() or
                    source.stat().st_size != record['bytes'] or
                    _sha256(source) != record['sha256']):
                raise RuntimeError('Selected world pack is missing or failed SHA-256 verification')
            with tempfile.TemporaryDirectory(prefix='bedrock-pack-', dir=self.run_root) as scratch:
                temp = Path(scratch)
                with zipfile.ZipFile(source) as addon:
                    nested = [row for row in addon.infolist() if row.filename.lower().endswith('.mcpack') and not row.is_dir()]
                    if not nested or len(nested) > 32:
                        raise RuntimeError('Selected Add-On must contain 1–32 .mcpack archives')
                    if sum(row.file_size for row in nested) > 4 * 1024**3:
                        raise RuntimeError('Selected pack exceeds unpacked size limit')
                    for index, member in enumerate(nested):
                        embedded = temp / ('module-' + str(index) + '.mcpack')
                        with addon.open(member) as reader, embedded.open('wb') as output:
                            shutil.copyfileobj(reader, output, length=1024 * 1024)
                        extracted = temp / ('unpacked-' + str(index))
                        extracted.mkdir()
                        _safe_extract(embedded, extracted, max_uncompressed_bytes=4 * 1024**3)
                        roots = [extracted] if (extracted / 'manifest.json').is_file() else [
                            row for row in extracted.iterdir() if row.is_dir() and (row / 'manifest.json').is_file()]
                        if len(roots) != 1:
                            raise RuntimeError('Pack module must contain exactly one manifest.json')
                        manifest = json.loads((roots[0] / 'manifest.json').read_text('utf-8'))
                        header = manifest.get('header') or {}
                        pack_id, version = header.get('uuid'), header.get('version')
                        modules = manifest.get('modules') or []
                        if (not isinstance(pack_id, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', pack_id)
                                or not isinstance(version, list) or len(version) != 3 or
                                any(type(v) is not int or v < 0 for v in version)):
                            raise RuntimeError('Selected pack has an invalid UUID/version')
                        kinds = {m.get('type') for m in modules if isinstance(m, dict)}
                        kind = 'resource' if 'resources' in kinds else 'behavior' if kinds.intersection(('data', 'script')) else None
                        if kind is None:
                            raise RuntimeError('Pack module has no behavior/resource modules')
                        # The client downloads resource textures; behavior code
                        # remains on this server. Validate its declared entry
                        # before registering a seemingly successful Add-On.
                        if kind == 'behavior':
                            for module in modules:
                                if not isinstance(module, dict) or module.get('type') != 'script':
                                    continue
                                entry = module.get('entry')
                                if (not isinstance(entry, str) or not entry.endswith(('.js', '.mjs'))
                                        or not re.fullmatch(r'[A-Za-z0-9_./-]+', entry)
                                        or any(part in ('', '.', '..') for part in entry.split('/'))
                                        or not (roots[0] / entry).is_file()):
                                    raise RuntimeError('Selected behavior pack is missing declared script: ' + str(entry))
                        key = 'cinterforge-selected-' + record['sha256'][:12] + '-' + str(index)
                        root = self.run_root / (kind + '_packs')
                        if root.is_symlink():
                            raise RuntimeError('Unsafe managed pack root')
                        root.mkdir(parents=True, exist_ok=True)
                        destination = root / key
                        if destination.is_symlink():
                            raise RuntimeError('Unsafe managed selected-pack directory')
                        if not destination.exists():
                            shutil.copytree(roots[0], destination, symlinks=False)
                        additions.append((kind, {'pack_id': pack_id, 'version': version}))
        if record and not any(kind == 'behavior' for kind, _ in additions):
            raise RuntimeError('Selected Add-On has no server behavior pack; resource download alone cannot provide items or commands')
        if record:
            behavior = sum(kind == 'behavior' for kind, _ in additions)
            resources = sum(kind == 'resource' for kind, _ in additions)
            self._append_log(f'CINTER_ADDON_VALIDATED name={record["name"]} '
                             f'archive_bytes={record["bytes"]} behavior_packs={behavior} resource_packs={resources}')
        # Commit the manifest registrations after the modules themselves were
        # prepared. Unknown user/addon entries and the status bridge survive.
        for kind, filename in (('behavior', 'world_behavior_packs.json'),
                               ('resource', 'world_resource_packs.json')):
            path = target / filename
            if path.is_symlink():
                raise RuntimeError('Unsafe Bedrock pack registry')
            entries = json.loads(path.read_text('utf-8')) if path.is_file() else []
            if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
                raise RuntimeError('Invalid Bedrock pack registry')
            removed_ids = {row['pack_id'] for row in old if isinstance(row, dict) and
                           row.get('kind') == kind and isinstance(row.get('pack_id'), str)}
            updated = [row for row in entries if row.get('pack_id') not in removed_ids]
            for category, entry in additions:
                if category == kind:
                    updated = [row for row in updated if row.get('pack_id') != entry['pack_id']]
                    updated.append(entry)
            temporary = path.with_name(filename + '-' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_text(json.dumps(updated, indent=2) + '\n', encoding='utf-8')
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        active = {'schema': 'cinterforge.terminal.applied-pack.v1',
                  'name': record['name'] if record else None,
                  'entries': [{'kind': kind, **entry} for kind, entry in additions]}
        temporary = previous_receipt.with_name(previous_receipt.name + '-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(active, indent=2) + '\n', encoding='utf-8')
            os.replace(temporary, previous_receipt)
        finally:
            temporary.unlink(missing_ok=True)

    def _install_cinter_utility_pack(self, target: Path) -> dict:
        """Install the existing Cinter chest command AFTER the selected Add-On.

        The legacy guest CraftMaster helper provides this exact command, but is
        not packaged inside the lightweight Terminal WebApp. This runtime owner
        must generate the function in the managed BDS copy, never in a Java save.
        Only item definitions belonging to the selected, registered behavior
        modules are included; an unrelated server pack cannot contaminate it.
        """
        pack_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
            'https://cinterforge.com/minecraft/cinter-utility-pack'))
        module_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
            'https://cinterforge.com/minecraft/cinter-utility-pack/module'))
        registry = target / 'world_behavior_packs.json'
        applied = target / 'cinterforge-selected-pack.json'
        if registry.is_symlink() or applied.is_symlink():
            raise RuntimeError('Unsafe world pack registration path')
        references = json.loads(registry.read_text('utf-8')) if registry.is_file() else []
        receipt = json.loads(applied.read_text('utf-8')) if applied.is_file() else {}
        if (not isinstance(references, list) or
                any(not isinstance(row, dict) for row in references) or
                not isinstance(receipt, dict) or not isinstance(receipt.get('entries', []), list)):
            raise RuntimeError('Invalid selected pack registration for utility commands')
        selected_ids = {row.get('pack_id') for row in receipt.get('entries', [])
                        if isinstance(row, dict) and row.get('kind') == 'behavior'
                        and isinstance(row.get('pack_id'), str)}
        active_ids = {row.get('pack_id') for row in references}
        selected_ids.intersection_update(active_ids)
        behavior_root = self.run_root / 'behavior_packs'
        if behavior_root.is_symlink():
            raise RuntimeError('Unsafe Bedrock behavior pack root')
        item_ids: set[str] = set()
        matched_ids: set[str] = set()
        if selected_ids:
            for pack_root in behavior_root.iterdir():
                if pack_root.is_symlink():
                    raise RuntimeError('Unsafe symlinked server behavior pack')
                if not pack_root.is_dir() or not (pack_root / 'manifest.json').is_file():
                    continue
                manifest_file = pack_root / 'manifest.json'
                if manifest_file.is_symlink():
                    raise RuntimeError('Unsafe symlinked pack manifest')
                manifest = json.loads(manifest_file.read_text('utf-8'))
                identity = (manifest.get('header') or {}).get('uuid')
                if identity not in selected_ids:
                    continue
                matched_ids.add(identity)
                items = pack_root / 'items'
                if not items.is_dir():
                    continue
                if items.is_symlink():
                    raise RuntimeError('Unsafe symlinked item definitions')
                for path in items.rglob('*.json'):
                    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents
                                               if parent == items or items in parent.parents):
                        raise RuntimeError('Unsafe linked item definition: ' + str(path))
                    entry = json.loads(path.read_text('utf-8'))
                    identifier = str((((entry.get('minecraft:item') or {}).get('description') or {})
                                      .get('identifier') or '')).strip()
                    if identifier.startswith('cinter:'):
                        if not re.fullmatch(r'cinter:[a-z0-9_./-]+', identifier):
                            raise RuntimeError('Invalid Cinter item identifier: ' + identifier)
                        item_ids.add(identifier)
        if matched_ids != selected_ids:
            raise RuntimeError('A selected behavior pack was not installed in the managed Bedrock server')
        if len(item_ids) + (len(item_ids) + 26) // 27 > 10000:
            raise RuntimeError('Cinter chest function exceeds Minecraft command limit')
        utility_dir = behavior_root / ('cinterforge_' + pack_id)
        if utility_dir.is_symlink():
            raise RuntimeError('Unsafe generated Cinter utility pack path')
        # No selected behavior pack: retire ONLY our own utility registration.
        # Preserve the status bridge and other user-owned pack references.
        if not selected_ids:
            updated = [row for row in references if row.get('pack_id') != pack_id]
            if updated != references:
                replacement = registry.with_name(registry.name + '-' + uuid.uuid4().hex + '.tmp')
                try:
                    replacement.write_text(json.dumps(updated, indent=2) + '\n', 'utf-8')
                    os.replace(replacement, registry)
                finally:
                    replacement.unlink(missing_ok=True)
            self._append_log('Cinter utility commands: no selected behavior pack; previous utility disabled')
            return {'items': 0, 'chests': 0, 'enabled': False}

        sorted_items = sorted(item_ids)
        # Change the pack version with the actual command contents so a different
        # per-world pack does not reuse Minecraft's cached utility definition.
        signature = hashlib.sha256('\n'.join(sorted_items).encode('utf-8')).digest()
        version = [1, int.from_bytes(signature[:2], 'big'),
                   int.from_bytes(signature[2:4], 'big')]
        manifest = {'format_version': 2,
                    'header': {'name': 'Cinter Utility Commands',
                               'description': 'World-level utilities generated by CinterForge.',
                               'uuid': pack_id, 'version': version,
                               'min_engine_version': [1, 20, 0]},
                    'modules': [{'type': 'data', 'uuid': module_id, 'version': version}]}
        lines = []
        # Preserve the original CraftMaster two-chest behavior: 27 items per
        # chest, five columns, spaced two blocks, starting three ahead.
        for index, item in enumerate(sorted_items):
            chest, slot = divmod(index, 27)
            x = (chest % 5) * 2
            z = 3 + (chest // 5) * 2
            if slot == 0:
                lines.append(f'setblock ^{x} ^ ^{z} chest replace')
            lines.append(f'replaceitem block ^{x} ^ ^{z} slot.container {slot} {item} 1')
        if not lines:
            lines.append('say CinterForge: the selected pack contains no cinter items')
        behavior_root.mkdir(parents=True, exist_ok=True)
        staging = behavior_root / ('cinterforge-utility-stage-' + uuid.uuid4().hex)
        backup = behavior_root / ('cinterforge-utility-backup-' + uuid.uuid4().hex)
        try:
            (staging / 'functions' / 'cinter').mkdir(parents=True)
            (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', 'utf-8')
            (staging / 'functions' / 'cinter' / 'makechests.mcfunction').write_text(
                '\n'.join(lines) + '\n', 'utf-8')
            if utility_dir.exists():
                utility_dir.rename(backup)
            try:
                staging.rename(utility_dir)
            except BaseException:
                if backup.exists():
                    backup.rename(utility_dir)
                raise
            updated = [row for row in references if row.get('pack_id') != pack_id]
            updated.append({'pack_id': pack_id, 'version': version})
            replacement = registry.with_name(registry.name + '-' + uuid.uuid4().hex + '.tmp')
            try:
                replacement.write_text(json.dumps(updated, indent=2) + '\n', 'utf-8')
                os.replace(replacement, registry)
            finally:
                replacement.unlink(missing_ok=True)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if backup.exists():
                shutil.rmtree(backup)
        chests = (len(sorted_items) + 26) // 27
        self._append_log(f'CINTER_UTILITY_READY items={len(sorted_items)} chests={chests} '
                         'command=/function cinter/makechests')
        if not item_ids:
            self._append_log('Cinter utility commands: selected Add-On provides no cinter:* items')
        return {'items': len(sorted_items), 'chests': chests,
                'enabled': True, 'command': '/function cinter/makechests'}

    # --- Bedrock world selection and managed server copy ---------------------
    def run_world(self, name: str, *, launch: bool = True, selected_world: dict | None = None) -> dict:
        with self._lock:
            # `bedrock run ""` explicitly reuses the previously chosen save;
            # never guess a world when no selection has been made.
            if not name.strip():
                selected = self._world_settings().get('selected') or {}
                name = str(selected.get('folder') or '')
                if not name:
                    raise ValueError('No Bedrock save selected; type bedrock start to list saves')
            matches = [world for world in self.list_worlds()['worlds']
                       if name.strip().casefold() in (world['name'].casefold(), world['folder'].casefold(),
                                                      _world_command_name(world).casefold())]
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
            self._install_world_status_bridge(target)
            self._install_selected_world_pack(target, selected_world or world)
            # Must follow Add-On installation: discover its actual cinter:* item
            # definitions, then register the generated function for this world.
            self._install_cinter_utility_pack(target)
            config = self.run_root / 'server.properties'
            original = config.read_text('utf-8') if config.is_file() else ''
            lines = [line for line in original.splitlines()
                     if not line.strip().startswith(('level-name=', 'content-log-console-output-enabled='))]
            lines.extend(('level-name=' + managed_name, 'content-log-console-output-enabled=true'))
            temp = config.with_name('server.properties.tmp-' + uuid.uuid4().hex)
            try:
                temp.write_text('\n'.join(lines) + '\n', 'utf-8')
                os.replace(temp, config)
            finally:
                temp.unlink(missing_ok=True)
            chosen = selected_world or world
            self._save_world_settings({'root': self._world_settings().get('root'),
                                       'selected': {key: chosen[key] for key in
                                                    ('id','path','edition','folder','name','command_name')}})
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
        self.access.apply_before_launch()
        # Server-wide policy, not per-world state: every Bedrock launch uses
        # cheats, including existing imported saves and CraftMaster updates.
        properties = self.run_root / 'server.properties'
        if properties.is_symlink():
            raise RuntimeError('Refusing linked Bedrock server.properties')
        if not properties.is_file():
            raise RuntimeError('Bedrock server.properties is missing')
        original = properties.read_text('utf-8')
        lines = []
        seen = False
        changed = False
        for line in original.splitlines():
            key = line.split('=', 1)[0].strip().casefold() if '=' in line and not line.lstrip().startswith('#') else ''
            if key == 'allow-cheats':
                if seen:
                    changed = True
                    continue
                seen = True
                if line.strip() != 'allow-cheats=true':
                    changed = True
                lines.append('allow-cheats=true')
            else:
                lines.append(line)
        if not seen:
            lines.append('allow-cheats=true')
            changed = True
        if changed:
            temporary = properties.with_name('server.properties.tmp-' + uuid.uuid4().hex)
            try:
                temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
                os.replace(temporary, properties)
            finally:
                temporary.unlink(missing_ok=True)

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

    # --- Bedrock server process identity and console -------------------------
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
        if process is not None and _owned_bedrock_exit_code(process) is None:
            if (self._terminal_session is not None and
                    self._terminal_session.process is process and
                    not self._terminal_session.closed):
                return process.pid, True, True
            if self.platform != 'mac':
                return process.pid, True, getattr(process, 'stdin', None) is not None
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
        if own and self._process is not None and getattr(self._process, 'stdin', None) is not None:
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
        elif owned and self._process is not None and _owned_bedrock_exit_code(self._process) is not None:
            errors = [line for line in output.decode('utf-8', errors='replace').splitlines()
                      if ('CINTER_VM_ERROR' in line or 'CINTER_BDS_ERROR' in line)]
            self._startup_error = ' | '.join(errors[-4:]) or f'VM exited; see {path}'

    def status(self) -> dict:
        with self._lock:
            pid, owned, attachable = self._live_session_locked()
            self._refresh_readiness_locked(pid=pid, owned=owned)
            if pid is None and self._process is not None and _owned_bedrock_exit_code(self._process) is not None and not self._startup_error:
                self._startup_error = f'Bedrock launcher exited (code {_owned_bedrock_exit_code(self._process)}); see {self._log_path()}'
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

    # --- Official EULA-gated Mojang server installation ----------------------
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

    # --- Server launch and shutdown ------------------------------------------
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
                self._log_stream.close()
                self._log_stream = None
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
                if command.casefold().startswith('say ') and command[4:].strip():
                    self._access_event('Server:' + command[4:].strip()[:240], kind='minecraft-say')
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
        # The native conversion is also owned by this Terminal instance.
        # App shutdown must not leave a Java-world converter writing in steamapps.
        running_conversion = self._craftmaster_process
        if running_conversion is not None and running_conversion.poll() is None:
            running_conversion.terminate()
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
                            _wait_owned_bedrock(self._process, 12)
                        except subprocess.TimeoutExpired:
                            _terminate_owned_bedrock(self._process, force=False)
                            try:
                                _wait_owned_bedrock(self._process, 4)
                            except subprocess.TimeoutExpired:
                                _terminate_owned_bedrock(self._process, force=True)
                                _wait_owned_bedrock(self._process, 4)
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

