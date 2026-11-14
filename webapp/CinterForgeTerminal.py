"""CinterForgeTerminal — portable Python backend for the native/web UI.

PUBLIC ENTRY POINTS (see ``main()`` immediately below configuration):
    no mode           Staged HTTP bootstrap/smoke service
    --terminal-cli    Human-facing text commands, game catalog, install actions
    --guest-shell     Legacy helper (guest Bash lives exclusively in Code-OSS)
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
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import unquote, urlsplit, parse_qs
import importlib.util
import ipaddress
import ctypes
import socket
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
    '/startup.js': ('startup.js', 'text/javascript; charset=utf-8'),
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
    --guest-shell: legacy guest status helper; never starts a duplicate Bash.
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
    if arguments and arguments[0] == '--runtime-watchdog':
        return _runtime_watchdog_main(arguments[1:])
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
    """Author shell bindings in developer Builds or installed runtime cache."""
    directory = root / 'scripts'
    directory.mkdir(parents=True, exist_ok=True)
    cli = ASSETS / 'CinterForgeTerminal.py'
    if not cli.is_file():
        raise RuntimeError(f'Missing staged CinterForgeTerminal command helper: {cli}')
    python = str(Path(sys.executable).resolve())
    functions = '\n'.join(
        f'{name}() {{ {shlex.quote(python)} {shlex.quote(str(cli))} --terminal-cli {name} "$@"; }}'
        for name in ('help', 'debugger', 'bedrock', 'minecraft', 'lagomorph', 'infinite-mac', 'wargames', 'discord', 'setpass', 'code', 'htmlmenus'))
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
                + "PS1='\\u@\\h \\w \\$ '\n"
                + "HISTIGNORE='setpass *'\n"  # Do not retain credentials in Bash history.
                + "printf 'Welcome to CinterForge. Type help for instructions.\\n'\n")

    (directory / 'terminal-init.sh').write_text(preamble + functions + '\n', encoding='utf-8')
    return directory


def _real_login_home() -> Path:
    """Get the real account home, never the isolated shell/Colima HOME."""
    configured = os.environ.get('CINTER_USER_HOME', '').strip()
    if configured:
        home = Path(configured).expanduser()
    elif os.name == 'nt':
        home = Path(os.environ.get('USERPROFILE', str(Path.home())))
    else:
        try:
            import pwd
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            home = Path.home()
    if not home.is_absolute() or not home.is_dir():
        raise RuntimeError('Installed Terminal requires the real account home for runtime cache')
    return home.resolve(strict=True)


def _installed_cache_root(home: Path) -> Path:
    """Transient runtime files live in the OS cache, NEVER in steamapps.

    Steam stores applications and persistent player configuration/history;
    Setup/developer intermediates stay in sibling Builds.  This cache is not
    an installer or a duplicate of the downloaded DevTools bundle.
    """
    system = platform.system()
    if system == 'Darwin':
        path = home / 'Library/Caches/CinterForgeTerminal'
    elif system == 'Windows':
        path = home / 'AppData/Local/CinterForgeTerminal/Cache'
    else:
        path = home / '.cache/CinterForgeTerminal'
    # An OS cache can be redirected by a user-controlled symlink. Never let
    # that redirect our multi-GB VM or download scratch into a Steam library.
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise RuntimeError('Unsafe installed Terminal cache: ' + str(path))
    resolved = path.resolve()
    if any(item.name.casefold() == 'steamapps' for item in (resolved, *resolved.parents)):
        raise RuntimeError('Installed Terminal cache must not resolve inside steamapps: ' + str(path))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_environment(builds: Path, system: str) -> dict[str, str]:
    """Keep persistent user data in Steam, transient runtime data out of it."""
    installed = (builds.name != 'Builds' and
                 _steam_library_for_installed_root(builds) is not None)
    user_home = _real_login_home()
    root = builds / 'Run' / 'CinterForgeTerminal' / system
    scratch = ((_installed_cache_root(user_home) / 'Sessions' / system)
               if installed else root)
    paths = {'home': root / 'home', 'config': root / 'config',
             'data': root / 'data', 'tmp': scratch / 'tmp',
             'cache': scratch / 'cache'}
    for path in paths.values():
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise RuntimeError('Unsafe Terminal session state: ' + str(path))
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['CINTER_USER_HOME'] = str(user_home)
    env['CINTER_TERMINAL_SCRATCH_ROOT'] = str(scratch)
    # Guest shells have isolated HOME, but Mac Bash must retain the real
    # login HOME. Host signing, Keychain preferences, and normal ./Cinter
    # publishing must work from the integrated terminal without workaround.
    # Temp/cache/config/history still use the managed paths below.
    # Preserve the actual installed Steam library independently of HOME.
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
    env.update({'HOME': str(user_home if system == 'mac' else paths['home']),
                'TMPDIR': str(paths['tmp']),
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
                 output_log: Path | None = None, internal_token: str | None = None,
                 child_env: dict[str, str] | None = None):
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
        # The local command helper can call the host API after setpass without
        # a browser cookie. This secret is never embedded in served HTML/JS.
        env.pop('CINTER_TERMINAL_CLI_TOKEN', None)
        if internal_token:
            env['CINTER_TERMINAL_CLI_TOKEN'] = internal_token
        env['CINTER_TERMINAL_SESSION_ID'] = self.id
        # Preserve the existing persistent session working directory in Steam.
        # Only generated shell bindings and disposable files use OS cache.
        self.root = builds / 'Run' / 'CinterForgeTerminal' / system
        env['CINTER_TARGET_PLATFORM'] = system
        if system == 'win':
            msys = _windows_msys2_root(builds)
            env['MSYSTEM'] = 'UCRT64'
            env['CHERE_INVOKING'] = '1'
            env['MSYS2_PATH_TYPE'] = 'inherit'
            env['PATH'] = os.pathsep.join([str(msys / 'ucrt64/bin'),
                                            str(msys / 'usr/bin'), env.get('PATH', '')])
        if working_directory is not None:
            env['CINTER_CODE_WORKSPACE'] = str(working_directory)
        scripts = _install_shell_commands(Path(env['CINTER_TERMINAL_SCRATCH_ROOT']), system, env)
        self.shell = command if command is not None else _shell_command(system, scripts, builds)
        if child_env:
            # Only the app-owned Docker subprocess needs this Colima context.
            # The host shell and its authenticated CLI token are unchanged.
            env.update(child_env)
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


def _devtools_menu_ready(system: str) -> bool:
    """Read-only host capability check; malformed/missing SDK never blocks the UI."""
    if system != 'mac':
        return False
    try:
        return sdk_root() is not None
    except (OSError, ValueError, RuntimeError):
        return False


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


LAN_HTTP_PORT = 20300
LAN_LOGIN_FILENAME = 'CinterForgeTerminal/Config/lan-login.json'
LAN_LOGIN_ITERATIONS = 200_000
LAN_LOGIN_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')


def _lan_login_path(writable_root: Path) -> Path:
    """Store login state only under developer Builds or installed Steam state."""
    return writable_root / LAN_LOGIN_FILENAME


def _load_lan_login(writable_root: Path) -> dict | None:
    """Read configured credentials; never generate or reveal a password."""
    path = _lan_login_path(writable_root)
    if path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise RuntimeError('Unsafe Terminal LAN login directory')
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise RuntimeError('Unsafe Terminal LAN login state')
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if (record.get('schema') != 'cinterforge.terminal.lan-login.v1' or
                not LAN_LOGIN_NAME.fullmatch(record['username']) or
                not re.fullmatch(r'[0-9a-f]{32}', record['salt']) or
                not re.fullmatch(r'[0-9a-f]{64}', record['digest']) or
                record['iterations'] != LAN_LOGIN_ITERATIONS):
            raise ValueError('invalid state')
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError('Invalid Terminal LAN login state; use setpass to replace it') from error
    return record


def _save_lan_login(writable_root: Path, username: str, password: str) -> dict:
    """Atomically replace the credential record; keep password out of files and logs."""
    if not isinstance(username, str) or not LAN_LOGIN_NAME.fullmatch(username):
        raise ValueError('Username must be 1-64 letters, digits, dots, dashes or underscores')
    if not isinstance(password, str) or not 1 <= len(password) <= 256 or '\x00' in password:
        raise ValueError('Password must contain 1-256 characters')
    directory = _lan_login_path(writable_root).parent
    parent = directory.parent
    for part in (parent, directory):
        if part.is_symlink() or (part.exists() and not part.is_dir()):
            raise RuntimeError('Unsafe Terminal LAN login directory')
        part.mkdir(exist_ok=True)
    path = _lan_login_path(writable_root)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError('Unsafe Terminal LAN login state file')
    salt = secrets.token_bytes(16)
    record = {'schema': 'cinterforge.terminal.lan-login.v1', 'username': username,
              'salt': salt.hex(), 'iterations': LAN_LOGIN_ITERATIONS,
              'digest': hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt,
                                            LAN_LOGIN_ITERATIONS).hex()}
    temporary = directory / ('lan-login-' + uuid.uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
            json.dump(record, file, separators=(',', ':'))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def _check_lan_login(record: dict | None, username: str, password: str) -> bool:
    """Compare username and salted password hash without echoing credentials."""
    if not record or not isinstance(username, str) or not isinstance(password, str):
        return False
    if len(username) > 64 or len(password) > 256:
        return False
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                bytes.fromhex(record['salt']), record['iterations'])
    return (secrets.compare_digest(username, record['username']) and
            secrets.compare_digest(digest, bytes.fromhex(record['digest'])))


# A private receipt is used only to recognize/reuse this user's *own* running
# Terminal HTTP service. It lives in the existing Builds/Steam runtime state;
# it is not a port lock, user password, or publicly advertised credential.
LAN_OWNER_FILENAME = 'CinterForgeTerminal/Config/http-owner.json'


def _lan_owner_path(writable_root: Path) -> Path:
    """Location of the current Terminal HTTP process receipt."""
    return writable_root / LAN_OWNER_FILENAME


def _write_lan_owner(server: 'CinterShellServer') -> None:
    """Atomically publish a private startup grant after port 20300 is bound."""
    path = _lan_owner_path(server.mutable_builds)
    if path.parent.is_symlink() or path.is_symlink():
        raise RuntimeError('Unsafe Terminal HTTP owner receipt')
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {'schema': 'cinterforge.terminal.http-owner.v1',
               'port': server.server_port, 'instance': server.lan_id,
               'native_grant': server.native_grant, 'pid': os.getpid(),
               'native_pid': os.getppid()}
    temporary = path.parent / ('http-owner-' + uuid.uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(receipt, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_lan_owner(server: 'CinterShellServer') -> None:
    """Remove only our own receipt, not that of a replacement service."""
    path = _lan_owner_path(server.mutable_builds)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2048:
            return
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('instance') == server.lan_id:
            path.unlink()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


def _existing_lan_owner(builds: Path, *, orphaned: bool = False) -> dict | None:
    """Find a verified Terminal listener, distinguishing active from orphaned owners.

    A developer build uses Builds for runtime state, while an installed app
    uses the selected Steam library. Both may start the *same* account's
    Terminal on port 20300. Check their existing receipts before deciding the
    occupied listener is foreign. Do not create folders, trust an unverified
    health response, or stop a process that we cannot identify.
    """
    if LAN_HTTP_PORT <= 0:
        return None  # In-process smoke fixtures use a randomly assigned port.

    roots = [_terminal_mutable_root(builds)]
    # Steam library discovery is already part of the Host runtime contract.
    # A second native launch may be installed even if the first came from the
    # developer's sibling Builds (or vice versa). Only inspect existing roots.
    for steam in _runtime_steam_locations():
        state = steam / 'common/CinterForge/Applications/TerminalState'
        if state.is_dir() and not state.is_symlink() and state not in roots:
            roots.append(state)

    from urllib.request import Request, urlopen
    for root in roots:
        path = _lan_owner_path(root)
        try:
            if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > 2048 or (os.name != 'nt' and path.stat().st_mode & 0o077):
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            if (record.get('schema') != 'cinterforge.terminal.http-owner.v1' or
                    record.get('port') != LAN_HTTP_PORT or
                    not re.fullmatch(r'[0-9a-f]{32}', str(record.get('instance', ''))) or
                    not re.fullmatch(r'[A-Za-z0-9_-]{40,128}', str(record.get('native_grant', '')))):
                continue
            # Do not mistake a stale receipt (or a reused PID) for authority.
            # Only a live service that proves its private grant AND instance
            # can be reused or asked to release its own listener.
            native_pid = record.get('native_pid')
            if type(native_pid) is not int or native_pid <= 1:
                continue
            try:
                os.kill(native_pid, 0)
                native_alive = True
            except ProcessLookupError:
                native_alive = False
            except PermissionError:
                continue
            if native_alive == orphaned:
                continue
            request = Request(f'http://127.0.0.1:{LAN_HTTP_PORT}/healthz',
                              headers={'X-Cinter-Owner': record['native_grant']})
            with urlopen(request, timeout=1.5) as response:
                status = json.loads(response.read(2048))
            if status.get('ready') and status.get('instance') == record['instance']:
                return record
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return None


def _request_orphaned_lan_shutdown(builds: Path) -> bool:
    """Ask a *verified* leftover Terminal HTTP worker to release port 20300.

    Never kill by port/PID. A 0600 owner receipt, challenge-response over
    loopback, dead native owner, and the server's own orphan check are all
    required. Foreign listeners and healthy Terminal windows remain untouched.
    """
    record = _existing_lan_owner(builds, orphaned=True)
    if record is None:
        return False
    from urllib.request import Request, urlopen
    request = Request(f'http://127.0.0.1:{LAN_HTTP_PORT}/_native_orphan_shutdown',
                      data=b'{}', method='POST', headers={
                          'Content-Type': 'application/json',
                          'X-Cinter-Owner': record['native_grant'],
                          'X-Cinter-Instance': record['instance']})
    try:
        with urlopen(request, timeout=2) as response:
            result = json.loads(response.read(2048))
        return result.get('stopping') is True and result.get('instance') == record['instance']
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


CINTERFORGE_PORT_FIRST = 20300
CINTERFORGE_PORT_LAST = 20399
DISCOVERY_TYPE = b'_http._tcp.local.'
# An app's port is its product identity. A discovered service additionally
# advertises suite/app/path in standard DNS-SD TXT records.
CINTERFORGE_SERVICE_PORTS = {20300: 'terminal', 20310: 'converter',
                             20320: 'os', 20380: 'webapp'}


class _DNSService(ctypes.Structure):
    """Exact CinterZeroconf.h callback ABI; callback strings expire on return."""
    _fields_ = [('name', ctypes.c_char_p), ('type', ctypes.c_char_p),
                ('host', ctypes.c_char_p), ('address', ctypes.c_char_p),
                ('port', ctypes.c_uint16), ('txt', ctypes.c_char_p)]


# The native Host already ships this C API alongside its Python worker. Reuse
# it instead of implementing or packaging a second Bonjour/mDNS responder.
_DNSCallback = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.POINTER(_DNSService), ctypes.c_void_p)


class CinterForgeDiscovery:
    """Advertise Terminal and browse *all* CinterForge HTTP services via DNS-SD.

    The CinterForge shared Host library owns UDP 5353, PTR/SRV/TXT/A messages,
    TTLs and goodbye announcements. Python owns the live HTTP service catalog.
    No additional discovery port or external daemon is involved.
    """

    def __init__(self, root: Path, server_name: str, port: int, log):
        self.log = log
        self.port = port
        self.local_name = 'CinterForge Terminal (' + server_name.split('.')[0][:24] + ')'
        self.entries = {}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None
        self.handle = None
        self.library = None
        self.callback = None
        filename = ('CinterForgePythonHost.dll' if os.name == 'nt' else
                    'libCinterForgePythonHost.dylib' if sys.platform == 'darwin' else
                    'libCinterForgePythonHost.so')
        library_path = root / ('bin' if os.name == 'nt' else 'lib') / filename
        if not library_path.is_file():
            raise RuntimeError('Staged Zeroconf library missing: ' + str(library_path))
        lib = ctypes.CDLL(str(library_path))
        lib.czc_api_version.argtypes = []
        lib.czc_api_version.restype = ctypes.c_uint32
        if lib.czc_api_version() != 2:
            raise RuntimeError('Staged Zeroconf API v2 required; rebuild Host')
        lib.czc_create.argtypes = []
        lib.czc_create.restype = ctypes.c_void_p
        lib.czc_destroy.argtypes = [ctypes.c_void_p]
        lib.czc_publish_txt.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                        ctypes.c_uint16, ctypes.c_char_p]
        lib.czc_publish_txt.restype = ctypes.c_int
        lib.czc_browse.argtypes = [ctypes.c_void_p, ctypes.c_char_p, _DNSCallback,
                                   ctypes.c_void_p]
        lib.czc_browse.restype = ctypes.c_int
        lib.czc_poll.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.czc_poll.restype = ctypes.c_int
        lib.czc_last_error.argtypes = [ctypes.c_void_p]
        lib.czc_last_error.restype = ctypes.c_char_p
        lib.czc_last_create_error.argtypes = []
        lib.czc_last_create_error.restype = ctypes.c_char_p
        ctx = lib.czc_create()
        if not ctx:
            raise RuntimeError('Zeroconf initialization failed: ' +
                               (lib.czc_last_create_error() or b'unknown').decode('utf-8', 'replace'))
        self.library, self.handle = lib, ctx
        try:
            self.callback = _DNSCallback(self._on_service)
            name = self.local_name.encode('utf-8')
            metadata = b'suite=cinterforge\napp=terminal\npath=/'
            self._require(lib.czc_publish_txt(ctx, name, DISCOVERY_TYPE, port, metadata))
            self._require(lib.czc_browse(ctx, DISCOVERY_TYPE, self.callback, None))
            self.thread = threading.Thread(target=self._run, name='cinter-zeroconf', daemon=True)
            self.thread.start()
        except Exception:
            lib.czc_destroy(ctx)
            self.handle = None
            raise

    def _require(self, result):
        if result != 0:
            raise RuntimeError((self.library.czc_last_error(self.handle) or
                                b'Zeroconf operation failed').decode('utf-8', 'replace'))

    def _on_service(self, event, pointer, _data):
        if not pointer:
            return
        item = pointer.contents
        def decoded(value):
            return (value or b'').decode('utf-8', 'replace')
        name, address = decoded(item.name), decoded(item.address)
        port = int(item.port)
        # Ignore unrelated HTTP advertisements. A CinterForge app can be
        # recognized via TXT suite=cinterforge or its public instance prefix.
        properties = {}
        for line in decoded(item.txt).split('\n'):
            key, separator, value = line.partition('=')
            if separator:
                properties[key.casefold()] = value
        if not (properties.get('suite', '').casefold() == 'cinterforge' or
                name.casefold().startswith('cinterforge ')):
            return
        if not (CINTERFORGE_PORT_FIRST <= port <= CINTERFORGE_PORT_LAST):
            return
        try:
            ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError:
            return
        if properties.get('path', '/') != '/':
            # Services may supply a public URL path, but never scripts or query.
            path = properties['path']
            if not re.fullmatch(r'/[a-zA-Z0-9/_-]{0,200}', path):
                return
        else:
            path = '/'
        app = properties.get('app', CINTERFORGE_SERVICE_PORTS.get(port, 'web'))
        key = (name.casefold(), address, port)
        with self.lock:
            if event == 3:  # CZC_REMOVED (goodbye / TTL expired)
                self.entries.pop(key, None)
            elif event in (1, 2):
                self.entries[key] = {'name': name[:100], 'app': app[:30],
                                     'platform': decoded(item.host)[:100],
                                     'host': decoded(item.host)[:100],
                                     'port': port, 'url': f'http://{address}:{port}{path}'}

    def _run(self):
        try:
            while not self.stopped.is_set():
                if self.library.czc_poll(self.handle, 200) < 0:
                    self.log('Zeroconf polling failed: ' +
                             (self.library.czc_last_error(self.handle) or b'unknown').decode('utf-8', 'replace'))
                    return
        except Exception as error:
            self.log('Zeroconf stopped: ' + str(error))

    def peers(self):
        with self.lock:
            return sorted((dict(peer) for peer in self.entries.values()
                           if not (peer['port'] == self.port and
                                   peer['name'] == self.local_name)),
                          key=lambda peer: (peer['app'], peer['name'], peer['url']))

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join()  # Poll is bounded to 200 ms; never free a live context.
        if self.handle:
            self.library.czc_destroy(self.handle)
            self.handle = None


def _discord_provider() -> str:
    """Read the existing external discord_dev provider; never ship credentials."""
    explicit = os.environ.get('CINTERFORGE_CONFIG', '').strip()
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        try:
            import pwd
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError):
            home = Path(os.environ.get('USERPROFILE') or Path.home())
        candidates = [home / 'cfdev/config.json', home / 'dev/config.json']
    for path in candidates:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        provider = config.get('discord_dev')
        if not isinstance(provider, dict):
            provider = config.get('providers', {}).get('discord_dev')
        if not isinstance(provider, dict):
            continue
        env = provider.get('env', {})
        if not isinstance(env, dict):
            env = {}
        enabled = os.environ.get('CINTERFORGE_DISCORD_DEV_ENABLED',
                  env.get('CINTERFORGE_DISCORD_DEV_ENABLED', '1' if provider.get('enabled') else '0'))
        if str(enabled).lower() not in ('1', 'true', 'yes'):
            raise RuntimeError('Discord Dev is disabled in the existing external config.json')
        token = os.environ.get('CINTERFORGE_DISCORD_DEV_BOT_TOKEN') or env.get('CINTERFORGE_DISCORD_DEV_BOT_TOKEN')
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError('Discord Dev Bot token is missing from the external provider')
        return token.strip()
    raise RuntimeError('Discord Dev provider not found in the existing external config.json')


def _discord_api(path: str, method: str = 'GET', payload: dict | None = None):
    """Discord REST runs only in the trusted Python service, never in the page."""
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    token = _discord_provider()
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = Request('https://discord.com/api/v10' + path, method=method, data=data,
                      headers={'Authorization': 'Bot ' + token,
                               'User-Agent': 'CinterForgeTerminal (development tool)',
                               'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise RuntimeError('Discord response too large')
            return json.loads(raw) if raw else {}
    except HTTPError as error:
        # Never echo Authorization; Discord's status is sufficient to diagnose.
        raise RuntimeError('Discord API HTTP ' + str(error.code) + ' (verify bot membership, scopes and channel permissions)') from None
    except (URLError, TimeoutError) as error:
        raise RuntimeError('Discord connection failed: ' + type(error).__name__) from None


def _discord_resolve(guild_name: str, channel_name: str) -> dict:
    if not (0 < len(guild_name) <= 120 and 0 < len(channel_name) <= 120):
        raise ValueError('Discord guild and channel names required')
    identity = _discord_api('/users/@me')
    guilds = _discord_api('/users/@me/guilds')
    if not isinstance(guilds, list):
        raise RuntimeError('Discord did not return bot guilds')
    guild = next((g for g in guilds if g.get('name', '').casefold() == guild_name.casefold()), None)
    if not guild or not str(guild.get('id', '')).isdigit():
        raise RuntimeError('Discord bot is not a member of server: ' + guild_name)
    channels = _discord_api('/guilds/' + guild['id'] + '/channels')
    if not isinstance(channels, list):
        raise RuntimeError('Discord did not return channels')
    channel = next((c for c in channels if c.get('name', '').casefold() == channel_name.casefold()
                    and c.get('type') in (0, 5)), None)
    if not channel or not str(channel.get('id', '')).isdigit():
        raise RuntimeError('Discord bot cannot see text channel: ' + channel_name)
    return {'guild': guild.get('name', guild_name), 'channel': channel.get('name', channel_name),
            'channel_id': channel['id'],
            'bot': identity.get('global_name') or identity.get('username') or 'Discord bot'}


class CodeWorkspace:
    """The selected host folder or .code-workspace, mounted at cinterforge:/.

    A single-folder workspace maps the virtual root directly to that directory.
    Multi-root .code-workspace files expose each named folder below the virtual
    root, using the existing Code-OSS filesystem provider. Only explicit
    workspace selections can change the roots; file operations stay inside them.
    State belongs to the developer Builds or installed TerminalState tree.
    """

    MAX_FILE_BYTES = 8 * 1024 * 1024

    def __init__(self, writable_root: Path):
        self.lock = threading.RLock()
        self.state_file = writable_root / 'CinterForgeTerminal/CodeOSS/workspace.json'
        self.home = self._user_home()
        default = self.home / 'Source'
        if not default.is_dir():
            default = self.home / 'Documents'
        if not default.is_dir():
            default = self.home
        self.selection = default
        self.kind = 'folder'
        self.theme = 'dark'
        self.roots = [(default.name or 'Home', default)]
        # The Host attaches its existing Builds/Steam Logs directory after startup.
        # Logs are readable/revealable without adding them to the editor workspace.
        self.host_log_roots = []
        self.revision = 0
        if self.state_file.is_file() and not self.state_file.is_symlink():
            try:
                state = json.loads(self.state_file.read_text('utf-8'))
                self.theme = state.get('theme') if state.get('theme') in ('dark', 'light') else 'dark'
                self._select(str(state['path']), str(state.get('kind', 'folder')))
            except (ValueError, OSError, KeyError, TypeError):
                # A stale/moved workspace must not keep the editor from starting.
                self.selection = default
                self.kind = 'folder'
                self.roots = [(default.name or 'Home', default)]

    @staticmethod
    def _user_home() -> Path:
        provided = os.environ.get('CINTER_USER_HOME')
        if provided:
            return Path(provided).expanduser().resolve()
        if os.name == 'nt':
            return Path(os.environ.get('USERPROFILE') or Path.home()).resolve()
        try:
            import pwd
            return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        except (ImportError, KeyError, OSError):
            return Path.home().resolve()

    def _select(self, raw: str, kind: str) -> None:
        if kind not in ('folder', 'workspace') or not raw or '\x00' in raw:
            raise ValueError('Open an existing folder or .code-workspace file')
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError('Provide an absolute host filesystem path')
        candidate = candidate.resolve(strict=True)
        if kind == 'folder':
            if not candidate.is_dir():
                raise ValueError('Selected folder does not exist')
            roots = [(candidate.name or 'Home', candidate)]
        else:
            if not candidate.is_file() or candidate.suffix != '.code-workspace':
                raise ValueError('Select an existing .code-workspace file')
            if candidate.stat().st_size > 256 * 1024:
                raise ValueError('Workspace definition is too large')
            workspace = json.loads(candidate.read_text('utf-8'))
            folders = workspace.get('folders')
            if not isinstance(folders, list) or not 1 <= len(folders) <= 32:
                raise ValueError('Workspace must contain 1–32 local folders')
            roots = []
            used = set()
            for folder in folders:
                if not isinstance(folder, dict) or not isinstance(folder.get('path'), str):
                    raise ValueError('Workspace must contain local path folders')
                target = Path(folder['path']).expanduser()
                if not target.is_absolute():
                    target = candidate.parent / target
                target = target.resolve(strict=True)
                if not target.is_dir():
                    raise ValueError('Workspace folder does not exist: ' + str(target))
                label = folder.get('name') or target.name
                if not isinstance(label, str) or not label or '/' in label or '\\' in label or label in ('.', '..') or label in used:
                    raise ValueError('Workspace folder names must be unique and path-safe')
                used.add(label)
                roots.append((label, target))
        self.selection, self.kind, self.roots = candidate, kind, roots
        self.revision += 1

    def persist(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if self.state_file.is_symlink():
            raise ValueError('Unsafe Code-OSS state file')
        pending = self.state_file.with_name('workspace-' + uuid.uuid4().hex + '.tmp')
        try:
            pending.write_text(json.dumps({'path': str(self.selection), 'kind': self.kind,
                                           'theme': self.theme}, indent=2) + '\n', 'utf-8')
            os.replace(pending, self.state_file)
        finally:
            pending.unlink(missing_ok=True)

    def details(self) -> dict:
        with self.lock:
            return {'kind': 'terminal', 'platform': target_platform(),
                    'workspace_kind': self.kind, 'path': str(self.selection),
                    'folders': [{'name': name, 'path': str(path)} for name, path in self.roots],
                    'cwd': str(self.roots[0][1]), 'theme': self.theme}

    def _path(self, uri_path: str) -> Path | None:
        if not isinstance(uri_path, str) or not uri_path.startswith('/') or '\x00' in uri_path:
            raise ValueError('Invalid Code-OSS workspace path')
        segments = uri_path.split('/')[1:]
        if any(part in ('.', '..') or '\\' in part for part in segments):
            raise ValueError('Unsafe workspace traversal')
        if self.kind == 'folder':
            root, rest = self.roots[0][1], segments
        else:
            if not segments or not segments[0]:
                return None  # The virtual multi-root directory itself.
            matched = next((root for name, root in self.roots if name == segments[0]), None)
            if matched is None:
                raise FileNotFoundError('No such workspace root')
            root, rest = matched, segments[1:]
        target = root.joinpath(*rest).resolve()
        if not target.is_relative_to(root):
            raise PermissionError('File is outside the selected workspace')
        return target

    def _host_path(self, raw: str) -> Path:
        """Resolve host files only inside the selected workspace or existing Logs roots.

        The browser's cinterforge-host: URI names an actual host path. Do not
        treat that virtual URL as permission to read files outside these roots.
        """
        if not isinstance(raw, str) or not raw or '\x00' in raw:
            raise ValueError('Invalid host file path')
        if os.name == 'nt' and re.match(r'^/[A-Za-z]:/', raw):
            raw = raw[1:]
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise ValueError('Host file path must be absolute')
        target = candidate.resolve(strict=True)
        roots = [root for _, root in self.roots] + self.host_log_roots
        if not any(target.is_relative_to(root.resolve()) for root in roots):
            raise PermissionError('Host file is outside the workspace and Logs')
        return target

    @staticmethod
    def _reveal_host_file(path: Path) -> None:
        """Show the actual host folder, selecting files when the OS supports it."""
        if sys.platform == 'darwin':
            command = ['open', str(path)] if path.is_dir() else ['open', '-R', str(path)]
        elif os.name == 'nt':
            command = (['explorer.exe', str(path)] if path.is_dir()
                       else ['explorer.exe', '/select,', str(path)])
        else:
            command = ['xdg-open', str(path if path.is_dir() else path.parent)]
        subprocess.Popen(command, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def operation(self, body: dict) -> dict:
        operation = body.get('operation')
        with self.lock:
            if operation in ('host-stat', 'host-read-file', 'host-read-directory',
                             'reveal-host', 'reveal-workspace'):
                raw = body.get('path', '')
                target = (self._path(str(raw)) if operation == 'reveal-workspace'
                          else self._host_path(raw))
                if target is None:
                    target = self.selection if self.kind == 'workspace' else self.roots[0][1]
                if operation.startswith('reveal-'):
                    self._reveal_host_file(target)
                    return {'revealed': str(target)}
                if operation == 'host-stat':
                    info = target.stat()
                    return {'type': 'directory' if target.is_dir() else 'file',
                            'size': info.st_size, 'mtime': int(info.st_mtime * 1000)}
                if operation == 'host-read-directory':
                    if not target.is_dir():
                        raise NotADirectoryError('Not a host directory')
                    entries = []
                    for item in sorted(target.iterdir(), key=lambda entry: entry.name.casefold()):
                        if len(entries) >= 10000:
                            break
                        try:
                            checked = self._host_path(str(item))
                        except (ValueError, PermissionError, OSError):
                            continue
                        entries.append({'name': item.name,
                                        'type': 'directory' if checked.is_dir() else 'file'})
                    return {'entries': entries}
                if not target.is_file() or target.stat().st_size > self.MAX_FILE_BYTES:
                    raise ValueError('Host log/file is not readable or exceeds 8 MiB')
                return {'data': base64.b64encode(target.read_bytes()).decode('ascii')}
            if operation == 'workspace-state':
                return {'revision': str(self.revision), **self.details()}
            if operation in ('open-folder', 'open-workspace'):
                self._select(str(body.get('path', '')), 'folder' if operation == 'open-folder' else 'workspace')
                self.persist()
                return self.details()
            if operation == 'set-theme':
                theme = body.get('theme')
                if theme not in ('dark', 'light'):
                    raise ValueError('Theme must be dark or light')
                self.theme = theme
                self.persist()
                return {'theme': self.theme}
            raw = str(body.get('path', '/'))
            target = self._path(raw)
            if operation == 'stat':
                if target is None:
                    return {'type': 'directory', 'size': 0, 'mtime': 0}
                info = target.stat()
                return {'type': 'directory' if target.is_dir() else 'file',
                        'size': info.st_size, 'mtime': int(info.st_mtime * 1000)}
            if operation == 'read-directory':
                if target is None:
                    return {'entries': [{'name': label, 'type': 'directory'} for label, _ in self.roots]}
                if not target.exists():
                    raise FileNotFoundError('Workspace directory does not exist')
                if not target.is_dir():
                    raise NotADirectoryError('Not a workspace directory')
                entries = []
                for item in sorted(target.iterdir(), key=lambda p: p.name.casefold()):
                    if len(entries) >= 10000:
                        break
                    # A symlink escaping this workspace cannot be followed.
                    try:
                        self._path(raw.rstrip('/') + '/' + item.name)
                    except (ValueError, PermissionError, OSError):
                        continue
                    entries.append({'name': item.name, 'type': 'directory' if item.is_dir() else 'file'})
                return {'entries': entries}
            if target is None or any(target == root for _, root in self.roots):
                raise ValueError('Cannot modify the workspace root')
            if operation == 'read-file':
                if not target.exists():
                    raise FileNotFoundError('Workspace file does not exist')
                if not target.is_file() or target.stat().st_size > self.MAX_FILE_BYTES:
                    raise ValueError('File is not readable or is over 8 MiB')
                return {'data': base64.b64encode(target.read_bytes()).decode('ascii')}
            if operation == 'write-file':
                raw_data = body.get('data', '')
                if not isinstance(raw_data, str) or len(raw_data) > self.MAX_FILE_BYTES * 2:
                    raise ValueError('File is too large')
                data = base64.b64decode(raw_data, validate=True)
                if len(data) > self.MAX_FILE_BYTES:
                    raise ValueError('File is over 8 MiB')
                if target.exists() and (target.is_dir() or body.get('overwrite') is False):
                    raise FileExistsError('File already exists')
                if not target.exists() and not body.get('create', False):
                    raise FileNotFoundError('File does not exist')
                target.write_bytes(data)
            elif operation == 'create-directory':
                target.mkdir()
            elif operation == 'delete':
                if target.is_dir():
                    if body.get('recursive'):
                        shutil.rmtree(target)
                    else:
                        target.rmdir()
                else:
                    target.unlink()
            elif operation == 'rename':
                destination = self._path(str(body.get('target', '')))
                if destination is None or any(destination == root for _, root in self.roots):
                    raise ValueError('Cannot replace the workspace root')
                if destination.exists() and not body.get('overwrite'):
                    raise FileExistsError('Destination already exists')
                if destination.exists() and destination.is_dir():
                    raise ValueError('Cannot overwrite a directory')
                target.replace(destination)
            else:
                raise ValueError('Unknown Code-OSS workspace operation')
            self.revision += 1
            return {'ok': True}


class CinterShellServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, builds: Path, *, lan: bool = False):
        self.lan = lan
        self.lan_id = uuid.uuid4().hex
        self.discovery = None
        self.builds = builds
        self.mutable_builds = _terminal_mutable_root(builds)
        self.code_workspace = CodeWorkspace(self.mutable_builds)
        self.code_sessions = {}  # Code-OSS terminal name -> real host Session or Server Log cursor
        # App-owned Bash PTYs survive Code-OSS tab close/reopen. Exactly one
        # Linux and one Windows process; only this owner may close them.
        self.guest_shells: dict[str, TerminalSession] = {}
        self.guest_shells_lock = threading.RLock()
        self.code_sessions_lock = threading.Lock()
        self.code_closed_owners = set()  # Late worker requests cannot revive a closed CodeOSS tab.
        self.lan_login_lock = threading.Lock()
        self.lan_login = _load_lan_login(self.mutable_builds)
        self.lan_cookie = secrets.token_urlsafe(36)
        # Only the local native launcher receives this grant via its private
        # RPC pipe; normal browser pages must use the configured setpass login.
        self.native_grant = secrets.token_urlsafe(48)
        self.native_cookie = secrets.token_urlsafe(48)
        self.cli_token = secrets.token_urlsafe(36)
        self.system = target_platform()
        log_directory = _runtime_logs_root(self.mutable_builds)
        self._stopping_lock = threading.Lock()
        self._stopping = False
        self.native_parent_pid = os.getppid()
        self._http_serving = threading.Event()
        # Expose the already-designated Logs tree to the read-only Code-OSS
        # host-file provider; a symlink resolving outside it is still rejected.
        self.code_workspace.host_log_roots = [next(
            (ancestor for ancestor in (log_directory, *log_directory.parents)
             if ancestor.name == 'Logs'), log_directory)]
        self.token = secrets.token_urlsafe(36)
        self.sessions: dict[str, TerminalSession] = {}
        self.sessions_lock = threading.Lock()
        self.bedrock_terminal_lock = threading.Lock()
        self.bedrock_terminal_requests = deque()
        self.devtools_requested = False
        # One app-owned guest initialization, never one per opened terminal tab.
        self.guest_start_lock = threading.Lock()
        self.guest_start_state = 'not-installed'
        self.guest_start_error = ''
        self.guest_ready = []  # Per-platform readiness survives a failed Wine probe.
        self.guest_errors = {}
        # Native app consumes these atomic reboot requests over its existing
        # private JSON-RPC pipe, never through an unauthenticated browser menu.
        self.lifecycle_actions = deque()
        self.debug_requests = 0
        self.lagomorph_requests = 0
        self.macgame_request = None
        self.wargames_requests = 0
        # Ordered shell-to-Code actions: run/reselect versus settings refresh.
        self.code_actions = deque()
        self.html_menus_override = False  # macOS development inspection only
        # Never overwrite pending browser story commands with a later click.
        # Each title has one live interpreter tab and one launch generation.
        self.if_actions = deque()
        self.if_active: dict[str, str] = {}
        self.discord_actions = deque()
        self.discord_channels = {}
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
        # Bind the reserved port before creating the persistent Server log.
        # Retrying after Xcode Stop must not create a new log file per failed bind.
        super().__init__((('0.0.0.0', LAN_HTTP_PORT) if lan else ('127.0.0.1', 0)), Handler)
        try:
            self.log = ServerRuntimeLog(log_directory)
        except Exception:
            self.server_close()
            raise
        if lan:
            try:
                self.discovery = CinterForgeDiscovery(ASSETS.parent, socket.gethostname(),
                                                       self.server_port, self.log_event)
            except (OSError, RuntimeError, AttributeError) as error:
                self.log_event('Bonjour unavailable (rebuild staged Host library): ' + str(error))
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.log_event('backend ready; platform=' + self.system + ' port=' + str(self.server_port))

    def log_event(self, message: str) -> None:
        self.log.append(message)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._http_serving.set()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self._http_serving.clear()

    def stop(self) -> None:
        with self._stopping_lock:
            if self._stopping:
                return
            self._stopping = True
        # Release the listener FIRST. Slow PTY/Bedrock cleanup must not hold
        # 20300 after a native Quit, Xcode Stop, or an authenticated recovery.
        try:
            if self._http_serving.is_set():
                self.shutdown()
        finally:
            self.server_close()
            if self.lan:
                _remove_lan_owner(self)
        try:
            if self.discovery is not None:
                self.discovery.close()
            for item in list(self.sessions.values()):
                item.close()
            with self.code_sessions_lock:
                code_sessions = list(self.code_sessions.values())
                self.code_sessions.clear()
            for entry in code_sessions:
                if isinstance(entry, TerminalSession) and not _is_owned_guest_shell(self, entry):
                    entry.close()
            _close_owned_guest_shells(self)
            self.sessions.clear()
            # Guest VM is application-owned, not HTTP-reload-owned.
            if self.bedrock is not None:
                self.bedrock.stop()
            self.log_event('backend shutdown completed')
        finally:
            self.log.close()


class Handler(BaseHTTPRequestHandler):
    server: CinterShellServer

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

    def _code_shell_output(self, code_id: str) -> dict:
        if not re.fullmatch(r'terminal-[a-z0-9-]{1,48}', code_id):
            raise ValueError('Invalid Code-OSS terminal ID')
        with self.server.code_sessions_lock:
            entry = self.server.code_sessions.get(code_id)
        if entry is None:
            raise FileNotFoundError('Code-OSS terminal is not open')
        if isinstance(entry, dict):
            batch = self.server.log.since(entry['cursor'])
            entry['cursor'] = batch['cursor']
            return {'rows': [{'text': line + '\r\n'} for line in batch['lines']], 'closed': False}
        data, closed = entry.poll(300)
        if closed:
            # The client retains the final transcript, but an exited PTY must
            # not keep occupying one of the six Code-OSS session slots.
            with self.server.code_sessions_lock:
                owned = self.server.code_sessions.get(code_id) is entry
                if owned:
                    self.server.code_sessions.pop(code_id)
            if owned and not _is_owned_guest_shell(self.server, entry):
                entry.close()
        return {'rows': [{'text': data.decode('utf-8', errors='replace')}] if data else [],
                'closed': closed}

    def _code_shell_action(self, route: str, body: dict) -> dict:
        if route == '/host/code-shell-close-owner':
            owner = body.get('owner')
            if not isinstance(owner, str) or not re.fullmatch(r'[a-z0-9]{1,24}', owner):
                raise ValueError('Invalid Code-OSS owner')
            prefix = 'terminal-' + owner + '-'
            with self.server.code_sessions_lock:
                self.server.code_closed_owners.add(owner)
                # Bound tombstones; closed owners are unguessable short-lived
                # workbench IDs, and this is only an additional race barrier.
                if len(self.server.code_closed_owners) > 256:
                    self.server.code_closed_owners.clear()
                    self.server.code_closed_owners.add(owner)
                names = [name for name in self.server.code_sessions if name.startswith(prefix)]
                entries = [self.server.code_sessions.pop(name) for name in names]
            for entry in entries:
                if isinstance(entry, TerminalSession) and not _is_owned_guest_shell(self.server, entry):
                    entry.close()
            return {'closed': len(entries)}
        code_id = body.get('id')
        if not isinstance(code_id, str) or not re.fullmatch(r'terminal-[a-z0-9-]{1,48}', code_id):
            raise ValueError('Invalid Code-OSS terminal ID')
        if route == '/host/code-shell-open':
            backend = body.get('backend', 'native')
            if backend not in ('native', 'server', 'linux', 'win', 'minecraft'):
                raise ValueError('Unknown Code-OSS terminal backend')
            # Dead guest wrappers may exit before their worker can poll the
            # final output. Release those PTYs before checking the bound, but
            # never evict a live shell to make space for another one.
            with self.server.code_sessions_lock:
                ended_ids = [key for key, entry in self.server.code_sessions.items()
                             if isinstance(entry, TerminalSession) and entry.closed]
                ended = [self.server.code_sessions.pop(key) for key in ended_ids]
            for entry in ended:
                if not _is_owned_guest_shell(self.server, entry):
                    entry.close()
            with self.server.code_sessions_lock:
                owner = code_id[len('terminal-'):].rsplit('-', 1)[0]
                if owner in self.server.code_closed_owners:
                    raise RuntimeError('Code-OSS workbench is closed; refusing a late PTY open')
                if code_id in self.server.code_sessions:
                    raise ValueError('Code-OSS terminal ID already in use')
                if backend in ('linux', 'win'):
                    # Defense in depth: even multiple Code-OSS clients may not
                    # allocate a second Windows/Linux development shell.
                    if any(getattr(item, 'code_backend', None) == backend
                           for item in self.server.code_sessions.values()):
                        raise RuntimeError(backend.title() + ' Bash is already open in Code-OSS')
                if len(self.server.code_sessions) >= MAX_SESSIONS:
                    raise RuntimeError('Maximum Code-OSS terminal sessions reached')
                if backend == 'server':
                    self.server.code_sessions[code_id] = {'cursor': 0}
                    return {'opened': True, 'backend': backend}
                command = None
                if backend == 'minecraft':
                    if self.server.bedrock is None:
                        raise RuntimeError('Minecraft needs an installed Bedrock server')
                    state = self.server.bedrock.status()
                    if not (state.get('running') or state.get('starting')):
                        raise RuntimeError('Minecraft disabled: start Bedrock with bedrock run first')
                    if state.get('console_available') is False:
                        raise RuntimeError('Bedrock console attachment unavailable')
                    # Reuse the exact console bridge from /api/terminal/open.
                    # This is a Code-OSS PTY tab, not a second Bedrock VM.
                    command = [sys.executable, str(ASSETS / 'CinterForgeTerminal.py'),
                               '--terminal-cli', 'minecraft']
                elif self.server.system == 'mac' and backend in ('linux', 'win'):
                    # Code-OSS attaches to the already-running app-owned PTY.
                    # Never fork another docker exec, Bash or VM for a tab.
                    with self.server.guest_shells_lock:
                        terminal = self.server.guest_shells.get(backend)
                        if terminal is None or terminal.closed or terminal.process.poll() is not None:
                            raise RuntimeError(backend.title() + ' Bash is not connected; see Server Log')
                        terminal.code_backend = backend
                        self.server.code_sessions[code_id] = terminal
                        self.server.devtools_requested = True
                    return {'opened': True, 'backend': backend, 'cwd':
                            self.server.code_workspace.details()['cwd'], 'reused': True}
                elif backend != 'native' and backend != self.server.system:
                    raise RuntimeError(backend + ' backend unavailable on ' + self.server.system)
                cwd = Path(self.server.code_workspace.details()['cwd'])
                terminal = TerminalSession(
                    self.server.mutable_builds, self.server.system, 100, 30,
                    url=self.server.origin, token=self.server.token,
                    command=command, working_directory=cwd,
                    internal_token=self.server.cli_token)
                terminal.code_backend = backend
                self.server.code_sessions[code_id] = terminal
            return {'opened': True, 'backend': backend, 'cwd': str(cwd)}
        with self.server.code_sessions_lock:
            entry = self.server.code_sessions.get(code_id)
            if route == '/host/code-shell-close':
                self.server.code_sessions.pop(code_id, None)
        if entry is None:
            raise FileNotFoundError('Code-OSS terminal is not open')
        if route == '/host/code-shell-close':
            if isinstance(entry, TerminalSession) and not _is_owned_guest_shell(self.server, entry):
                entry.close()
            return {'closed': True}
        if isinstance(entry, dict):
            raise ValueError('Server Log is read-only')
        if route == '/host/code-shell-write':
            entry.write(body.get('data', ''))
            return {'ok': True}
        if route == '/host/code-shell-resize':
            entry.resize(body.get('cols', 100), body.get('rows', 30))
            return {'ok': True}
        raise ValueError('Unknown Code-OSS shell operation')

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
                    self._json(200, {**self.server.code_workspace.details(),
                                     'devtools_installed': _devtools_menu_ready(self.server.system)})
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
            root = ASSETS / 'launcher'
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
                    "frame-src http://127.0.0.1:20601 http://127.0.0.1:20602; "
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
                messages = _discord_api('/channels/' + channel_id + '/messages' + query)
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
            self._json(200, {'enabled': self.server.lan, 'port': LAN_HTTP_PORT,
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
            self._json(200, _guest_boot_snapshot(self.server))
            return
        if path == '/api/status':
            if not self._authorized():
                self._json(403, {'error': 'not authorized'})
                return
            self._json(200, {'platform': self.server.system, 'sessions': len(self.server.sessions),
                             'debug': True, 'devtools_installed':
                             _devtools_menu_ready(self.server.system)})
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
                self._json(200, _lagomorph_media_url(self.server.builds))
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
        if _boot_splash_required(self.server, path, urlsplit(self.path).query):
            # Only the owner boot thread starts the VM; WebView just polls.
            info = ('startup.html', 'text/html; charset=utf-8')
        if not info:
            self._json(404, {'error': 'not found'})
            return
        name, ctype = info
        file = ASSETS / name
        if not file.is_file():
            self._json(503, {'error': f'Incomplete shell package: {name}'})
            return
        data = file.read_bytes()
        if name in ('index.html', 'startup.html'):
            data = data.replace(b'__CPH_TOKEN__', self.server.token.encode('ascii'))
            data = data.replace(b'__CPH_PLATFORM__', self.server.system.encode('ascii'))
        self._send(200, data, ctype)

    def do_POST(self) -> None:
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
                global _server
                try:
                    self.server.stop()
                finally:
                    if _server is self.server:
                        _server = None
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
                limit = 12 * 1024 * 1024 if route == '/code/command' else MAX_BODY
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
                            if isinstance(item, TerminalSession) and not _is_owned_guest_shell(self.server, item):
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
                if n < 1 or n > MAX_BODY:
                    raise ValueError('Invalid lifecycle request length')
                body = json.loads(self.rfile.read(n))
                if not isinstance(body, dict):
                    raise ValueError('Expected lifecycle object')
                if route == '/api/lifecycle/guest-boot':
                    if body:
                        raise ValueError('Guest boot takes no options')
                    result = _schedule_guest_boot(self.server)
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
                        try:
                            _close_owned_guest_shells(self.server)
                            result = reboot_runtime_guests(component)
                            sdk = sdk_root()
                            if sdk is None:
                                raise RuntimeError('DevTools disappeared during guest reboot')
                            for kind in ('linux', 'win'):
                                connected = _new_owned_guest_shell(self.server, kind, sdk)
                                with self.server.guest_shells_lock:
                                    self.server.guest_shells[kind] = connected
                        except Exception as error:
                            with self.server.guest_start_lock:
                                self.server.guest_start_state = 'failed'
                                self.server.guest_start_error = str(error)
                            raise
                        with self.server.guest_start_lock:
                            self.server.guest_start_state = 'ready'
                            self.server.guest_start_error = ''
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
                         _check_lan_login(self.server.lan_login,
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
            limit = 2 * 1024 * 1024 if route.startswith('/api/if/save/') else MAX_BODY
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
                result = {'platform': kind} if kind == 'mac' else _launcher_start(kind, self.server)
                self._json(200, {'ready': True, **result})
                return
            if route == '/api/launcher/launch':
                if self.server.system != 'mac':
                    raise ValueError('Cross-platform Launchers are only available on the Mac host')
                kind, app = body.get('platform'), body.get('app')
                if kind not in ('mac', 'linux', 'win') or not isinstance(app, str):
                    raise ValueError('Invalid graphical launcher request')
                self._json(200, _launcher_launch(kind, app, self.server))
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
                    self.server.lan_login = _save_lan_login(
                        self.server.mutable_builds, username, password)
                    # Force already logged-in browsers to use the new credentials.
                    self.server.lan_cookie = secrets.token_urlsafe(36)
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
                resolved = _discord_resolve(guild, channel)
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
                if not isinstance(active, list) or len(active) > MAX_SESSIONS:
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
                _discord_api('/channels/' + channel_id + '/messages', 'POST', {'content': message, 'allowed_mentions': {'parse': []}})
                self._json(200, {'sent': True})
                return
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
                    command=[sys.executable, str(ASSETS/'CinterForgeTerminal.py'),'--terminal-cli','minecraft']
                elif backend in ('mac','linux','win') and backend!=self.server.system:
                    raise RuntimeError(backend+' Bash unavailable on '+self.server.system+'; refusing native-shell fallback')
                with self.server.sessions_lock:
                    if len(self.server.sessions) >= MAX_SESSIONS:
                        raise ValueError('Maximum terminal sessions reached')
                    terminal = TerminalSession(self.server.mutable_builds, self.server.system,
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
                if command not in _if_catalog():
                    raise ValueError('Unknown story filename command')
                source = body.get('session_id')
                if source is not None:
                    if not isinstance(source,str) or not re.fullmatch(r'[0-9a-f]{32}', source):
                        raise ValueError('Invalid source terminal Session ID')
                    with self.server.sessions_lock:
                        if source not in self.server.sessions:
                            raise ValueError('Source terminal Session has closed')
                if route.endswith('/start'):
                    _if_story(self.server,command)  # Verify installed story first.
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
                        if isinstance(item, TerminalSession) and not _is_owned_guest_shell(self.server, item):
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


def _watch_native_http_parent(server: CinterShellServer, parent_pid: int) -> None:
    """Release this worker's listener when its native window exits abruptly.

    Xcode Stop may terminate the parent without giving the Python RPC worker a
    shutdown message. Do not leave its HTTP service holding the reserved 20300
    port. This observer only shuts down this worker's own server; it never
    inspects, adopts, or terminates another process.
    """
    global _server
    while _server is server and os.getppid() == parent_pid:
        time.sleep(0.5)
    if _server is not server:
        return
    server.log_event('Native HTTP owner exited; releasing managed listener')
    try:
        server.stop()
    except (OSError, RuntimeError) as error:
        server.log_event('Native listener cleanup failed: ' + str(error))
    finally:
        if _server is server:
            _server = None


def handle(method: str, params: dict) -> dict:
    """Invoked through the already-shipped PythonHost service RPC worker."""
    global _server, _server_thread
    if method == 'start':
        if _server is not None:
            raise RuntimeError('CinterForgeTerminal HTTP server already started')
        builds = Path(params['builds']).resolve(strict=True)
        if builds.name != 'Builds' or not ASSETS.is_relative_to(builds):
            raise ValueError('Shell resources must be staged inside the shared Builds directory')
        # Opening a second native window should reuse the running local Host,
        # rather than trying to bind a second listener to the reserved port.
        existing = _existing_lan_owner(builds)
        if existing is not None:
            return {'url': f'http://127.0.0.1:{LAN_HTTP_PORT}/',
                    'native_grant': existing['native_grant'], 'attached': True}
        try:
            _server = CinterShellServer(builds, lan=True)
        except OSError as error:
            if getattr(error, 'errno', None) not in (48, 98, 10048):
                raise
            # On abrupt native/Xcode exit a previous worker can survive past
            # the old four-second grace period. Ask only an authenticated,
            # orphaned CinterForge listener to stop itself, then wait for bind.
            # A foreign listener is never killed or re-used.
            recovery_requested = _request_orphaned_lan_shutdown(builds)
            for _ in range(60):
                time.sleep(0.25)
                if not recovery_requested:
                    recovery_requested = _request_orphaned_lan_shutdown(builds)
                existing = _existing_lan_owner(builds)
                if existing is not None:
                    return {'url': f'http://127.0.0.1:{LAN_HTTP_PORT}/',
                            'native_grant': existing['native_grant'], 'attached': True}
                try:
                    _server = CinterShellServer(builds, lan=True)
                    break
                except OSError as again:
                    if getattr(again, 'errno', None) not in (48, 98, 10048):
                        raise
            else:
                raise RuntimeError('HTTP port 20300 is occupied by an unverified listener; '
                                   'no other process was terminated') from error
        try:
            # Startup is app-owned, not PTY-owned. Installed DevTools (or a
            # newly staged bundle) boots one VM and two persistent guests.
            writable = _server.mutable_builds
            if writable.name == 'Builds':
                os.environ['CINTER_BUILDS_ROOT'] = str(writable)
                os.environ['CINTER_TERMINAL_RUNTIME_MODE'] = 'developer'
                # Xcode's native service worker starts before any PTY. Resolve
                # the same real Steam library that a later shell would use,
                # otherwise sdk_root() may report no DevTools until a tab opens.
                # Do not invent a default path or create a library here.
                steam = _steam_library_for_installed_root(builds)
                if steam is not None:
                    os.environ['CINTER_STEAMAPPS'] = str(steam)
            else:
                steam = _steam_library_for_installed_root(builds)
                if steam is not None:
                    os.environ['CINTER_STEAMAPPS'] = str(steam)
                    os.environ['CINTER_TERMINAL_RUNTIME_MODE'] = 'installed'
            os.environ['CINTER_CODE_WORKSPACE'] = str(Path(_server.code_workspace.details()['cwd']))
            _write_lan_owner(_server)
            _server_thread = threading.Thread(target=_server.serve_forever, name='cinter-http', daemon=True)
            _server_thread.start()
            threading.Thread(target=_watch_native_http_parent,
                             args=(_server, os.getppid()),
                             name='cinter-native-http-owner', daemon=True).start()
            if platform.system() == 'Darwin':
                # Start as part of native application startup. The WebView can
                # render while the one owned VM and two reusable guests boot.
                # A tab must only wait for this work, never start it again.
                _schedule_guest_boot(_server)
        except Exception:
            _server.stop()
            _server = None
            raise
        return {'url': _server.origin + '/', 'port': _server.server_port,
                'native_grant': _server.native_grant,
                'platform': _server.system, 'server_log': str(_server.log.path)}
    if method == 'service_health':
        # The native app must not remain alive with a dead HTTP listener while
        # its Colima watchdog sees the native PID and keeps the guests running.
        alive = (_server is not None and _server_thread is not None and
                 _server_thread.is_alive() and _server._http_serving.is_set() and
                 not _server._stopping)
        return {'alive': alive}
    if method == 'next_lifecycle_reboot_request':
        if _server is None:
            return {'requested': False}
        with _server.debug_lock:
            if not _server.lifecycle_actions:
                return {'requested': False}
            action = _server.lifecycle_actions.popleft()
            return {'requested': True, **action}
    if method == 'next_debugger_request':
        if _server is None:
            return {'requested': False}
        with _server.debug_lock:
            if _server.debug_requests:
                _server.debug_requests -= 1
                return {'requested': True}
            return {'requested': False}
    if method == 'shutdown_runtime':
        # Called after HTTP stop by the native application, NOT for server
        # reload. An independent watcher performs the same action on a crash.
        stop_runtime_vm()
        return {'stopped': True, 'guests': ['linux', 'win'], 'vm': PROFILE}
    if method == 'stop':
        closing = _server
        _server = None
        if closing is not None:
            closing.stop()
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


def _devtools_download_partial(release: str, filename: str) -> Path:
    """Store multi-GB resumable downloads outside installed Steam libraries.

    Only the verified, unpacked DevTools distribution is an installed app;
    incomplete ZIPs are disposable OS-cache content.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', filename):
        raise ValueError('Unsafe DevTools download filename')
    return (_installed_cache_root(_real_login_home()) / 'Downloads' / release /
            (filename + '.partial'))


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
    pending = _devtools_download_partial(release, artifact['filename'])
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
    # A completed optional install changes startup capabilities. Relaunch the
    # existing native owner so its single boot supervisor checks both shells
    # BEFORE it displays the normal UI. Do not boot Colima in this PTY worker.
    try:
        _api('/api/lifecycle/reboot', body={'component': 'app'})
        print('[DEVTOOLS] Relaunching CinterForge Terminal to initialize DevTools...', flush=True)
    except (OSError, RuntimeError, ValueError) as error:
        print('[DEVTOOLS] Installed, but automatic relaunch failed: ' + str(error) +
              '; relaunch Terminal normally to initialize the guests.', flush=True)
        return 1
    return 0


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
"""Real persistent guest Bash for macOS Code-OSS integrated terminals.

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
    if (record.get('schema') not in (FOUR_SCHEMA, 'cinterforge.terminal.unpacked-build.v1') or
            record.get('target') != 'devtools' or
            record.get('release') != _devtools_release()):
        return None
    sdk = destination / 'DevTools/Builds/CinterForgeTerminal/Toolchains'
    needed = ('mac/env/bin/colima', 'mac/env/bin/docker', 'mac/lima/bin/limactl',
              'mac/qemu/bin/qemu-img', 'mac/qemu/bin/qemu-system-x86_64',
              'Windows/MSYS2/usr/bin/bash.exe', 'Windows/MSYS2/usr/bin/msys-2.0.dll')
    if any(not (sdk / name).is_file() or (sdk / name).is_symlink() for name in needed):
        return None
    if (not (sdk / 'mac/qemu/share/qemu').is_dir() or
            (sdk / 'mac/qemu/share/qemu').is_symlink() or
            not (sdk / 'mac/qemu/cinter-qemu-receipt.json').is_file()):
        return None
    for kind in ('WindowsCross',):
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
            # An older DevTools archive may contain WineHQ instead of MSYS2's
            # patched Wine. Never consider that Windows closure runtime-ready.
            if data.get('wine_version') != 'wine-11.15':
                continue
            # Linux and Windows share the one pinned WindowsCross image;
            # reject obsolete LinuxSample-only SDKs instead of asking the user
            # to reinstall a valid unified package on each launch.
            if (isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest) and
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
    """VM scratch: sibling Builds for development, macOS cache when installed.

    Steam is strictly applications and persistent user data. No Lima/Colima
    disks, Docker images, caches, or temporary files may be created there.
    Installed apps must also never create a developer Builds directory.
    """
    installed = os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'installed'
    if installed:
        value = os.environ.get('CINTER_STEAMAPPS', '')
        if not value:
            raise RuntimeError('Installed DevTools require the real Steam library')
        steam = Path(value).expanduser()
        if steam.name.lower() != 'steamapps' or steam.is_symlink() or not steam.is_dir():
            raise RuntimeError('Installed DevTools require an existing real steamapps directory')
        if platform.system() != 'Darwin':
            raise RuntimeError('Managed macOS guest VM requires a macOS runtime')
        # A short Lima socket path is mandatory on macOS. Cache contains only
        # disposable app-owned VM state; DevTools archives remain in Steam.
        roots = [_real_login_home() / 'Library/Caches/CFTVM/T']
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
    tail = '/l/colima-t/ssh.sock.1234567890123456'
    for root in roots:
        if len(os.fsencode(str(root) + tail)) < 96:
            if root.is_symlink() or (root.exists() and not root.is_dir()):
                raise RuntimeError('Unsafe managed VM state: ' + str(root))
            root.mkdir(parents=True, exist_ok=True)
            return root
    raise RuntimeError('Managed VM socket root too long under developer Builds or user cache')


def _env(sdk: Path) -> dict[str, str]:
    root = _state_root()
    for rel in ('h', 'c', 'l', 'cache', 'cache/colima',
                'd', 'config', 'tmp'):
        path = root / rel
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise RuntimeError('Unsafe managed VM directory: ' + str(path))
        path.mkdir(parents=True, exist_ok=True)
    lima_home = root / 'l'
    socket = lima_home / ('colima-' + PROFILE) / 'ssh.sock.1234567890123456'
    if len(os.fsencode(socket)) >= 100:
        raise RuntimeError('Managed VM socket path too long within developer Builds or user cache: ' + str(socket))
    env = dict(os.environ)
    for key in ('DOCKER_HOST', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        env.pop(key, None)
    env.update(HOME=str(root/'h'), COLIMA_HOME=str(root/'c'),
               LIMA_HOME=str(lima_home), COLIMA_CACHE_HOME=str(root/'cache/colima'),
               DOCKER_CONFIG=str(root/'d'), XDG_CONFIG_HOME=str(root/'config'),
               XDG_CACHE_HOME=str(root/'cache'), TMPDIR=str(root/'tmp'),
               DOCKER_CONTEXT='colima-' + PROFILE,
               PATH=os.pathsep.join([str(sdk/'mac/qemu/bin'),
                                     str(sdk/'mac/lima/bin'), str(sdk/'mac/env/bin'),
                                     os.environ.get('PATH', '')]))
    return env


def _image_archive(sdk: Path, kind: str) -> tuple[Path, str]:
    if kind not in ('linux', 'win'):
        raise ValueError('Unknown managed guest platform')
    # The same verified amd64 Debian image supplies both native Linux and
    # Wine/MSYS2 Windows tools. Two UI platforms, ONE running container.
    folder = sdk / 'WindowsCross'
    receipts = sorted(folder.glob('*.json'))
    for receipt in receipts:
        if receipt.is_symlink():
            continue
        try:
            record = json.loads(receipt.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        # Archive selection must not silently fall back to an old stock-Wine
        # image when multiple valid hashes remain in installed DevTools.
        if record.get('wine_version') != 'wine-11.15':
            continue
        digest = record.get('dockerfile_sha256', '')
        name = record.get('image')
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


@contextmanager
def _managed_vm_lock(env: dict[str, str]):
    """Serialize Colima mutations across native and Code-OSS Session processes.

    Linux and Windows guest shells start as independent children. A thread lock
    cannot protect against two concurrent `colima start` invocations, which can
    race for the same Lima host-agent PID/socket and leave a stale Docker context.
    The lock resides in the permitted app-owned cache/Builds VM state only.
    """
    home = Path(env['COLIMA_HOME'])
    lock_file = home / 'CinterForgeTerminalGuestVM.lock'
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(lock_file, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ensure_docker(sdk: Path, env: dict[str, str], workspace: Path | None = None) -> str:
    """Start/recover one shared guest VM with the code workspace really mounted.

    The lock protects VM startup from the separate Win/Linux PTY children, but
    is released *before* either long-lived interactive shell starts. Never
    restart an already-running guest with live containers.
    """
    with _managed_vm_lock(env):
        return _ensure_docker_locked(sdk, env, workspace)


def _mac_mounted_volumes() -> list[Path]:
    """Discover visible host volumes; preserve spaces in the *host* paths.

    Sharing /Volumes itself covers volume names containing spaces, which
    Colima cannot accept as individual mount locations. Extra mounts are
    provided for volumes with space-free names and nonstandard mount points.
    """
    if platform.system() != 'Darwin':
        return []
    try:
        result = subprocess.run(['/sbin/mount'], capture_output=True, text=True,
                                check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode or not isinstance(getattr(result, 'stdout', None), str):
        return []
    found: set[Path] = set()
    for line in result.stdout.splitlines():
        match = re.search(r' on (.+?) \(([^,)]+)', line)
        if not match:
            continue
        location = Path(match.group(1).replace('\\040', ' '))
        filesystem = match.group(2).lower()
        # Pseudo-filesystems are not user drives, and exposing /dev would
        # obscure Linux's actual device filesystem.
        if (location == Path('/') or not location.is_dir() or
                filesystem in ('devfs', 'procfs', 'autofs', 'fdesc', 'synthfs') or
                location == Path('/dev')):
            continue
        found.add(location)
    return sorted(found, key=str)


def _mac_host_mounts() -> list[tuple[Path, str]]:
    """Share the entire Mac filesystem once, subject to macOS permissions.

    Lima/virtiofs does not support overlapping host mounts. Sharing both `/`
    and `/Volumes` (or individual `/Volumes/*` disks) creates overlapping
    locations, even when the guest destinations are distinct. One writable
    root share includes the host's mounted-volume namespace and preserves
    paths containing spaces without any app-defined drive or folder allowlist.
    This does not bypass macOS privacy permissions or Virtualization.framework
    limitations on nested host mounts.
    """
    return [(Path('/'), '/host/mac')]


def _colima_mount_entries(yaml: str) -> list[tuple[str, str, bool]] | None:
    """Read only the narrow mount schema that our Colima profile uses.

    Colima may serialize unquoted scalars when saving its configuration. Treat
    semantically equivalent settings as equal instead of restarting a running
    VM merely because YAML formatting changed. Unknown mount syntax is never
    assumed equivalent to the requested full-root share.
    """
    match = re.search(r'(?ms)^mounts:[^\n]*\n(.*?)(?=^[a-zA-Z][a-zA-Z0-9_-]*:|\Z)', yaml)
    if match is None:
        return None

    def scalar(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith('"'):
            try:
                result = json.loads(raw)
                return result if isinstance(result, str) else raw
            except ValueError:
                return raw
        if len(raw) >= 2 and raw[0] == raw[-1] == "'":
            return raw[1:-1].replace("''", "'")
        return raw

    records: list[dict[str, str]] = []
    for line in match.group(1).splitlines():
        item = re.match(r'^\s*-\s+location:\s*(.*?)\s*$', line)
        if item:
            records.append({'location': scalar(item.group(1))})
            continue
        attribute = re.match(r'^\s+(mountPoint|writable):\s*(.*?)\s*$', line)
        if attribute and records:
            records[-1][attribute.group(1)] = scalar(attribute.group(2))
        elif line.strip() and not line.lstrip().startswith('#'):
            return None
    if not records or any(set(item) != {'location', 'mountPoint', 'writable'} for item in records):
        return None
    if any(item['writable'] not in ('true', 'false') for item in records):
        return None
    return [(item['location'], item['mountPoint'], item['writable'] == 'true')
            for item in records]


def _configure_colima_host_mounts(env: dict[str, str], mounts: list[tuple[Path, str]],
                                  *, dry_run: bool = False) -> bool:
    """Change only the owned t-profile's mount list, never another profile.

    Returns whether the mount topology changed. A live VM must be restarted
    *only after* its running containers have been checked for foreign owners.
    """
    profile = Path(env['COLIMA_HOME']) / PROFILE
    config = profile / 'colima.yaml'
    if profile.is_symlink() or config.is_symlink():
        raise RuntimeError('Managed Terminal VM configuration must not be a symlink')
    if not dry_run:
        profile.mkdir(parents=True, exist_ok=True)
    original = config.read_text('utf-8') if config.is_file() else ''
    desired = [(str(host), guest, True) for host, guest in mounts]
    if _colima_mount_entries(original) == desired:
        return False
    block = 'mounts:\n' + ''.join(
        '  - location: ' + json.dumps(str(host)) + '\n'
        '    mountPoint: ' + json.dumps(guest) + '\n'
        '    writable: true\n'
        for host, guest in mounts)
    keys = list(re.finditer(r'(?m)^[a-zA-Z][a-zA-Z0-9_-]*:[^\n]*$', original))
    entry = next((idx for idx, item in enumerate(keys)
                  if item.group().split(':', 1)[0] == 'mounts'), None)
    if entry is None:
        updated = original.rstrip() + ('\n\n' if original.strip() else '') + block
    else:
        beginning = keys[entry].start()
        ending = keys[entry + 1].start() if entry + 1 < len(keys) else len(original)
        updated = original[:beginning] + block + original[ending:]
    if dry_run:
        return True
    temporary = config.with_name('colima.yaml.pending')
    if temporary.is_symlink():
        raise RuntimeError('Unsafe managed Terminal VM pending configuration')
    temporary.write_text(updated, encoding='utf-8')
    os.replace(temporary, config)
    return True


def _configure_colima_runtime_emulation(env: dict[str, str], *, dry_run: bool = False) -> bool:
    """Ensure the *runtime t profile* uses QEMU binfmt, never Rosetta.

    Apple Silicon Rosetta can abort Wine/MSYS2 Bash during rt_sigreturn
    (x86 AVX-state assertion, exit 133). Do not alter the working Setup w/c
    profiles or the separate LinuxKit Bedrock appliance. For existing t
    profiles, normalize only explicit stale flags; a new Colima profile is
    configured by the matching flags on `colima start`.
    """
    profile = Path(env['COLIMA_HOME']) / PROFILE
    config = profile / 'colima.yaml'
    if profile.is_symlink() or config.is_symlink():
        raise RuntimeError('Managed Terminal VM configuration must not be a symlink')
    if not config.is_file():
        return False
    original = config.read_text(encoding='utf-8')
    desired = {'rosetta': 'false', 'binfmt': 'false',
               'arch': 'x86_64', 'vmType': 'qemu', 'mountType': '9p'}
    updated = original
    for key, value in desired.items():
        pattern = r'(?m)^(' + re.escape(key) + r':)[^\n]*$'
        if re.search(pattern, updated):
            updated = re.sub(pattern, lambda match: match.group(1) + ' ' + value, updated)
        else:
            # An earlier ARM64/VZ profile may omit explicit architecture
            # fields. Leaving them absent lets Colima reuse the wrong ABI.
            updated = key + ': ' + value + '\n' + updated
    if updated == original:
        return False
    if dry_run:
        return True
    pending = config.with_name('colima.yaml.emulation.pending')
    if pending.is_symlink():
        raise RuntimeError('Unsafe managed Terminal VM pending configuration')
    pending.write_text(updated, encoding='utf-8')
    os.replace(pending, config)
    return True


def _ensure_docker_locked(sdk: Path, env: dict[str, str], workspace: Path | None) -> str:
    colima = str(sdk / 'mac/env/bin/colima')
    docker = str(sdk / 'mac/env/bin/docker')
    mounts = _mac_host_mounts()

    def call(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(argv, env=env, check=False, capture_output=True,
                              text=True, timeout=timeout)

    def guest_directory(directory: str) -> bool:
        probe = call([colima, 'ssh', '--profile', PROFILE, '--', 'test', '-d', directory])
        return probe.returncode == 0

    def active_containers(via_guest: bool) -> bool:
        argv = ([colima, 'ssh', '--profile', PROFILE, '--', 'docker', 'ps', '--quiet']
                if via_guest else [docker, 'ps', '--quiet'])
        result = call(argv, timeout=35)
        if result.returncode:
            raise RuntimeError('Managed Terminal VM state cannot be verified; refusing '
                               'to restart a potentially active guest: ' +
                               (result.stderr or result.stdout).strip()[:240])
        return bool(result.stdout.strip())

    def recover_stale_owned_guests() -> None:
        # A crashed Terminal may leave its owned container
        # running inside this dedicated VM. Never sacrifice foreign workloads.
        listing = call([docker, 'ps', '--format',
                        '{{.Names}}|{{.Label "io.cinterforge.terminal"}}'], timeout=35)
        if listing.returncode:
            raise RuntimeError('Cannot inspect managed VM container ownership')
        foreign = []
        for row in listing.stdout.splitlines():
            name, _, owner = row.partition('|')
            if name not in (_guest_name('linux'), _guest_name('win')) or owner != PROFILE:
                foreign.append(name)
        if foreign:
            raise RuntimeError('Cannot reconfigure Terminal VM while unrelated containers run: ' +
                               ', '.join(foreign))
        _stop_owned_guests_locked(docker, env)

    # Setup verifies the native amd64 Docker daemon, but installed-app boot
    # previously only checked its VERSION. Colima ignores VM type/architecture
    # flags once a profile exists; an old arm64/VZ cache can therefore appear
    # healthy while immediately killing every linux/amd64 container.
    installed = os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') == 'installed'

    def verified_installed_architecture() -> str:
        probe = call([docker, 'info', '--format', '{{.Architecture}}'], timeout=30)
        if probe.returncode:
            raise RuntimeError('Cannot verify installed Terminal guest Docker architecture: ' +
                               (probe.stderr or probe.stdout).strip()[-400:])
        return probe.stdout.strip().lower()

    def require_native_installed_architecture() -> None:
        actual = verified_installed_architecture()
        if actual not in ('amd64', 'x86_64'):
            # Colima's VM architecture is immutable after profile creation.
            # Deleting it from normal app launch destroys user cache and makes
            # Colima download a fresh Ubuntu disk behind the splash screen.
            # Preserve it for an explicit, separately consented repair; never
            # try to run amd64 Debian/Wine against the wrong VM.
            raise RuntimeError('Installed Terminal t guest is ' + actual +
                               ', not native x86-64. Preserving the existing '
                               'Colima profile; startup will not delete it or '
                               'download a replacement VM. Repair the managed '
                               'guest explicitly before using Linux/Windows Bash')

    # Check the app-owned profile's *saved* topology as well as live paths.
    # A legacy t-profile may still be running with the old overlapping mount
    # config. A healthy Docker socket alone cannot prove that all host drives
    # will survive the next Colima start.
    required = ['/host/mac', '/host/mac' + str(sdk)]
    if workspace is not None:
        required.append('/host/mac' + str(workspace))
    installed_profile = bool(env.get('COLIMA_HOME')) and (
        Path(env['COLIMA_HOME']) / PROFILE / 'colima.yaml').is_file()
    mount_changed = _configure_colima_host_mounts(env, mounts, dry_run=True)
    # Installed profiles have immutable VM type/architecture. Updating their
    # YAML cannot convert an already-created ARM/VZ disk to AMD64/QEMU.
    # Inspect the *running* engine rather than rewriting these settings.
    emulation_changed = (False if installed and installed_profile else
                         _configure_colima_runtime_emulation(env, dry_run=True))
    config_changed = mount_changed or emulation_changed
    ping = call([docker, 'info', '--format', '{{.ServerVersion}}'], timeout=20)
    if ping.returncode == 0 and installed:
        require_native_installed_architecture()
    if ping.returncode == 0:
        missing = [path for path in required if not guest_directory(path)]
        if not missing and not config_changed:
            return docker  # One already-ready VM; every tab uses docker exec.
        if active_containers(via_guest=False):
            recover_stale_owned_guests()  # Verifies labels before stopping.
        reason = ('Rosetta translator disabled for real Wine/MSYS2 Bash'
                  if emulation_changed else
                  'outdated overlapping host mount configuration' if mount_changed else
                  'inaccessible host paths: ' + ', '.join(missing))
        print('[DEVTOOLS] Reconfiguring owned Terminal VM: ' + reason, flush=True)
        _run_checked([colima, 'stop', PROFILE], env,
                     title='Stopping idle guest VM for corrected host mounts', timeout=120)
    else:
        status = call([colima, 'status', PROFILE])
        if status.returncode == 0:
            if active_containers(via_guest=True):
                raise RuntimeError('Managed Terminal VM has live guest containers '
                                   'but its Docker socket is unavailable; refusing restart')
            _run_checked([colima, 'stop', PROFILE], env,
                         title='Recovering idle VM with stale Docker socket', timeout=120)

    _configure_colima_host_mounts(env, mounts)
    if not installed or not installed_profile:
        _configure_colima_runtime_emulation(env)
    print('[DEVTOOLS] Sharing accessible Mac filesystem under /host/mac', flush=True)
    # Only the terminal-owned runtime profile t changes. Boot the actual
    # QEMU x86_64 Linux guest; Wine/MSYS2 runs natively inside that guest.
    # Do not run amd64 Wine through ARM64 binfmt or Rosetta user translation.
    # Pass the full-root share explicitly. On first profile creation Colima
    # constructs its configuration from CLI flags; --save-config otherwise
    # writes the default mounts over the YAML edited above. The three-part
    # flag preserves the nondefault guest mount point and write permission.
    # Keep the YAML normalization for existing profiles and the live path
    # probes below: neither a successful VM boot nor a saved config proves
    # that VirtioFS actually mounted the host's source tree.
    # Only initial profile creation needs immutable QEMU/architecture flags.
    # Reusing a saved installed profile with those flags causes warnings and
    # cannot change its VM. Do not rebuild an existing profile at app launch.
    args = ([colima, 'start', PROFILE, '--runtime', 'docker']
            if installed and installed_profile else
            [colima, 'start', PROFILE, '--runtime', 'docker',
             '--vm-type', 'qemu', '--mount-type', '9p',
             '--mount', '/:/host/mac:w', '--arch', 'x86_64',
             '--cpus', '4', '--memory', '8', '--disk', '32', '--save-config'])
    if installed and not installed_profile:
        print('[DEVTOOLS] Initializing new x86-64 t profile (no existing VM to replace)',
              flush=True)
    _run_checked(args, env, title='Starting managed guest VM with host volumes', timeout=1200)
    daemon = call([docker, 'info', '--format', '{{.ServerVersion}}'], timeout=45)
    if daemon.returncode:
        raise RuntimeError('Managed Terminal Docker socket still unavailable after VM start: ' +
                           (daemon.stderr or daemon.stdout).strip()[:240])
    if installed:
        require_native_installed_architecture()
    missing = [path for path in required if not guest_directory(path)]
    if missing:
        raise RuntimeError('Managed Terminal VM cannot access shared Mac paths ' +
                           ', '.join(missing) + '; refusing a broken Docker bind mount')
    return docker


def _boot_splash_required(server: CinterShellServer, path: str, query: str) -> bool:
    """Only with installed DevTools warming. Failure NEVER blocks Mac Bash."""
    if path not in ('/', '/index.html') or server.system != 'mac':
        return False
    parameters = parse_qs(query)
    # Acceptance and explicit splash completion must reach the normal UI.
    if 'accept-ui' in parameters or 'devtools-boot-complete' in parameters:
        return False
    return _guest_boot_snapshot(server)['state'] == 'starting'


def _guest_boot_snapshot(server: CinterShellServer) -> dict:
    with server.guest_start_lock:
        state = server.guest_start_state
        return {'state': state, 'error': server.guest_start_error,
                'guests': list(getattr(server, 'guest_ready',
                                       ['linux', 'win'] if state == 'ready' else [])),
                'errors': dict(getattr(server, 'guest_errors', {}))}


def _schedule_guest_boot(server: CinterShellServer) -> dict:
    """Exactly one app-owned initialization per run, including a later SDK install.

    The HTTP service is already accepting requests when this is called. It
    publishes readiness explicitly rather than claiming the guests are ready
    just because the native WebView opened. An explicit reinstall may retry a
    failed boot; ordinary terminal tabs never call this function.
    """
    with server.guest_start_lock:
        if server.guest_start_state in ('starting', 'ready'):
            return {'state': server.guest_start_state, 'error': server.guest_start_error}
        try:
            installed = sdk_root() is not None
        except (OSError, ValueError, RuntimeError) as error:
            server.guest_start_state = 'failed'
            server.guest_start_error = 'Cannot inspect installed DevTools: ' + str(error)
            server.log_event('Managed reusable guests FAILED: ' + server.guest_start_error)
            return {'state': 'failed', 'error': server.guest_start_error}
        if not installed:
            server.guest_start_state = 'not-installed'
            server.guest_start_error = ''
            server.log_event('Managed reusable guests: DevTools not installed; no VM started')
            return {'state': 'not-installed', 'error': ''}
        server.guest_start_state = 'starting'
        server.guest_start_error = ''
        server.guest_ready = []
        server.guest_errors = {}
    try:
        # Startup runs off the HTTP/UI thread. The guest boot thread waits
        # until any previous native owner's crash watchdog releases the VM;
        # otherwise a fast relaunch can start new guests while old cleanup is
        # still stopping the same t profile.
        threading.Thread(target=_boot_guests_logged, args=(server,),
                         daemon=True, name='cinter-runtime-guests').start()
    except (OSError, RuntimeError, ValueError) as error:
        with server.guest_start_lock:
            server.guest_start_state = 'failed'
            server.guest_start_error = 'Cannot start guest supervisor: ' + str(error)
        server.log_event('Managed reusable guests FAILED: ' + str(error))
        return _guest_boot_snapshot(server)
    server.log_event('Managed reusable guests: starting Linux and Windows at application launch')
    return _guest_boot_snapshot(server)


def _is_owned_guest_shell(server: CinterShellServer, entry: TerminalSession) -> bool:
    with server.guest_shells_lock:
        return any(entry is shell for shell in server.guest_shells.values())


def _close_owned_guest_shells(server: CinterShellServer) -> None:
    """Release PTYs before Docker/Colima shutdown (also on HTTP worker stop)."""
    with server.guest_shells_lock:
        shells = list(server.guest_shells.values())
        server.guest_shells.clear()
    for shell in shells:
        try:
            shell.close()
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            server.log_event('Guest Bash PTY cleanup FAILED: ' + str(error))


def _runtime_guest_shell_command(sdk: Path, kind: str, workspace: Path | None) -> list[str]:
    """One real interactive Docker/Wine Bash, shared by all Code-OSS attachments."""
    if kind not in ('linux', 'win'):
        raise ValueError('Unknown persistent guest Bash kind')
    docker = str(sdk / 'mac/env/bin/docker')
    cwd = '/host/mac' + str(workspace) if workspace else '/'
    command = [docker, 'exec', '-it', *_launcher_guest_env(kind),
               '--workdir', cwd, _guest_name(kind)]
    if kind == 'linux':
        return command + ['/bin/bash', '--noprofile', '-i']
    # Use the exact profile-free MSYS2 entry point already proven by
    # _verify_guest_bash().  --login invokes /etc/profile, whose first-start
    # pacman-key hook fails against the deliberately read-only /msys2 bind.
    # Establish coreutils/UCRT64 PATH explicitly, then replace the bootstrap
    # process with ONE long-lived interactive Bash on the existing Docker PTY.
    # No machine-wide initialization, pacman setup, or second guest is needed.
    bootstrap = ('export PATH=/usr/bin:/ucrt64/bin:$PATH; '
                 'exec /usr/bin/bash --noprofile --norc -i')
    return command + ['wine', r'Z:\msys2\usr\bin\bash.exe',
                      '--noprofile', '--norc', '-c', bootstrap]


def _verify_persistent_guest_shell(shell: TerminalSession, kind: str, *, timeout: float = 120) -> None:
    """Exercise the actual long-lived PTY, not a separate --version probe."""
    nonce = uuid.uuid4().hex
    # Split the marker in the transmitted command: terminal echo is NOT proof
    # of command execution. Only the command's expanded output contains it.
    marker = ('CINTER_SHELL_READY:' + nonce).encode('ascii')
    shell.write('printf "CINTER_SHELL_READY:%s%s\\n" "' + nonce[:16] +
                '" "' + nonce[16:] + '"\n')
    started = time.monotonic()
    deadline = started + timeout
    next_progress = started + 15
    label = 'Windows' if kind == 'win' else 'Linux'
    print('[DEVTOOLS] ' + label + ' persistent Bash PTY START', flush=True)
    output = bytearray()
    while time.monotonic() < deadline:
        data, closed = shell.poll(500)
        output.extend(data)
        if marker in output:
            # Keep one real shell prompt buffered for Code-OSS to render after
            # the initial splash; there is no new Bash when the tab attaches.
            shell.write('\n')
            print('[DEVTOOLS] ' + label + ' persistent Bash PTY PASS (' +
                  str(round(time.monotonic() - started, 1)) + 's)', flush=True)
            return
        if len(output) > 4096:
            del output[:-4096]
        now = time.monotonic()
        if now >= next_progress:
            print('[DEVTOOLS] ' + label + ' persistent Bash PTY WORKING (' +
                  str(int(now - started)) + 's)', flush=True)
            next_progress = now + 15
        if closed or shell.process.poll() is not None:
            break
    raise RuntimeError(kind + ' persistent Bash did not execute PTY readiness command: ' +
                       output.decode('utf-8', 'replace')[-700:])


def _guest_docker_context(sdk: Path) -> dict[str, str]:
    full = _env(sdk)
    return {key: full[key] for key in ('HOME', 'COLIMA_HOME', 'LIMA_HOME',
                                      'COLIMA_CACHE_HOME', 'DOCKER_CONFIG',
                                      'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                                      'DOCKER_CONTEXT', 'TMPDIR', 'PATH')}


def _new_owned_guest_shell(server: CinterShellServer, kind: str, sdk: Path) -> TerminalSession:
    """Create and validate once. No Code-OSS tab creates guest processes."""
    workspace = Path(server.code_workspace.details()['cwd'])
    shell = TerminalSession(server.mutable_builds, server.system, 100, 30,
                            url=server.origin, token=server.token,
                            command=_runtime_guest_shell_command(sdk, kind, workspace),
                            working_directory=workspace, internal_token=server.cli_token,
                            child_env=_guest_docker_context(sdk), output_log=_runtime_logs_root(server.mutable_builds) /
                            ('Sessions/persistent-' + kind + '.log'))
    try:
        _verify_persistent_guest_shell(shell, kind)
        if server._stopping:
            raise RuntimeError('Native Terminal quit during Bash startup')
        return shell
    except BaseException:
        shell.close()
        raise


def _check_owned_guest_vnc(sdk: Path, kind: str) -> bool:
    """Keep the same guest's VNC display alive; never create new containers.

    True means a stopped endpoint was restarted idempotently within the
    existing named guest. A healthy endpoint is only probed, not relaunched.
    """
    env = _env(sdk)
    docker = str(sdk / 'mac/env/bin/docker')
    name = _guest_name(kind)
    try:
        _verify_guest_vnc(docker, env, kind, name)
        return False
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        # _activate_guest_vnc verifies the RFB handshake and HTTP endpoint
        # after restarting *only* missing helpers inside this owned container.
        _activate_guest_vnc(docker, env, kind, name)
        return True


def _maintain_owned_guest_shells(server: CinterShellServer, sdk: Path) -> None:
    """Maintain Bash PTYs and both VNC endpoints without starting another VM.

    If a guest is unavailable, log a bounded failure and preserve Mac Bash.
    The independent native-lifetime watchdog owns crash recovery for the VM.
    """
    failures = {'linux': 0, 'win': 0}
    vnc_failures = {'linux': 0, 'win': 0}
    last_vnc_check = time.monotonic()
    while not server._stopping:
        time.sleep(2)
        if server._stopping:
            return
        with server.guest_shells_lock:
            lost = [(kind, session) for kind, session in server.guest_shells.items()
                    if session.closed or session.process.poll() is not None]
        for kind, previous in lost:
            if server._stopping or failures[kind] >= 3:
                continue
            failures[kind] += 1
            server.log_event(kind + ' persistent Bash exited; reconnecting within its existing guest')
            try:
                replacement = _new_owned_guest_shell(server, kind, sdk)
                with server.guest_shells_lock:
                    if server._stopping or server.guest_shells.get(kind) is not previous:
                        replacement.close()
                        continue
                    server.guest_shells[kind] = replacement
                with server.code_sessions_lock:
                    for code_id, entry in list(server.code_sessions.items()):
                        if entry is previous:
                            server.code_sessions.pop(code_id, None)
                previous.close()
                failures[kind] = 0
                server.log_event(kind + ' persistent Bash reconnected (Code-OSS can reattach)')
            except Exception as error:
                server.log_event(kind + ' persistent Bash reconnect FAILED: ' + str(error))
                if failures[kind] >= 3:
                    with server.guest_start_lock:
                        server.guest_errors[kind] = str(error)
                        server.guest_ready = [v for v in server.guest_ready if v != kind]
                        server.guest_start_state = 'failed'
                        server.guest_start_error = '; '.join(k + ': ' + v
                                                           for k, v in server.guest_errors.items())
        # The Session viewers use the same two VNC endpoints, but can remain
        # closed for hours. Probe them periodically and repair only missing
        # guest-local helpers; do not create an additional RFB server/VM.
        if not server._stopping and time.monotonic() - last_vnc_check >= 30:
            last_vnc_check = time.monotonic()
            for kind in ('linux', 'win'):
                with server.guest_start_lock:
                    ready = kind in server.guest_ready
                if not ready or server._stopping or vnc_failures[kind] >= 3:
                    continue
                try:
                    if _check_owned_guest_vnc(sdk, kind):
                        server.log_event(kind + ' VNC endpoint recovered in existing guest')
                    vnc_failures[kind] = 0
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    vnc_failures[kind] += 1
                    server.log_event(kind + ' VNC health/recovery FAILED (' +
                                     str(vnc_failures[kind]) + '/3): ' + str(error))
                    if vnc_failures[kind] >= 3:
                        server.log_event(kind + ' VNC unavailable; Launcher can explicitly retry '
                                         'without creating a guest or VM')


def _boot_guests_logged(server: CinterShellServer) -> None:
    watchdog_ready = False
    try:
        # A separate process watches the native PID, surviving both a Python
        # worker crash and Xcode Stop. It acknowledges exclusive lifetime
        # ownership BEFORE the boot thread is allowed to start Colima.
        _start_runtime_watchdog(server.native_parent_pid,
                                _runtime_logs_root(server.mutable_builds))
        watchdog_ready = True
        if server._stopping:
            raise RuntimeError('Native Terminal shut down before guest startup completed')
        result = boot_runtime_guests()
        if not result.get('installed'):
            raise RuntimeError('DevTools disappeared during managed guest startup')
        ready = list(result.get('guests', []))
        errors = dict(result.get('errors', {}))
        sdk = sdk_root()
        if sdk is None:
            raise RuntimeError('Verified DevTools disappeared while connecting Bash')
        for kind in list(ready):
            if server._stopping:
                raise RuntimeError('Native Terminal quit during guest startup')
            try:
                connected = _new_owned_guest_shell(server, kind, sdk)
                with server.guest_shells_lock:
                    if server._stopping:
                        connected.close()
                        raise RuntimeError('Native Terminal quit during guest startup')
                    server.guest_shells[kind] = connected
                server.log_event(kind + ' persistent Bash connected (Code-OSS reusable PTY)')
            except Exception as error:
                ready.remove(kind)
                errors[kind] = str(error)
                server.log_event(kind + ' persistent Bash FAILED: ' + str(error))
        with server.guest_start_lock:
            server.guest_ready = ready
            server.guest_errors = errors
            server.guest_start_state = 'ready' if not errors else 'failed'
            server.guest_start_error = '; '.join(k + ': ' + v for k, v in errors.items())
        if ready and not server._stopping:
            threading.Thread(target=_maintain_owned_guest_shells, args=(server, sdk),
                             daemon=True, name='cinter-guest-shell-supervisor').start()
        if errors:
            # A failed Wine guest must not destroy a working Linux guest.
            # Splash closes on failure; Mac Bash is always available.
            server.log_event('Managed reusable guests PARTIAL/FAILED: ' +
                             json.dumps(result, sort_keys=True))
        else:
            server.log_event('Managed reusable guests ready: ' + json.dumps(result, sort_keys=True))
    except Exception as error:
        # Do not leave a half-booted Colima profile alive after a failed image
        # load / Bash verification. The guardian is already responsible for
        # abnormal native exits; cleanup here handles failure while it lives.
        if watchdog_ready and not server._stopping:
            _close_owned_guest_shells(server)
            try:
                stop_runtime_vm()
            except Exception as cleanup_error:
                server.log_event('Managed reusable guests cleanup FAILED: ' + str(cleanup_error))
        with server.guest_start_lock:
            server.guest_start_state = 'failed'
            server.guest_start_error = str(error)
            server.guest_ready = []
            server.guest_errors = {'linux': str(error), 'win': str(error)}
        server.log_event('Managed reusable guests FAILED: ' + str(error))


def _wait_for_app_guests(kind: str, *, timeout_seconds: float = 1800) -> None:
    """Attach to an app-booted guest. Never provision/start a VM in a tab."""
    deadline = time.monotonic() + timeout_seconds
    announced = False
    while True:
        state = _api('/api/lifecycle/guest-status')
        current = state.get('state')
        if kind in state.get('guests', ['linux', 'win'] if current == 'ready' else []):
            return
        if current == 'ready':
            raise RuntimeError(kind + ' Bash was not verified at application startup')
        if current == 'failed':
            raise RuntimeError('App-owned Linux/Windows guest startup failed: ' +
                               str(state.get('error') or 'see Server log') +
                               '. Use reboot colima after repairing the underlying error.')
        if current == 'not-installed':
            raise RuntimeError('The native application has not initialized DevTools guests; '
                               'install DevTools or restart the application')
        if current != 'starting':
            raise RuntimeError('Unknown managed guest state: ' + str(current))
        if not announced:
            print('[DEVTOOLS] Waiting for the Linux and Windows guests started at app launch', flush=True)
            announced = True
        if time.monotonic() >= deadline:
            raise RuntimeError('Timed out waiting for app-owned ' + kind + ' Bash guest readiness; '
                               'see Server log. No additional VM was started')
        time.sleep(1)


def _runtime_watchdog_owner(env: dict[str, str]) -> Path:
    return Path(env['COLIMA_HOME']) / 'CinterForgeTerminalLifetime.owner.json'


def _live_runtime_watchdog(native_pid: int, env: dict[str, str]) -> bool:
    """The same native owner keeps its watcher across `reboot server`."""
    receipt = _runtime_watchdog_owner(env)
    if receipt.is_symlink() or not receipt.is_file():
        return False
    try:
        record = json.loads(receipt.read_text(encoding='utf-8'))
        watcher_pid = record.get('watchdog_pid')
        if record.get('native_pid') != native_pid or not isinstance(watcher_pid, int) or watcher_pid <= 1:
            return False
        os.kill(watcher_pid, 0)
        return True
    except (OSError, ValueError, TypeError, PermissionError):
        return False


def _start_runtime_watchdog(native_pid: int, logs: Path) -> None:
    """Acquire the single t-profile lifetime before starting either guest.

    Crash recovery from an earlier app might still be stopping its Colima VM
    when the user relaunches. A pipe acknowledgment makes the new boot wait
    for that older watcher, rather than racing and having its new guests
    stopped by the old watchdog. This runs only in the boot worker thread.
    """
    if native_pid <= 1:
        raise RuntimeError('A native Terminal owner is required for managed guests')
    # `reboot server` replaces only Python/HTTP while keeping the SAME native
    # PID and already-running Colima. Its original independent watcher remains
    # responsible; starting another would deadlock on the lifetime lock.
    sdk = sdk_root()
    if sdk is None:
        raise RuntimeError('Unified DevTools disappeared before guest startup')
    if _live_runtime_watchdog(native_pid, _env(sdk)):
        return
    script = Path(__file__).resolve(strict=True)
    logs.mkdir(parents=True, exist_ok=True)
    read_fd, write_fd = os.pipe()
    watcher = None
    try:
        with (logs / 'guest-lifetime-watchdog.log').open('a', encoding='utf-8') as stream:
            watcher = subprocess.Popen(
                [sys.executable, '-B', str(script), '--runtime-watchdog',
                 str(native_pid), str(write_fd)],
                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True, pass_fds=(write_fd,),
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        os.close(write_fd)
        write_fd = -1
        # The old watchdog can still be completing a bounded Colima shutdown.
        # Keep this wait out of the HTTP thread so Server Log remains usable.
        ready, _, _ = select.select([read_fd], [], [], 300)
        if not ready or os.read(read_fd, 1) != b'1':
            raise RuntimeError('Managed guest watchdog did not acquire exclusive '
                               'Colima lifetime; see guest-lifetime-watchdog.log')
    finally:
        os.close(read_fd)
        if write_fd != -1:
            os.close(write_fd)


def _runtime_watchdog_main(argv: list[str]) -> int:
    """Detached app lifetime owner; serializes sequential native owners.

    The exclusive lifetime lock is held while the native PID exists and until
    its Colima cleanup completes. A new owner cannot start Colima before this
    lock is released; the pipe signals that takeover is complete. The lock
    protects only the dedicated t profile, never Setup's w/c or other VMs.
    """
    if (len(argv) not in (1, 2) or not argv[0].isdigit() or
            int(argv[0]) <= 1 or (len(argv) == 2 and not argv[1].isdigit())):
        raise ValueError('Invalid native Terminal owner')
    native_pid = int(argv[0])
    ready_fd = int(argv[1]) if len(argv) == 2 else None
    import fcntl as local_fcntl
    if sdk_root() is None or platform.system() != 'Darwin':
        return 1 if ready_fd is not None else 0
    env = _env(sdk_root())
    lock_path = Path(env['COLIMA_HOME']) / 'CinterForgeTerminalLifetime.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        # Blocking is deliberate. A previous crashing owner's cleanup *must*
        # finish before the next owner starts this same profile.
        local_fcntl.flock(fd, local_fcntl.LOCK_EX)
        owner = _runtime_watchdog_owner(env)
        pending = owner.with_name(owner.name + '.pending.' + str(os.getpid()))
        if owner.is_symlink() or pending.is_symlink():
            raise RuntimeError('Unsafe managed Terminal lifetime receipt')
        try:
            pending.write_text(json.dumps({'native_pid': native_pid,
                                           'watchdog_pid': os.getpid()}), encoding='utf-8')
            os.chmod(pending, 0o600)
            os.replace(pending, owner)
        finally:
            pending.unlink(missing_ok=True)
        if ready_fd is not None:
            os.write(ready_fd, b'1')
            os.close(ready_fd)
        while True:
            try:
                os.kill(native_pid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                break
            time.sleep(1)
        try:
            stop_runtime_vm()
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            print('[DEVTOOLS WATCHDOG] Guest cleanup FAILED: ' + str(error), flush=True)
            return 1
        return 0
    finally:
        # Only our own acknowledged guardian may remove its receipt. Another
        # app might already be launching once the lock is released.
        owner = _runtime_watchdog_owner(env)
        try:
            record = json.loads(owner.read_text(encoding='utf-8'))
            if record.get('watchdog_pid') == os.getpid() and record.get('native_pid') == native_pid:
                owner.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            pass
        local_fcntl.flock(fd, local_fcntl.LOCK_UN)
        os.close(fd)


def _guest_name(kind: str) -> str:
    if kind not in ('linux', 'win'):
        raise ValueError('Invalid Terminal guest')
    return 'cinterforge-terminal-t-debian'


def _guest_host_binds(sdk: Path, kind: str, workspace: Path | None) -> list[str]:
    """All accessible Mac drives, not a sandbox/allowlist; stable startup cwd."""
    binds = ['--mount', 'type=bind,src=/host/mac,dst=/host/mac']
    mounts = _mac_host_mounts()
    for _, guest in mounts[1:]:
        binds += ['--mount', 'type=bind,src=' + guest + ',dst=' + guest]
    if workspace is not None:
        binds += ['--mount', 'type=bind,src=/host/mac' + str(workspace) + ',dst=/workspace']
    if kind in ('linux', 'win'):
        msys = sdk / 'Windows/MSYS2'
        binds += ['--mount', 'type=bind,src=/host/mac' + str(msys) + ',dst=/msys2,readonly',
                  '--tmpfs', '/msys2/tmp:rw,nosuid,nodev,size=64m']
    return binds


def _guest_workspace() -> Path | None:
    path = os.environ.get('CINTER_CODE_WORKSPACE', '')
    if not path:
        # The selected repo workspace is authoritative. When no explicit
        # folder is selected, use the normal sibling repository parent.
        builds = os.environ.get('CINTER_BUILDS_ROOT', '')
        if builds:
            source = Path(builds)
            if source.name == 'Builds' and source.is_dir():
                return source.parent.resolve()
        return None
    workspace = Path(path).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise RuntimeError('Selected Code-OSS source workspace is not a directory')
    return workspace


def _docker_call(docker: str, env: dict[str, str], *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run([docker, *args], env=env, check=False, text=True,
                          capture_output=True, timeout=timeout)


def _guest_container_failure_evidence(docker: str, env: dict[str, str], name: str) -> str:
    """Collect exit cause before cleanup destroys the only useful evidence.

    App-installed failures may only be reported through the Steam Server log:
    unlike developer Setup, a customer has no access to the builder or a
    persistent Docker container.  Probe only this known, owned container and
    bound each read; never retry/reinstall anything during diagnosis.
    """
    result = _docker_call(
        docker, env, 'container', 'inspect', '--format',
        'status={{.State.Status}} exit={{.State.ExitCode}} '
        'oom={{.State.OOMKilled}} error={{.State.Error}} '
        'finished={{.State.FinishedAt}}', name, timeout=15)
    if result.returncode:
        state = ('inspect unavailable: ' +
                 (result.stderr or result.stdout or 'no Docker response').strip()[-350:])
    else:
        state = (result.stdout or 'no Docker state returned').strip()[-500:]
    logs = _docker_call(docker, env, 'logs', '--tail', '30', name, timeout=15)
    output = ((logs.stdout or '') + '\n' + (logs.stderr or '')).strip()[-1400:]
    if logs.returncode and not output:
        output = 'Docker logs unavailable (exit ' + str(logs.returncode) + ')'
    return 'container ' + name + ' ' + state + '; last Docker output: ' + (output or '(empty)')


def _stop_owned_guests_locked(docker: str, env: dict[str, str]) -> None:
    """Stop exactly the shared owned runtime container, never foreign workloads."""
    for name in {_guest_name('linux'), _guest_name('win')}:
        result = _docker_call(docker, env, 'container', 'inspect', '--format',
                              '{{index .Config.Labels "io.cinterforge.terminal"}}', name)
        if result.returncode:
            continue
        if result.stdout.strip() != PROFILE:
            raise RuntimeError('Refusing to remove unrelated Docker container: ' + name)
        stopped = _docker_call(docker, env, 'stop', '--time', '5', name, timeout=45)
        if stopped.returncode:
            # Stopped containers may have exited before we arrived.
            running = _docker_call(docker, env, 'inspect', '--format', '{{.State.Running}}', name)
            if running.returncode == 0 and running.stdout.strip() == 'true':
                raise RuntimeError('Cannot stop owned Docker guest: ' + name)
        removed = _docker_call(docker, env, 'rm', '-f', name)
        if removed.returncode:
            raise RuntimeError('Cannot remove owned Docker guest: ' + name)


def _start_guest_locked(sdk: Path, env: dict[str, str], docker: str,
                        kind: str, workspace: Path | None) -> None:
    name = _guest_name(kind)
    tar, image = _image_archive(sdk, kind)
    if _docker_call(docker, env, 'image', 'inspect', image).returncode:
        _run_checked([docker, 'load', '--input', str(tar)], env,
                     title='Loading verified ' + kind + ' OCI image', timeout=1200)
    existing = _docker_call(docker, env, 'container', 'inspect', '--format',
                            '{{index .Config.Labels "io.cinterforge.terminal"}}|{{.State.Running}}', name)
    if existing.returncode == 0:
        owner, _, running = existing.stdout.strip().partition('|')
        if owner != PROFILE:
            raise RuntimeError('Existing Docker container is not Terminal-owned: ' + name)
        if running == 'true':
            # A named running container is not proof Bash actually works.
            # Wine needs the guest display before it initializes its prefix.
            try:
                if kind == 'win':
                    _ensure_wine_display(docker, env, name)
                _verify_guest_bash(docker, env, kind, name)
                _activate_guest_vnc(docker, env, kind, name)
            except Exception as error:
                # The one shared container can exit between inspect and exec.
                # Keep its exit evidence even on the reuse path.
                try:
                    evidence = _guest_container_failure_evidence(docker, env, name)
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as diagnostic_error:
                    evidence = 'Docker exit evidence unavailable: ' + str(diagnostic_error)
                raise RuntimeError(str(error) + '; ' + evidence) from error
            return
        _stop_owned_guests_locked_one(docker, env, name)
    environment = ['-e', 'MSYSTEM=UCRT64', '-e', 'CHERE_INVOKING=1',
                   '-e', 'HOME=/tmp', '-e', 'TMPDIR=/tmp', '-e', 'TMP=/tmp', '-e', 'TEMP=/tmp']
    # Both loopback VNC endpoints belong to ONE persistent guest container.
    # Setup includes GUI dependencies; the two displays remain independent.
    args = [docker, 'run', '-d', '--restart', 'no', '--name', name,
            '--label', 'io.cinterforge.terminal=' + PROFILE,
            '--platform', 'linux/amd64',
            '--publish', f'127.0.0.1:{_LAUNCHER_PORTS["linux"]}:6080',
            '--publish', f'127.0.0.1:{_LAUNCHER_PORTS["win"]}:6081',
            *_guest_host_binds(sdk, kind, workspace),
            *environment, '--workdir', '/workspace' if workspace else '/',
            # Do not use a shell-owned background sleep/wait loop as PID 1.
            # An interactive shell is created only by an explicit docker exec;
            # this foreground keepalive has no inherited stdin or shell traps.
            '--entrypoint', '/bin/sleep', image, 'infinity']
    _run_checked(args, env, title='Starting one reusable ' + kind + ' guest', timeout=120)
    try:
        if kind == 'win':
            _ensure_wine_display(docker, env, name)
        _verify_guest_bash(docker, env, kind, name)
        _activate_guest_vnc(docker, env, kind, name)
    except Exception as error:
        # Docker exec only says "container ... is not running". Capture the
        # real exit status / OOM flag / entrypoint stderr BEFORE removing it.
        # There is exactly one named Debian container for Linux and Windows.
        try:
            evidence = _guest_container_failure_evidence(docker, env, name)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as diagnostic_error:
            evidence = 'Docker exit evidence unavailable: ' + str(diagnostic_error)
        # Linux and Windows are two capabilities of THIS SAME container.
        # A broken Windows Wine/VNC probe must not delete the functioning
        # Linux Bash container (and vice versa).  Only remove it when Docker
        # confirms that the shared container has actually stopped.
        try:
            running = _docker_call(docker, env, 'container', 'inspect', '--format',
                                   '{{.State.Running}}', name, timeout=15)
            if running.returncode or running.stdout.strip() != 'true':
                _stop_owned_guests_locked_one(docker, env, name)
            else:
                evidence += '; keeping running shared guest for the other platform'
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as cleanup_error:
            evidence += '; cleanup inspection failed: ' + str(cleanup_error)
        raise RuntimeError(str(error) + '; ' + evidence) from error
    except BaseException:
        _stop_owned_guests_locked_one(docker, env, name)
        raise


def _ensure_wine_display(docker: str, env: dict[str, str], name: str) -> None:
    """Start the owned headless framebuffer *before* Wine first-run.

    It stays in the same Windows guest container and is reused by Launcher.
    No GUI is opened in the app until the Launcher tab is explicitly chosen.
    """
    script = r'''set -eu
command -v Xvfb >/dev/null
command -v xdpyinfo >/dev/null
if ! DISPLAY=:100 xdpyinfo >/dev/null 2>&1; then
  nohup Xvfb :100 -screen 0 1600x900x24 -nolisten tcp >/tmp/cinter-launcher-win-xvfb.log 2>&1 </dev/null &
fi
for n in 1 2 3 4 5 6 7 8 9 10; do
  if DISPLAY=:100 xdpyinfo >/dev/null 2>&1; then exit 0; fi
  sleep 1
done
cat /tmp/cinter-launcher-win-xvfb.log >&2 || true
exit 18
'''
    result = _docker_call(docker, env, 'exec', name, '/bin/sh', '-c', script, timeout=35)
    if result.returncode:
        raise RuntimeError('Windows guest framebuffer failed before Wine: ' +
                           (result.stderr or result.stdout or '').strip()[-700:])


def _guest_verification_probe(docker: str, env: dict[str, str], name: str,
                              stage: str, command: list[str], *, timeout: int,
                              expected: str = '') -> subprocess.CompletedProcess:
    """Verify a real owned guest with bounded time and visible idle progress.

    A Docker exec may provide no stdout while Wine initializes. Never mistake
    that silence for a successful guest; report the exact layer and exit code.
    The lifecycle console already persists these messages under Builds/Logs.
    """
    started = time.monotonic()
    completed = threading.Event()
    print('[DEVTOOLS] Windows Bash probe START ' + stage, flush=True)

    def heartbeat() -> None:
        while not completed.wait(15):
            print('[DEVTOOLS] Windows Bash probe WORKING ' + stage +
                  ' (' + str(int(time.monotonic() - started)) + 's)', flush=True)

    ticker = threading.Thread(target=heartbeat, daemon=True)
    ticker.start()
    try:
        try:
            result = _docker_call(docker, env, *command, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('win guest Bash verification timed out during ' + stage +
                               ' after ' + str(timeout) + 's') from error
    finally:
        completed.set()
        ticker.join(timeout=1)

    output = ((result.stdout or '') + '\n' + (result.stderr or '')).strip()
    if result.returncode != 0:
        signal = (' (SIGSEGV / signal 11)' if result.returncode in (139, -11) else '')
        diagnostic = output[-1200:] if output else 'no guest output'
        raise RuntimeError('win guest Bash verification exited ' + str(result.returncode) +
                           signal + ' during ' + stage + ': ' + diagnostic)
    if expected and expected.lower() not in output.lower():
        raise RuntimeError('win guest Bash verification did not confirm ' + stage +
                           ': ' + (output[-700:] if output else 'no guest output'))
    print('[DEVTOOLS] Windows Bash probe PASS ' + stage +
          ' (' + str(round(time.monotonic() - started, 1)) + 's)', flush=True)
    return result


def _verify_guest_bash(docker: str, env: dict[str, str], kind: str, name: str) -> None:
    if kind == 'linux':
        print('[DEVTOOLS] Verifying reusable linux guest', flush=True)
        result = _docker_call(docker, env, 'exec', name, '/bin/bash', '--version', timeout=150)
        if result.returncode:
            diagnostic = (result.stderr or result.stdout or '').strip()[-1200:]
            raise RuntimeError('linux guest Bash verification exited ' + str(result.returncode) +
                               (': ' + diagnostic if diagnostic else '; no guest output'))
        return

    # Check each layer before launching the real Windows MSYS2 shell. An exit
    # 139 from a qemu-backed guest previously carried no stdout or stderr and
    # could not distinguish a broken Linux binary translator, Wine startup,
    # or the Windows PE/Cygwin runtime. These are read-only smoke commands;
    # they neither provision a dependency nor substitute a simulated shell.
    common = ['exec', *_launcher_guest_env('win'), name]
    _guest_verification_probe(docker, env, name, 'amd64 Linux userspace',
                              ['exec', name, '/bin/bash', '--version'],
                              timeout=30, expected='GNU bash')
    _guest_verification_probe(docker, env, name, 'Wine loader',
                              [*common, 'wine', '--version'], timeout=45,
                              expected='wine-')
    _guest_verification_probe(docker, env, name, 'Wine Windows PE execution',
                              [*common, 'wine', 'cmd.exe', '/c', 'echo',
                               'CINTER_WINE_PE_READY'], timeout=90,
                              expected='CINTER_WINE_PE_READY')
    # The base MSYS2 Bash starts without its PATH when --noprofile is
    # specified. Test actual shell plus coreutils, not merely wine --version.
    _guest_verification_probe(
        docker, env, name, 'MSYS2 UCRT64 Bash + coreutils',
        [*common, 'wine', r'Z:\msys2\usr\bin\bash.exe', '--noprofile', '--norc',
         '-c', 'export PATH=/usr/bin:/ucrt64/bin:$PATH; '
               'test -x /usr/bin/rm.exe || exit 73; '
               'f=/tmp/cinterforge-bash-$$; : > "$f" && rm "$f" && '
               '/usr/bin/uname -s'],
        timeout=150, expected='msys')


def _stop_owned_guests_locked_one(docker: str, env: dict[str, str], name: str) -> None:
    _docker_call(docker, env, 'rm', '-f', name)


def boot_runtime_guests() -> dict:
    """On app startup: one VM, one persistent Debian guest, two desktops."""
    if platform.system() != 'Darwin':
        return {'platform': platform.system(), 'guests': 'native'}
    sdk = sdk_root()
    if sdk is None:
        return {'installed': False, 'guests': 'not-started'}
    env = _env(sdk)
    workspace = _guest_workspace()
    with _managed_vm_lock(env):
        docker = _ensure_docker_locked(sdk, env, workspace)
        ready, failures = [], {}
        for kind in ('linux', 'win'):
            try:
                _start_guest_locked(sdk, env, docker, kind, workspace)
                ready.append(kind)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                failures[kind] = str(error)
                print('[DEVTOOLS] ' + kind + ' guest FAILED: ' + str(error), flush=True)
        if not ready:
            # No verified capability depends on this container. Capture of
            # each startup failure has already happened before this cleanup.
            # Do not keep a completely unusable guest alive until app exit.
            # A cleanup failure must NOT mask the platform-specific cause.
            try:
                _stop_owned_guests_locked(docker, env)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as cleanup_error:
                failures['cleanup'] = str(cleanup_error)
                print('[DEVTOOLS] Unusable shared guest cleanup FAILED: ' +
                      str(cleanup_error), flush=True)
    return {'installed': True, 'guests': ready, 'errors': failures,
            'docker': 'one-managed-engine'}


def stop_runtime_vm(*, setup_sdk: Path | None = None) -> None:
    """Stop the app-owned VM on normal quit or watchdog-detected native exit.

    If the host Docker context has broken, inspect the same t profile through
    Colima SSH before deciding it is safe to stop. Never take down an unrelated
    container just because the host socket failed during application exit.
    """
    if platform.system() != 'Darwin':
        return
    sdk = setup_sdk if setup_sdk is not None else sdk_root()
    if sdk is None:
        return
    env = _env(sdk)
    colima = str(sdk / 'mac/env/bin/colima')
    docker = str(sdk / 'mac/env/bin/docker')
    with _managed_vm_lock(env):
        status = subprocess.run([colima, 'status', PROFILE], env=env,
                                check=False, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=25)
        if status.returncode != 0:
            return
        engine = _docker_call(docker, env, 'info', '--format', '{{.ServerVersion}}', timeout=20)
        if engine.returncode == 0:
            _stop_owned_guests_locked(docker, env)
            foreign = _docker_call(docker, env, 'ps', '--quiet', timeout=25)
            if foreign.returncode or foreign.stdout.strip():
                raise RuntimeError('Leaving VM running: unowned Docker containers may be active')
        else:
            # A stale Docker context can coexist with a healthy daemon *inside*
            # Colima. Use the named VM's SSH connection; never guess from an
            # unavailable host socket that the VM has no running workloads.
            listing = subprocess.run(
                [colima, 'ssh', '--profile', PROFILE, '--', 'docker', 'ps',
                 '--format', '{{.Names}}|{{.Label "io.cinterforge.terminal"}}'],
                env=env, check=False, text=True, capture_output=True, timeout=35)
            if listing.returncode:
                raise RuntimeError('Cannot inspect the owned Colima Docker engine during shutdown; '
                                   'refusing to stop potentially unrelated containers: ' +
                                   (listing.stderr or listing.stdout).strip()[:240])
            for row in listing.stdout.splitlines():
                name, sep, owner = row.partition('|')
                if not sep or name not in (_guest_name('linux'), _guest_name('win')) or owner != PROFILE:
                    raise RuntimeError('Leaving VM running: unowned Docker container ' + name)
            for row in listing.stdout.splitlines():
                name = row.partition('|')[0]
                removed = subprocess.run([colima, 'ssh', '--profile', PROFILE, '--',
                                          'docker', 'rm', '-f', name], env=env, check=False,
                                         text=True, capture_output=True, timeout=45)
                if removed.returncode:
                    raise RuntimeError('Cannot stop owned container ' + name +
                                       ': ' + (removed.stderr or removed.stdout).strip()[:240])
        _run_checked([colima, 'stop', PROFILE], env,
                     title='Stopping app-owned guest VM', timeout=120)


def reboot_runtime_guests(component: str) -> dict:
    """Never recompile, download, install, Stage, ZIP or touch Setup VMs."""
    if component not in ('docker', 'colima'):
        raise ValueError('Usage: reboot docker|colima')
    sdk = sdk_root()
    if sdk is None:
        raise RuntimeError('Install DevTools before rebooting Docker or Colima')
    env = _env(sdk)
    colima = str(sdk / 'mac/env/bin/colima')
    docker = str(sdk / 'mac/env/bin/docker')
    workspace = _guest_workspace()
    with _managed_vm_lock(env):
        _stop_owned_guests_locked(docker, env)
        if component == 'colima':
            foreign = _docker_call(docker, env, 'ps', '--quiet')
            if foreign.returncode or foreign.stdout.strip():
                raise RuntimeError('Cannot reboot Colima while unowned containers are active')
            _run_checked([colima, 'stop', PROFILE], env, title='Rebooting owned Colima', timeout=120)
            docker = _ensure_docker_locked(sdk, env, workspace)
        else:
            # Colima's default Linux guest uses OpenRC; tolerate systemd-based
            # images only when their actual service command exists. Never
            # silently substitute a destructive VM restart for docker reboot.
            services = (('sudo', 'rc-service', 'docker', 'restart'),
                        ('sudo', 'systemctl', 'restart', 'docker'),
                        ('sudo', 'service', 'docker', 'restart'))
            restarted = False
            for service in services:
                check = subprocess.run([colima, 'ssh', '--profile', PROFILE, '--', *service],
                                       env=env, text=True, capture_output=True,
                                       timeout=120, check=False)
                if check.returncode == 0:
                    restarted = True
                    break
            if not restarted:
                raise RuntimeError('Could not restart managed Docker daemon in the existing Colima VM')
            for attempt in range(30):
                ping = _docker_call(docker, env, 'info', '--format', '{{.ServerVersion}}',
                                    timeout=10)
                if ping.returncode == 0:
                    break
                if attempt == 29:
                    raise RuntimeError('Managed Docker did not become ready after daemon restart')
                time.sleep(2)
        for kind in ('linux', 'win'):
            _start_guest_locked(sdk, env, docker, kind, workspace)
    return {'rebooted': component, 'guests': ['linux', 'win'], 'ready': True}



# ============================================================================
# SHARED GRAPHICAL LAUNCHERS: one runtime display per owned guest, never a KDE
# image or another VM. Setup provisions these packages in existing OCI images.
# The launcher is dormant until selected; guest Bash and Code share DISPLAY.
# ============================================================================
_LAUNCHER_PORTS = {'linux': 20601, 'win': 20602}
_LAUNCHER_APPS = frozenset(('CinterForgeTerminal',))
_launcher_lock = threading.RLock()


def _launcher_guest_env(kind: str) -> list[str]:
    result = ['-e', 'DISPLAY=' + (':100' if kind == 'win' else ':99'),
              '-e', r'PS1=\u@\h \w \$ ']
    if kind == 'win':
        # Match the guest's existing MSYS2 Bash environment and prefix, not
        # the separate builder-only Wine PREFIX or a second Windows image.
        result += ['-e', 'HOME=/tmp', '-e', 'WINEPREFIX=/tmp/.wine']
    return result


def _launcher_container(kind: str, server=None) -> tuple[str, dict[str, str], str]:
    if kind not in _LAUNCHER_PORTS or platform.system() != 'Darwin':
        raise RuntimeError('Remote graphical launcher requires the managed macOS DevTools guest')
    sdk = sdk_root()
    if sdk is None:
        raise RuntimeError('Optional unified DevTools is not installed')
    if server is None:
        _wait_for_app_guests(kind)  # CLI children carry a trusted shell token.
    else:
        # Browser requests are already authenticated by Handler. Check that
        # native application's guest startup finished without a recursive HTTP
        # request requiring shell-only credentials.
        deadline = time.monotonic() + 120
        while True:
            with server.guest_start_lock:
                state, error = server.guest_start_state, server.guest_start_error
            if kind in getattr(server, 'guest_ready', ['linux', 'win'] if state == 'ready' else []):
                break
            if state != 'starting':
                detail = getattr(server, 'guest_errors', {}).get(kind, error or state)
                raise RuntimeError('Managed guest unavailable: ' + detail)
            if server._stopping or time.monotonic() >= deadline:
                raise RuntimeError('Managed guest startup timed out; check the Server log')
            time.sleep(0.5)
    env = _env(sdk)
    return str(sdk / 'mac/env/bin/docker'), env, _guest_name(kind)


def _verify_guest_vnc(docker: str, env: dict[str, str], kind: str, name: str) -> None:
    """Verify actual RFB handshake and noVNC HTTP on the retained guest."""
    rfb_port = 5901 if kind == 'win' else 5900
    web_port = 6081 if kind == 'win' else 6080
    script = r'''import socket, time, urllib.request, sys
rfb_port, web_port = map(int, sys.argv[1:])
error = 'not ready'
for i in range(25):
    try:
        with socket.create_connection(('127.0.0.1', rfb_port), timeout=2) as conn:
            banner = conn.recv(12)
            if len(banner) != 12 or not banner.startswith(b'RFB '):
                raise RuntimeError('invalid RFB handshake: ' + repr(banner))
    except (OSError, RuntimeError, ValueError) as exc:
        error = 'RFB port %d: %s' % (rfb_port, exc)
        time.sleep(0.3)
        continue
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/vnc.html' % web_port, timeout=2) as response:
            if response.status != 200 or b'<html' not in response.read(2048).lower():
                raise RuntimeError('noVNC HTTP endpoint incomplete')
    except (OSError, RuntimeError, ValueError) as exc:
        error = 'noVNC port %d: %s' % (web_port, exc)
        time.sleep(0.3)
        continue
    print('CINTER_VNC_READY')
    break
else:
    raise RuntimeError('VNC unavailable: ' + error)
'''
    result = _docker_call(docker, env, 'exec', name, 'python3', '-c', script,
                          str(rfb_port), str(web_port), timeout=90)
    if result.returncode or 'CINTER_VNC_READY' not in (result.stdout or ''):
        raise RuntimeError(kind + ' guest VNC handshake failed: ' +
                           (result.stderr or result.stdout or '').strip()[-650:])


def _activate_guest_vnc(docker: str, env: dict[str, str], kind: str, name: str) -> dict:
    with _launcher_lock:
        # Do not install/repair packages at runtime. All dependencies belong
        # to Setup's existing pinned guest images and the unified SDK.
        probe_script = ("for tool in Xvfb xdpyinfo openbox x11vnc websockify feh pgrep; do "
                        'command -v "$tool" >/dev/null 2>&1 || echo missing:$tool; done; '
                        "test -f /usr/share/novnc/vnc.html || echo missing:novnc; true")
        probe = _docker_call(docker, env, 'exec', name, '/bin/sh', '-c', probe_script,
                             timeout=30)
        if probe.returncode or (probe.stdout or '').strip():
            detail = (probe.stdout or probe.stderr or 'probe failed').strip()[-400:]
            raise RuntimeError('Guest GUI tools missing from staged ' + kind + ' image (' + detail +
                               '); rebuild DevTools in Setup, then Build/Stage')
        # The guest and host display the SAME replaceable platform PNG. Copy
        # the staged resource into the owned runtime container; never install
        # packages or write to Builds from a downloaded application.
        wallpaper_name = 'windows' if kind == 'win' else 'linux'
        wallpaper = ASSETS / 'launcher/assets/backgrounds' / (wallpaper_name + '.png')
        if not wallpaper.is_file() or wallpaper.is_symlink():
            raise RuntimeError('Missing staged ' + kind + ' Launcher wallpaper')
        copied = _docker_call(docker, env, 'cp', str(wallpaper),
                              name + ':/tmp/cinterforge-launcher-' + wallpaper_name + '-wallpaper.png', timeout=30)
        if copied.returncode:
            raise RuntimeError('Unable to load ' + kind + ' Launcher wallpaper: ' + copied.stderr.strip()[:400])
        # Exact one-off helper script is idempotent within this owned container.
        # No root-level host agent, no additional VM; Docker container removal
        # terminates every helper when the application exits.
        # Independent desktops inside ONE Debian guest. Do not use global
        # `pgrep -x openbox/x11vnc`: another display can already own that PID.
        display = ':100' if kind == 'win' else ':99'
        rfb_port = 5901 if kind == 'win' else 5900
        web_port = 6081 if kind == 'win' else 6080
        script = r'''set -eu
DISPLAY="$1"
RFB="$2"
WEB="$3"
WALLPAPER="$4"
PREFIX="$5"
if ! DISPLAY="$DISPLAY" xdpyinfo >/dev/null 2>&1; then
  nohup Xvfb "$DISPLAY" -screen 0 1600x900x24 -nolisten tcp >"/tmp/cinter-${PREFIX}-xvfb.log" 2>&1 </dev/null &
fi
for n in 1 2 3 4 5 6 7 8 9 10; do
  DISPLAY="$DISPLAY" xdpyinfo >/dev/null 2>&1 && break
  sleep 1
done
DISPLAY="$DISPLAY" xdpyinfo >/dev/null 2>&1 || exit 18
# Per-desktop helpers are keyed to their display and network port.
if ! test -s "/tmp/cinter-${PREFIX}-openbox.pid" ||
   ! kill -0 "$(cat "/tmp/cinter-${PREFIX}-openbox.pid")" 2>/dev/null; then
  DISPLAY="$DISPLAY" nohup openbox --sm-disable >"/tmp/cinter-${PREFIX}-openbox.log" 2>&1 </dev/null &
  echo $! >"/tmp/cinter-${PREFIX}-openbox.pid"
fi
DISPLAY="$DISPLAY" feh --bg-fill "$WALLPAPER"
if ! python3 -c 'import socket,sys;s=socket.socket();s.settimeout(0.5);r=s.connect_ex(("127.0.0.1",int(sys.argv[1])));s.close();sys.exit(0 if r==0 else 1)' "$RFB"; then
  DISPLAY="$DISPLAY" nohup x11vnc -display "$DISPLAY" -rfbport "$RFB" -localhost -forever -shared -nopw >"/tmp/cinter-${PREFIX}-vnc.log" 2>&1 </dev/null &
fi
if ! python3 -c 'import socket,sys;s=socket.socket();s.settimeout(0.5);r=s.connect_ex(("127.0.0.1",int(sys.argv[1])));s.close();sys.exit(0 if r==0 else 1)' "$WEB"; then
  nohup websockify --web /usr/share/novnc "$WEB" "127.0.0.1:$RFB" >"/tmp/cinter-${PREFIX}-web.log" 2>&1 </dev/null &
fi
'''
        result = _docker_call(docker, env, 'exec', *_launcher_guest_env(kind),
                              name, '/bin/bash', '-c', script, 'gui', display,
                              str(rfb_port), str(web_port),
                              '/tmp/cinterforge-launcher-' + wallpaper_name + '-wallpaper.png',
                              kind, timeout=45)
        if result.returncode:
            raise RuntimeError('Guest graphical display startup failed: ' +
                               (result.stderr or result.stdout).strip()[-600:])
        try:
            _verify_guest_vnc(docker, env, kind, name)
        except RuntimeError as error:
            # The image's transient preflight starts its own display, so its
            # earlier PASS cannot diagnose a failed *retained* runtime helper.
            # Capture the guest-local startup logs before the owning container
            # is torn down by Setup's failure cleanup.
            details = ''
            try:
                diagnostic = _docker_call(
                    docker, env, 'exec', name, '/bin/sh', '-c',
                    'for service in xvfb openbox vnc web; do '
                    'file=/tmp/cinter-' + kind + '-$service.log; '
                    'if test -f "$file"; then printf "\n[%s]\n" "$service"; '
                    'tail -n 18 "$file"; fi; done', timeout=20)
                details = (diagnostic.stdout or diagnostic.stderr or '').strip()[-1800:]
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise RuntimeError(str(error) + ('\nGuest display logs:\n' + details
                                             if details else '')) from error
        port = _LAUNCHER_PORTS[kind]
        # A forwarded Colima localhost port is not an Internet service.
        # Its browser frame appears only after an explicit Launcher action.
        return {'platform': kind, 'viewer_url':
                f'http://127.0.0.1:{port}/vnc.html?autoconnect=true&resize=scale&show_dot=true'}


def _launcher_start(kind: str, server=None) -> dict:
    docker, env, name = _launcher_container(kind, server)
    return _activate_guest_vnc(docker, env, kind, name)

def _launcher_staged_app(kind: str) -> Path:
    """Read-only resolution of the verified Steam Stage for one platform.

    Never fall back to a developer binary, a source checkout, or an arbitrary
    executable supplied by a WebView. A missing stage is a visible error.
    """
    if kind not in ('mac', 'linux', 'win'):
        raise ValueError('Unknown CinterForge Terminal launcher platform')
    steam = _steam_library_for_installed_root(ASSETS.parent)
    if steam is None:
        raise RuntimeError('No installed CinterForge Steam library for staged application')
    release = _devtools_release()
    platform_folder = {'mac': 'macOS', 'linux': 'Linux', 'win': 'Windows'}[kind]
    root = (steam / 'common/CinterForge/Applications' / platform_folder /
            'CinterForge Terminal' / release)
    receipt = root / 'stage-receipt.json'
    executable = root / ('CinterForge Terminal.app/Contents/MacOS/CinterForgeTerminal'
                         if kind == 'mac' else 'CinterForge Terminal/' +
                         ('Terminal.sh' if kind == 'linux' else 'Terminal.cmd'))
    # Older installed payloads sometimes stored their receipt inside the
    # application directory. Do not confuse that with a missing build; still
    # require a valid stage receipt for the selected platform and release.
    if not receipt.is_file() and kind in ('linux', 'win'):
        receipt = executable.parent / 'stage-receipt.json'
    # Verify the Stage marker and each path component. A signed staged app or
    # immutable guest payload must not be swapped via an untrusted symlink.
    for path in (root, receipt, executable, *executable.parents):
        if path.is_symlink():
            raise RuntimeError('Staged CinterForge Terminal contains a redirected path: ' + str(path))
    if not receipt.is_file() or not executable.is_file():
        raise RuntimeError('Missing staged ' + platform_folder + ' CinterForge Terminal build for release ' +
                           release + '; run the normal Build/Stage lifecycle')
    try:
        record = json.loads(receipt.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('Cannot read staged ' + platform_folder + ' build receipt') from exc
    if (record.get('schema') not in (FOUR_SCHEMA, 'cinterforge.terminal.unpacked-build.v1') or
            record.get('target') != kind or record.get('release') != release):
        raise RuntimeError('Staged ' + platform_folder + ' CinterForge Terminal receipt does not match ' + release)
    if record['schema'] == 'cinterforge.terminal.unpacked-build.v1' and (
            not isinstance(record.get('build_fingerprint'), str) or
            not isinstance(record.get('installed_state'), str)):
        raise RuntimeError('Staged ' + platform_folder + ' unpacked Build receipt is incomplete')
    return executable


def _launcher_launch(kind: str, app: str, server=None) -> dict:
    if app not in _LAUNCHER_APPS or kind not in ('mac', 'linux', 'win'):
        raise ValueError('Unknown CinterForge launcher application')
    staged = _launcher_staged_app(kind)
    if kind == 'mac':
        # Launch/focus the real staged .app on the host, never in the WebView.
        # In particular, do not redirect to the user's system Terminal.app.
        if platform.system() != 'Darwin':
            raise RuntimeError('The staged Mac application requires a macOS host')
        app_bundle = staged.parents[2]   # .../CinterForge Terminal.app
        started = subprocess.run(['/usr/bin/open', '-a', str(app_bundle)],
                                 capture_output=True, text=True, timeout=20, check=False)
        if started.returncode:
            raise RuntimeError('Cannot open staged Mac CinterForge Terminal: ' +
                               started.stderr.strip()[-400:])
        return {'opened': True, 'platform': kind, 'external': True}

    # Both graphical apps run inside the ONE managed Debian Docker guest.
    # Linux uses DISPLAY=:99; Windows/Wine uses independent DISPLAY=:100. The X11/VNC framebuffer remains
    # inside its one Launcher Session tab. No second VM or detached desktop.
    _launcher_start(kind, server)
    docker, env, name = _launcher_container(kind, server)
    guest_executable = '/host/mac' + str(staged)
    present = _docker_call(docker, env, 'exec', name, 'test', '-f', guest_executable,
                           timeout=20)
    if present.returncode:
        raise RuntimeError('Staged ' + kind + ' build is not visible through the managed guest mount: ' +
                           str(staged))
    if kind == 'linux':
        # Run the staged launcher script rather than its raw ELF executable:
        # it owns runtime Python, worker, service and resource arguments.
        command = ['/bin/sh', '-c',
                   'cd "$(dirname "$1")" && exec /bin/sh "$1" >>/tmp/cinterforge-staged-app.log 2>&1',
                   'cinterforge-launch', guest_executable]
    else:
        # Preserve the staged Windows .cmd wrapper, including its WebView2 and
        # runtime setup, instead of invoking an incomplete bare PE executable.
        # Wine's Z: drive maps to the Linux filesystem mounted at /host/mac.
        win_path = 'Z:' + guest_executable.replace('/', '\\')
        command = ['/bin/sh', '-c',
                   'cd "$(dirname "$1")" && exec wine cmd.exe /c "$2" >>/tmp/cinterforge-staged-app.log 2>&1',
                   'cinterforge-launch', guest_executable, win_path]
    launched = _docker_call(docker, env, 'exec', '-d', *_launcher_guest_env(kind),
                            name, *command, timeout=45)
    if launched.returncode:
        raise RuntimeError('Could not launch staged ' + kind + ' CinterForge Terminal: ' +
                           (launched.stderr or launched.stdout).strip()[-400:])
    # Docker's detached launch acknowledges submission, not an application
    # readiness proof. The guest desktop will show its actual window if ready.
    return {'opened': True, 'platform': kind, 'external': False,
            'message': 'Staged application launch requested'}


def guest_shell_main(kind: str) -> int:
    if kind not in ('win', 'linux') or platform.system() != 'Darwin':
        raise RuntimeError('DevTools shell bridge is only for Windows/Linux tabs on macOS')
    sdk = sdk_root()
    if sdk is None:
        print(f'{kind.upper()} Bash needs the optional unified DevTools package.', flush=True)
        if install_devtools() != 0:
            return 1
        sdk = sdk_root()
        if sdk is None:
            raise RuntimeError('DevTools installer completed without usable SDK')
        # The verified installer already requested a native app relaunch.
        # Do not open a PTY or race that owner's pending shutdown/restart.
        print('[DEVTOOLS] The Terminal app is relaunching; reopen the guest Bash in Code-OSS.', flush=True)
        return 0
    _wait_for_app_guests(kind)
    print('[DEVTOOLS] ' + kind + ' Bash is already connected. Open/focus its existing '
          'Code-OSS integrated terminal; Session does not create guest Bash.', flush=True)
    return 0



if __name__ == '__main__':
    raise SystemExit(main())
