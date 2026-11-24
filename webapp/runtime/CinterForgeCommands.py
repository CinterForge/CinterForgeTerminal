"""CinterForgeCommands — CinterForge Terminal application runtime.

PURPOSE: User terminal commands, game catalog, Bedrock console, help and CLI entry.
METHODS: _cli_if_catalog, _cli_mac_catalog, _game_catalog, _launch_wargames, _mac_launch, _if_root, _if_path, _if_command, _api, _download_lagomorph_manual, _open_lagomorph_manual, _monitor_bedrock_install, minecraft_console, execute, terminal_cli_main
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



# Only the canonical commands are shown to every user. Optional commands
# are added according to the running Host's DevTools and external grants.
HELP = '''Commands:
  games                         List of playable games
  play <title>                  Launch a game (play is optional)'''

GAMES_HELP = '''Games:
  joshua                        WarGames (JOSHUA) simulator'''

DEBUGGER_HELP = '''debugger [open|status|help]
  debugger             Request the live JavaScript WebView inspector
  debugger open        Same as debugger
  debugger status      Show whether developer inspection is enabled
  debugger help        Show this help
'''

CODE_HELP = '''code [open|status|help]
  code                      Open the CinterForge Code editor
  code open                 Same as code
  code status               Show whether the code editor is enabled
  code help                 Show this help
  code open <workspace>     Open the specified workspace in the CinterForge Code editor
'''

BEDROCK_HELP = '''bedrock [command]
  bedrock report                Open the Java and Bedrock worlds report
  bedrock packs                 Open Minecraft behavior pack gallery
  bedrock <name>                Fetch latest beta Java world if published; start it
  bedrock world <name>          Select the Current World
  bedrock pack <name>           Select a pack for the Current World
  bedrock pack none             Clear the Current World's pack
  bedrock start                 Start the Current World and its selected pack
  bedrock run                   Start the Current World, including an older conversion
  bedrock stop                  Stop the running Bedrock server
  bedrock op [name|remove name]  List or manage persistent operators
  bedrock allowlist [name|remove name]  List or manage allowed players
  bedrock properties            Show all server and world property values
  bedrock properties list       Include descriptions, defaults and allowed values
  bedrock <property> "<value>"   Set a persistent property for subsequent launches
  bedrock help                  Show these commands
  Each world remembers its own current pack.
  Operators and allowlist entries persist across worlds.'''

DEVTOOLS_HELP = '''devtools [install|update|uninstall|version|list|report|help]
    devtools                          Ask to start installed DevTools or install them
    devtools install <path/to/source>  Install a local Developer Tools source tree
    devtools update                    Update the installed Developer Tools to the latest staged version
    devtools uninstall                 Remove the installed Developer Tools
    devtools version                   Show the installed Developer Tools version
    devtools list                      List managed media utilities for all three platforms
    devtools report                    Show the installed Developer Tools version and staged version
    devtools help                      Show this help

The Terminal enables native WebView inspection itself. Windows opens WebView2
DevTools and Linux opens WebKitGTK Inspector. On macOS, Terminal requests the
WKWebView Inspector and enables inspection through Safari > Develop as the
supported fallback if direct opening is unavailable.
'''

ASSETS_HELP = '''assets [report|import|view|blockify|setup|publish]
  assets report                    Open/refresh the local Assets gallery tab
  assets import [absolute path]   Import a GLB/GLTF or canonical UCI folder/ZIP
  assets view <uci>               Open/refresh the individual UCI Asset View
  assets delete <uci>             Delete the canonical asset and all versions
  assets blockify <CSV path>      Run original CSV/Blockify/ContentPack authoring in a tab
  assets setup <channel>          Download verified individual Assets files without overwriting edits
  assets publish <channel>        Upload individual Assets web files and commit their index
  channels: internal | beta | release

Native file picker: assets import with no path offers the OS file selection dialog
in Terminal. Blockify runs in the original Babylon browser and Server exporter.
'''

def _external_capabilities() -> dict[str, bool]:
    """Get only launch-validated grants; never inspect credentials in CLI mode."""
    try:
        status = _api('/api/status')
        return status.get('capabilities', {}) if isinstance(status.get('capabilities'), dict) else {}
    except (RuntimeError, ValueError, OSError):
        return {}  # Fail closed if the live owner is missing.


def _cfdev_config_present() -> bool:
    """Check only for the existing external config file, never its credentials."""
    from runtime.assets import CinterForgeAssetCloudflare
    try:
        path = CinterForgeAssetCloudflare._config_path()
        return not path.is_symlink() and path.is_file()
    except (OSError, ValueError):
        return False


def _visible_help(capabilities: dict[str, bool], *, devtools_installed: bool,
                   cfdev_config: bool) -> str:
    """The single conditional main-help ordering, independent of game details."""
    lines = [HELP, '', '  devtools                      Manage Developer Tools. See devtools help']
    if cfdev_config:
        if capabilities.get('assets'):
            lines.append('  assets                        Manage the Assets library. See assets help')
        if capabilities.get('discord'):
            lines.append('  discord                       Open a Discord Session. See discord help')
    if devtools_installed:
        lines.extend(('  code                          Open CinterForge Code. See code help',
                      '  debugger                      Debug the current WebView. See debugger help'))
    lines.extend(('  Other commands                Run in the real OS shell (zsh, bash, or PowerShell).', ''))
    return '\n'.join(lines)


def _assets_root_for_session() -> Path:
    """One mutable root selected by the already-running Terminal owner."""
    root = os.environ.get('CINTER_TERMINAL_STATE_ROOT', '')
    if not root or not Path(root).is_absolute() or Path(root).is_symlink():
        raise RuntimeError('assets requires an active trusted Terminal session')
    from runtime.assets import CinterForgeAssetViews
    return CinterForgeAssetViews.root_for_state(Path(root))


DISCORD_HELP = '''discord <user> <server> <channel>
  discord "<user>" "<server>" "<channel>"   Open a bot-connected Discord Session tab
  discord help                            Show this help

Quote names containing spaces; commas between quoted arguments are optional.
The first argument is a user/context label, not an account to impersonate.
The configured Discord bot must belong to the named server and have access to
the named text channel. Close the Discord tab to disconnect its Session.
'''

SETPASS_HELP = '''setpass <username> <password>
  setpass <username> <password>    Enable or change this host's LAN login
  setpass help                     Show this help

No default username or password exists. The command invalidates existing
remote LAN logins. It runs in a real OS shell: entered credentials may remain
visible in the terminal transcript; avoid exposing them or sharing the log.
The Terminal HTTP interface is not encrypted. Use a trusted LAN or secure tunnel.
'''


def _cli_if_catalog():
    from runtime import CinterForgeSettings as _CinterForgeSettings
    from pathlib import Path
    manifest = _CinterForgeSettings.ASSETS / 'data/if_titles.json'
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('schema') != 'cinterforge.terminal.external-if-titles.v1' or len(data.get('titles', [])) != 16:
        raise RuntimeError('Invalid Terminal external story catalog')
    result = {}
    for entry in data['titles']:
        filename = entry['file']
        name, dot, ext = filename.rpartition('.')
        if not dot or ext.lower() not in ('z3', 'z5', 'z8', 'zblorb') or not __import__('re').fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]*', name):
            raise RuntimeError('Invalid story filename in Terminal catalog')
        parsed = urlparse(entry['url'])
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or not parsed.path.endswith('/' + filename):
            raise RuntimeError('Invalid original story URL for ' + name)
        command = entry.get('command', name)
        if not isinstance(command, str) or not re.fullmatch(r'[a-z0-9]+', command):
            raise RuntimeError('Invalid short IF command for ' + filename)
        if command in result:
            raise RuntimeError('Duplicate short IF command: ' + command)
        result[command] = entry
    return result


def _cli_mac_catalog():
    """Read the staged, curated Infinite Mac catalog (not a checkout or network call)."""
    from runtime import CinterForgeSettings as _CinterForgeSettings
    from pathlib import Path
    data = json.loads((_CinterForgeSettings.ASSETS / 'data/mac_games.json').read_text(encoding='utf-8'))
    if data.get('schema') != 'cinterforge.terminal.macgames.v1' or len(data.get('games', [])) != 61:
        raise RuntimeError('Incomplete Macintosh games catalog')
    result = {}
    for game in data['games']:
        command = game['command']
        if not __import__('re').fullmatch(r'[a-z0-9]+',command) or command in result:
            raise RuntimeError('Invalid Macintosh game command')
        if not game['library_path'].startswith('Library/Games/') or '..' in game['library_path'].split('/'):
            raise RuntimeError('Invalid Macintosh game source')
        result[command] = game
    return result


def _game_catalog():
    """Original JOSHUA plus the 61 curated Classic Mac game commands."""
    return {'wargames': {'title':'WarGames (JOSHUA)', 'kind':'joshua'},
            **_cli_mac_catalog()}


def _launch_wargames():
    # Reuse the working standalone JOSHUA Session; never alter its application.
    response = _api('/api/wargames/open', body={})
    if not response.get('requested'):
        raise RuntimeError('WarGames Session launch was not accepted')
    print('Opening WarGames (JOSHUA) in a new Terminal Session.', flush=True)
    return 0


def _mac_launch(command, entry):
    # The host reuses the two canonical 68K emulators, waits for Terminal OCR
    # and types the guest's short game command using supported keyboard events.
    if not entry.get('guest_command'):
        raise RuntimeError(f"{entry['title']} has no verified matching game on Infinite HD")
    print(f"Launching {entry['title']}. This may take a second...", flush=True)
    data = _api('/api/macgames/open', body={'command':command})
    if not data.get('requested'):
        raise RuntimeError('Macintosh emulator launch was not accepted')
    return 0


def _if_root():
    from pathlib import Path
    import platform
    configured = os.environ.get('CINTER_STEAMAPPS') or os.environ.get('CFOS_STEAMAPPS')
    if configured:
        steam = Path(configured).expanduser()
    elif platform.system() == 'Darwin':
        steam = Path.home() / 'Library/Application Support/Steam/steamapps'
    elif platform.system() == 'Linux':
        steam = Path.home() / '.local/share/Steam/steamapps'
    else:
        steam = Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Steam/steamapps'
    if steam.name.lower() != 'steamapps' or not steam.is_dir():
        raise RuntimeError('Configure the actual installed Steam library with CINTER_STEAMAPPS: ' + str(steam))
    # Resolve real libraries (including intentionally linked default Steam locations).
    return steam.resolve() / 'common/CinterForge/InteractiveFiction/Titles'


def _if_path(name, entry):
    # The short public command must never rename installed story files or saves.
    return _if_root() / Path(entry['file']).stem / entry['file']


def _if_command(name, operation, entry):
    import hashlib
    from pathlib import Path
    import shutil
    import subprocess
    import tempfile
    path = _if_path(name, entry)
    if operation == 'install':
        if path.is_symlink() or path.parent.is_symlink():
            raise RuntimeError('Refusing linked story installation path')
        if path.is_file():
            print(f'{name} already installed: {path}')
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            raise RuntimeError('Refusing linked story installation path')
        print(f'{name}: downloading directly from {entry["url"]}', flush=True)
        temp = None
        try:
            from urllib.request import Request, urlopen
            from urllib.parse import urlparse as parse
            with urlopen(Request(entry['url'], headers={'User-Agent':'CinterForgeTerminal/1.0'}), timeout=45) as response:
                if getattr(response, 'status', 200) != 200 or parse(response.geturl()).scheme != 'https':
                    raise RuntimeError('Unexpected download response or insecure redirect')
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix='download-', delete=False) as dst:
                    temp = Path(dst.name)
                    digest = hashlib.sha256()
                    length = 0
                    while True:
                        chunk = response.read(65536)
                        if not chunk: break
                        length += len(chunk)
                        if length > 32 * 1024 * 1024:
                            raise RuntimeError('Story exceeds 32 MiB limit')
                        dst.write(chunk)
                        digest.update(chunk)
                        print(f'\r{name}: {length} bytes received', end='', flush=True)
                    dst.flush()
                    os.fsync(dst.fileno())
            print()
            if length < 64:
                raise RuntimeError('Downloaded story is too short')
            with temp.open('rb') as stream:
                head = stream.read(16)
            # Z-code must begin with its VM version. Blorb is an IFF FORM/IFRS container.
            valid = head[:1] in (b'\x03', b'\x05', b'\x08') if path.suffix != '.zblorb' else (head[:4] == b'FORM' and head[8:12] == b'IFRS')
            if not valid:
                raise RuntimeError('Downloaded bytes are not a valid Z-machine story format')
            os.replace(temp, path)
            temp = None
            print(f'{name}: installed at {path} (SHA-256 {digest.hexdigest()})')
            return 0
        finally:
            if temp is not None: temp.unlink(missing_ok=True)
    if operation == 'play':
        if not path.is_file():
            result = _if_command(name, 'install', entry)
            if result:
                return result
        # HTTP IF sessions and their autosaves retain the original story identity.
        body = {'command': Path(entry['file']).stem}
        source = os.environ.get('CINTER_TERMINAL_SESSION_ID', '')
        if re.fullmatch(r'[0-9a-f]{32}', source):
            body['session_id'] = source
        result = _api('/api/if/start', body=body)
        print(f'{name}: opening interactive fiction tab; close the tab to stop')
        return 0 if result.get('requested') else 1
    raise ValueError('Unknown internal story operation')


def _api(path: str, *, body: dict | None = None) -> dict:
    origin = os.environ.get('CINTER_TERMINAL_URL', '')
    token = os.environ.get('CINTER_TERMINAL_TOKEN', '')
    parsed = urlparse(origin)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or parsed.path not in ('', '/') or not token:
        raise RuntimeError('Not attached to a trusted CinterForgeTerminal session')
    headers = {'X-CPH-Token': token, 'Content-Type': 'application/json'}
    cli_token = os.environ.get('CINTER_TERMINAL_CLI_TOKEN', '')
    if cli_token:
        headers['X-Cinter-CLI-Token'] = cli_token
    request = Request(origin.rstrip('/') + path,
                      data=json.dumps(body).encode('utf-8') if body is not None else None,
                      headers=headers, method='POST' if body is not None else 'GET')
    try:
        with urlopen(request, timeout=1800 if path == '/api/bedrock/run' or (path == '/api/bedrock/world/select' and body and body.get('published') is True) else 120 if path in ('/api/bedrock/install', '/api/craftmaster/map/run') else 90 if path in ('/api/bedrock/stop', '/api/bedrock/restart') else 10) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        try:
            message = json.loads(error.read().decode('utf-8')).get('error', error.reason)
        except (ValueError, UnicodeError):
            message = error.reason
        raise RuntimeError(str(message)) from error
    except URLError as error:
        raise RuntimeError(f'CinterForgeTerminal server unavailable: {error.reason}') from error


def _download_lagomorph_manual() -> str:
    """Reuse a verified host Downloads PDF, fetching the staged copy if absent.

    The embedded shell HOME points into Builds. Only the native host account's
    explicit CINTER_USER_HOME is suitable for a user's Downloads folder.
    """
    from pathlib import Path
    home = os.environ.get('CINTER_USER_HOME', '')
    if not home or not Path(home).is_dir():
        raise RuntimeError('Cannot locate the actual host Downloads directory')
    downloads = Path(home) / 'Downloads'
    if downloads.is_symlink() or not downloads.is_dir():
        raise RuntimeError('Host Downloads folder is not available: ' + str(downloads))
    dest = downloads / 'Lagomorph Manual.pdf'
    if dest.is_file() and not dest.is_symlink():
        with dest.open('rb') as existing:
            if existing.read(4) == b'%PDF' and 4 <= dest.stat().st_size <= 40*1024*1024:
                return str(dest)
    # Never overwrite user files, symlinks, or an invalid existing manual.
    for index in range(1, 10000):
        candidate = downloads / ('Lagomorph Manual.pdf' if index == 1 else
                                 f'Lagomorph Manual ({index}).pdf')
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_file() and not candidate.is_symlink():
                with candidate.open('rb') as existing:
                    if existing.read(4) == b'%PDF' and 4 <= candidate.stat().st_size <= 40*1024*1024:
                        return str(candidate)
            continue
        dest = candidate
        break
    else:
        raise RuntimeError('Host Downloads folder has too many Lagomorph manuals')
    origin = os.environ.get('CINTER_TERMINAL_URL', '')
    token = os.environ.get('CINTER_TERMINAL_TOKEN', '')
    parsed = urlparse(origin)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or not token:
        raise RuntimeError('Lagomorph manual requires the trusted Terminal session')
    headers = {'X-CPH-Token': token}
    cli_token = os.environ.get('CINTER_TERMINAL_CLI_TOKEN', '')
    if cli_token:
        headers['X-Cinter-CLI-Token'] = cli_token
    request = Request(origin.rstrip('/') + '/api/lagomorph/manual', headers=headers)
    with urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError('Staged Lagomorph manual unavailable')
        data = response.read(40*1024*1024 + 1)
    if not data.startswith(b'%PDF') or len(data) > 40*1024*1024:
        raise RuntimeError('Invalid bundled Lagomorph manual PDF')
    # Exclusive creation protects existing user files. Incomplete writes are
    # removed; the game launch never depends on a manual download succeeding.
    try:
        with dest.open('xb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if dest.exists() and not dest.is_symlink():
            try:
                if dest.read_bytes() == data:
                    dest.unlink()
            except OSError:
                pass
        raise
    return str(dest)


def _open_lagomorph_manual(path: str) -> None:
    """Ask the host desktop to open the PDF; don't wait for the viewer."""
    import subprocess
    if sys.platform == 'win32':
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(['xdg-open', path], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _monitor_bedrock_install() -> int:
    """Report the real EULA-gated installation, not an internal status object.

    The Python HTTP service owns the download worker. Keep this command on
    the user's PTY until install succeeds or fails; Ctrl-C only stops watching,
    never kills the installer or discards its state.
    """
    cursor = 0
    last_activity = time.monotonic()
    try:
        while True:
            state = _api('/api/bedrock/status')
            events = _api('/api/bedrock/events?cursor=' + str(cursor))
            for event in events.get('events', []):
                if int(event['id']) > cursor:
                    print('[bedrock] ' + str(event['message']), flush=True)
                    cursor = int(event['id'])
                    last_activity = time.monotonic()
            if events.get('log_error'):
                print('[bedrock] Host logging failure: ' + str(events['log_error']), file=sys.stderr)
                return 1
            if time.monotonic() - last_activity >= 8:
                print('[bedrock] Waiting for response. Host log: ' + str(events.get('path')), flush=True)
                last_activity = time.monotonic()
            if state.get('install_error'):
                print('[bedrock] Install failed: ' + str(state['install_error']), file=sys.stderr)
                print('[bedrock] Type bedrock log for download diagnostics.', file=sys.stderr)
                return 1
            if state.get('installed'):
                print('[bedrock] Installed. Type bedrock start to launch the server.', flush=True)
                return 0
            if not state.get('installing'):
                print('[bedrock] Installation is not running; type bedrock install to retry.', file=sys.stderr)
                return 1
            time.sleep(0.8)
    except KeyboardInterrupt:
        print('\n[bedrock] Monitoring stopped; download continues in Terminal. Type bedrock status to check.', flush=True)
        return 130



def _packaged_craftmaster() -> Path:
    """Verify the single native CraftMaster packaged beside this staged webapp.

    The selected developer Builds/Toolchains tree is not an application binary
    search path.  The staged app owns exactly one platform-matched package.
    """
    from runtime import CinterForgeSettings as settings

    system = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}[platform.system()]
    filename = 'CinterForgeCraftMaster.exe' if system == 'win' else 'CinterForgeCraftMaster'
    assets = settings.ASSETS.resolve(strict=True)
    # .../Builds/Python/Host/<platform>/<architecture>/<version>/webapp
    if (assets.name != 'webapp' or assets.parents[2].name != system
            or assets.parents[3].name != 'Host'
            or assets.parents[4].name != 'Python'
            or assets.parents[5].name != 'Builds'):
        raise RuntimeError('Invalid staged CraftMaster application layout: ' + str(assets))
    package = assets.parents[5] / 'CinterForgeCraftMaster' / system
    manifest_path = package / 'manifest.json'
    binary = package / 'bin' / filename
    if (package.is_symlink() or manifest_path.is_symlink() or binary.is_symlink()
            or not manifest_path.is_file() or not binary.is_file()):
        raise RuntimeError('Missing packaged CraftMaster at ' + str(package))
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    digest = manifest.get('files', {}).get('bin/' + filename)
    if (manifest.get('schema') != 'cinterforge.craftmaster.stage.v1'
            or manifest.get('platform') != system
            or not isinstance(digest, str)
            or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise RuntimeError('Invalid packaged CraftMaster receipt at ' + str(manifest_path))
    actual = hashlib.sha256()
    with binary.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            actual.update(chunk)
    if actual.hexdigest() != digest:
        raise RuntimeError('Packaged CraftMaster binary SHA-256 mismatch: ' + str(binary))
    return binary


def _bedrock_request_with_progress(world_name: str, path: str, body: dict) -> dict:
    """Stream conversion/download status while the host creates the Bedrock tab.

    The HTTP run call may take minutes for a large Java world. Keep the calling
    terminal responsive with real progress and a 15-second heartbeat; the
    separate Zoomify browser tab opens as soon as its map request is queued.
    """
    completion = threading.Event()
    result = {}
    failures = []
    try:
        initial_cursor = _api('/api/bedrock/events?cursor=0').get('cursor', 0)
    except RuntimeError:
        initial_cursor = 0

    def request_server() -> None:
        try:
            result.update(_api(path, body=body))
        except Exception as error:
            failures.append(error)
        finally:
            completion.set()

    worker = threading.Thread(
        target=request_server, name='bedrock-visible-run', daemon=True,
    )
    worker.start()
    cursor = initial_cursor
    last_output = time.monotonic()
    started = last_output
    while not completion.wait(1.5):
        try:
            events = _api('/api/bedrock/events?cursor=' + str(cursor))
            cursor = events.get('cursor', cursor)
            for event in events.get('events', []):
                message = str(event.get('message', ''))
                # Per-chunk diagnostics remain in the Server log. Display the
                # meaningful dimensions, regions, tiles, download and phases.
                if any(word in message.lower() for word in (
                    'convert dimension', 'stream ', 'map ', 'download',
                    'install', 'reuse', 'reus', 'ready', 'packag', 'converting',
                    'craftmaster conversion', 'failed', 'beta', 'preserv', 'verifying',
                )):
                    print('[bedrock] ' + message, flush=True)
                    last_output = time.monotonic()
        except (RuntimeError, OSError, TimeoutError):
            pass  # A transient polling issue must not abort the actual run.
        if time.monotonic() - last_output >= 15:
            elapsed = int(time.monotonic() - started)
            print(f'[bedrock] WORKING {world_name}: elapsed={elapsed}s; see live Map / Server tabs', flush=True)
            last_output = time.monotonic()
    worker.join()
    if failures:
        raise failures[0]
    return result


def _open_bedrock_with_progress(world_name: str) -> dict:
    return _bedrock_request_with_progress(world_name, '/api/bedrock/run', {})


def _games_download() -> int:
    """Undocumented export of current in-memory game URL verification state."""
    from runtime import CinterForgeSessions as sessions
    response = _api('/api/games/verification')
    if not isinstance(response.get('verification'), dict):
        raise RuntimeError('Game verification state unavailable')
    catalog = _game_catalog()
    games = []
    for command, entry in catalog.items():
        state = response['verification'].get(command, {})
        games.append({'command': command, 'title': entry['title'],
                      'kind': entry.get('kind', 'classic-macintosh'),
                      'library_path': entry.get('library_path'),
                      'verification': state})
    downloads = sessions._real_login_home() / 'Downloads'
    if downloads.is_symlink() or not downloads.is_dir():
        raise RuntimeError('Host Downloads directory unavailable: ' + str(downloads))
    target = downloads / 'Games.json'
    if target.is_symlink():
        raise RuntimeError('Unsafe Games.json destination')
    payload = {'schema': 'cinterforge.games.verification.v1',
               'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'games': games}
    import tempfile
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=downloads,
                                     prefix='games-export-', delete=False) as handle:
        temp = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    temp.replace(target)
    print('Exported game verification state: ' + str(target), flush=True)
    return 0


def execute(args: list[str]) -> int:
    from runtime import CinterForgeDevTools as _CinterForgeDevTools
    capabilities = _external_capabilities()
    if args and args[0] in ('assets', 'discord') and not capabilities.get(args[0]):
        print('Unknown custom command. Type help.', file=sys.stderr)
        return 2
    if not args or args[0] in ('help', '--help', '-h'):
        option = args[1:] if args else []
        if not option:
            try:
                devtools_installed = bool(_api('/api/status').get('devtools_installed'))
            except (RuntimeError, ValueError, OSError):
                devtools_installed = False
            print(_visible_help(capabilities, devtools_installed=devtools_installed,
                                cfdev_config=_cfdev_config_present()))
        elif len(option) == 1 and option[0] in ('infinitemac', 'infinite-mac'):
            print('infinitemac — Open a searchable gallery of Macintosh, NeXTSTEP and BeOS emulators.')
            print('Select a thumbnail to open that machine in a new Session tab.')
        elif len(option) == 1 and (option[0] in ('bedrock', 'devtools', 'setpass') or option[0] in ('debugger', 'code') and bool(_api('/api/status').get('devtools_installed'))
                                   or option[0] == 'assets'
                                   or option[0] == 'discord' and capabilities.get('discord')):
            print({'debugger': DEBUGGER_HELP, 'bedrock': BEDROCK_HELP,
                   'devtools': DEVTOOLS_HELP, 'code': CODE_HELP,
                   'assets': ASSETS_HELP,
                   'discord': DISCORD_HELP, 'setpass': SETPASS_HELP}[option[0]])
        else:
            permitted = [name for name in ('assets', 'discord') if capabilities.get(name)]
            print('Usage: help [debugger|bedrock|devtools|code|setpass' +
                  ''.join('|' + name for name in permitted) + ']', file=sys.stderr)
            return 2
        return 0
    if args[0] == 'devtools':
        operation = args[1] if len(args) >= 2 else 'start'
        if operation == 'start' and len(args) == 1:
            status = _api('/api/status')
            if status.get('devtools_installed'):
                if input('Start installed DevTools guests now? [y/N] ').strip().casefold() != 'y':
                    print('DevTools not started.')
                    return 0
            else:
                if input('DevTools are not installed. Install now? [y/N] ').strip().casefold() != 'y':
                    print('DevTools not installed.')
                    return 0
                source = input('Absolute Source directory for DevTools: ').strip()
                if not source or not Path(source).is_absolute():
                    print('DevTools requires an existing absolute Source directory.', file=sys.stderr)
                    return 2
                result = _CinterForgeDevTools.install_devtools(source)
                if result != 0:
                    return result
            response = _api('/api/lifecycle/guest-boot', body={})
            if response.get('state') in ('failed', 'not-installed'):
                raise RuntimeError(response.get('error') or 'DevTools failed to start')
            print('DevTools guest startup requested; progress is in Server Log.', flush=True)
            return 0
        if operation == 'install' and len(args) == 3:
            return _CinterForgeDevTools.install_devtools(args[2])
        if operation == 'update' and len(args) == 2:
            return _CinterForgeDevTools.install_devtools(update=True)
        if operation == 'uninstall' and len(args) == 2:
            return _CinterForgeDevTools.uninstall_devtools()
        if operation == 'version' and len(args) == 2:
            return _CinterForgeDevTools.version_devtools()
        if operation == 'report' and len(args) == 2:
            return _CinterForgeDevTools.report_devtools()
        if operation == 'list' and len(args) == 2:
            return _CinterForgeDevTools.list_devtools()
        if operation == 'help' and len(args) in (1, 2):
            return _CinterForgeDevTools.devtools_help()
        print('Usage: devtools install <path/to/source> | update | uninstall | version | list | report | help', file=sys.stderr)
        return 2
    if args[0] == 'craftmaster':
        # A world name opens the native incremental conversion and its live
        # Zoomify map in a separate tab. Retain the original CLI passthrough
        # for flags and subcommands such as worlds, players and convert.
        native_commands = {
            'worlds', 'players', 'convert', 'export-name', 'self-test',
            'capabilities', '--version', '--help', '-h', 'help',
        }
        if args[1:] and args[1] not in native_commands and not args[1].startswith('-'):
            selected_world = ' '.join(args[1:]).strip()
            response = _api('/api/craftmaster/map/run', body={'name': selected_world})
            if not response.get('started'):
                raise RuntimeError('CraftMaster map conversion did not start')
            print('[CraftMaster] Opening live Zoomify map for ' + selected_world + ' in a new tab', flush=True)
            return 0
        binary = _packaged_craftmaster()
        return subprocess.call([str(binary), *args[1:]])
    if args[0] == 'setpass':
        if args[1:] == ['help']:
            print(SETPASS_HELP)
            return 0
        if len(args) != 3:
            print(SETPASS_HELP, file=sys.stderr)
            return 2
        _api('/api/lan/setpass', body={
            'username': args[1], 'password': args[2],
            'session_id': os.environ.get('CINTER_TERMINAL_SESSION_ID', '')})
        print('LAN login updated. Existing remote sessions must log in again.', flush=True)
        return 0
    if args[0] == 'reboot':
        if len(args) != 2 or args[1] not in ('server', 'docker', 'colima', 'app'):
            print('Usage: reboot server|docker|colima|app', file=sys.stderr)
            return 2
        response = _api('/api/lifecycle/reboot', body={'component': args[1]})
        if args[1] in ('server', 'app'):
            print('CinterForge Terminal ' + args[1] + ' restart accepted; native owner will relaunch it.', flush=True)
        else:
            print('CinterForge Terminal ' + args[1] + ' restarted: ' +
                  ('ready' if response.get('ready') else 'not ready'), flush=True)
        return 0
    if args[0] == 'htmlmenus':
        if len(args) != 2 or args[1] not in ('show', 'hide', 'toggle'):
            print('Usage: htmlmenus show|hide|toggle', file=sys.stderr)
            return 2
        state = _api('/api/ui/html-menus', body={
            'action': args[1],
            'session_id': os.environ.get('CINTER_TERMINAL_SESSION_ID', '')})
        print('HTML menus: ' + ('visible' if state['show'] else 'hidden'), flush=True)
        return 0
    if args[0] in ('code', 'debugger') and not _api('/api/status').get('devtools_installed'):
        print('Install DevTools to use ' + args[0], file=sys.stderr)
        return 2
    if args[0] == 'code':
        if args[1:] == ['help']:
            print(CODE_HELP)
            return 0
        if args[1:] == ['status']:
            status = _api('/api/status')
            print('CinterForge Code:', 'enabled' if status.get('devtools_installed') else 'disabled')
            return 0
        # Use the same host workspace and theme store as the Code-OSS UI.
        # Bash has already parsed quoted POSIX paths into a single argument.
        operation = {'session_id': os.environ.get('CINTER_TERMINAL_SESSION_ID', '')}
        usage = 'Usage: code [run | mode light|dark | open "<absolute path>"]'
        if len(args) == 1 or args[1:] in (['run'], ['open']):
            operation['operation'] = 'run'
        elif len(args) == 3 and args[1] == 'mode' and args[2] in ('light', 'dark'):
            operation.update(operation='mode', theme=args[2])
        elif len(args) == 3 and args[1] == 'open':
            operation.update(operation='open', path=args[2])
        else:
            print(usage, file=sys.stderr)
            return 2
        response = _api('/api/code/open', body=operation)
        if operation['operation'] == 'mode':
            print('Code-OSS theme: ' + response['theme'], flush=True)
        elif operation['operation'] == 'open':
            print('Code-OSS workspace: ' + response['path'], flush=True)
        else:
            print('Opening Code-OSS workbench.', flush=True)
        return 0
    if args[0] == 'assets':
        if len(args) == 1 or args[1:] == ['help']:
            print(ASSETS_HELP)
            return 0
        session_id = os.environ.get('CINTER_TERMINAL_SESSION_ID', '')
        if args[1:] == ['report']:
            result = _api('/api/assets/report', body={})
            print('[assets] Opened library report: ' + str(result.get('count', 0)) + ' assets', flush=True)
            return 0
        if len(args) == 3 and args[1] == 'delete':
            deleted = _api('/api/assets/delete', body={'session_id': session_id, 'uci': args[2]})
            print('[assets] Deleted asset ' + deleted['deleted'], flush=True)
            return 0
        if len(args) == 3 and args[1] == 'view':
            result = _api('/api/assets/view', body={'uci': args[2]})
            print('[assets] Opened viewer for ' + result['uci'], flush=True)
            return 0
        if len(args) == 3 and args[1] == 'blockify':
            job = _api('/api/assets/blockify/start', body={'session_id': session_id, 'path': args[2]})
            print('[assets] Blockify source CSV: ' + job['csv'], flush=True)
            print('[assets] Original WebApp Blockify started for ' + str(job['count']) + ' assets; opening its authoring tab.', flush=True)
            completed = -1
            recent = ''
            started_at = time.monotonic()
            last_heartbeat = started_at
            while True:
                time.sleep(2)
                progress = _api('/api/assets/blockify/job/' + job['job'] + '?summary=1')
                current = int(progress['completed']) + int(progress['failed'])
                latest = (progress.get('messages') or [''])[-1]
                if current != completed or latest != recent:
                    print(f"[assets] {current}/{progress['total']} · {progress['completed']} complete · {progress['failed']} failed" + (' · ' + latest if latest else ''), flush=True)
                    completed, recent = current, latest
                now = time.monotonic()
                if progress['state'] == 'queued' and now - started_at > 90:
                    print('[assets] Blockify browser did not start; no conversions were reported. Check the Asset tab and Server log.', file=sys.stderr, flush=True)
                    return 1
                if now - last_heartbeat >= 30:
                    print('[assets] Blockify ' + str(progress['state']) + ' · ' + str(current) + '/' + str(progress['total']) + ' rows processed', flush=True)
                    last_heartbeat = now
                if progress['state'] == 'error':
                    print('[assets] Blockify could not start: ' + str(progress.get('error') or latest), file=sys.stderr, flush=True)
                    return 1
                if progress['state'] == 'finished':
                    return 0 if not progress['failed'] else 1
        if args[1:] == ['import']:
            _api('/api/assets/picker', body={'session_id': session_id})
            print('[assets] Choose a GLB or canonical asset ZIP in Terminal to import.', flush=True)
            return 0
        if len(args) == 3 and args[1] == 'import':
            result = _api('/api/assets/import', body={'session_id': session_id, 'path': args[2]})
            print('[assets] ' + ('Existing' if result['existing'] else 'Saved') + ' UCI: ' + result['uci'], flush=True)
            print('[assets] Verified in: ' + result['saved_path'], flush=True)
            if result.get('conversion_job'):
                job_id = result['conversion_job']
                print('[assets] Original migration queued: ' + job_id, flush=True)
                started_at = time.monotonic()
                last_progress = None
                last_heartbeat = started_at
                while True:
                    time.sleep(2)
                    progress = _api('/api/assets/blockify/job/' + job_id + '?summary=1')
                    processed = int(progress['completed']) + int(progress['failed'])
                    message = (progress.get('messages') or [''])[-1]
                    current = (progress['state'], processed, message)
                    if current != last_progress:
                        print('[assets] Migration ' + progress['state'] + ' '
                              + str(processed) + '/' + str(progress['total'])
                              + (' · ' + message if message else ''), flush=True)
                        last_progress = current
                    if progress['state'] == 'finished':
                        print('[assets] Imported ' + result['uci'] + ': original Solid/Block migration complete', flush=True)
                        return 0
                    if progress['state'] == 'error':
                        print('[assets] Source preserved; original migration failed: '
                              + str(progress.get('error') or message), file=sys.stderr, flush=True)
                        return 1
                    if progress['state'] == 'queued' and time.monotonic() - started_at > 90:
                        print('[assets] Source preserved; migration never started in the Asset tab',
                              file=sys.stderr, flush=True)
                        return 1
                    if time.monotonic() - last_heartbeat >= 30:
                        print('[assets] Migration running; ' + str(processed) + '/' + str(progress['total'])
                              + ' processed', flush=True)
                        last_heartbeat = time.monotonic()
            return 0
        if len(args) != 3 or args[1] not in ('setup', 'publish') or args[2] not in ('internal', 'beta', 'release'):
            print(ASSETS_HELP, file=sys.stderr)
            return 2
        channel = args[2]
        allowed = 'cloudflare_internal' if channel == 'internal' else 'cloudflare_public'
        if not capabilities.get(allowed):
            print('Unknown custom command. Type help.', file=sys.stderr)
            return 2
        from runtime.assets import CinterForgeAssetCloudflare as cloudflare
        root = _assets_root_for_session()
        result = cloudflare.setup(channel, root) if args[1] == 'setup' else cloudflare.publish(channel, root)
        if result.get('conflicts'):
            print('[assets] Existing local changes were preserved; see reported conflicts.', flush=True)
        return 0
    if args[0] == 'discord':
        if args[1:] == ['help']:
            print(DISCORD_HELP)
            return 0
        # Bash strips quotes and leaves optional trailing commas from the
        # human-friendly: discord "user", "server", "channel" syntax.
        fields = [part.strip().strip(',').strip() for part in args[1:]]
        if len(fields) != 3 or not all(fields):
            print(DISCORD_HELP, file=sys.stderr)
            return 2
        result = _api('/api/discord/open', body={
            'username': fields[0], 'guild': fields[1], 'channel': fields[2],
            'session_id': os.environ.get('CINTER_TERMINAL_SESSION_ID', '')})
        print('Discord tab opening: ' + result['guild'] + ' / #' + result['channel'] +
              ' (connected as bot ' + result['bot'] + ')', flush=True)
        return 0
    command, *options = args
    if command == 'games':
        if options == ['report']:
            result = _api('/api/games/report/open', body={})
            if not result.get('requested'):
                raise RuntimeError('Games Report could not be opened')
            print('Opening Games Report: 61 Macintosh games (System 7.1 / Mac OS 7.6).', flush=True)
            return 0
        if options == ['download']:
            return _games_download()
        if options:
            print('Usage: games [report|download]', file=sys.stderr)
            return 2
        print(GAMES_HELP)
        for name, entry in _cli_if_catalog().items():
            print(f"  {name:29} {entry['title']}")
        print('  games report                  Open Macintosh Games Report (61 playable games)')
        print('  infinitemac                   Search historical Macintosh, NeXTSTEP and BeOS emulators')
        print('  infinitemac list              List installed classic Mac games and play commands')
        print('  bedrock                       Serve any Java or Bedrock world with CinterForge Behavior Packs')
        return 0
    if command in ('infinitemac', 'infinite-mac') and options == ['list']:
        print('Infinite Mac games on the installed 68K catalog:')
        for game in _cli_mac_catalog().values():
            if game.get('alias_of'): continue
            if not game.get('guest_command'):
                # Do not claim a game is launchable without a matching guest APPL.
                continue
            print(f"  {game['title']:<34} {game['guest_os']:<4} play {game['command']}")
        return 0
    if command in ('infinitemac', 'infinite-mac') and options:
        print('Usage: infinitemac [list]', file=sys.stderr)
        return 2
    if command in ('infinitemac', 'infinite-mac') and not options:
        result = _api('/api/emulators/open', body={})
        if not result.get('requested'):
            raise RuntimeError('Emulator gallery was not opened')
        print('[infinitemac] Opening searchable emulator gallery in a Session tab.', flush=True)
        return 0
    if command in ('joshua', 'wargames') and not options:
        return _launch_wargames()
    if command == 'stories':
        if options:
            print('Usage: stories', file=sys.stderr)
            return 2
        for name, entry in _cli_if_catalog().items():
            print(f"{name:30} {entry['title']}")
        return 0
    if command == 'play':
        selected = ' '.join(options).strip()
        if not selected:
            print('Usage: play <game-name> (or type the game name directly)', file=sys.stderr)
            return 2
        # "play" is an optional prefix for every entry printed by `games`.
        # Preserve Bedrock's own subcommands under either spelling, e.g.
        # `bedrock report` and `play bedrock report`.
        if len(options) > 1 and options[0].casefold() == 'bedrock':
            return execute(['bedrock', *options[1:]])
        stories = _cli_if_catalog()
        games = _game_catalog()  # JOSHUA's wargames alias and Classic Mac titles.
        commands = {'joshua': 'joshua', 'wargames': 'joshua',
                    'infinitemac': 'infinitemac', 'infinite-mac': 'infinitemac',
                    'bedrock': 'bedrock'}
        commands.update({name.casefold(): name for name in stories})
        # Preserve existing filename-stem commands as optional legacy aliases.
        commands.update({Path(entry['file']).stem.casefold(): name
                         for name, entry in stories.items()})
        commands.update({name.casefold(): name for name in games})
        commands['wargames'] = 'joshua'  # Catalog's legacy key is an alias, not a CLI handler.
        by_title = {entry['title'].casefold(): name for name, entry
                    in {**games, **stories}.items() if not entry.get('alias_of')}
        for name, entry in games.items():
            for alias in entry.get('aliases', []):
                by_title[alias.casefold()] = name
        by_title['wargames (joshua)'] = 'joshua'
        name = commands.get(selected.casefold()) or by_title.get(selected.casefold())
        if name is None:
            print('Unknown game/story: ' + selected + '. Type games to see the available commands.', file=sys.stderr)
            return 2
        # Exactly the same existing command handler runs with or without `play`.
        return execute([name])
    # Easter egg: deliberately not listed in help, menu commands, or CLI usage.
    stories = _cli_if_catalog()
    story_entry = stories.get(command)
    if story_entry is None:
        story_entry = next((entry for entry in stories.values()
                            if Path(entry['file']).stem == command), None)
    if story_entry is not None and not options:
        return _if_command(story_entry.get('command', Path(story_entry['file']).stem),
                           'play', story_entry)
    if command in _cli_mac_catalog() and not options:
        return _mac_launch(command, _cli_mac_catalog()[command])
    if command == 'lagomorph':
        if options:
            return 2
        try:
            manual = _download_lagomorph_manual()
            _open_lagomorph_manual(manual)
            print('Lagomorph manual opened: ' + manual, flush=True)
        except (OSError, RuntimeError) as error:
            # The game should still launch if the desktop PDF viewer is missing.
            print('Lagomorph manual unavailable: ' + str(error), file=sys.stderr, flush=True)
        result = _api('/api/lagomorph/open', body={})
        if result.get('requested'):
            print('Opening a Macintosh...')
            return 0
        return 1
    if command == 'debugger':
        option = options or ['open']
        if len(option) != 1 or option[0] not in ('open', 'status', 'help'):
            print(DEBUGGER_HELP, file=sys.stderr)
            return 2
        if option[0] == 'help':
            print(DEBUGGER_HELP)
        elif option[0] == 'status':
            data = _api('/api/status')
            print('JavaScript debugger:', 'enabled' if data.get('debug') else 'disabled')
        else:
            data = _api('/api/debugger', body={})
            if not data.get('requested'):
                print('Inspector request failed.', file=sys.stderr)
                return 1
            print('Native WebView inspector requested.')
            if sys.platform == 'darwin':
                print('If no Inspector appears, open Safari > Develop > CinterForge Terminal.')
        return 0
    if command == 'bedrock':
        if options == ['packs']:
            _api('/api/assets/packs/open', body={})
            print('[bedrock] Opening Minecraft content packs.', flush=True)
            return 0
        if options and options[0].casefold() in ('op', 'allowlist'):
            kind = options[0].casefold()
            remainder = options[1:]
            if not remainder or remainder[0].casefold() in ('list', 'show') and len(remainder) == 1:
                state = _api('/api/bedrock/access')
            else:
                action = 'remove' if remainder[0].casefold() in ('remove', 'delete', 'del') else 'add'
                names = remainder[1:] if action == 'remove' else (
                    remainder[1:] if remainder[0].casefold() == 'add' else remainder)
                name = ' '.join(names).strip()
                if not name:
                    print(BEDROCK_HELP, file=sys.stderr)
                    return 2
                state = _api('/api/bedrock/access', body={'kind': kind, 'action': action, 'name': name})
            if kind == 'op':
                print('Server operators (all worlds):')
                for item in state.get('operators', []):
                    print('  ' + item['name'] + (' [XUID ' + item['xuid'] + ']' if item.get('xuid') else ' [pending authenticated join]'))
                for xuid in state.get('other_operator_xuids', []):
                    print('  [existing operator XUID ' + xuid + ']')
                if not state.get('operators') and not state.get('other_operator_xuids'):
                    print('  (none)')
            else:
                print('Server allowlist (all worlds):')
                for name in state.get('allowlist', []):
                    print('  ' + name)
                if not state.get('allowlist'):
                    print('  (none)')
            if kind == 'allowlist' and not state.get('allowlist_enabled'):
                print('Allow-list enforcement is independent; set bedrock allow-list true to enable it.')
            return 0
        if not options or options in (['help'], ['--help'], ['-h']):
            print(BEDROCK_HELP)
            return 0
        action = options[0].casefold()
        if action == 'properties' and len(options) <= 2:
            detailed = len(options) == 2 and options[1].casefold() == 'list'
            if len(options) == 2 and not detailed:
                print('Usage: bedrock properties [list]', file=sys.stderr)
                return 2
            state = _api('/api/bedrock/properties')
            for scope in ('server.properties', 'level.dat'):
                print('\n'+scope+':')
                for row in state['properties']:
                    if row['scope'] != scope:
                        continue
                    print('  '+row['name']+' = '+json.dumps(row['value'], ensure_ascii=False))
                    if detailed:
                        print('    '+row['description'])
                        print('    Default: '+json.dumps(row['default'], ensure_ascii=False)+'; Allowed: '+row['allowed'])
                    if row['overridden'] and row['native_value'] != row['value']:
                        print('    Saved override; applies at next launch.')
                    if row['name']=='level-name' and row['native_value'] and row['native_value']!=row['value']:
                        print('    Native managed folder: '+row['native_value'])
            if state.get('world_error'):
                print('World values unavailable: '+state['world_error'])
            print('\nSettings are shared across worlds. Restart Bedrock to apply changes.')
            return 0
        if action == 'report' and len(options) == 1:
            _api('/api/bedrock/report/open', body={})
            print('[bedrock] Opening Java and Bedrock Worlds report.', flush=True)
            return 0
        if action == 'world' and len(options) >= 2:
            result = _api('/api/bedrock/world/select', body={'name': ' '.join(options[1:])})
            print('[bedrock] Current World: ' + str(result['current_world']), flush=True)
            return 0
        if action == 'pack' and len(options) >= 2:
            result = _api('/api/bedrock/pack/set', body={'name': ' '.join(options[1:])})
            print('[bedrock] Current World pack: ' + str(result.get('current') or 'None'), flush=True)
            return 0
        if action == 'stop' and len(options) == 1:
            result = _api('/api/bedrock/stop', body={})
            print('[bedrock] Server stopped.', flush=True)
            return 0
        # Preserve the two-argument property interface; quote a multiword world name.
        named_launch = len(options) == 1 and action not in (
            'report', 'world', 'pack', 'start', 'run', 'stop', 'help', 'packs',
            'install', 'status', 'restart', 'properties')
        if named_launch or (action in ('start', 'run') and len(options) == 1):
            if named_launch:
                requested = options[0].strip()
                selection = _bedrock_request_with_progress(requested, '/api/bedrock/world/select',
                    {'name': requested, 'published': True})
                if selection.get('publication_warning'):
                    print('[bedrock] ' + selection['publication_warning'], flush=True)
                current = selection['world']
                if selection.get('publication_matched') and not selection.get('publication_warning'):
                    print('[bedrock] Beta Java world ready: ' + current['folder'], flush=True)
                if selection.get('publication_matched') and not selection.get('publication_warning'):
                    readiness = selection.get('conversion') or {}
                    if readiness.get('needs_update'):
                        print('[bedrock] Updating the beta Java world for Bedrock.', flush=True)
                        job = _api('/api/craftmaster/map/run', body={'name': current['id']})
                        map_id = job['map']['id']
                        previous_message = ''
                        last_heartbeat = time.monotonic()
                        while True:
                            time.sleep(1.5)
                            progress = _api('/api/craftmaster/map/status?id=' + map_id)
                            message = progress.get('message', '')
                            if message and message != previous_message:
                                print('[CraftMaster] ' + message, flush=True)
                                previous_message = message
                            if progress['phase'] == 'failed':
                                raise RuntimeError('Update for Bedrock failed: ' + message)
                            if progress['phase'] == 'ready':
                                break
                            if time.monotonic() - last_heartbeat >= 15:
                                print('[CraftMaster] Conversion continues; map and Server Log show progress.', flush=True)
                                last_heartbeat = time.monotonic()
            else:
                info = _api('/api/bedrock/report')
                current = next((w for w in info['worlds'] + info['java_worlds'] if w['current']), None)
            if current is None:
                raise ValueError('No Current World selected. Use bedrock world <name> or bedrock report.')
            state = _api('/api/bedrock/status')
            if not state.get('eula_accepted'):
                _api('/api/bedrock/eula', body={})
                print('[bedrock] Review and accept the official Minecraft EULA in Terminal.', flush=True)
            elif not state.get('installed') and not state.get('installing'):
                _api('/api/bedrock/install', body={})
            last_progress = None
            started = time.monotonic()
            while not state.get('installed'):
                if time.monotonic() - started > 1800:
                    raise RuntimeError('Bedrock installation did not complete; view the persistent Bedrock log')
                time.sleep(0.75)
                state = _api('/api/bedrock/status')
                if state.get('install_error'):
                    raise RuntimeError('Bedrock installation failed: ' + state['install_error'])
                if state.get('eula_accepted') and not state.get('installing') and not state.get('installed'):
                    _api('/api/bedrock/install', body={})
                if not state.get('eula_accepted') and not state.get('eula_requested'):
                    print('[bedrock] EULA dialog dismissed; no server started.', flush=True)
                    return 1
                progress = state.get('install_progress')
                if progress and progress != last_progress:
                    print('[bedrock] ' + str(progress), flush=True)
                    last_progress = progress
            print('[bedrock] Starting ' + current['command_name'] + ' in an interactive tab…', flush=True)
            started_server = _open_bedrock_with_progress(current['command_name'])
            if not started_server.get('terminal_session'):
                raise RuntimeError('Bedrock failed to create its interactive console tab')
            print('[bedrock] Opened ' + current['command_name'] + '; close its tab or use bedrock stop.', flush=True)
            return 0
        if len(options) == 2:
            result = _api('/api/bedrock/properties', body={'name':options[0], 'value':options[1]})
            print(result['name']+' = '+json.dumps(result['value'], ensure_ascii=False)+' ('+result['applies']+')')
            return 0
        print(BEDROCK_HELP, file=sys.stderr)
        return 2
    print('Unknown custom command. Type help.', file=sys.stderr)
    return 2


def terminal_cli_main(argv: list[str] | None = None) -> int:
    try:
        return execute(list(sys.argv[1:] if argv is None else argv))
    except (RuntimeError, ValueError) as error:
        print('CinterForgeTerminal:', error, file=sys.stderr)
        return 1


"""Real persistent guest Bash for macOS Code-OSS integrated terminals.

This module is intentionally shipped with the lightweight application; all
SDK binaries and OCI images come from the separately consented DevTools ZIP.
It never falls back to a host Mac shell for a Windows or Linux request.
"""

