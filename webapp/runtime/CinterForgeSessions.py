"""CinterForgeSessions — CinterForge Terminal application runtime.

PURPOSE: Native Mac Bash and Windows PTY/ConPTY sessions, session environment, writable paths, user/Steam directory selection.
METHODS: target_platform, _bounded_size, _windows_msys2_root, _shell_command, _mac_catalog, _install_shell_commands, _real_login_home, _installed_cache_root, _session_environment, CinterForgeTerminalSession, CinterForgeBedrockTerminalSession, _load_bedrock_manager, _runtime_steam_locations, _cinterforge_steam_manifest, _steam_library_for_installed_root, _terminal_mutable_root, _runtime_logs_root, _devtools_menu_ready, open_bedrock_terminal
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
    from runtime import CinterForgeSettings as _CinterForgeSettings
    for ancestor in _CinterForgeSettings.ASSETS.resolve().parents:
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
    from runtime import CinterForgeSettings as _CinterForgeSettings
    data = json.loads((_CinterForgeSettings.ASSETS / 'data/mac_games.json').read_text(encoding='utf-8'))
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
    from runtime import CinterForgeSettings as _CinterForgeSettings
    directory = root / 'scripts'
    directory.mkdir(parents=True, exist_ok=True)
    cli = _CinterForgeSettings.ASSETS / 'CinterForgeTerminal.py'
    if not cli.is_file():
        raise RuntimeError(f'Missing staged CinterForgeTerminal command helper: {cli}')
    python = str(Path(sys.executable).resolve())
    functions = '\n'.join(
        f'{name}() {{ {shlex.quote(python)} {shlex.quote(str(cli))} --terminal-cli {name} "$@"; }}'
        for name in ('help', 'play', 'devtools', 'debugger', 'bedrock', 'minecraft', 'lagomorph', 'infinite-mac', 'wargames', 'discord', 'setpass', 'code', 'htmlmenus', 'craftmaster'))
    # These exact source filenames are the shell commands. Load the bundled
    # Terminal manifest; no sibling WebApp source or story package is required.
    catalog = json.loads((_CinterForgeSettings.ASSETS / 'data/if_titles.json').read_text(encoding='utf-8'))
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
    if set(mac_names) & set(['help', 'play', 'devtools', 'debugger', 'bedrock', 'minecraft', 'lagomorph', 'infinite-mac', 'wargames', 'games', 'stories', 'install', 'craftmaster'] + story_names):
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
                + 'if [ "${CINTER_TERMINAL_BRIDGE:-}" = bedrock ]; then\n'
                + "  PS1=''\n  stty -echo\n"
                + "else\n  PS1='\\u@\\h \\w \\$ '\n"
                + "  printf 'Welcome to CinterForge. Type help for instructions.\\n'\n"
                + "fi\n"
                + "HISTIGNORE='setpass *'\n")  # Do not retain credentials in Bash history.

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


def _standalone_state_root(*, create: bool = True) -> Path:
    """Writable user state for a downloaded Terminal without Steam.

    This is NOT the signed app's embedded Builds directory and never starts a
    developer tool installation. Steam-installed apps retain their Steam state.
    """
    home = _real_login_home()
    if platform.system() == 'Darwin':
        path = home / 'Library/Application Support/CinterForgeTerminal'
    elif platform.system() == 'Windows':
        path = home / 'AppData/Local/CinterForgeTerminal/State'
    else:
        path = home / '.local/share/CinterForgeTerminal'
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise RuntimeError('Unsafe standalone Terminal state directory: ' + str(path))
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path.resolve(strict=create)


def _standalone_logs_root() -> Path:
    """Use normal OS user logs when there is no installed Steam library."""
    home = _real_login_home()
    if platform.system() == 'Darwin':
        return home / 'Library/Logs/CinterForgeTerminal'
    if platform.system() == 'Windows':
        return home / 'AppData/Local/CinterForgeTerminal/Logs'
    return home / '.local/state/CinterForgeTerminal/Logs'


def _session_environment(builds: Path, system: str) -> dict[str, str]:
    """Keep installed state in Steam or user support, transient files in cache."""
    from runtime import CinterForgeSettings as _CinterForgeSettings
    installed = builds.name != 'Builds'
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
        # A standalone DMG is not an implicit Steam installation. Never pass
        # an invented steamapps path to commands launched from a PTY.
        steam = _steam_library_for_installed_root(builds)
        if steam is not None:
            env['CINTER_STEAMAPPS'] = str(steam)
    # In the installed Host tree this directory is named <release>.
    # The checkout path uses the same canonical pinned release as the builder.
    release = _CinterForgeSettings.ASSETS.parent.name
    if not (release[:4].isdigit() and release.count('.') == 2):
        release = _CinterForgeSettings.CINTERFORGE_TERMINAL_RELEASE
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
    if installed:
        # Installed apps use user cache and optional consented DevTools; merely
        # launching them never creates a development Builds directory.
        env.pop('CINTER_BUILDS_ROOT', None)
        env['CINTER_TERMINAL_RUNTIME_MODE'] = 'installed'
    if system == 'win':
        env.update({'USERPROFILE': str(paths['home']), 'APPDATA': str(paths['data']),
                    'LOCALAPPDATA': str(paths['data'])})
    return env


class CinterForgeTerminalSession:
    def __init__(self, builds: Path, system: str, cols: int, rows: int, *, url: str, token: str,
                 command: list[str] | None = None, working_directory: Path | None = None,
                 output_log: Path | None = None, internal_token: str | None = None,
                 child_env: dict[str, str] | None = None,
                 command_session_id: str | None = None):
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
        env['CINTER_TERMINAL_SESSION_ID'] = command_session_id or self.id
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
            # The session environment starts from the native process. Remove
            # inherited Docker selectors before applying the verified managed
            # socket: a stale DOCKER_CONTEXT overrides DOCKER_HOST and breaks
            # the real Linux/Windows Bash PTYs even after _env() was fixed.
            # Native Mac Bash (without child_env) keeps its normal user env.
            if 'DOCKER_HOST' in child_env:
                for key in ('DOCKER_CONTEXT', 'DOCKER_HOST',
                            'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
                    env.pop(key, None)
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
            if env.get('CINTER_TERMINAL_BRIDGE') == 'bedrock':
                # Disable PTY echo BEFORE Bash starts. Minecraft's command
                # echo is already rendered by its owning Terminal tab.
                attributes = termios.tcgetattr(slave)
                attributes[3] &= ~termios.ECHO
                termios.tcsetattr(slave, termios.TCSANOW, attributes)
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
        from runtime import CinterForgeSettings as _CinterForgeSettings
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
            # Never acquire the Bedrock manager's lock while holding this
            # Session's output lock: shutdown takes them in the other order.
            bedrock_manager = getattr(self, 'manager', None)
            if bedrock_manager is not None and getattr(self, 'kind', None) == 'bedrock':
                bedrock_manager.observe_console_output(data)
            with self.lock:
                if self.output_log is not None:
                    try:
                        self.output_log.write(data)
                        self.output_log.flush()
                    except (OSError, ValueError):
                        pass  # The terminal itself remains readable if logging fails.
                # Bedrock keeps the complete native VM trace in its persistent
                # log, but presents a *console*, not the VM's kernel debugger.
                visible = self._visible_output(data)
                if visible:
                    self.output.append(visible)
                    self.output_size += len(visible)
                # Bound memory on a stalled browser, never allow unbounded output.
                while self.output_size > _CinterForgeSettings.MAX_OUTPUT and self.output:
                    self.output_size -= len(self.output.popleft())
                self.lock.notify_all()
        with self.lock:
            self.closed = True
            self.lock.notify_all()

    def _visible_output(self, data: bytes) -> bytes:
        """Default terminal transports every PTY byte without filtering."""
        return data

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


class CinterForgeBedrockTerminalSession(CinterForgeTerminalSession):
    """One real PTY, using the same Setup acceptance VM launcher.

    The visible Session owns stdin/stdout and the process group. Switching
    away from its tab does not detach, terminate, or restart the server.
    """
    def __init__(self, manager, builds: Path, system: str, cols: int, rows: int,
                 *, url: str, token: str, internal_token: str):
        # This state must exist before the parent starts the PTY reader thread.
        self._bedrock_console_live = False
        self._bedrock_console_announced = False
        self._bedrock_boot_buffer = b''
        self._bedrock_console_partial = b''
        self.host_shell = None
        self._host_shell_lock = threading.Lock()
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
            # Mirror CinterForgeBedrockManager.start(): a launcher rejected by macOS must
            # fail the run request instead of opening a misleading dead tab.
            try:
                self.process.wait(timeout=0.7)
            except subprocess.TimeoutExpired:
                pass
            else:
                tail = path.read_bytes()[-1600:].decode('utf-8', errors='replace') if path.is_file() else ''
                raise RuntimeError(f'Bedrock VM exited immediately (code {self.process.returncode}): {tail}')
            # This is a real host Bash PTY, with the SAME generated command
            # bindings as Mac Bash. It is owned by the Minecraft tab, not a
            # second interpreter, HTTP server or browser Session tab.
            shell = CinterForgeTerminalSession(
                builds, system, cols, rows, url=url, token=token,
                internal_token=internal_token, command_session_id=self.id,
                child_env={'CINTER_TERMINAL_BRIDGE': 'bedrock'},
            )
            with self._host_shell_lock:
                if self.closed:
                    shell.close()
                    raise RuntimeError('Bedrock exited while host Bash was starting')
                self.host_shell = shell
            threading.Thread(target=self._relay_host_output,
                             name='bedrock-host-bash-' + self.id[:8],
                             daemon=True).start()
        except Exception:
            # Never abandon a VM if its owner/watchdog fails to initialize.
            self.close()
            raise

    def _read_output(self) -> None:
        try:
            super()._read_output()
        finally:
            self._close_host_shell()  # Also on unexpected BDS/VM exit.

    def _close_host_shell(self) -> None:
        with self._host_shell_lock:
            shell, self.host_shell = self.host_shell, None
        if shell is not None:
            shell.close()

    def _append_host_output(self, data: bytes) -> None:
        with self.lock:
            if self.closed:
                return
            self.output.append(data)
            self.output_size += len(data)
            from runtime import CinterForgeSettings as _settings
            while self.output_size > _settings.MAX_OUTPUT and self.output:
                self.output_size -= len(self.output.popleft())
            self.lock.notify_all()

    def _relay_host_output(self) -> None:
        shell = self.host_shell
        while shell is not None and not self.closed:
            data, closed = shell.poll(250)
            if data:
                self._append_host_output(data)
            if closed:
                break
        if not self.closed:
            self._append_host_output(b'\r\n[Bedrock] Host Bash session ended.\r\n')

    def forward_host_command(self, sender: str, command: str) -> None:
        """Enter one Minecraft command into this tab's persistent host Bash."""
        shell = self.host_shell
        if shell is None or shell.closed:
            self._append_host_output(b'\r\n[Bedrock] Host Bash unavailable.\r\n')
            return
        self._append_host_output((f'\r\n{sender}>{command}\r\n').encode('utf-8'))
        # No parsing, dispatch switch, whitelist or per-command subprocess:
        # this is literally the existing Bash PTY's standard input.
        shell.write(command + '\n')

    def _visible_output(self, data: bytes) -> bytes:
        """Quiet VM boot chatter in the tab; retain every byte in the log.

        Once the actual Bedrock `Server started.` marker arrives, switch to
        byte-exact PTY streaming. That keeps live command echo, output and
        interactive input working without introducing a second console.
        """
        if self._bedrock_console_live:
            # Keep the source marker out of the visible transcript; the host
            # Bash echo and command result replace it. Handle split PTY reads.
            pending = self._bedrock_console_partial + data
            lines = pending.split(b'\n')
            self._bedrock_console_partial = lines.pop()
            result = b''.join(line + b'\n' for line in lines
                              if b'CINTERFORGE_MC_STATUS:' not in line)
            if len(self._bedrock_console_partial) > 8192:
                result += self._bedrock_console_partial
                self._bedrock_console_partial = b''
            return result
        heading = b''
        if not self._bedrock_console_announced:
            self._bedrock_console_announced = True
            heading = (b'[Bedrock] Starting server. Full VM boot output is in '
                       b'the persistent Bedrock log.\r\n')
        pending = self._bedrock_boot_buffer + data
        marker = pending.find(b'Server started.')
        if marker != -1:
            end = pending.find(b'\n', marker)
            if end == -1:
                # Wait for a complete line before switching to raw output.
                self._bedrock_boot_buffer = pending[-4096:]
                return heading
            self._bedrock_console_live = True
            self._bedrock_boot_buffer = b''
            return (heading + b'[Bedrock] Server ready. Type commands here '
                    b'(e.g. list); close this tab to stop the server.\r\n'
                    + self._visible_output(pending[end + 1:]))
        # Only essential startup errors and readiness markers belong in the
        # user console. Everything else is already preserved in output_log.
        lines = pending.split(b'\n')
        self._bedrock_boot_buffer = lines.pop()[-512:]
        important = (b'CINTER_VM_ERROR', b'CINTER_BDS_ERROR',
                     b'CINTER_BDS_NETHERNET_FAIL',
                     b'CINTER_BDS_LAN_RELAY_ERROR')
        for line in lines:
            if b'CINTERFORGE_MC_STATUS_BRIDGE_READY' in line:
                heading += b'[Bedrock] cinter: script-event bridge ready.\r\n'
            elif any(marker in line for marker in important):
                heading += line + b'\n'
        return heading

    def close(self) -> None:
        try:
            if getattr(self, 'manager', None) is not None and self.manager._terminal_session is self:
                self.manager.stop()  # Deliver `stop` through this PTY first.
        finally:
            self._close_host_shell()
            super().close()


