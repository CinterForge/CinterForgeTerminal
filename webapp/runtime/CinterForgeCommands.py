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



HELP = '''CinterForgeTerminal custom commands

  <command> help               Display this help or one command's options

  games                         List Playable Infinite-Mac macOS Games
  stories                       List Interactive Fiction Stories
  play <title>                  Launch a game or story in a Session Window
  bedrock <world>               List and load and serve a Minecraft Bedrock world
  craftmaster <world>           Convert a Minecraft Java world to Bedrock

  debugger                      Debug the current WebView (default: open). See debugger help
  devtools                      Manage Developer Tools. See devtools help
  code                          Open CinterForge Code. See code help

  discord                       Open a Discord bot-connected Session tab. See discord help
  setpass                       Set this host's LAN login credentials. See setpass help

  Other commands                Run in the real OS shell (zsh, bash, or PowerShell).
'''


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

BEDROCK_HELP = """bedrock [world name]
  bedrock                      List local Bedrock and Java worlds
  bedrock <world>              Start a world in its own interactive console tab
  bedrock op                   List persistent server operators
  bedrock op <gamertag>       Remember an operator and also allowlist them
  bedrock op remove <name>    Remove persistent operator privilege
  bedrock allowlist            List server-wide allowed players
  bedrock allowlist <name>     Allow a player onto this server
  bedrock allowlist remove <name>  Remove allowed player (if not an op)
  Operators are assigned by Xbox XUID once their authenticated player joins.
  These rules persist across world changes. Close Bedrock tab to stop server.
"""

DEVTOOLS_HELP = '''devtools [install|update|uninstall|version|report|help]
    devtools                          Ask to start installed DevTools or install them
    devtools install <path/to/source>  Install a local Developer Tools source tree
    devtools update                    Update the installed Developer Tools to the latest staged version
    devtools uninstall                 Remove the installed Developer Tools
    devtools version                   Show the installed Developer Tools version
    devtools report                    Show the installed Developer Tools version and staged version
    devtools help                      Show this help

The Terminal enables native WebView inspection itself. Windows opens WebView2
DevTools and Linux opens WebKitGTK Inspector. On macOS, Terminal requests the
WKWebView Inspector and enables inspection through Safari > Develop as the
supported fallback if direct opening is unavailable.
'''

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
        if name in result:
            raise RuntimeError('Duplicate story command: ' + name)
        result[name] = entry
    return result


def _cli_mac_catalog():
    """Read the staged, curated Infinite Mac catalog (not a checkout or network call)."""
    from runtime import CinterForgeSettings as _CinterForgeSettings
    from pathlib import Path
    data = json.loads((_CinterForgeSettings.ASSETS / 'data/mac_games.json').read_text(encoding='utf-8'))
    if data.get('schema') != 'cinterforge.terminal.macgames.v1' or len(data.get('games', [])) != 67:
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
    """The 68-game launch list: original JOSHUA plus 67 classic Mac titles."""
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
    # The emulator can boot with Infinite HD present. Its public embedding API
    # does not yet provide a verified program-by-name launch capability.
    data = _api('/api/macgames/open', body={'command':command})
    if not data.get('requested'):
        raise RuntimeError('Macintosh emulator launch was not accepted')
    print(f"Starting Macintosh environment for {entry['title']} (experimental direct launch)", flush=True)
    print('If the game does not auto-open, locate it on Infinite HD; this title needs a guest startup recipe.', flush=True)
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
    return _if_root() / name / entry['file']


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
        body = {'command': name}
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
        with urlopen(request, timeout=1800 if path == '/api/bedrock/run' else 120 if path in ('/api/bedrock/install', '/api/craftmaster/map/run') else 90 if path in ('/api/bedrock/stop', '/api/bedrock/restart') else 10) as response:
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


