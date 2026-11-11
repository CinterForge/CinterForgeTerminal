"""CinterForgeTerminal — portable Python backend for the native/web UI.

PUBLIC ENTRY POINTS (see ``main()`` immediately below configuration):
    no mode           Staged HTTP bootstrap/smoke service
    --terminal-cli    Human-facing text commands, game catalog, install actions
    --guest-shell     Managed Win/Linux guest Bash Session on macOS
    --vm-watchdog     Private VM lifetime guardian (never starts Bedrock)

COMPONENTS
    Session objects   PTY/ConPTY input/output, resize and lifecycle
    HTTP service      Authenticated loopback origin for webapp/ assets and API
    BedrockManager    EULA, server state, world preservation and console
    DevTools          Optional consented SDK installation / guest shell setup
    Commands          help, debugger, bedrock, minecraft, stories and games

AUTHORING/LIFECYCLE CONTRACT
    The browser UI is ``webapp/terminal.js``. The native C++ host owns the
    process lifetime, and ``Cinter`` owns all build/setup/publish commands.
    Runtime files must work *without* the original source checkout installed.
    Installed apps write exclusively to their resolved Steam library; explicit
    repository development/lifecycle runs use sibling Builds. Do not repurpose
    or terminate another process's managed VM.
    Do not change the Infinite Mac guest-input automation in this backend.

Configuration and externally callable dispatch are at the beginning of this
file. Supporting implementations follow, grouped by responsibility.
"""
from __future__ import annotations

import base64
import hashlib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import unquote, urlsplit
import importlib.util
import ipaddress
import mimetypes
import os
import shlex
from pathlib import Path
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid

if os.name != 'nt':
    import fcntl
    import pty
    import select
    import struct
    import termios

# =============================================================================
# CONFIGURATION: constants used by the HTTP server, game shell, and guest VMs
# =============================================================================
# Bounds for one POST request, concurrent interactive Sessions and queued PTY
# output respectively; keep these independent of host operating-system limits.
MAX_BODY = 65_536                 # bytes per HTTP request
MAX_SESSIONS = 6                  # concurrent terminal tabs
MAX_OUTPUT = 1024 * 1024         # maximum buffered PTY output bytes
SERVER_LOG_MEMORY_LINES = 2048     # rolling Server tab backlog
SERVER_LOG_BATCH_LINES = 120       # bounded HTTP response per poll
ASSETS = Path(__file__).resolve().parent  # bundled webapp/ (not cwd)

# The guest VM is *different* from the Setup-only 'w'/'c' environments.
PROFILE = 't'
# Maximum verified Bedrock server download; rejects unexpected large payloads.
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
# Endpoints and protocol versions. Release/publication remains in ./Cinter.
EULA_URL = 'https://www.minecraft.net/en-us/eula'
PRIVACY_URL = 'https://www.minecraft.net/en-us/privacy'
DOWNLOAD_URL = 'https://www.minecraft.net/en-us/download/server/bedrock'
METADATA_ROOT = 'https://raw.githubusercontent.com/EndstoneMC/bedrock-server-data/v2/'
PUBLIC_DISTRIBUTION = 'https://cinterforge.ucreg.io/CinterForgeTerminal/Distribution'
FOUR_SCHEMA = 'cinterforge.terminal.four-artifact-release.v1'
VERSION_PATTERN = re.compile(r'^\d+\.\d+\.\d+(?:\.\d+)?$')
APP_PATTERN = re.compile(r'^[a-zA-Z][a-zA-Z0-9_-]{0,55}$')
# The staged Code-OSS page runs in a same-origin iframe. Capture bootstrap
# failures before its modules load; report to its owning Terminal WebView.
# This is injected into the HTTP response, never into the published Code tree.
CODEOSS_OBSERVER = r"""<script nonce="cinterforge">
(() => {
  const report = (status, message='') => {
    try {
      if (window.parent !== window) window.parent.postMessage({
        type:'cinterforge-codeoss-health', status, message:String(message).slice(0,450)
      }, window.location.origin);
    } catch (_) { /* no parent on a standalone Code-OSS page */ }
  };
  let ready=false, lastError='';
  const check = () => {
    if (ready) return;
    const workbench=document.querySelector('.monaco-workbench');
    if (workbench && workbench.querySelector('.part, .monaco-grid-view')) {
      ready=true;report('ready');
    }
  };
  window.addEventListener('error', event => {
    const resource=event.target!==window && (event.target?.src || event.target?.href);
    lastError=resource ? 'Resource failed: '+new URL(resource, location.href).pathname
      : (event.message || 'Workbench script error');
    report('error', lastError);
  }, true);
  window.addEventListener('unhandledrejection', event => {
    lastError=String(event.reason?.message || event.reason || 'Unhandled workbench rejection');
    report('error', lastError);
  });
  const observer=new MutationObserver(check);
  const started=() => {
    observer.observe(document.body || document.documentElement, {subtree:true,childList:true});
    check();
    setTimeout(() => {
      if (!ready) report('timeout',lastError || 'Workbench DOM did not initialize');
      observer.disconnect();
    },12000);
  };
  if (document.readyState==='loading') document.addEventListener('DOMContentLoaded',started,{once:true});
  else started();
})();
</script>
""".encode('utf-8')
STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/terminal.js': ('terminal.js', 'text/javascript; charset=utf-8'),
    '/terminal.css': ('terminal.css', 'text/css; charset=utf-8'),
    '/terminal_styles.json': ('terminal_styles.json', 'application/json; charset=utf-8'),
    '/mac_games.json': ('mac_games.json', 'application/json; charset=utf-8'),
    '/vendor/xterm.js': ('vendor/xterm.js', 'text/javascript; charset=utf-8'),
    '/vendor/xterm.css': ('vendor/xterm.css', 'text/css; charset=utf-8'),
    '/vendor/addon-fit.js': ('vendor/addon-fit.js', 'text/javascript; charset=utf-8'),
}


# ============================================================================
# PUBLIC ENTRY POINT: dispatch all packaged service/CLI/guest modes in one file
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Select an explicit mode without importing or spawning extra source files.

    No arguments: the staged HTTP readiness smoke exercise.
    --terminal-cli: user-facing shell commands (help, games, Bedrock, etc.).
    --guest-shell: interactive Linux/Windows guest, when user permitted.
    --vm-watchdog: private Bedrock VM lifetime owner (never a server launcher).
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == '--terminal-cli':
        return terminal_cli_main(arguments[1:])
    if arguments and arguments[0] == '--guest-shell':
        if len(arguments) != 2:
            raise ValueError('Usage: --guest-shell linux|win')
        return guest_shell_main(arguments[1])
    if arguments and arguments[0] == '--vm-watchdog':
        return _vm_watchdog_main(arguments[1:])
    return bootstrap_smoke_main(arguments)


def _lagomorph_media_url(builds: Path) -> dict:
    """Select public HTTPS classic HFS media for Infinite Mac.

    Infinite Mac cannot fetch an HTTP loopback image: its disk worker only
    accepts approved HTTPS origins or HTTPS/CORS-enabled sources. The private
    local ISO remains staged in Builds, but is never uploaded automatically.
    """
    config = builds / 'CinterForgeTerminal/Input/Lagomorph/CDROM_URL.txt'
    value = os.environ.get('CINTER_LAGOMORPH_CDROM_URL', '').strip()
    if not value and config.is_file():
        if config.is_symlink() or config.stat().st_size > 2048:
            raise ValueError('Unsafe Lagomorph CD-ROM URL configuration')
        value = config.read_text(encoding='utf-8').strip()
    if not value:
        # Published, range/CORS-verified production disk. This object is independent
        # of the four release ZIPs, and must also work from the packaged app's
        # resource-only Builds directory (which has no local publish receipt).
        release = ASSETS.parent.name
        if not __import__('re').fullmatch(r'20[0-9]{2}\.[0-9]{2}\.[0-9]{2}', release):
            release = '2026.11.23'
        value = ('https://cinterforge.ucreg.io/CinterForgeTerminal/Distribution/'
                 + release + '/Lagomorph-Quadra650.iso')
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or not host or parsed.username or parsed.password or
            parsed.fragment or any(ch.isspace() for ch in value) or
            host in ('localhost', 'localhost.localdomain') or
            host.endswith(('.localhost', '.local', '.internal')) or not parsed.path.lower().endswith(('.iso', '.img', '.toast', '.bin'))):
        raise ValueError('Lagomorph CD-ROM URL must be a public HTTPS disk-image URL')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise ValueError('Lagomorph CD-ROM URL cannot use a private or loopback address')
    # Only use the floppy pair if both have actually been published. Never
    # point Infinite Mac at inferred/stale objects. The hosted application
    # may have been staged before the public media was uploaded.
    result = {'configured': True, 'url': value}
    if host == 'cinterforge.ucreg.io' and value.endswith('/Lagomorph-Quadra650.iso'):
        names = ('Lagomorph-Application-1440K.img', 'Lagomorph-Battles-1440K.img')
        urls = [value.rsplit('/', 1)[0] + '/' + name for name in names]
        if all(_verified_public_hfs_floppy(url) for url in urls):
            result['floppies'] = urls
    return result


def _verified_public_hfs_floppy(url: str) -> bool:
    """A narrow public CORS/range check, not an arbitrary remote URL fetch."""
    from urllib.request import Request, urlopen
    from urllib.error import URLError
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'cinterforge.ucreg.io':
            return False
        request = Request(url, headers={'Range': 'bytes=1024-1025',
                                        'Origin': 'https://infinitemac.org'})
        with urlopen(request, timeout=1.5) as response:
            cors = response.headers.get('Access-Control-Allow-Origin', '')
            return (response.status == 206 and response.read(2) == b'BD' and
                    cors in ('*', 'https://infinitemac.org'))
    except (OSError, URLError, ValueError):
        return False


def _if_catalog() -> dict:
    data = json.loads((ASSETS / 'if_titles.json').read_text(encoding='utf-8'))
    if data.get('schema') != 'cinterforge.terminal.external-if-titles.v1' or len(data.get('titles', [])) != 16:
        raise RuntimeError('Incomplete Terminal IF title manifest')
    return {Path(item['file']).stem:item for item in data['titles']}


def _if_story(server, name: str) -> bytes:
    item = _if_catalog().get(name)
    if not item or not server.bedrock or not server.bedrock.steamapps:
        raise FileNotFoundError('IF game or Steam library unavailable: '+name)
    root = server.bedrock.steamapps / 'common/CinterForge/InteractiveFiction/Titles' / name
    path = root / item['file']
    if root.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size > 32*1024*1024:
        raise FileNotFoundError('Install the game first: '+name+' install')
    story = path.read_bytes()
    if path.suffix != '.zblorb':
        if story[:1] not in (b'\x03', b'\x05', b'\x08'):
            raise ValueError('Invalid installed Z-code story')
        return story
    # Blorb FORM/IFRS: extract only an embedded ZCOD chunk for the JS VM.
    if len(story) < 12 or story[:4] != b'FORM' or story[8:12] != b'IFRS':
        raise ValueError('Invalid ZBlorb container')
    offset = 12
    while offset + 8 <= len(story):
        kind = story[offset:offset+4]
        length = int.from_bytes(story[offset+4:offset+8], 'big')
        end = offset + 8 + length
        if end > len(story):
            raise ValueError('Malformed ZBlorb chunk length')
        if kind == b'ZCOD':
            content = story[offset+8:end]
            if content[:1] not in (b'\x03',b'\x05',b'\x08'):
                raise ValueError('Unsupported ZBlorb executable')
            return content
        offset = end + (length & 1)
    raise ValueError('ZBlorb has no ZCOD executable')




def _if_save_path(server, name: str) -> Path:
    if name not in _if_catalog():
        raise ValueError('Unknown IF title')
    steam = getattr(getattr(server, 'bedrock', None), 'steamapps', None)
    if not steam:
        raise RuntimeError('IF saves require a resolved host Steam library')
    root = steam / 'common/CinterForge/InteractiveFiction/Saves' / name
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError('Unsafe IF save location')
    return root / 'autosave.conv.json'


def _if_load_save(server, name: str) -> dict:
    story_hash = hashlib.sha256(_if_story(server, name)).hexdigest()
    path = _if_save_path(server, name)
    if path.is_symlink():
        raise ValueError('Unsafe IF autosave link')
    if not path.is_file():
        return {'found': False, 'story_sha256': story_hash}
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('IF autosave exceeds size limit')
    saved = json.loads(path.read_text(encoding='utf-8'))
    if (saved.get('schema') != 'cinterforge.conv.if.v1' or
            saved.get('story') != name or saved.get('story_sha256') != story_hash or
            not isinstance(saved.get('vm_state'), dict)):
        raise ValueError('Autosave is incompatible with the installed story')
    return {'found': True, 'story_sha256': story_hash, 'save': saved}


