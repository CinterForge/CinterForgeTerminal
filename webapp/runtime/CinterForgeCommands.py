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

  help [infinite-mac|debugger|bedrock|devtools] Display this help or one command's options
  debugger [open|status|help]   Inspect the current WebView (default: open)
  bedrock [world name]          List Bedrock and Java worlds; convert Java when launched
  minecraft                     Enter running Bedrock server console (exit to return)
  craftmaster [args]            Run the bundled native CraftMaster CLI
  install devtools              Download optional unified SDK after confirmation
  <story-file-stem> install|start|stop|status   On-demand Z-machine games
  games                         List all 68 games (WarGames first, then 67 classic Macs)
  wargames                      Launch original JOSHUA as its own Session tab
  stories                       List 16 IF titles (interactive fiction)
  discord <user> <server> <channel>  Open a Discord bot-connected Session tab
  setpass <username> <password>      Set this host's LAN login credentials
  code [run]                        Open Code-OSS in a tab
  code mode light|dark              Set and remember the Code-OSS theme
  code open <absolute-path>         Select host folder or .code-workspace
  htmlmenus show|hide|toggle        Inspect HTML menus on macOS (native menu remains)
  infinite-mac                  List 67 classic Macintosh games (help infinite-mac)
  <game-name>                   Launch the named game in its existing player
  Infinite Mac: https://infinitemac.org/

Your other commands run in the real OS shell (zsh, bash, or PowerShell).
Minecraft Bedrock downloads require an explicit EULA decision in the WebView.
'''


DEBUGGER_HELP = '''debugger [open|status|help]
  debugger             Request the live JavaScript WebView inspector
  debugger open        Same as debugger
  debugger status      Show whether developer inspection is enabled
  debugger help        Show this help