def _load_bedrock_manager():
    # Bedrock is part of this single authored service, not a copied helper.
    from runtime import CinterForgeBedrock as _CinterForgeBedrock
    return _CinterForgeBedrock.CinterForgeBedrockManager


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
    from runtime import CinterForgeSettings as _CinterForgeSettings
    for item in (builds.resolve(), _CinterForgeSettings.ASSETS.resolve()):
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
    from runtime import CinterForgeSettings as _CinterForgeSettings
    physical = builds.resolve(strict=True)
    embedded_app = any(part.endswith('.app') for part in physical.parts)
    installed_steam = None
    for item in (physical, _CinterForgeSettings.ASSETS.resolve()):
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
                        _CinterForgeSettings.ASSETS.resolve().is_relative_to(physical) and
                        _CinterForgeSettings.ASSETS.resolve().relative_to(physical).parts[:2] == ('Python', 'Host'))
    if installed_steam is not None or (
            packaged_windows and os.environ.get('CINTER_TERMINAL_RUNTIME_MODE') != 'developer'
            and os.environ.get('CINTER_BUILDS_ROOT') != str(physical)):
        steam = installed_steam or _steam_library_for_installed_root(physical)
        if steam is None:
            return _standalone_state_root()
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
        return _standalone_state_root()
    raise RuntimeError('Runtime state needs repository Builds, a Steam installation, or a packaged application')