def _if_store_save(server, name: str, saved: dict) -> None:
    path = _if_save_path(server, name)
    story_hash = hashlib.sha256(_if_story(server, name)).hexdigest()
    if (saved.get('schema') != 'cinterforge.conv.if.v1' or
            saved.get('card_type') != 'CONV' or saved.get('story') != name or
            saved.get('story_sha256') != story_hash or
            not isinstance(saved.get('vm_state'), dict) or
            saved['vm_state'].get('schema') != 'cinterforge.if.quetzal.v1'):
        raise ValueError('Invalid CONV IF autosave or story fingerprint')
    raw = json.dumps(saved, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('IF autosave exceeds size limit')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Unsafe IF autosave target')
    # One previous save survives a torn write or a newer incompatible save.
    candidate = path.with_name('autosave-' + uuid.uuid4().hex + '.tmp')
    with candidate.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if path.exists():
            shutil.copyfile(path, path.with_name('autosave.previous.conv.json'))
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def target_platform() -> str:
    return {'Darwin': 'mac', 'Linux': 'linux', 'Windows': 'win'}[platform.system()]


def _bounded_size(cols: int, rows: int) -> tuple[int, int]:
    return max(20, min(int(cols), 300)), max(5, min(int(rows), 120))


def _windows_msys2_root(writable_root: Path) -> Path:
    """Read Bash from immutable packaged resources, not the Steam state tree.

    The downloaded Windows application bundles its MSYS2 runtime under its
    read-only Builds/ resources directory; native session state must instead
    be written under the selected steamapps/TerminalState directory. A source
    lifecycle run may use its provisioned developer MSYS2 closure directly.
    """
    for ancestor in ASSETS.resolve().parents:
        if ancestor.name == 'Builds':
            candidate = ancestor / 'CinterForgeTerminal/Runtime/MSYS2'
            if (candidate / 'usr/bin/bash.exe').is_file():
                return candidate
    for relative in ('CinterForgeTerminal/Runtime/MSYS2',
                     'CinterForgeTerminal/Toolchains/Windows/MSYS2'):
        candidate = writable_root / relative
        if (candidate / 'usr/bin/bash.exe').is_file():
            return candidate
    raise RuntimeError('Packaged Windows MSYS2 UCRT64 Bash is missing; '
                       'rebuild the Windows application with the complete runtime')


def _shell_command(system: str, scripts: Path, builds: Path | None = None) -> list[str]:
    """Use actual native Bash, with Windows binaries read from packaged MSYS2."""
    if system == 'win':
        if builds is None:
            raise RuntimeError('Windows native Bash requires a staged runtime')
        root = _windows_msys2_root(builds)
        bash = root / 'usr/bin/bash.exe'
        if not bash.is_file():
            raise RuntimeError(f'Pinned MSYS2 UCRT64 Bash not staged: {bash}')
        cygpath = root / 'usr/bin/cygpath.exe'
        if not cygpath.is_file():
            raise RuntimeError(f'MSYS2 Windows path translator missing: {cygpath}')
        translate = subprocess.run([str(cygpath), '-u', str(scripts / 'terminal-init.sh')],
                                   capture_output=True, text=True, timeout=15, check=False)
        if translate.returncode or not translate.stdout.strip():
            raise RuntimeError('MSYS2 cannot translate the Terminal initialization script path')
        return [str(bash), '--noprofile', '--rcfile', translate.stdout.strip(), '-i']
    bash = Path('/bin/bash')
    if bash.is_file():
        return [str(bash), '--noprofile', '--rcfile', str(scripts / 'terminal-init.sh'), '-i']
    raise RuntimeError(f'Native Bash required on {system}; refusing unverified shell fallback')


def _mac_catalog():
    data = json.loads((ASSETS / 'mac_games.json').read_text(encoding='utf-8'))
    if data.get('schema') != 'cinterforge.terminal.macgames.v1' or len(data.get('games', [])) != 67:
        raise RuntimeError('Incomplete staged Macintosh game catalog')
    result = {}
    for game in data['games']:
        command = game['command']
        if not re.fullmatch(r'[a-z0-9]+', command) or command in result:
            raise RuntimeError('Invalid Macintosh command')
        result[command] = game
    return result


def _install_shell_commands(root: Path, system: str, env: dict[str, str]) -> Path:
    """Author command bindings and native directory listing under Builds."""
    directory = root / 'scripts'
    directory.mkdir(parents=True, exist_ok=True)
    cli = ASSETS / 'CinterForgeTerminal.py'
    if not cli.is_file():
        raise RuntimeError(f'Missing staged CinterForgeTerminal command helper: {cli}')
    python = str(Path(sys.executable).resolve())
    functions = '\n'.join(
        f'{name}() {{ {shlex.quote(python)} {shlex.quote(str(cli))} --terminal-cli {name} "$@"; }}'
        for name in ('help', 'debugger', 'bedrock', 'minecraft', 'lagomorph', 'infinite-mac', 'wargames'))
    # These exact source filenames are the shell commands. Load the bundled
    # Terminal manifest; no sibling WebApp source or story package is required.
    catalog = json.loads((ASSETS / 'if_titles.json').read_text(encoding='utf-8'))
    if len(catalog.get('titles', [])) != 16:
        raise RuntimeError('Incomplete staged external IF catalog')
    story_names = []
    for entry in catalog['titles']:
        stem = Path(entry['file']).stem
        if not __import__('re').fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]*', stem):
            raise RuntimeError('Unsafe IF command name: ' + stem)
        story_names.append(stem)
    if len(set(story_names)) != 16:
        raise RuntimeError('Duplicate IF commands')
    functions += '\n' + '\n'.join(
        f'function {name} {{ {shlex.quote(python)} {shlex.quote(str(cli))} --terminal-cli {name} "$@"; }}'
        for name in ['games', 'stories'] + story_names)
    mac_names = list(_mac_catalog())
    if set(mac_names) & set(['help', 'debugger', 'bedrock', 'minecraft', 'lagomorph', 'infinite-mac', 'wargames', 'games', 'stories', 'install'] + story_names):
        raise RuntimeError('Mac game command collides with existing Terminal command')
    functions += '\n' + '\n'.join(
        f'function {name} {{ {shlex.quote(python)} {shlex.quote(str(cli))} --terminal-cli {name} "$@"; }}'
        for name in mac_names)
    # only the explicitly requested `install devtools` native-app command.
    functions += ('\ninstall() { if [ "$1" = devtools ]; then '
                  + shlex.quote(python) + ' ' + shlex.quote(str(cli))
                  + ' --terminal-cli install "$@"; else command install "$@"; fi; }')
    # The package controls MSYSTEM, UCRT64 PATH and HOME. Sourcing the global
    # /etc/profile on every interactive Session reruns MSYS2 first-start hooks
    # (pacman-key, /dev/shm and /dev/mqueue setup), even for read-only bundles.
    # Our explicit --rcfile requires no machine-wide profile or pacman setup.
    preamble = ("# Native CinterForge Terminal session (preconfigured UCRT64 environment)\n"
                "printf 'Welcome to CinterForge. Type help for instructions.\\n'\n")

    (directory / 'terminal-init.sh').write_text(preamble + functions + '\n', encoding='utf-8')
    return directory