The Terminal enables native WebView inspection itself. Windows opens WebView2
DevTools and Linux opens WebKitGTK Inspector. On macOS, Terminal requests the
WKWebView Inspector and enables inspection through Safari > Develop as the
supported fallback if direct opening is unavailable.
'''


BEDROCK_HELP = """bedrock [world name]
  bedrock             List Bedrock saves and local Java Minecraft worlds
  bedrock <world>     Convert Java if needed; open the interactive Bedrock server tab
  Close the Bedrock tab to stop its server; accept the EULA once when prompted.
"""


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
    if operation == 'status':
        print(f'{name}: ' + ('installed' if path.is_file() else 'not installed') + ' at ' + str(path))
        return 0
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
    if operation == 'start':
        if not path.is_file():
            print(f'{name} is not installed. Type {name} install.', file=sys.stderr)
            return 1
        body = {'command': name}
        source = os.environ.get('CINTER_TERMINAL_SESSION_ID', '')
        if __import__('re').fullmatch(r'[0-9a-f]{32}', source):
            body['session_id'] = source
        result = _api('/api/if/start', body=body)
        print(f'{name}: opening a separate Z-machine Session tab in Terminal WebView')
        return 0 if result.get('requested') else 1
    if operation == 'stop':
        body = {'command':name}
        source = os.environ.get('CINTER_TERMINAL_SESSION_ID', '')
        if __import__('re').fullmatch(r'[0-9a-f]{32}', source):
            body['session_id'] = source
        result = _api('/api/if/stop', body=body)
        print(f'{name}: browser interpreter stopped' if result.get('stopped') else f'{name}: not running')
        return 0
    print(f'Usage: {name} [install|start|stop|status]', file=sys.stderr)
    return 2


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
        with urlopen(request, timeout=1800 if path == '/api/bedrock/run' else 120 if path == '/api/bedrock/install' else 90 if path in ('/api/bedrock/stop', '/api/bedrock/restart') else 10) as response:
            return json.loads(response.read(65536).decode('utf-8'))
    except HTTPError as error:
        try:
            message = json.loads(error.read(65536).decode('utf-8')).get('error', error.reason)
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


def minecraft_console(input_fn=None) -> int:
    """Interactive Bedrock console in the current PTY (other tabs stay live)."""
    import threading
    state = _api('/api/bedrock/status')
    if not (state.get('running') or state.get('starting')):
        print('Minecraft server is not running. Start it with bedrock <world name>.', file=sys.stderr)
        return 1
    read = input if input_fn is None else input_fn
    finished = threading.Event()
    def follow_output():
        offset = -1
        while not finished.wait(0.35):
            try:
                result = _api('/api/bedrock/console/output?offset=' + str(offset))
                offset = result['offset']
                text = result.get('output', '')
                if text:
                    # The server's output belongs in this console tab, not in
                    # the separate shell session or the VM management process.
                    sys.stdout.write('\r\n' + text)
                    sys.stdout.flush()
                if not (result.get('running') or result.get('starting')):
                    finished.set()
            except (OSError, ValueError, RuntimeError):
                pass
    watcher = threading.Thread(target=follow_output, name='bedrock-console-view', daemon=True)
    watcher.start()
    if state.get('starting'):
        print('Minecraft VM is alive; server readiness has not been independently verified.', flush=True)
    if state.get('console_available') is False:
        print('This older VM has no console input. Run bedrock restart to replace it', file=sys.stderr, flush=True)
        print('with the reconnectable console, then enter minecraft again.', file=sys.stderr, flush=True)
        return 1
    print('Minecraft server console. Enter commands without /; exit to return.', flush=True)
    try:
        while not finished.is_set():
            try:
                line = read('minecraft> ')
            except (EOFError, KeyboardInterrupt):
                break
            command = line.strip()
            if not command: continue
            if command.lower() in ('exit','quit'): break
            if '\r' in command or '\n' in command or len(command.encode('utf-8')) > 2048:
                print('Expected one Minecraft command up to 2048 bytes.', file=sys.stderr)
                continue
            try:
                result = _api('/api/bedrock/command', body={'command': command.lstrip('/')})
                if not result.get('sent'):
                    print('Minecraft command not delivered.', file=sys.stderr)
            except (RuntimeError, ValueError) as error:
                print('Minecraft:', error, file=sys.stderr)
                if not _api('/api/bedrock/status').get('running'):
                    break
    finally:
        finished.set()
        watcher.join(timeout=1)
    print('Leaving Minecraft console; server continues running.', flush=True)
    return 0


def _packaged_craftmaster() -> Path:
    """Find the current platform's packaged CraftMaster, never a sibling repo."""
    from runtime import CinterForgeSettings as settings
    system = {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}[platform.system()]
    filename = 'CinterForgeCraftMaster.exe' if system == 'win' else 'CinterForgeCraftMaster'
    # Runtime's ASSETS lives under the staged Python/Host/webapp subtree.
    # A native app's Build root can be named differently from repo Builds.
    roots = []
    configured = os.environ.get('CINTER_BUILDS_ROOT', '').strip()
    if configured:
        roots.append(Path(configured))
    roots.extend(settings.ASSETS.resolve().parents)
    for root in roots:
        candidate = root / 'CinterForgeCraftMaster' / system
        receipt = candidate / 'manifest.json'
        binary = candidate / 'bin' / filename
        if (not candidate.is_symlink() and receipt.is_file() and
                not receipt.is_symlink() and binary.is_file() and not binary.is_symlink()):
            doc = json.loads(receipt.read_text('utf-8'))
            expected = doc.get('files', {}).get('bin/' + filename)
            if (doc.get('schema') != 'cinterforge.craftmaster.stage.v1' or
                    doc.get('platform') != system or
                    not isinstance(expected, str) or len(expected) != 64):
                raise RuntimeError('Packaged CraftMaster receipt is invalid')
            digest = hashlib.sha256()
            with binary.open('rb') as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise RuntimeError('Packaged CraftMaster binary SHA-256 mismatch')
            return binary
    raise RuntimeError('This Terminal release lacks a verified native CraftMaster package')


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
        elif len(option) == 1 and option[0] in ('debugger', 'bedrock', 'devtools'):
            print(DEBUGGER_HELP if option[0] == 'debugger' else BEDROCK_HELP if option[0] == 'bedrock' else 'install devtools\n  Check the installed SDK, or offer verified public download with size/path confirmation.')
        else:
            print('Usage: help [infinite-mac|debugger|bedrock|devtools]', file=sys.stderr)
            return 2
        return 0
    if args[0] == 'craftmaster':
        binary = _packaged_craftmaster()
        return subprocess.call([str(binary), *args[1:]])
    if args[0] == 'setpass':
        if len(args) != 3:
            print('Usage: setpass <username> <password>', file=sys.stderr)
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
        # Use the same host workspace and theme store as the Code-OSS UI.
        # Bash has already parsed quoted POSIX paths into a single argument.
        operation = {'session_id': os.environ.get('CINTER_TERMINAL_SESSION_ID', '')}
        usage = 'Usage: code [run | mode light|dark | open "<absolute path>"]'
        if len(args) == 1 or args[1:] == ['run']:
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
        # Bash strips quotes and leaves optional trailing commas from the
        # human-friendly: discord "user", "server", "channel" syntax.
        fields = [part.strip().strip(',').strip() for part in args[1:]]
        if len(fields) != 3 or not all(fields):
            print('Usage: discord "Hope AI Dev" "CinterForge Internal" "builds"', file=sys.stderr)
            return 2
        result = _api('/api/discord/open', body={
            'username': fields[0], 'guild': fields[1], 'channel': fields[2],
            'session_id': os.environ.get('CINTER_TERMINAL_SESSION_ID', '')})
        print('Discord tab opening: ' + result['guild'] + ' / #' + result['channel'] +
              ' (connected as bot ' + result['bot'] + ')', flush=True)
        return 0
    if args[0] == 'minecraft':
        if len(args) != 1:
            print('Usage: minecraft', file=sys.stderr)
            return 2
        return minecraft_console()
    command, *options = args
    catalog = _cli_if_catalog()
    if command == 'games':
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
        for name, entry in catalog.items():
            print(f"{name:30} {entry['title']}")
        return 0
    if command == 'wargames':
        if options:
            print('Usage: wargames', file=sys.stderr)
            return 2
        return _launch_wargames()
    if command == 'infinite-mac':
        if options:
            print('Usage: infinite-mac', file=sys.stderr)
            return 2
        for slug, game in _cli_mac_catalog().items():
            print(f"{slug:30} {game['title']}")
        return 0
    mac_catalog = _cli_mac_catalog()
    if command in mac_catalog:
        if options:
            print('Usage: '+command, file=sys.stderr)
            return 2
        return _mac_launch(command, mac_catalog[command])
    if command in catalog:
        if len(options) > 1 or (options and options[0] not in ('install', 'start', 'stop', 'status')):
            return _if_command(command, 'invalid', catalog[command])
        return _if_command(command, options[0] if options else 'start', catalog[command])
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
    if command == 'install':
        if options != ['devtools']:
            print('Usage: install devtools', file=sys.stderr)
            return 2
        return _CinterForgeDevTools.install_devtools()
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
        if not options:
            state = _api('/api/bedrock/worlds')
            print('Available Bedrock worlds:')
            for world in state.get('worlds', []):
                print('  ' + world['name'] + '  [' + world['folder'] + ']')
            if not state.get('worlds'):
                print('  (none installed)')
            print('Available Java worlds:')
            for world in state.get('java_worlds', []):
                print('  ' + world['name'] + '  [' + world['folder'] + ']')
            if not state.get('java_worlds'):
                print('  (none discovered)')
            if state.get('java_error'):
                print('  CraftMaster inventory unavailable: ' + state['java_error'])
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
        result = _api('/api/bedrock/run', body={'name': selected})
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