def _runtime_logs_root(writable_root: Path) -> Path:
    """Return logs in developer Builds or installed steamapps/common/CinterForge."""
    root = writable_root.resolve()
    for parent in (root, *root.parents):
        if parent.name.lower() == 'steamapps':
            return parent / 'common/CinterForge/Logs/CinterForgeTerminal'
    if root.name == 'Builds':
        return root / 'Logs/CinterForgeTerminal/Runtime'
    if root == _standalone_state_root(create=False):
        return _standalone_logs_root()
    raise RuntimeError('Runtime logs cannot escape the developer, Steam, or standalone user directories')


def _devtools_menu_ready(system: str) -> bool:
    """Read-only host capability check; malformed/missing SDK never blocks the UI."""
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    if system != 'mac':
        return False
    try:
        return _CinterForgeRuntimeBootstrap.sdk_root() is not None
    except (OSError, ValueError, RuntimeError):
        return False


if str(_CinterForgeSettings.ASSETS) not in sys.path:
    sys.path.insert(0, str(_CinterForgeSettings.ASSETS))


def open_bedrock_terminal(server, name: str, cols: int = 80, rows: int = 24,
                          *, restart: bool = False) -> dict:
    """Create one visible Session PTY using the same command as Setup acceptance.

    A run request never steals the requesting shell. Switching sessions never
    affects process ownership; only explicit stop/close or app shutdown does.
    """
    with server.bedrock_terminal_lock:
        from runtime import CinterForgeSettings as _CinterForgeSettings
        if server.bedrock is None:
            raise RuntimeError('Bedrock is unavailable: ' + server.bedrock_error)
        # Existing Bedrock saves are authoritative. Never regenerate their maps or
        # run CraftMaster just because a Java save with a similar name exists.
        from runtime.CinterForgeBedrock import _world_command_name
        requested = name.strip().casefold()
        # Reopening an already-running world must not trigger any conversion.
        if not restart:
            with server.sessions_lock:
                active = [item for item in server.sessions.values()
                          if getattr(item, 'kind', None) == 'bedrock' and item.process.poll() is None]
                if active:
                    selected = server.bedrock._world_settings().get('selected') or {}
                    if requested and requested not in (
                            str(selected.get('name', '')).casefold(),
                            str(selected.get('folder', '')).casefold(),
                            str(selected.get('command_name', '')).casefold()):
                        raise RuntimeError('A different Bedrock world is already running; stop it before switching')
                    item = active[0]
                    server.bedrock_terminal_requests.append({
                        'id': item.id, 'label': 'Bedrock', 'kind': 'bedrock',
                        'shell': 'Bedrock', 'platform': server.system})
                    return {**server.bedrock.status(), 'terminal_session': item.id,
                            'reused': True}
        existing = [world for world in server.bedrock.list_worlds()['worlds']
                    if requested in (world['name'].casefold(), world['folder'].casefold(),
                                     _world_command_name(world).casefold())]
        java_world = None
        if not existing and requested:
            java_world = server.bedrock.find_java_world(name)
            if java_world is not None:
                export_name = server.bedrock.java_world_export_name(java_world).casefold()
                existing = [world for world in server.bedrock.list_worlds()['worlds']
                            if export_name in (world['name'].casefold(), world['folder'].casefold(),
                                               _world_command_name(world).casefold())]
        from runtime import CinterForgeCraftMasterMap as _CinterForgeCraftMasterMap
        map_request = (_CinterForgeCraftMasterMap.open_map_tab(server, java_world)
                       if java_world is not None and not existing else None)

        def report_progress(message: str) -> None:
            if map_request is None:
                server.bedrock._append_log(message)
            else:
                _CinterForgeCraftMasterMap.report_progress(server, map_request['id'], message)

        # Preparation is synchronous. The Bedrock PTY must not start until a missing
        # Java world has been fully converted, validated and imported.
        try:
            prepared_name = server.bedrock.prepare_world(name, progress=report_progress)
        except Exception as error:
            if map_request is not None:
                _CinterForgeCraftMasterMap.mark_finished(server, map_request['id'], error)
            raise
        if map_request is not None:
            _CinterForgeCraftMasterMap.mark_finished(server, map_request['id'])
        with server.sessions_lock:
            live = [item for item in server.sessions.values()
                    if getattr(item, 'kind', None) == 'bedrock' and
                    item.process.poll() is None]
            if live and not restart:
                # Select the existing Bedrock tab only for the same saved world.
                selected = (server.bedrock._world_settings().get('selected') or {})
                if prepared_name.strip() and prepared_name.strip().casefold() not in (
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
            if len(server.sessions) >= _CinterForgeSettings.MAX_SESSIONS:
                raise ValueError('Maximum terminal sessions reached')
        state = server.bedrock.run_world(prepared_name, launch=False)
        if state.get('running'):
            raise RuntimeError('Bedrock already running outside a Session; stop it explicitly first')
        terminal = CinterForgeBedrockTerminalSession(server.bedrock, server.mutable_builds,
            server.system, cols, rows, url=server.origin, token=server.token,
            internal_token=server.cli_token)
        with server.sessions_lock:
            server.sessions[terminal.id] = terminal
        server.bedrock_terminal_requests.append({
            'id': terminal.id, 'label': 'Bedrock', 'kind': 'bedrock',
            'shell': 'Bedrock', 'platform': server.system})
        return {**state, 'running': terminal.process.poll() is None,
                'console_available': True, 'terminal_session': terminal.id}