def _session_environment(builds: Path, system: str) -> dict[str, str]:
    """Confine shell history/cache/temp to developer Builds or installed Steam."""
    root = builds / 'Run' / 'CinterForgeTerminal' / system
    paths = {name: root / name for name in ('home', 'tmp', 'cache', 'config', 'data')}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if system == 'win':
        user_home = env.get('USERPROFILE','')
    else:
        try:
            import pwd
            user_home = pwd.getpwuid(os.getuid()).pw_dir
        except (ImportError, KeyError, OSError):
            user_home = str(Path.home())
    env['CINTER_USER_HOME'] = str(user_home)
    # The interactive shell HOME is relocated into managed per-session state.
    # Preserve the *real* Steam root before rewriting HOME, otherwise an
    # optional public DevTools install would target the session sandbox.
    # An installed application may be on any Steam library drive. Its real
    # installation path takes precedence over the inherited/default library,
    # and must be passed into the PTY before its HOME is moved into Builds.
    installed_steam = None
    for parent in (builds.resolve(), *builds.resolve().parents):
        if (parent.name.lower() == 'steamapps' and
                builds.resolve().relative_to(parent).parts[:3] == ('common', 'CinterForge', 'Applications')):
            installed_steam = parent
            break
    if installed_steam is not None:
        env['CINTER_STEAMAPPS'] = str(installed_steam)
    elif not env.get('CINTER_STEAMAPPS'):
        if platform.system() in ('Darwin', 'Linux'):
            try:
                import pwd
                login_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
            except (ImportError, KeyError, OSError):
                login_home = Path.home()
            steam = (login_home / 'Library/Application Support/Steam/steamapps'
                     if platform.system() == 'Darwin'
                     else login_home / '.local/share/Steam/steamapps')
        else:
            steam = Path(env.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Steam/steamapps'
        env['CINTER_STEAMAPPS'] = str(steam)
    # In the installed Host tree this directory is named <release>.
    # The checkout path uses the same canonical pinned release as the builder.
    release = ASSETS.parent.name
    if not (release[:4].isdigit() and release.count('.') == 2):
        from host import HOST_RELEASE
        release = HOST_RELEASE
    env['CINTER_TERMINAL_RELEASE'] = release
    if builds.name == 'Builds':
        env['CINTER_BUILDS_ROOT'] = str(builds.resolve())
    env.update({'HOME': str(paths['home']), 'TMPDIR': str(paths['tmp']),
                'TMP': str(paths['tmp']), 'TEMP': str(paths['tmp']),
                'XDG_CACHE_HOME': str(paths['cache']), 'XDG_CONFIG_HOME': str(paths['config']),
                'XDG_DATA_HOME': str(paths['data']), 'HISTFILE': str(paths['home'] / 'history'),
                'TERM': 'xterm-256color', 'COLORTERM': 'truecolor', 'PYTHONDONTWRITEBYTECODE': '1'})
    logs = _runtime_logs_root(builds)
    logs.mkdir(parents=True, exist_ok=True)
    env['CINTER_TERMINAL_DIRECTORY_LOG'] = str(logs / ('native-terminal-' + system + '.log'))
    if _steam_library_for_installed_root(builds) is not None and builds.name != 'Builds':
        # The optional guest VM and downloaded DevTools must never use ~/Builds.
        env.pop('CINTER_BUILDS_ROOT', None)
        env['CINTER_TERMINAL_RUNTIME_MODE'] = 'installed'
    if system == 'win':
        env.update({'USERPROFILE': str(paths['home']), 'APPDATA': str(paths['data']),
                    'LOCALAPPDATA': str(paths['data'])})
    return env


class TerminalSession:
    def __init__(self, builds: Path, system: str, cols: int, rows: int, *, url: str, token: str,
                 command: list[str] | None = None, working_directory: Path | None = None,
                 output_log: Path | None = None):
        self.id = uuid.uuid4().hex
        self.system = system
        self.cols, self.rows = _bounded_size(cols, rows)
        self.lock = threading.Condition()
        self.output = deque()
        self.output_size = 0
        self.closed = False
        self.output_log = None
        env = _session_environment(builds, system)
        env['CINTER_TERMINAL_URL'] = url
        env['CINTER_TERMINAL_TOKEN'] = token
        self.root = builds / 'Run' / 'CinterForgeTerminal' / system
        env['CINTER_TARGET_PLATFORM'] = system
        if system == 'win':
            msys = _windows_msys2_root(builds)
            env['MSYSTEM'] = 'UCRT64'
            env['CHERE_INVOKING'] = '1'
            env['MSYS2_PATH_TYPE'] = 'inherit'
            env['PATH'] = os.pathsep.join([str(msys / 'ucrt64/bin'),
                                            str(msys / 'usr/bin'), env.get('PATH', '')])
        scripts = _install_shell_commands(self.root, system, env)
        self.shell = command if command is not None else _shell_command(system, scripts, builds)
        working_directory = working_directory or self.root
        if output_log is None:
            output_log = _runtime_logs_root(builds) / 'Sessions' / (self.id + '.log')
        if output_log is not None:
            output_log.parent.mkdir(parents=True, exist_ok=True)
            self.output_log = output_log.open('ab')
        if system == 'win':
            # pywinpty uses the native Windows pseudoterminal API. Do not fake
            # an interactive terminal by piping cmd.exe stdout/stdin.
            try:
                from winpty import PtyProcess
            except ImportError as exc:
                raise RuntimeError('Windows Python runtime is missing the pinned pywinpty module; run ./Cinter setup all and rebuild Windows (not MSYS2 winpty.exe)') from exc
            self.process = PtyProcess.spawn(self.shell, cwd=str(working_directory), env=env,
                                            dimensions=(self.rows, self.cols))
            self.fd = None
        else:
            self.fd, slave = pty.openpty()
            self._winsize(self.cols, self.rows)
            try:
                self.process = subprocess.Popen(self.shell, cwd=working_directory, env=env,
                                                stdin=slave, stdout=slave, stderr=slave,
                                                start_new_session=True, close_fds=True)
            finally:
                os.close(slave)
        self.reader = threading.Thread(target=self._read_output, name='cinter-pty-' + self.id[:8], daemon=True)
        self.reader.start()

    def _winsize(self, cols: int, rows: int) -> None:
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def _read_output(self) -> None:
        while not self.closed:
            try:
                if self.system == 'win':
                    data = self.process.read(4096)
                    if isinstance(data, str):
                        data = data.encode('utf-8', errors='replace')
                else:
                    ready, _, _ = select.select([self.fd], [], [], 0.25)
                    if not ready:
                        if self.process.poll() is not None:
                            break
                        continue
                    data = os.read(self.fd, 4096)
                if not data:
                    break
            except (OSError, EOFError):
                break
            with self.lock:
                if self.output_log is not None:
                    try:
                        self.output_log.write(data)
                        self.output_log.flush()
                    except (OSError, ValueError):
                        pass  # The terminal itself remains readable if logging fails.
                self.output.append(data)
                self.output_size += len(data)
                # Bound memory on a stalled browser, never allow unbounded output.
                while self.output_size > MAX_OUTPUT and self.output:
                    self.output_size -= len(self.output.popleft())
                self.lock.notify_all()
        with self.lock:
            self.closed = True
            self.lock.notify_all()

    def poll(self, wait_ms: int) -> tuple[bytes, bool]:
        with self.lock:
            if not self.output and not self.closed:
                self.lock.wait(timeout=max(0, min(int(wait_ms), 750)) / 1000)
            data = b''.join(self.output)
            self.output.clear()
            self.output_size = 0
            return data, self.closed

    def write(self, value: str) -> None:
        if self.closed:
            raise RuntimeError('Terminal has closed')
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError('Invalid terminal input')
        if self.system == 'win':
            self.process.write(value)
        else:
            os.write(self.fd, value.encode('utf-8'))

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = _bounded_size(cols, rows)
        if self.system == 'win':
            self.process.setwinsize(self.rows, self.cols)
        else:
            self._winsize(self.cols, self.rows)
            try:
                os.killpg(self.process.pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    def close(self) -> None:
        if self.closed and self.system == 'win':
            return
        self.closed = True
        if self.system == 'win':
            try:
                self.process.terminate(force=True)
            except (OSError, AttributeError):
                pass
        else:
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGHUP)
                    self.process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            os.close(self.fd)
        with self.lock:
            if self.output_log is not None:
                self.output_log.close()
                self.output_log = None
            self.lock.notify_all()


class BedrockTerminalSession(TerminalSession):
    """One real PTY, using the same Setup acceptance VM launcher.

    The visible Session owns stdin/stdout and the process group. Switching
    away from its tab does not detach, terminate, or restart the server.
    """
    def __init__(self, manager, builds: Path, system: str, cols: int, rows: int,
                 *, url: str, token: str):
        command, extra = manager._run_command()  # Same run-mac-bds.sh as acceptance.
        if extra:
            raise RuntimeError('Bedrock Session needs an unsupported environment override')
        path = manager._log_path()
        manager._launch_offset = path.stat().st_size if path.is_file() else 0
        super().__init__(builds, system, cols, rows, url=url, token=token,
                         command=command, working_directory=manager.run_root,
                         output_log=path)
        self.manager = manager
        self.kind = 'bedrock'
        manager._process = self.process
        manager._terminal_session = self
        manager._ready = False
        manager._startup_error = ''
        try:
            if system == 'mac':
                manager._record_launch(self.process.pid)
                manager._watch_vm(self.process.pid)
            # Mirror BedrockManager.start(): a launcher rejected by macOS must
            # fail the run request instead of opening a misleading dead tab.
            try:
                self.process.wait(timeout=0.7)
            except subprocess.TimeoutExpired:
                pass
            else:
                tail = path.read_bytes()[-1600:].decode('utf-8', errors='replace') if path.is_file() else ''
                raise RuntimeError(f'Bedrock VM exited immediately (code {self.process.returncode}): {tail}')
        except Exception:
            # Never abandon a VM if its owner/watchdog fails to initialize.
            self.close()
            raise

    def close(self) -> None:
        try:
            if getattr(self, 'manager', None) is not None and self.manager._terminal_session is self:
                self.manager.stop()  # Deliver `stop` through this PTY first.
        finally:
            super().close()


def _load_bedrock_manager():
    # Bedrock is part of this single authored service, not a copied helper.
    return BedrockManager


def _runtime_steam_locations() -> list[Path]:
    """Discover existing Steam libraries without source-only host imports.

    Steam's libraryfolders.vdf is authoritative for nondefault disk volumes.
    Never create or infer a library merely from the default home pathname.
    """
    if sys.platform == 'darwin':
        try:
            import pwd
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            home = Path.home()
        roots = [home / 'Library/Application Support/Steam']
    elif os.name == 'nt':
        roots = [Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Steam']
    else:
        try:
            import pwd
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            home = Path.home()
        roots = [home / '.local/share/Steam', home / '.steam/steam', home / '.steam/root']
    candidates: list[Path] = []
    for root in roots:
        for config in (root / 'config/libraryfolders.vdf',
                       root / 'steamapps/libraryfolders.vdf'):
            if not config.is_file() or config.is_symlink():
                continue
            try:
                source = config.read_text(encoding='utf-8-sig', errors='replace')
            except OSError:
                continue
            for raw in re.findall(r'(?im)^\s*"path"\s*"((?:\\.|[^"\\])*)"', source):
                library = Path(raw.replace('\\\\', '\\').replace('\\"', '"')).expanduser()
                if library.is_absolute():
                    candidates.append(library / 'steamapps')
        candidates.append(root / 'steamapps')
    found: list[Path] = []
    for candidate in candidates:
        if candidate.name.casefold() == 'steamapps' and candidate.is_dir() and not candidate.is_symlink():
            resolved = candidate.resolve(strict=True)
            if resolved not in found:
                found.append(resolved)
    return found


def _cinterforge_steam_manifest(steamapps: Path) -> bool:
    for entry in steamapps.glob('appmanifest_*.acf'):
        if not entry.is_file() or entry.is_symlink():
            continue
        try:
            source = entry.read_text(encoding='utf-8-sig', errors='replace')
        except OSError:
            continue
        found = re.search(r'(?i)"(?:installdir|name)"\s*"(cinterforge[^"\r\n]*)"', source)
        if found:
            return True
    return False


def _steam_library_for_installed_root(builds: Path) -> Path | None:
    """Resolve the installed Steam library from the staged application's path.

    A Steam library outside the default disk is authoritative. An explicitly
    supplied Steam root permits stand-alone package launches, without creating
    a developer Builds directory or inventing a Steam installation path.
    """
    for item in (builds.resolve(), ASSETS.resolve()):
        for ancestor in (item, *item.parents):
            if ancestor.name.lower() != 'steamapps':
                continue
            rel = item.relative_to(ancestor).parts
            if (len(rel) >= 3 and rel[:2] == ('common', 'CinterForge') and
                    rel[2] in ('Applications', 'CinterForge Terminal')):
                # Both the current Applications/<platform>/... layout and
                # older native Windows common/CinterForge/CinterForge Terminal
                # releases are installed apps, even though they contain an
                # immutable directory literally named Builds.
                return ancestor
    value = os.environ.get('CINTER_STEAMAPPS', '').strip()
    if value:
        candidate = Path(value).expanduser()
        if candidate.name.lower() != 'steamapps' or not candidate.is_dir() or candidate.is_symlink():
            raise RuntimeError('CINTER_STEAMAPPS must identify an existing real Steam steamapps directory')
        return candidate.resolve(strict=True)
    candidates = _runtime_steam_locations()
    manifested = [item for item in candidates if _cinterforge_steam_manifest(item)]
    if len(manifested) == 1:
        return manifested[0]
    if len(manifested) > 1:
        raise RuntimeError('Multiple Steam libraries report CinterForge installations; set CINTER_STEAMAPPS')
    active = [item for item in candidates if any(
        manifest.is_file() and not manifest.is_symlink()
        for manifest in item.glob('appmanifest_*.acf'))]
    if len(active) == 1:
        return active[0]
    existing = [item for item in candidates if (item / 'common/CinterForge').is_dir() and
                not (item / 'common/CinterForge').is_symlink()]
    if len(existing) == 1:
        return existing[0]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise RuntimeError('Multiple Steam libraries found; select the real one with CINTER_STEAMAPPS')
    return None


def _terminal_mutable_root(builds: Path) -> Path:
    """Separate signed, read-only packaged content from writable session state.

    Source/lifecycle and explicit developer stage use sibling Builds. Installed
    native app (even one invoked from Finder) uses its actual Steam library.
    The existence of VS Code elsewhere is not an authorization to write Builds.
    """
    physical = builds.resolve(strict=True)
    embedded_app = any(part.endswith('.app') for part in physical.parts)
    installed_steam = None
    for item in (physical, ASSETS.resolve()):
        for parent in (item, *item.parents):
            if parent.name.lower() != 'steamapps':
                continue
            relative = item.relative_to(parent).parts
            if (len(relative) >= 3 and relative[:2] == ('common', 'CinterForge') and
                    relative[2] in ('Applications', 'CinterForge Terminal')):
                installed_steam = parent
                break
        if installed_steam is not None:
            break
    # Critical: the Windows lightweight distribution names its immutable
    # payload folder Builds. Being named Builds does NOT make it a developer
    # tree. A downloaded app outside Steam also uses the portable bundle
    # layout; only explicit repository development may write to that tree.
    packaged_windows = (os.name == 'nt' and physical.name == 'Builds' and
                        ASSETS.resolve().is_relative_to(physical) and
                        ASSETS.resolve().relative_to(physical).parts[:2] == ('Python', 'Host'))
    if installed_steam is not None or (
            packaged_windows and os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') != 'developer'
            and os.environ.get('CINTER_BUILDS_ROOT') != str(physical)):
        steam = installed_steam or _steam_library_for_installed_root(physical)
        if steam is None:
            raise RuntimeError('Installed Windows Terminal requires a real Steam library; '
                               'set CINTER_STEAMAPPS to select an existing library')
        candidate = steam / 'common/CinterForge/Applications/TerminalState'
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
            raise RuntimeError('Unsafe installed Terminal state root: ' + str(candidate))
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    if not embedded_app and physical.name == 'Builds':
        return physical  # ./Cinter and explicit repository development stage
    if not embedded_app:
        for parent in physical.parents:
            if parent.name == 'Builds':
                return parent  # developer stage beneath sibling Builds
    if (embedded_app and os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'developer'
            and not any(parent.name.lower() == 'steamapps' for parent in physical.parents)):
        for parent in physical.parents:
            if parent.name == 'Builds' and not any(part.endswith('.app') for part in parent.parts):
                return parent  # explicitly launched repository/development app only
    steam = _steam_library_for_installed_root(physical)
    if steam is not None:
        candidate = steam / 'common/CinterForge/Applications/TerminalState'
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
            raise RuntimeError('Unsafe installed Terminal state root: ' + str(candidate))
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    if embedded_app:
        # Only explicit repository development runs may write a sibling Builds.
        # A downloaded .app can itself sit inside a directory named Builds.
        if os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'developer':
            for parent in physical.parents:
                if parent.name == 'Builds' and not any(p.endswith('.app') for p in parent.parts):
                    return parent
        raise RuntimeError('Packaged Terminal needs an actual Steam library for runtime state; set CINTER_STEAMAPPS')
    raise RuntimeError('Runtime state needs a repository Builds or an installed Steam library')


def _runtime_logs_root(writable_root: Path) -> Path:
    """Return logs in developer Builds or installed steamapps/common/CinterForge."""
    root = writable_root.resolve()
    for parent in (root, *root.parents):
        if parent.name.lower() == 'steamapps':
            return parent / 'common/CinterForge/Logs/CinterForgeTerminal'
    if root.name == 'Builds':
        return root / 'Logs/CinterForgeTerminal/Runtime'
    raise RuntimeError('Runtime logs cannot escape developer Builds or the Steam installation')


class ServerRuntimeLog:
    """Thread-safe, bounded live Server tab with a persistent session log."""
    def __init__(self, directory: Path):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise RuntimeError('Unsafe CinterForge Terminal log directory: ' + str(directory))
        directory.mkdir(parents=True, exist_ok=True)
        filename = time.strftime('server-%Y%m%d-%H%M%S-') + str(os.getpid()) + '-' + secrets.token_hex(4) + '.log'
        self.path = directory / filename
        self.stream = self.path.open('x', encoding='utf-8', buffering=1)
        self.lock = threading.Lock()
        self.cursor = 0
        self.lines = deque(maxlen=SERVER_LOG_MEMORY_LINES)
        self.append('HTTP/Python server log opened; path=' + str(self.path))

    def append(self, message: str) -> None:
        # Never log API tokens, request bodies, cookies, or whole URL queries.
        line = time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + str(message).replace('\r', ' ').replace('\x00', '')
        with self.lock:
            for part in line.splitlines():
                self.cursor += 1
                self.lines.append((self.cursor, part))
                self.stream.write(part + '\n')
            self.stream.flush()

    def since(self, cursor: int) -> dict:
        if cursor < 0:
            raise ValueError('Invalid Server log cursor')
        with self.lock:
            values = [(i, line) for i, line in self.lines if i > cursor][:SERVER_LOG_BATCH_LINES]
            return {'lines': [line for _, line in values],
                    'cursor': values[-1][0] if values else max(cursor, self.cursor),
                    'log_path': str(self.path), 'read_only': True,
                    'truncated': bool(self.lines and cursor < self.lines[0][0]-1)}

    def close(self) -> None:
        with self.lock:
            if not self.stream.closed:
                self.stream.flush()
                self.stream.close()


def open_bedrock_terminal(server, name: str, cols: int = 80, rows: int = 24,
                          *, restart: bool = False) -> dict:
    """Create one visible Session PTY using the same command as Setup acceptance.

    A run request never steals the requesting shell. Switching sessions never
    affects process ownership; only explicit stop/close or app shutdown does.
    """
    if server.bedrock is None:
        raise RuntimeError('Bedrock is unavailable: ' + server.bedrock_error)
    with server.bedrock_terminal_lock:
        with server.sessions_lock:
            live = [item for item in server.sessions.values()
                    if getattr(item, 'kind', None) == 'bedrock' and
                    item.process.poll() is None]
            if live and not restart:
                # Select the existing Bedrock tab only for the same saved world.
                selected = (server.bedrock._world_settings().get('selected') or {})
                if name.strip() and name.strip().casefold() not in (
                        str(selected.get('name', '')).casefold(),
                        str(selected.get('folder', '')).casefold()):
                    raise RuntimeError('A different Bedrock world is already running; stop it before switching')
                item = live[0]
                server.bedrock_terminal_requests.append({
                    'id': item.id, 'label': 'Bedrock', 'kind': 'bedrock',
                    'shell': 'Bedrock', 'platform': server.system})
                return {**server.bedrock.status(), 'terminal_session': item.id,
                        'reused': True}
        if restart:
            server.bedrock.stop()  # Preserve the selected managed world.
        # Completed Bedrock sessions have no live process to preserve. Release
        # their backend PTY slots; the browser retains each transcript tab.
        with server.sessions_lock:
            ended = [(key, item) for key, item in server.sessions.items()
                     if getattr(item, 'kind', None) == 'bedrock' and
                     item.process.poll() is not None]
            for key, _ in ended:
                server.sessions.pop(key, None)
        for _, item in ended:
            item.close()
        with server.sessions_lock:
            if len(server.sessions) >= MAX_SESSIONS:
                raise ValueError('Maximum terminal sessions reached')
        state = server.bedrock.run_world(name, launch=False)
        if state.get('running'):
            raise RuntimeError('Bedrock already running outside a Session; stop it explicitly first')
        terminal = BedrockTerminalSession(server.bedrock, server.mutable_builds,
            server.system, cols, rows, url=server.origin, token=server.token)
        with server.sessions_lock:
            server.sessions[terminal.id] = terminal
        server.bedrock_terminal_requests.append({
            'id': terminal.id, 'label': 'Bedrock', 'kind': 'bedrock',
            'shell': 'Bedrock', 'platform': server.system})
        return {**state, 'running': terminal.process.poll() is None,
                'console_available': True, 'terminal_session': terminal.id}


class CinterShellServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, builds: Path):
        self.builds = builds
        self.mutable_builds = _terminal_mutable_root(builds)
        self.system = target_platform()
        self.log = ServerRuntimeLog(_runtime_logs_root(self.mutable_builds))
        self.token = secrets.token_urlsafe(36)
        self.sessions: dict[str, TerminalSession] = {}
        self.sessions_lock = threading.Lock()
        self.bedrock_terminal_lock = threading.Lock()
        self.bedrock_terminal_requests = deque()
        self.devtools_requested = False
        self.debug_requests = 0
        self.lagomorph_requests = 0
        self.macgame_request = None
        self.wargames_requests = 0
        self.if_action = None
        self.if_active = None
        self.debug_lock = threading.Lock()
        self.eula_lock = threading.Lock()
        self.eula_requested = False
        # Bedrock is optional. A missing user Steam library must never prevent
        # the WebView, real PTY, Code, debugger or Help from starting.
        self.bedrock = None
        self.bedrock_error = ''
        try:
            self.bedrock = _load_bedrock_manager()(builds, self.system, user_install=True)
        except RuntimeError as exc:
            self.bedrock_error = str(exc)
            print('[CinterForgeTerminal] Bedrock unavailable: ' + self.bedrock_error,
                  file=sys.stderr, flush=True)
        super().__init__(('127.0.0.1', 0), Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.log_event('backend ready; platform=' + self.system + ' port=' + str(self.server_port))

    def log_event(self, message: str) -> None:
        self.log.append(message)

    def stop(self) -> None:
        for item in list(self.sessions.values()):
            item.close()
        self.sessions.clear()
        if self.devtools_requested:
            # Only our optional 't' profile. Setup's 'w'/'c' are untouched.
            try:
                stop_runtime_vm()
            except (OSError, RuntimeError, ValueError) as error:
                print('[DEVTOOLS] Terminal VM cleanup: '+str(error), file=sys.stderr, flush=True)
        if self.bedrock is not None:
            self.bedrock.stop()
        self.log_event('backend shutdown completed')
        self.shutdown()
        self.server_close()
        self.log.close()


class Handler(BaseHTTPRequestHandler):
    server: CinterShellServer

    def _local(self) -> bool:
        # DNS rebinding protection even though the listener is loopback-only.
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def _authorized(self) -> bool:
        # WebKit may classify an in-app loopback request as `same-site` rather
        # than `same-origin`. Classification alone is not an auth decision:
        # require the unguessable session token, exact Host, and exact Origin
        # whenever the browser supplies one; reject genuinely cross-site calls.
        origin = self.headers.get('Origin')
        host_ok = self._local()
        token_ok = secrets.compare_digest(self.headers.get('X-CPH-Token', ''), self.server.token)
        origin_ok = origin is None or origin == self.server.origin
        site = self.headers.get('Sec-Fetch-Site', 'same-origin')
        allowed = host_ok and token_ok and origin_ok and site != 'cross-site'
        if not allowed:
            # No token, URL, or session identifier in logs. Keep a precise
            # rejection reason for diagnosing native WebView initialization.
            message = (f'rejected local API request: host={host_ok} token={token_ok} '
                       f'origin={origin_ok} fetch_site={site!r}')
            if hasattr(self.server, 'log_event'):
                self.server.log_event(message)
            sys.stderr.write('[CinterForgeTerminal] ' + message + '\n')
        return allowed

    def _send(self, code: int, body: bytes, ctype='application/json; charset=utf-8', *, same_origin_frame: bool = False) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN' if same_origin_frame else 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self' https://infinitemac.org; object-src 'none'; base-uri 'none'; frame-ancestors " + ("'self'" if same_origin_frame else "'none'"))
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
        path = unquote(raw_path)
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
                workbench_body = source[:marker.start()] + CODEOSS_OBSERVER + source[marker.start():]
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
            self.send_header('Content-Security-Policy', "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; connect-src 'self' https:; img-src 'self' data: blob:; font-src 'self' data:; frame-src 'self' blob:; frame-ancestors 'self'")
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

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if not self._local():
            self._json(403, {'error': 'invalid host'})
            return
        if self._serve_artifact(path):
            return
        if path == '/healthz':
            self._json(200, {'ready': True})
            return
        if path == '/api/server/log':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            from urllib.parse import parse_qs
            raw = parse_qs(urlsplit(self.path).query).get('cursor', ['0'])[0]
            try:
                self._json(200, self.server.log.since(int(raw)))
            except (ValueError, OverflowError) as error:
                self._json(400, {'error': str(error)})
            return
        if path == '/api/status':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            self._json(200, {'platform': self.server.system, 'sessions': len(self.server.sessions),
                             'debug': True})
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
            if name not in _if_catalog():
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
                      '<script src="/terminal.js" defer></script></html>')
            # Only the dedicated player document may be embedded by this same-origin Terminal.
            self._send(200, markup.encode(), 'text/html; charset=utf-8', same_origin_frame=True)
            return
        if path.startswith('/api/if/save/'):
            if not self._authorized():
                self._json(403, {'error':'not authorized'})
                return
            try:
                self._json(200, _if_load_save(self.server, unquote(path[len('/api/if/save/'):])))
            except (ValueError, FileNotFoundError, OSError, RuntimeError, json.JSONDecodeError) as exc:
                self._json(400, {'error':str(exc)})
            return
        if path.startswith('/api/if/story/'):
            if not self._authorized():
                self._json(403,{'error':'not authorized'})
                return
            try:
                self._send(200,_if_story(self.server,path[len('/api/if/story/'):]),
                           'application/octet-stream')
            except (ValueError,FileNotFoundError,OSError) as exc:
                self._json(404,{'error':str(exc)})
            return
        if path == '/api/if/next':
            if not self._authorized():
                self._json(403,{'error':'not authorized'})
                return
            with self.server.debug_lock:
                action = self.server.if_action
                self.server.if_action = None
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
                self._json(200, _lagomorph_media_url(self.server.builds))
            except (ValueError, OSError, UnicodeError) as error:
                self._json(400, {'error': str(error)})
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
            from urllib.parse import parse_qs
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
            root=ASSETS / 'joshua'
            if (not parts or any(part in ('..','.') or part.startswith('.') for part in parts)
                    or '\\' in relative or root.is_symlink()):
                self._json(404, {'error':'Unknown JOSHUA asset'});return
            asset=root.joinpath(*parts)
            if asset.is_symlink() or not asset.is_file():
                self._json(404, {'error':'Unknown JOSHUA asset'});return
            mime=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream'
            self._send(200,asset.read_bytes(),mime,same_origin_frame=asset.name=='index.html')
            return
        info = STATIC_FILES.get(path)
        if not info:
            self._json(404, {'error': 'not found'})
            return
        name, ctype = info
        file = ASSETS / name
        if not file.is_file():
            self._json(503, {'error': f'Incomplete shell package: {name}'})
            return
        data = file.read_bytes()
        if name == 'index.html':
            data = data.replace(b'__CPH_TOKEN__', self.server.token.encode('ascii'))
        self._send(200, data, ctype)

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(403, {'error': 'not authorized'})
            return
        try:
            n = int(self.headers.get('Content-Length', '0'))
            route = self.path.split('?', 1)[0]
            limit = 2 * 1024 * 1024 if route.startswith('/api/if/save/') else MAX_BODY
            if not (0 < n <= limit):
                raise ValueError('Invalid request length')
            body = json.loads(self.rfile.read(n))
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object')
            if route.startswith('/api/if/save/'):
                _if_store_save(self.server, unquote(route[len('/api/if/save/'):]), body)
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
                if self.headers.get('Origin') != self.server.origin:
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
                self._json(200, open_bedrock_terminal(self.server,
                    str(body.get('name', '')), body.get('cols', 80), body.get('rows', 24)))
                return
            if route == '/api/bedrock/restart':
                selected = (self.server.bedrock._world_settings().get('selected') or {})
                if not selected.get('folder'):
                    raise RuntimeError('No selected Bedrock save to restart; type bedrock start')
                self._json(200, open_bedrock_terminal(self.server,
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
                command=None
                if backend=='minecraft':
                    if self.server.bedrock is None:
                        raise RuntimeError('Minecraft needs an installed Bedrock server')
                    state=self.server.bedrock.status()
                    if not (state.get('running') or state.get('starting')):
                        raise RuntimeError('Minecraft disabled: start Bedrock first with bedrock run')
                    if state.get('console_available') is False:
                        raise RuntimeError('Bedrock console attachment unavailable')
                    command=[sys.executable, str(ASSETS/'CinterForgeTerminal.py'),'--terminal-cli','minecraft']
                elif self.server.system == 'mac' and backend in ('linux','win'):
                    # Run the requested real guest shell in THIS Session's PTY.
                    # The child checks the optional unified DevTools receipt,
                    # obtains explicit consent if absent, streams progress,
                    # and starts the verified Linux OCI or Windows MSYS2/Wine
                    # backend. Never substitute macOS /bin/bash.
                    command=[sys.executable, '-u', str(ASSETS/'CinterForgeTerminal.py'),'--guest-shell',backend]
                    self.server.devtools_requested = True
                elif backend in ('mac','linux','win') and backend!=self.server.system:
                    raise RuntimeError(backend+' Bash unavailable on '+self.server.system+'; refusing native-shell fallback')
                with self.server.sessions_lock:
                    if len(self.server.sessions) >= MAX_SESSIONS:
                        raise ValueError('Maximum terminal sessions reached')
                    terminal = TerminalSession(self.server.mutable_builds, self.server.system,
                                               body.get('cols', 80), body.get('rows', 24),
                                               url=self.server.origin, token=self.server.token,
                                               command=command)
                    self.server.sessions[terminal.id] = terminal
                self.server.log_event('terminal opened: id=' + terminal.id[:10] +
                    ' command=' + Path(terminal.shell[0]).name + ' output=' + str(terminal.output_log.name if terminal.output_log else 'none'))
                self._json(200, {'id': terminal.id, 'shell': 'Minecraft' if backend=='minecraft' else (backend.title()+' Bash' if self.server.system == 'mac' and backend in ('win','linux') else Path(terminal.shell[0]).name),
                                 'platform': backend if backend!='native' else self.server.system})
                return
            if route in ('/api/if/start','/api/if/stop'):
                command = str(body.get('command',''))
                if command not in _if_catalog():
                    raise ValueError('Unknown story filename command')
                if route.endswith('/start'):
                    _if_story(self.server,command)  # Verify installed story first.
                    with self.server.debug_lock:
                        self.server.if_active = command
                        self.server.if_action = {'start':command}
                    self._json(200,{'requested':True})
                else:
                    with self.server.debug_lock:
                        stopped = self.server.if_active == command
                        if stopped:
                            self.server.if_action = {'stop':command}
                            self.server.if_active = None
                    self._json(200,{'stopped':stopped})
                return
            if route == '/api/wargames/open':
                with self.server.debug_lock:
                    self.server.wargames_requests += 1
                self._json(200, {'requested': True})
                return
            if route == '/api/macgames/open':
                command = body.get('command')
                if not isinstance(command,str) or command not in _mac_catalog():
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

    def log_message(self, fmt, *args):
        # Log useful errors/terminal lifecycle without recording private bodies,
        # bearer tokens, query strings or every Server tab poll.
        route = urlsplit(self.path).path
        if route in ('/api/server/log', '/api/terminal/poll'):
            return
        status = str(args[1]) if len(args) > 1 else ''
        if status.startswith(('4', '5')) or route in ('/api/terminal/open', '/api/terminal/close'):
            self.server.log_event('HTTP ' + self.command + ' ' + route + ' status=' + status)


_server = None
_server_thread = None


def handle(method: str, params: dict) -> dict:
    """Invoked through the already-shipped PythonHost service RPC worker."""
    global _server, _server_thread
    if method == 'start':
        if _server is not None:
            raise RuntimeError('CinterForgeTerminal HTTP server already started')
        builds = Path(params['builds']).resolve(strict=True)
        if builds.name != 'Builds' or not ASSETS.is_relative_to(builds):
            raise ValueError('Shell resources must be staged inside the shared Builds directory')
        _server = CinterShellServer(builds)
        _server_thread = threading.Thread(target=_server.serve_forever, name='cinter-http', daemon=True)
        _server_thread.start()
        return {'url': _server.origin + '/', 'port': _server.server_port,
                'platform': _server.system, 'server_log': str(_server.log.path)}
    if method == 'next_debugger_request':
        if _server is None:
            return {'requested': False}
        with _server.debug_lock:
            if _server.debug_requests:
                _server.debug_requests -= 1
                return {'requested': True}
            return {'requested': False}
    if method == 'stop':
        if _server is not None:
            _server.stop()
            _server = None
        return {'stopped': True}
    raise ValueError(f'Unsupported CinterForgeTerminal host method: {method}')


def bootstrap_smoke_main(argv: list[str] | None = None) -> int:
    """Launch the *staged* HTTP service using the packaged Python interpreter.

    This entry point is used only by the CinterForgeTerminal Setup smoke. Readiness
    is sent through a private file in Builds, rather than an unauthenticated
    public API or an application log containing the bearer token.
    """
    import argparse
    parser = argparse.ArgumentParser(description='CinterForgeTerminal staged HTTP smoke')
    parser.add_argument('--builds', type=Path, required=True)
    parser.add_argument('--ready', type=Path, required=True)
    args = parser.parse_args(argv)
    builds = args.builds.resolve(strict=True)
    ready = args.ready.resolve()
    if builds.name != 'Builds' or not ready.is_relative_to(builds) or ready.parent.is_symlink():
        raise ValueError('Bootstrap smoke must run entirely under sibling Builds')
    if not ASSETS.resolve().is_relative_to(builds):
        raise ValueError('Bootstrap smoke requires staged HTML/service under Builds')
    server = CinterShellServer(builds)
    stopped = threading.Event()

    def request_stop(_signum, _frame):
        stopped.set()

    for name in ('SIGINT', 'SIGTERM'):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, request_stop)
    ready.parent.mkdir(parents=True, exist_ok=True)
    with ready.open('x', encoding='utf-8') as stream:
        os.chmod(ready, 0o600) if os.name != 'nt' else None
        json.dump({'schema': 'cinterforge.terminal.http-ready.v1',
                   'origin': server.origin, 'token': server.token,
                   'pid': os.getpid(), 'platform': server.system}, stream)
        stream.flush()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        stopped.wait()
    finally:
        server.stop()
        worker.join(timeout=5)
        ready.unlink(missing_ok=True)
    return 0



# ============================================================================
# BEDROCK SERVER AND VM OWNERSHIP
# ============================================================================
"""Optional, EULA-gated Bedrock dedicated-server lifecycle for Python Host.

The official binary is never bundled into Host source. Explicit EULA consent
in the local WebView installs into the host Steam library. Developer Setup
continues to use sibling Builds and never acquires user content implicitly.
No user-authored package.json or prior Bedrock archive is required.
"""

import hashlib
from collections import deque
import sys
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import socket
import select
import struct
import subprocess
import tempfile
import uuid
import threading
import time
from urllib.parse import urlparse
from typing import Callable
from urllib.request import urlopen, Request
import zipfile

ASSET_LOCATION = ASSETS  # compatibility within bundled Bedrock helpers


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
    with zipfile.ZipFile(archive) as source:
        names = source.infolist()
        if len(names) > 25000:
            raise ValueError('Bedrock archive has too many entries')
        total = sum(item.file_size for item in names)
        if total > 4 * MAX_DOWNLOAD_BYTES:
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
               '--referer', DOWNLOAD_URL,
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
                if count > MAX_DOWNLOAD_BYTES:
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
    builds = builds.resolve(strict=True)
    chosen = _steam_library_for_installed_root(builds)
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


# RakNet's standard unconnected ping/pong is the conventional Bedrock LAN
# discovery transport. Education/NetherNet may also use other protocols.
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


class BedrockManager:
    def __init__(self, builds: Path, system: str, *, user_install: bool = False):
        self.builds = builds.resolve(strict=True)
        self.platform = system
        self.target = 'linux' if system == 'mac' else system
        self.app = os.environ.get('CPH_APP_ID', 'CinterForgeTerminal')
        if not APP_PATTERN.fullmatch(self.app):
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
        # must not depend on a BedrockManager/Popen instance remaining in memory.
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
        if self.platform != 'mac':
            return
        native_owner = os.getppid()
        self._watchdog = subprocess.Popen(
            [sys.executable, '-B', str(Path(__file__).resolve()),
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
        try:
            receipt = json.loads(self.receipt.read_text('utf-8'))
            return receipt.get('eula') == EULA_URL and receipt.get('accepted') is True
        except (OSError, ValueError, TypeError):
            return False

    def accept(self, confirmed: bool) -> dict:
        if confirmed is not True:
            raise ValueError('Explicit EULA acceptance is required')
        self.run_root.mkdir(parents=True, exist_ok=True)
        receipt = {'schema': 'cinterforge.bedrock.acceptance.v1', 'accepted': True,
                   'eula': EULA_URL, 'privacy': PRIVACY_URL, 'timestamp_utc': time.time(),
                   'application': self.app}
        self.receipt.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        return {'eula_accepted': True}

    @staticmethod
    def _registry_json(path: str) -> dict:
        """Fetch bounded, HTTPS metadata, not unverified executable content."""
        url = METADATA_ROOT + path
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
        try:
            versions = self._registry_json('versions.json')['release']
            version = versions['latest']
            if (not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version)
                    or version not in versions['versions']):
                raise ValueError('Bedrock stable release registry is invalid')
            metadata = self._registry_json('release/' + version + '/metadata.json')
            if metadata.get('version') != version:
                raise ValueError('Bedrock version metadata does not match the registry')
            binary = metadata['binary']['windows' if self.target == 'win' else 'linux']
            entry = {'url': binary['url'], 'sha256': binary['sha256'], 'version': version}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError('Unable to resolve the official Bedrock server and SHA-256. '
                               f'Official download information: {DOWNLOAD_URL}. Detail: {exc}') from exc
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
                            if total > MAX_DOWNLOAD_BYTES:
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
                                if copied > MAX_DOWNLOAD_BYTES:
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
    manager = BedrockManager(Path(builds), system, user_install=(install == '1'))
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




# ============================================================================
# TERMINAL COMMANDS AND OPTIONAL DEVTOOLS ACQUISITION
# ============================================================================
"""CinterForgeTerminal's application commands, invoked by the real native shell.

Help text is pure/local. Developer and Bedrock actions use the authenticated
loopback API owned by the running CinterForgeTerminal Python HTTP service.
"""

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

HELP = '''CinterForgeTerminal custom commands

  help [infinite-mac|debugger|bedrock|devtools] Display this help or one command's options
  debugger [open|status|help]   Inspect the current WebView (default: open)
  bedrock <operation>           Manage this app's Minecraft Bedrock server
  minecraft                     Enter running Bedrock server console (exit to return)
  install devtools              Download optional unified SDK after confirmation
  <story-file-stem> install|start|stop|status   On-demand Z-machine games
  games                         List all 68 games (WarGames first, then 67 classic Macs)
  wargames                      Launch original JOSHUA as its own Session tab
  stories                       List 16 IF titles (interactive fiction)
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

BEDROCK_HELP = '''bedrock [start|run <save name>|restart|command <Minecraft command>|path [directory]|stop|status|log|eula|install|help]
  bedrock start          List local saves and probe nearby Bedrock servers; do not launch
  bedrock run <save name> Open a dedicated Bedrock Session, leaving this terminal intact
  bedrock command <cmd> Send one Minecraft server-console command
  bedrock path           Show selected discovery root(s)
  bedrock path <folder>  Set an existing minecraftWorlds directory (server must be stopped)
  bedrock stop           Stop the active server (also handles old unattached VMs)
  bedrock restart        Stop this world and open a fresh dedicated Bedrock Session
  bedrock status         Display selected world, actual path and configured UDP port
  bedrock log            Show server installation diagnostics
  bedrock eula           Present official EULA in the application WebView
  bedrock install        Install official server after consent
  bedrock help           Show these commands

Original world saves are not modified. Each selected world is copied into the
server's writable Run/worlds tree. A later run reuses that managed copy.
For Linux and custom locations, use: bedrock path "/full/path/to/minecraftWorlds"
'''

# Public DevTools acquisition is deliberately self-contained: command_cli.py
# is shipped in every native app; the host build/publisher modules are not.


# The 16-title acquisition manifest is part of Terminal, not a Cloudflare pack.
# No game binaries are staged, mirrored or downloaded by Setup.
def _cli_if_catalog():
    from pathlib import Path
    manifest = Path(__file__).with_name('if_titles.json')
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
    from pathlib import Path
    data = json.loads(Path(__file__).with_name('mac_games.json').read_text(encoding='utf-8'))
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
        result = _api('/api/if/start', body={'command':name})
        print(f'{name}: starting with staged JavaScript Z-machine in Terminal WebView')
        return 0 if result.get('requested') else 1
    if operation == 'stop':
        result = _api('/api/if/stop', body={'command':name})
        print(f'{name}: browser interpreter stopped' if result.get('stopped') else f'{name}: not running')
        return 0
    print(f'Usage: {name} [install|start|stop|status]', file=sys.stderr)
    return 2


def _devtools_release() -> str:
    import re
    from pathlib import Path
    value = os.environ.get('CINTER_TERMINAL_RELEASE', '')
    if not value:
        # Staged entry point: <Host>/<platform>/<arch>/<release>/webapp/CinterForgeTerminal.py
        value = Path(__file__).resolve().parents[1].name
    if not re.fullmatch(r'[0-9]{4}\.[0-9]+\.[A-Za-z0-9._-]+', value):
        try:
            from host import HOST_RELEASE
            value = HOST_RELEASE
        except ImportError as exc:
            raise RuntimeError('Missing packaged release identifier') from exc
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value):
        raise RuntimeError('Invalid packaged release identifier')
    return value


def _devtools_destination(release: str):
    from pathlib import Path
    configured = os.environ.get('CINTER_STEAMAPPS') or os.environ.get('CFOS_STEAMAPPS')
    if configured:
        steamapps = Path(configured).expanduser()
        if steamapps.name.lower() != 'steamapps' or steamapps.is_symlink() or not steamapps.is_dir():
            raise RuntimeError('Provide a real Steam steamapps root via CINTER_STEAMAPPS: ' + str(steamapps))
        steamapps = steamapps.resolve(strict=True)
    else:
        steamapps = _steam_library_for_installed_root(ASSETS.parent)
        if steamapps is None:
            raise RuntimeError('Cannot identify the installed Steam library; set CINTER_STEAMAPPS')
    return steamapps / 'common/CinterForge/Applications/DevTools/CinterForge Terminal' / release


def _public_manifest(release: str) -> tuple[dict, str]:
    """Read the public release manifest, tolerating short CDN visibility delays.

    The user has not approved a download at this point: this GET is only a
    small release manifest. It never fetches the multi-GB DevTools archive.
    """
    import re
    import time
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    url = f'{PUBLIC_DISTRIBUTION}/{release}/four-artifacts.json'
    headers = {'Accept': 'application/json', 'Cache-Control': 'no-cache',
               'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/131.0.0.0 Safari/537.36')}
    retry_delays = (2, 4, 8, 12, 12)
    for index in range(len(retry_delays) + 1):
        try:
            with urlopen(Request(url, headers=headers), timeout=40) as stream:
                if getattr(stream, 'status', 200) != 200:
                    raise RuntimeError('Public DevTools manifest did not return HTTP 200')
                raw = stream.read(1024 * 1024 + 1)
            break
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as exc:
            if index == len(retry_delays):
                raise RuntimeError(f'Public DevTools manifest unavailable at {url} after '
                                   f'{index + 1} checks: {exc}') from exc
            print(f'[DEVTOOLS] Public manifest not ready ({exc}); retrying in '
                  f'{retry_delays[index]}s, no package downloaded', flush=True)
            time.sleep(retry_delays[index])
    if len(raw) > 1024 * 1024:
        raise RuntimeError('Public release manifest is unexpectedly large')
    data = json.loads(raw)
    if (data.get('schema') != FOUR_SCHEMA or data.get('release') != release or
            not isinstance(data.get('artifacts'), dict) or
            set(data['artifacts']) != {'mac', 'linux', 'win', 'devtools'}):
        raise RuntimeError('Public release manifest does not identify all four bundles')
    entry = data['artifacts']['devtools']
    if (entry.get('filename') != f'CinterForgeTerminal-devtools-{release}.zip' or
            not isinstance(entry.get('bytes'), int) or entry['bytes'] <= 0 or
            not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256', '')))):
        raise RuntimeError('Public DevTools release manifest is invalid')
    return entry, url


def _file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _download_public_devtools(url: str, partial, *, length: int, digest: str):
    """Download a verified DevTools ZIP using bounded HTTPS ranges and retries.

    The public archive is several GiB. A single CDN/TLS stream may be severed
    before the transfer completes, so transfer 64 MiB at a time and resume from
    the last fully written byte after a transient disconnect. Preserve an
    incomplete .partial file across sessions; never extract unverified bytes.
    """
    import time
    from http.client import HTTPException
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlsplit
    from urllib.request import Request, urlopen

    if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'cinterforge.ucreg.io':
        raise RuntimeError('DevTools must come from the approved public Cloudflare host')
    partial.parent.mkdir(parents=True, exist_ok=True)
    if partial.is_symlink():
        raise RuntimeError('Unsafe DevTools download target')
    current = partial.stat().st_size if partial.is_file() else 0
    if current > length:
        partial.unlink()
        current = 0
    if current == length:
        if _file_sha256(partial) == digest:
            print('[DEVTOOLS] previously downloaded package verified', flush=True)
            return
        partial.unlink()
        current = 0
    if current:
        print(f'[DEVTOOLS] resuming saved download at '
              f'{current / 1073741824:.2f}/{length / 1073741824:.2f} GiB', flush=True)

    window = 64 * 1024 * 1024
    consecutive_failures = 0
    max_failures = 12
    last_report = time.monotonic()
    while current < length:
        requested_end = min(current + window, length) - 1
        headers = {'User-Agent': 'CinterForgeTerminal-devtools/1',
                   'Accept-Encoding': 'identity',
                   'Range': f'bytes={current}-{requested_end}'}
        try:
            with urlopen(Request(url, headers=headers), timeout=90) as source:
                status = getattr(source, 'status', 200)
                if status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',
                                         source.headers.get('Content-Range', '').strip())
                    if (not match or int(match[1]) != current or
                            int(match[2]) < current or int(match[2]) > requested_end or
                            int(match[3]) != length):
                        raise RuntimeError('Public DevTools CDN returned an invalid byte range; '
                                           'saved download was not modified')
                    stop = int(match[2]) + 1
                elif status == 200 and current == 0:
                    # A CDN that ignores Range can still deliver one full stream,
                    # but a later disconnect cannot safely resume on that CDN.
                    stop = length
                    print('[DEVTOOLS] CDN ignored Range; streaming full archive', flush=True)
                else:
                    raise RuntimeError(f'Public DevTools CDN did not honor resume '
                                       f'(HTTP {status}); saved download retained')
                with partial.open('ab') as output:
                    while current < stop:
                        block = source.read(min(4 * 1024 * 1024, stop - current))
                        if not block:
                            raise ConnectionError('Remote stream closed before the requested bytes arrived')
                        output.write(block)
                        current += len(block)
                        now = time.monotonic()
                        if now - last_report >= 2 or current == length:
                            print(f'[DEVTOOLS] download {100 * current / length:.1f}% '
                                  f'({current / 1073741824:.2f}/{length / 1073741824:.2f} GiB)',
                                  flush=True)
                            last_report = now
            consecutive_failures = 0
        except HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504):
                raise RuntimeError(f'Public DevTools download HTTP {error.code}; '
                                   f'saved download retained at {partial}') from error
            failure = error
        except (URLError, HTTPException, OSError, TimeoutError) as error:
            # Includes BrokenPipeError, ConnectionResetError and incomplete TLS reads.
            failure = error
        else:
            continue
        # Read failures can happen after writing part of the requested range.
        # Recompute the actual durable offset before starting the next request.
        current = partial.stat().st_size if partial.is_file() else 0
        consecutive_failures += 1
        if consecutive_failures > max_failures:
            raise RuntimeError('Public DevTools download repeatedly disconnected; '
                               f'{current / 1073741824:.2f} GiB retained at {partial}. '
                               'The next approved INSTALL resumes this file.') from failure
        delay = min(2 ** min(consecutive_failures - 1, 4), 16)
        print(f'[DEVTOOLS] connection interrupted ({type(failure).__name__}: {failure}); '
              f'resuming from {current / 1073741824:.2f} GiB in {delay}s '
              f'({consecutive_failures}/{max_failures})', flush=True)
        time.sleep(delay)

    if partial.stat().st_size != length or _file_sha256(partial) != digest:
        # Never stage an archive that differs from the signed-release manifest.
        partial.unlink(missing_ok=True)
        raise RuntimeError('Public DevTools ZIP size/SHA-256 mismatch; invalid partial discarded')
    print('[DEVTOOLS] download 100% — verified published SHA-256', flush=True)


def _stage_devtools_zip(archive_path, destination, release: str, artifact: dict):
    import hashlib
    import os as _os
    from pathlib import Path, PurePosixPath
    import stat
    import tempfile
    import time
    import zipfile
    if destination.is_symlink():
        raise RuntimeError('Unsafe DevTools destination redirect')
    with zipfile.ZipFile(archive_path) as archive:
        contents = archive.namelist()
        if len(set(contents)) != len(contents) or 'release-manifest.json' not in contents:
            raise RuntimeError('DevTools ZIP has duplicate entries or no manifest')
        meta = json.loads(archive.read('release-manifest.json'))
        files = meta.get('files')
        if (meta.get('schema') != FOUR_SCHEMA or meta.get('release') != release or
                meta.get('target') != 'devtools' or meta.get('root') != 'DevTools' or
                not isinstance(files, dict) or set(contents) != set(files) | {'release-manifest.json'}):
            raise RuntimeError('DevTools ZIP member manifest invalid')
        for name, info in files.items():
            parts = PurePosixPath(name).parts
            # README is an authored root-level member of the published ZIP,
            # generated by _devtools_assets. It is not a traversal or SDK path.
            if (not isinstance(info, dict) or
                    (name != 'DevTools/README.txt' and
                     (len(parts) < 4 or parts[:2] != ('DevTools', 'Builds'))) or
                    name.startswith('/') or '\\' in name or '..' in parts or '.' in parts or
                    archive.getinfo(name).is_dir() or
                    stat.S_IFMT(archive.getinfo(name).external_attr >> 16) == stat.S_IFLNK or
                    not isinstance(info.get('bytes'), int) or info['bytes'] < 0 or
                    info.get('mode') not in (0o644, 0o755) or
                    ((archive.getinfo(name).external_attr >> 16) & 0o777) != info.get('mode') or
                    not isinstance(info.get('sha256'), str) or len(info['sha256']) != 64 or
                    archive.getinfo(name).file_size != info['bytes'] or
                    '/Servers/Bedrock/' in name):
                raise RuntimeError('Unsafe DevTools ZIP member: ' + name)
        total = sum(x['bytes'] for x in files.values())
        completed = 0
        last = time.monotonic()
        for name, info in files.items():
            item = destination.joinpath(*PurePosixPath(name).parts)
            for parent in [destination, *reversed(list(item.parents)[:-1])]:
                # Every parent in the final destination must remain real.
                if parent.is_symlink():
                    raise RuntimeError('Unsafe DevTools install symlink: ' + str(parent))
            item.parent.mkdir(parents=True, exist_ok=True)
            if item.is_symlink():
                raise RuntimeError('Unsafe DevTools symlink redirect: ' + str(item))
            if item.is_dir():
                # Build-owned file paths can be obstructed by an old directory.
                # Preserve it in Steam, never discard it or fail for normal drift.
                import uuid
                backup = destination.parent / 'Previous DevTools Conflicts' / uuid.uuid4().hex / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                item.replace(backup)
            fd, temp_path = tempfile.mkstemp(prefix='Installing-', dir=item.parent)
            _os.close(fd)
            temp = Path(temp_path)
            try:
                h = hashlib.sha256()
                size = 0
                with archive.open(name) as src, temp.open('wb') as dst:
                    for chunk in iter(lambda: src.read(4 * 1024 * 1024), b''):
                        dst.write(chunk)
                        size += len(chunk)
                        h.update(chunk)
                if size != info['bytes'] or h.hexdigest() != info['sha256']:
                    raise RuntimeError('DevTools installed member failed SHA-256: ' + name)
                temp.chmod(info['mode'])
                temp.replace(item)
            finally:
                temp.unlink(missing_ok=True)
            completed += info['bytes']
            now = time.monotonic()
            if now - last >= 2:
                print(f'[DEVTOOLS] install {100 * completed / total:.1f}% '
                      f'({completed / 1073741824:.2f}/{total / 1073741824:.2f} GiB)', flush=True)
                last = now
    destination.mkdir(parents=True, exist_ok=True)
    receipt = {'schema': FOUR_SCHEMA, 'release': release, 'target': 'devtools',
               'artifact': artifact, 'source': 'public', 'bedrock': 'excluded-until-app-eula-acceptance'}
    temp_receipt = destination / 'stage-receipt.pending'
    temp_receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    temp_receipt.replace(destination / 'stage-receipt.json')
    print('[DEVTOOLS] install 100% — all ZIP members verified', flush=True)


def _verify_native_devtools(destination) -> bool:
    """Actually execute local tools where possible; never invent probe success."""
    import platform
    import subprocess
    targets = []
    root = destination / 'DevTools/Builds/CinterForgeTerminal/Toolchains'
    if platform.system() == 'Darwin':
        for name in ('cmake', 'ninja', 'python3', 'node'):
            targets.append(root / 'mac/env/bin' / name)
    elif platform.system() == 'Windows':
        targets.append(root / 'Windows/MSYS2/usr/bin/bash.exe')
    else:
        for name in ('gcc', 'cmake', 'ninja'):
            candidate = root / 'LinuxSample/bin' / name
            if candidate.is_file():
                targets.append(candidate)
    if not targets:
        print('[DEVTOOLS] No directly runnable host tools in archive; OCI toolchain acceptance requires its native target runner.', flush=True)
        return True
    complete = True
    for exe in targets:
        if not exe.is_file():
            print(f'[DEVTOOLS] version check unavailable: {exe}', flush=True)
            complete = False
            continue
        try:
            run = subprocess.run([str(exe), '--version'], text=True, capture_output=True,
                                 check=False, timeout=30)
            if run.returncode:
                raise RuntimeError(run.stderr.strip() or f'exit={run.returncode}')
            print(f'[DEVTOOLS] version check {exe.name}: '
                  f'{(run.stdout or run.stderr).strip().splitlines()[0]}', flush=True)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, IndexError) as error:
            print(f'[DEVTOOLS] version check FAILED {exe}: {error}', flush=True)
            complete = False
    return complete


def install_devtools() -> int:
    """User-initiated public download only; never run during Terminal startup."""
    import zipfile
    from pathlib import Path
    release = _devtools_release()
    destination = _devtools_destination(release)
    receipt_file = destination / 'stage-receipt.json'
    if receipt_file.is_file() and not receipt_file.is_symlink():
        # The installed-receipt check must agree with the Windows/Linux guest
        # launcher. Directory presence alone wrongly treated partial or
        # manually deleted SDKs as installed and skipped the consent dialog.
        try:
            sdk = sdk_root()
        except (ImportError, OSError, ValueError, RuntimeError):
            sdk = None
        if sdk is not None and sdk == destination / 'DevTools/Builds/CinterForgeTerminal/Toolchains':
            print(f'[DEVTOOLS] already staged: {destination}', flush=True)
            return 0 if _verify_native_devtools(destination) else 1
        print('[DEVTOOLS] Existing installation incomplete; reinstall requires your consent.', flush=True)
    artifact, manifest = _public_manifest(release)
    url = f'{PUBLIC_DISTRIBUTION}/{release}/{artifact["filename"]}'
    print('Unified macOS + Linux + Windows developer toolchains:', flush=True)
    print(f'  Public release: {manifest}', flush=True)
    print(f'  Package: {url}', flush=True)
    print(f'  Download: {artifact["bytes"] / 1073741824:.2f} GiB', flush=True)
    print(f'  Install to: {destination}', flush=True)
    if input('Type INSTALL to confirm download and installation: ').strip() != 'INSTALL':
        print('[DEVTOOLS] canceled; nothing downloaded', flush=True)
        return 1
    pending = destination.parent / 'Downloads' / release / (artifact['filename'] + '.partial')
    try:
        _download_public_devtools(url, pending, length=artifact['bytes'], digest=artifact['sha256'])
        _stage_devtools_zip(pending, destination, release, artifact)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        print(f'[DEVTOOLS] installation failed: {type(error).__name__}: {error}', flush=True)
        print('[DEVTOOLS] Mac Session remains open; retry INSTALL to resume any saved download.',
              flush=True)
        return 1
    # Keep the already downloaded, verified archive until the installed SDK
    # has passed its local tool checks. Otherwise a failed version probe forces
    # another multi-GiB network download even though the ZIP is still valid.
    if not _verify_native_devtools(destination):
        print('[DEVTOOLS] Installed files retained, as is the verified ZIP for '
              'a local repair; no re-download is needed.', flush=True)
        return 1
    pending.unlink(missing_ok=True)
    print(f'[DEVTOOLS] installed: {destination}', flush=True)
    return 0


def _api(path: str, *, body: dict | None = None) -> dict:
    origin = os.environ.get('CINTER_TERMINAL_URL', '')
    token = os.environ.get('CINTER_TERMINAL_TOKEN', '')
    parsed = urlparse(origin)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or parsed.path not in ('', '/') or not token:
        raise RuntimeError('Not attached to a trusted CinterForgeTerminal session')
    request = Request(origin.rstrip('/') + path,
                      data=json.dumps(body).encode('utf-8') if body is not None else None,
                      headers={'X-CPH-Token': token, 'Content-Type': 'application/json'},
                      method='POST' if body is not None else 'GET')
    try:
        with urlopen(request, timeout=120 if path in ('/api/bedrock/install', '/api/bedrock/run') else 90 if path in ('/api/bedrock/stop', '/api/bedrock/restart') else 10) as response:
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
    request = Request(origin.rstrip('/') + '/api/lagomorph/manual',
                      headers={'X-CPH-Token': token})
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
        print('Minecraft server is not running. Start it with bedrock run <save name>.', file=sys.stderr)
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


def execute(args: list[str]) -> int:
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
        return install_devtools()
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
        option = options or ['prompt']
        if not option or option[0] not in ('prompt', 'status', 'log', 'eula', 'install', 'start', 'stop', 'restart', 'help', 'run', 'path', 'command') or (option[0] not in ('run', 'path', 'command') and len(option) != 1) or (option[0] in ('run', 'command') and len(option) < 2):
            print(BEDROCK_HELP, file=sys.stderr)
            return 2
        op = option[0]
        if op == 'help':
            print(BEDROCK_HELP)
        elif op == 'eula':
            _api('/api/bedrock/eula', body={})
            print('Review the Bedrock EULA in the CinterForgeTerminal WebView.')
        elif op == 'log':
            data = _api('/api/bedrock/log')
            print('[bedrock] Installer log:', data.get('path', ''))
            for line in data.get('lines', []):
                print(line)
        elif op == 'path':
            if len(option) == 1:
                state = _api('/api/bedrock/worlds')
            else:
                state = _api('/api/bedrock/path', body={'path': ' '.join(option[1:])})
            print('World search roots:')
            for root in state.get('roots', []):
                print('  ' + root)
            print(f"Discovered {len(state.get('worlds', []))} saves. Type bedrock start to list them.")
        elif op == 'command':
            result = _api('/api/bedrock/command', body={'command': ' '.join(option[1:])})
            if not result.get('sent'):
                raise RuntimeError('Minecraft command was not delivered to the server console')
            print('[bedrock] Submitted to server console:', result.get('command', ''))
        elif op == 'restart':
            state = _api('/api/bedrock/restart', body={})
            print('[bedrock] Restarting the same managed world in a fresh Bedrock Terminal.')
            print('Console available:', 'yes' if state.get('console_available') else 'no')
            print('The new Bedrock Session owns the live console; this shell remains available.')
        elif op == 'run':
            state = _api('/api/bedrock/run', body={'name': ' '.join(option[1:])})
            config = state.get('server_configuration') or {}
            if state.get('terminal_session'):
                print('[bedrock] Dedicated Bedrock Terminal opening; this session remains available.')
                print('[bedrock] Server stdin and stdout belong to the new Session tab.')
            else:
                print('[bedrock] Server state:', 'running' if state.get('running') else 'not running')
            print('Save:', state.get('world_name', ''))
            print('Source world:', state.get('source_world', ''))
            print('Active server world:', state.get('active_world', ''))
            print('Configured UDP IPv4:', config.get('server-port', '19132'),
                  '| IPv6:', config.get('server-portv6', '19133'))
        elif op == 'status':
            state = _api('/api/bedrock/status')
            print(f"Bedrock ({state.get('host', 'unknown')} / {state.get('platform', 'unknown')}): "
                  f"EULA {'accepted' if state.get('eula_accepted') else 'not accepted'}, "
                  f"{'installed' if state.get('installed') else 'not installed'}, "
                  f"{'running' if state.get('running') else ('starting' if state.get('starting') else 'stopped')}")
            if state.get('installing'):
                print('Installing: ' + str(state.get('install_progress') or 'starting...'))
            if state.get('install_error'):
                print('Last installation error: ' + str(state['install_error']))
                print('Type bedrock log to read download diagnostics.')
            if state.get('pid') is not None:
                print('Launcher PID:', state['pid'])
            if state.get('startup_error'):
                print('Startup error:',state['startup_error'])
            config = state.get('server_configuration') or {}
            if config:
                print('World:', config.get('level-name', '(not specified)'),
                      '| Server:', config.get('server-name', '(not specified)'),
                      '| Bedrock UDP IPv4:', config.get('server-port', '19132'),
                      '| IPv6:', config.get('server-portv6', '19133'))
            selected = (state.get('world_selection') or {}).get('selected') or {}
            if selected:
                print('Source world:', selected.get('path', ''))
                level = config.get('level-name', '')
                if level:
                    print('Active server world:', str(__import__('pathlib').Path(state.get('run_root', '')) / 'worlds' / level))

            if state.get('running') and not state.get('network_verified'):
                print('Bedrock VM is alive; LAN connections are not independently verified by this status check.')
                if state.get('guest_nat'):
                    print('macOS LinuxKit uses NAT: LAN discovery/UDP forwarding is not guaranteed.')
                if state.get('console_available') is False:
                    print('Existing launch has no console input; type bedrock restart to recover it explicitly.')
                else:
                    print('Enter minecraft to use the live server console.')
        elif op == 'start':
            state = _api('/api/bedrock/worlds')
            print('Available Bedrock saves:')
            for world in state.get('worlds', []):
                print('  ' + world['name'] + '  [' + world['folder'] + ']')
                print('    ' + world['path'])
            if not state.get('worlds'):
                print('  No saves discovered. Use bedrock path <minecraftWorlds directory>.')
            else:
                print('Run one with: bedrock run <save name>')
            print('Checking for other Minecraft servers on the LAN...')
            scan = _api('/api/bedrock/lan')
            peers = scan.get('servers') or []
            print('Other Minecraft servers visible to this host:')
            if not peers:
                print('  None answered the bounded RakNet UDP discovery probe.')
            for peer in peers:
                print('  ' + peer.get('name', '(unnamed)') + '  ' + peer.get('address', '') + ':' + str(peer.get('port', '')))
            events = scan.get('relay_events') or []
            if events:
                print('Recent LinuxKit relay observations:')
                for event in events[-6:]:
                    print('  ' + event)
            else:
                print('LAN scan is host-side; bridged guest listens directly on its own LAN IP.')
        elif op == 'prompt':
            state = _api('/api/bedrock/status')
            if state.get('installed'):
                print('Bedrock server installed. Type bedrock start, stop or status.')
            else:
                print('Bedrock server not installed.')
                if state.get('installing'):
                    print('Installation in progress:', state.get('install_progress', ''))
                elif state.get('install_error'):
                    print('Installation failed:', state['install_error'])
                    print('Type bedrock install to retry without accepting the EULA again.')
                elif not state.get('eula_accepted'):
                    _api('/api/bedrock/eula', body={})
                    print('Review the Bedrock EULA in the Terminal. Acceptance starts installation.')
                else:
                    print('Type bedrock install to retry the server download.')
        elif op == 'install':
            state = _api('/api/bedrock/status')
            if not state.get('eula_accepted'):
                _api('/api/bedrock/eula', body={})
                print('Review the Minecraft EULA: https://www.minecraft.net/en-us/eula')
                print('Accept the EULA in the WebView to start download and installation.')
            else:
                state = _api('/api/bedrock/install', body={})
                if state.get('installed'):
                    print('[bedrock] Already installed. Type bedrock start.')
                elif state.get('install_error'):
                    print('[bedrock] ' + str(state['install_error']), file=sys.stderr)
                    return 1
                else:
                    print('[bedrock] EULA previously accepted. Installation started. Monitoring live progress (Ctrl-C stops watching only)...', flush=True)
                    return _monitor_bedrock_install()
        else:
            state = _api('/api/bedrock/' + op, body={})
            verb = 'started' if op == 'start' else 'stopped'
            if state.get('running') is (op == 'start'):
                print('[bedrock] ' + ('Launcher started; Minecraft readiness not yet verified.' if op == 'start' else 'Server stopped.'))
            else:
                print('[bedrock] ' + json.dumps(state, sort_keys=True))
        return 0
    print('Unknown custom command. Type help.', file=sys.stderr)
    return 2


def terminal_cli_main(argv: list[str] | None = None) -> int:
    try:
        return execute(list(sys.argv[1:] if argv is None else argv))
    except (RuntimeError, ValueError) as error:
        print('CinterForgeTerminal:', error, file=sys.stderr)
        return 1




# ============================================================================
# REAL LINUX/WINDOWS GUEST SHELL SESSIONS
# ============================================================================
"""On-demand real guest Bash for macOS CinterForgeTerminal Session tabs.

This module is intentionally shipped with the lightweight application; all
SDK binaries and OCI images come from the separately consented DevTools ZIP.
It never falls back to a host Mac shell for a Windows or Linux request.
"""

import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import threading
import time




def sdk_root() -> Path | None:
    """Locate a complete, receipt-backed SDK without network access.

    The path checks are deliberately lightweight. Each archive and installed
    member was SHA-256 verified during Stage; `_image_archive` rehashes the
    selected OCI image before its first Docker load. A stale receipt alone is
    never proof that Windows/Linux Bash can start.
    """
    destination = _devtools_destination(_devtools_release())
    receipt = destination / 'stage-receipt.json'
    if not receipt.is_file() or receipt.is_symlink() or destination.is_symlink():
        return None
    try:
        record = json.loads(receipt.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (record.get('schema') != FOUR_SCHEMA or
            record.get('target') != 'devtools' or
            record.get('release') != _devtools_release()):
        return None
    sdk = destination / 'DevTools/Builds/CinterForgeTerminal/Toolchains'
    needed = ('mac/env/bin/colima', 'mac/env/bin/docker', 'mac/lima/bin/limactl',
              'Windows/MSYS2/usr/bin/bash.exe', 'Windows/MSYS2/usr/bin/msys-2.0.dll')
    if any(not (sdk / name).is_file() or (sdk / name).is_symlink() for name in needed):
        return None
    for kind in ('LinuxSample', 'WindowsCross'):
        directory = sdk / kind
        if not directory.is_dir() or directory.is_symlink():
            return None
        valid = False
        for manifest in directory.glob('*.json'):
            if manifest.is_symlink():
                continue
            try:
                data = json.loads(manifest.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            digest, sha, image = (data.get('dockerfile_sha256'),
                                  data.get('archive_sha256'), data.get('image'))
            # LinuxSample's actual producer stores only the leading 16 hex
            # digits of its Dockerfile hash. WindowsCross stores all 64.
            # Requiring 64 for both rejects valid installed Linux images and
            # causes every Win/Linux Session to demand DevTools again.
            if (isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{16}|[0-9a-f]{64}', digest) and
                    isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{64}', sha) and
                    isinstance(image, str) and image and
                    (directory / (digest[:16] + '.tar')).is_file() and
                    not (directory / (digest[:16] + '.tar')).is_symlink()):
                valid = True
                break
        if not valid:
            return None
    return sdk


def _state_root() -> Path:
    """Short, app-owned VM socket root: Builds in developer mode, Steam at runtime.

    Never create ~/Builds when an installed app invokes optional DevTools.
    The resolved Steam library is inherited from the authenticated PTY host.
    """
    if os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'installed':
        value = os.environ.get('CINTER_STEAMAPPS', '')
        if not value:
            raise RuntimeError('Installed DevTools require the real Steam library')
        steam = Path(value).expanduser()
        if steam.name.lower() != 'steamapps' or not steam.is_dir():
            raise RuntimeError('Installed DevTools require an existing steamapps directory')
        # LIMA_HOME must be short enough for its own nested socket filename.
        # Place the VM profile directly under the selected steamapps root;
        # additional optional DevTools state goes in small named subfolders.
        roots = [steam.resolve(strict=True)]
    else:
        configured = os.environ.get('CINTER_BUILDS_ROOT', '')
        if not configured:
            raise RuntimeError('Developer VM state requires explicit CINTER_BUILDS_ROOT')
        source = Path(configured).expanduser()
        if source.name != 'Builds' or source.is_symlink() or not source.is_dir():
            raise RuntimeError('CINTER_BUILDS_ROOT must identify the existing developer Builds directory')
        # The explicit repository Builds is authoritative. Older developer
        # stages can also contain nested signed .app/Contents/Resources/Builds;
        # those copies are read-only and must not hold VM state. Prefer the
        # shortest *containing* Builds, not a home-directory fallback.
        roots = [parent / 'CFTVM/T' for parent in reversed((source, *source.parents))
                 if parent.name == 'Builds' and parent.is_dir() and not parent.is_symlink()]
    tail = ('/colima-t/ssh.sock.1234567890123456'
            if os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'installed'
            else '/l/colima-t/ssh.sock.1234567890123456')
    for root in roots:
        if len(os.fsencode(str(root) + tail)) < 96:
            if root.is_symlink() or (root.exists() and not root.is_dir()):
                raise RuntimeError('Unsafe managed VM state: ' + str(root))
            root.mkdir(parents=True, exist_ok=True)
            return root
    raise RuntimeError('Managed VM socket root too long under permitted developer Builds / Steam library')


def _env(sdk: Path) -> dict[str, str]:
    root = _state_root()
    for rel in ('h', 'c', 'l', 'cache', 'cache/colima',
                'd', 'config', 'tmp'):
        path = root / rel
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise RuntimeError('Unsafe managed VM directory: ' + str(path))
        path.mkdir(parents=True, exist_ok=True)
    installed = os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'installed'
    lima_home = root if installed else root / 'l'
    socket = lima_home / ('colima-' + PROFILE) / 'ssh.sock.1234567890123456'
    if len(os.fsencode(socket)) >= 100:
        raise RuntimeError('Managed VM socket path too long within the permitted Steam / Builds tree: ' + str(socket))
    env = dict(os.environ)
    for key in ('DOCKER_HOST', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        env.pop(key, None)
    env.update(HOME=str(root/'h'), COLIMA_HOME=str(root/'c'),
               LIMA_HOME=str(lima_home), COLIMA_CACHE_HOME=str(root/'cache/colima'),
               DOCKER_CONFIG=str(root/'d'), XDG_CONFIG_HOME=str(root/'config'),
               XDG_CACHE_HOME=str(root/'cache'), TMPDIR=str(root/'tmp'),
               DOCKER_CONTEXT='colima-' + PROFILE,
               PATH=os.pathsep.join([str(sdk/'mac/lima/bin'), str(sdk/'mac/env/bin'),
                                     os.environ.get('PATH', '')]))
    return env


def _image_archive(sdk: Path, kind: str) -> tuple[Path, str]:
    folder = sdk / ('LinuxSample' if kind == 'linux' else 'WindowsCross')
    receipts = sorted(folder.glob('*.json'))
    for receipt in receipts:
        if receipt.is_symlink():
            continue
        try:
            record = json.loads(receipt.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        digest = record.get('dockerfile_sha256', '')
        name = record.get('image') or (record.get('image_tag') if kind == 'win' else '')
        tar = folder / (str(digest)[:16] + '.tar')
        if not tar.is_file() or tar.is_symlink() or not name or '/' in str(name).split(':')[0]:
            continue
        expected = record.get('archive_sha256')
        if not isinstance(expected, str) or len(expected) != 64:
            continue
        checksum = hashlib.sha256()
        with tar.open('rb') as src:
            for block in iter(lambda: src.read(4 * 1024 * 1024), b''):
                checksum.update(block)
        if checksum.hexdigest() != expected:
            raise RuntimeError('DevTools OCI image differs from its recorded SHA-256: ' + str(tar))
        return tar, str(name)
    raise RuntimeError(f'Unified DevTools has no verified {kind} container image')


def _run_checked(command: list[str], env: dict[str, str], *, title: str, timeout: int = 600) -> None:
    print('[DEVTOOLS] ' + title, flush=True)
    subprocess.run(command, check=True, env=env, timeout=timeout)


def _ensure_docker(sdk: Path, env: dict[str, str]) -> str:
    colima = str(sdk/'mac/env/bin/colima')
    docker = str(sdk/'mac/env/bin/docker')
    ping = subprocess.run([docker, 'info', '--format', '{{.ServerVersion}}'],
                          env=env, check=False, capture_output=True, text=True, timeout=20)
    if ping.returncode:
        print('[DEVTOOLS] Starting managed Terminal shell VM (first launch may provision its disk)', flush=True)
        _run_checked([colima, 'start', PROFILE, '--runtime', 'docker',
                      '--vm-type', 'vz', '--vz-rosetta', '--arch', 'aarch64',
                      '--cpus', '4', '--memory', '8', '--disk', '32',
                      '--mount', str(sdk), '--save-config'], env,
                     title='Booting guest execution environment', timeout=1200)
    _run_checked([docker, 'info', '--format', '{{.ServerVersion}}'], env,
                 title='Checking managed Docker engine', timeout=45)
    return docker


def stop_runtime_vm() -> None:
    """Best-effort app-owned cleanup; never stop either Setup VM (w or c)."""
    if platform.system() != 'Darwin':
        return
    sdk = sdk_root()
    if sdk is None:
        return
    env = _env(sdk)
    subprocess.run([str(sdk/'mac/env/bin/colima'), 'stop', PROFILE], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=False, timeout=35)


def guest_shell_main(kind: str) -> int:
    if kind not in ('win', 'linux') or platform.system() != 'Darwin':
        raise RuntimeError('DevTools shell bridge is only for Windows/Linux tabs on macOS')
    parent = os.getppid()
    def parent_watch() -> None:
        # The native app may crash without calling HTTP server.shutdown().
        # The Session child then owns best-effort VM teardown.
        while os.getppid() == parent:
            time.sleep(2)
        try:
            stop_runtime_vm()
        finally:
            os._exit(0)
    threading.Thread(target=parent_watch, daemon=True, name='devtools-parent-watch').start()
    sdk = sdk_root()
    if sdk is None:
        print(f'{kind.upper()} Bash needs the optional unified DevTools package.', flush=True)
        print('You can decline installation; the existing Mac Session stays open.', flush=True)
        # This existing installer supplies exact bytes, destination, explicit
        # INSTALL confirmation, resumable download and extraction progress.
        if install_devtools() != 0:
            return 1
        sdk = sdk_root()
        if sdk is None:
            raise RuntimeError('DevTools installer completed without a usable installed SDK')
    env = _env(sdk)
    docker = _ensure_docker(sdk, env)
    tar, image = _image_archive(sdk, kind)
    image_check = subprocess.run([docker, 'image', 'inspect', image], check=False, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    if image_check.returncode:
        _run_checked([docker, 'load', '--input', str(tar)], env, title='Loading verified ' + kind + ' environment', timeout=900)
    command = [docker, 'run', '--rm', '-it', '--platform', 'linux/amd64']
    if kind == 'linux':
        _run_checked([docker, 'run', '--rm', '--platform', 'linux/amd64',
                      image, '/bin/bash', '--version'], env,
                     title='Verifying Linux guest Bash before opening Session', timeout=90)
        command.extend([image, '/bin/bash', '--noprofile', '-i'])
    else:
        # The Wine runtime is inside the WindowsCross OCI image; the MSYS2
        # PE executable and DLLs come from the verified unified SDK.
        msys = sdk / 'Windows/MSYS2'
        runtime_args = ['--mount', 'type=bind,src='+str(msys)+',dst=/msys2,readonly',
                        '-e', 'MSYSTEM=UCRT64', '-e', 'HOME=/tmp']
        guest_executable = r'Z:\msys2\usr\bin\bash.exe'
        _run_checked([docker, 'run', '--rm', '--platform', 'linux/amd64',
                      *runtime_args, image, 'wine', guest_executable, '--version'], env,
                     title='Verifying Windows MSYS2 Bash under Wine before opening Session', timeout=150)
        command.extend([*runtime_args, image, 'wine', guest_executable, '--noprofile', '-i'])
    print('[DEVTOOLS] Verified backend. Opening actual '+('Linux/amd64 Bash' if kind == 'linux' else 'Windows MSYS2 Bash under Wine'), flush=True)
    process = subprocess.Popen(command, env=env)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return process.wait(timeout=10)






if __name__ == '__main__':
    raise SystemExit(main())