def _open_bedrock_with_progress(world_name: str) -> dict:
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
            result.update(_api('/api/bedrock/run', body={'name': world_name}))
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
                    'craftmaster conversion', 'failed',
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
    if not args or args[0] in ('help', '--help', '-h'):
        option = args[1:] if args else []
        if not option:
            print(HELP)
        elif len(option) == 1 and option[0] == 'infinite-mac':
            print('67 Macintosh games — https://infinitemac.org/ (experimental guest auto-open):')
            for slug, game in _cli_mac_catalog().items():
                print(f"{slug:30} {game['title']}")
        elif len(option) == 1 and option[0] in ('debugger', 'bedrock', 'devtools', 'code', 'discord', 'setpass'):
            print({'debugger': DEBUGGER_HELP, 'bedrock': BEDROCK_HELP,
                   'devtools': DEVTOOLS_HELP, 'code': CODE_HELP,
                   'discord': DISCORD_HELP, 'setpass': SETPASS_HELP}[option[0]])
        else:
            print('Usage: help [debugger|bedrock|devtools|code|discord|setpass]', file=sys.stderr)
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
        if operation == 'help' and len(args) in (1, 2):
            return _CinterForgeDevTools.devtools_help()
        print('Usage: devtools install <path/to/source> | update | uninstall | version | help | report', file=sys.stderr)
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
        if options == ['download']:
            return _games_download()
        if options:
            print('Usage: games', file=sys.stderr)
            return 2
        for index, (name, entry) in enumerate(_game_catalog().items(), start=1):
            print(f"{index:2}. {name:30} {entry['title']}")
        return 0
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
            print('Usage: play <game-or-story-name>', file=sys.stderr)
            return 2
        # Accept an exact command slug or a displayed title (case-insensitive).
        stories = _cli_if_catalog()
        games = _game_catalog()
        by_title = {str(entry['title']).casefold(): name for name, entry
                    in {**stories, **games}.items()}
        name = selected if selected in stories or selected in games else by_title.get(selected.casefold(), selected)
        if name in stories:
            return _if_command(name, 'play', stories[name])
        if name == 'wargames':
            return _launch_wargames()
        if name in games:
            return _mac_launch(name, games[name])
        print('Unknown game/story: ' + selected + '. Type games or stories.', file=sys.stderr)
        return 2
    # Easter egg: deliberately not listed in help, menu commands, or CLI usage.
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
            if state.get('allowlist') and not state.get('allowlist_enabled'):
                print('Note: allow-list will be enabled on the next server launch.')
            return 0
        if not options:
            state = _api('/api/bedrock/worlds')
            print('Available Bedrock worlds:')
            for world in state.get('worlds', []):
                print('  ' + world.get('command_name', world['name']))
            if not state.get('worlds'):
                print('  (no installed Bedrock worlds)')
            print('Available Java worlds:')
            for world in state.get('java_worlds', []):
                print('  ' + world.get('command_name', world['name']))
            if state.get('java_error'):
                print('  (Java discovery error: ' + state['java_error'] + ')')
            print('Launch with: bedrock <world name>')
            return 0
        selected = ' '.join(options).strip()
        if not selected or selected in ('help', '--help', '-h'):
            print(BEDROCK_HELP)
            return 0
        # No separate install/start/run/stop/status public commands. Retain
        # internal HTTP actions for the owning Bedrock tab and EULA UI only.
        if selected.casefold() in ('install', 'run', 'stop', 'start', 'status', 'restart', 'eula', 'log', 'command', 'path'):
            print('Usage: bedrock <world name> (or bedrock to list worlds)', file=sys.stderr)
            return 2
        state = _api('/api/bedrock/status')
        if not state.get('eula_accepted'):
            _api('/api/bedrock/eula', body={})
            print('[bedrock] Review and accept the official Minecraft EULA in Terminal.', flush=True)
        elif not state.get('installed') and not state.get('installing'):
            _api('/api/bedrock/install', body={})
        # The accepted EULA starts the existing background Bedrock installer.
        # Poll only while the user-requested command remains open. Cancellation
        # of the EULA returns without downloading any server or world content.
        last_progress = None
        start_wait = time.monotonic()
        while not state.get('installed'):
            if time.monotonic() - start_wait > 1800:
                raise RuntimeError('Bedrock installation did not complete; view the persistent Bedrock log')
            time.sleep(0.75)
            state = _api('/api/bedrock/status')
            if state.get('install_error'):
                raise RuntimeError('Bedrock installation failed: ' + state['install_error'])
            if state.get('eula_accepted') and not state.get('installing') and not state.get('installed'):
                _api('/api/bedrock/install', body={})
            if not state.get('eula_accepted') and not state.get('eula_requested'):
                print('[bedrock] EULA dialog dismissed; no installation was started.')
                return 1
            progress = state.get('install_progress')
            if progress and progress != last_progress:
                print('[bedrock] ' + str(progress), flush=True)
                last_progress = progress
        print('[bedrock] Starting ' + selected + ' in a new interactive terminal tab…', flush=True)
        result = _open_bedrock_with_progress(selected)
        if not result.get('terminal_session'):
            raise RuntimeError('Bedrock did not create its interactive terminal tab')
        print('[bedrock] Opened ' + selected + '; close its tab to stop the server.', flush=True)
        return 0
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

