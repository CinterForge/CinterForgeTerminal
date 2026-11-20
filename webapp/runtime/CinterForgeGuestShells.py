"""CinterForgeGuestShells — CinterForge Terminal application runtime.

PURPOSE: Real per-guest persistent Bash PTYs and independent VNC readiness probes.
METHODS: _is_owned_guest_shell, _close_owned_guest_shells, _runtime_guest_shell_command, _verify_persistent_guest_shell, _guest_docker_context, _new_owned_guest_shell, _check_owned_guest_vnc, _wait_for_app_guests, _runtime_watchdog_owner, _live_runtime_watchdog, _start_runtime_watchdog
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


def _is_owned_guest_shell(server: _CinterForgeHTTP.CinterForgeHTTPServer, entry: _CinterForgeSessions.CinterForgeTerminalSession) -> bool:
    from runtime import CinterForgeHTTP as _CinterForgeHTTP
    from runtime import CinterForgeSessions as _CinterForgeSessions
    with server.guest_shells_lock:
        return any(entry is shell for shell in server.guest_shells.values())


def _close_owned_guest_shells(server: _CinterForgeHTTP.CinterForgeHTTPServer) -> None:
    """Release PTYs before Docker/Colima shutdown (also on HTTP worker stop)."""
    from runtime import CinterForgeHTTP as _CinterForgeHTTP
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
    from runtime import CinterForgeContainers as _CinterForgeContainers
    from runtime import CinterForgeLaunchers as _CinterForgeLaunchers
    if kind not in ('linux', 'win'):
        raise ValueError('Unknown persistent guest Bash kind')
    docker = str(sdk / 'mac/env/bin/docker')
    cwd = '/host/mac' + str(workspace) if workspace else '/'
    command = [docker, 'exec', '-it', *_CinterForgeLaunchers._launcher_guest_env(kind),
               '--workdir', cwd, _CinterForgeContainers._guest_name(kind)]
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


def _verify_persistent_guest_shell(shell: _CinterForgeSessions.CinterForgeTerminalSession, kind: str, *, timeout: float = 120) -> None:
    """Exercise the actual long-lived PTY, not a separate --version probe."""
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    from runtime import CinterForgeSessions as _CinterForgeSessions
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
    _CinterForgeRuntimeBootstrap._app_devtools_progress(label + ' persistent Bash PTY START')
    output = bytearray()
    while time.monotonic() < deadline:
        data, closed = shell.poll(500)
        output.extend(data)
        if marker in output:
            # Keep one real shell prompt buffered for Code-OSS to render after
            # the initial splash; there is no new Bash when the tab attaches.
            shell.write('\n')
            _CinterForgeRuntimeBootstrap._app_devtools_progress(label + ' persistent Bash PTY PASS (' +
                                   str(round(time.monotonic() - started, 1)) + 's)')
            return
        if len(output) > 4096:
            del output[:-4096]
        now = time.monotonic()
        if now >= next_progress:
            _CinterForgeRuntimeBootstrap._app_devtools_progress(label + ' persistent Bash PTY WORKING (' +
                                   str(int(now - started)) + 's)')
            next_progress = now + 15
        if closed or shell.process.poll() is not None:
            break
    raise RuntimeError(kind + ' persistent Bash did not execute PTY readiness command: ' +
                       output.decode('utf-8', 'replace')[-700:])


def _guest_docker_context(sdk: Path) -> dict[str, str]:
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    full = _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.environment(sdk, state_root=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.state_root)
    # Match _env(): the managed t engine is selected by its owned Unix
    # socket, not Colima's disposable Docker context metadata. This child
    # environment is used by BOTH Linux and Windows persistent Bash PTYs.
    return {key: full[key] for key in ('HOME', 'COLIMA_HOME', 'LIMA_HOME',
                                      'COLIMA_CACHE_HOME', 'DOCKER_CONFIG',
                                      'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                                      'DOCKER_HOST', 'TMPDIR', 'PATH')}


def _new_owned_guest_shell(server: _CinterForgeHTTP.CinterForgeHTTPServer, kind: str, sdk: Path) -> _CinterForgeSessions.CinterForgeTerminalSession:
    """Create and validate once. No Code-OSS tab creates guest processes."""
    from runtime import CinterForgeHTTP as _CinterForgeHTTP
    from runtime import CinterForgeSessions as _CinterForgeSessions
    workspace = Path(server.code_workspace.details()['cwd'])
    shell = _CinterForgeSessions.CinterForgeTerminalSession(server.mutable_builds, server.system, 100, 30,
                            url=server.origin, token=server.token,
                            command=_runtime_guest_shell_command(sdk, kind, workspace),
                            working_directory=workspace, internal_token=server.cli_token,
                            capabilities=server.capabilities,
                            child_env=_guest_docker_context(sdk), output_log=_CinterForgeSessions._runtime_logs_root(server.mutable_builds) /
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
    from runtime import CinterForgeContainers as _CinterForgeContainers
    from runtime import CinterForgeLaunchers as _CinterForgeLaunchers
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    env = _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.environment(sdk, state_root=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.state_root)
    docker = str(sdk / 'mac/env/bin/docker')
    name = _CinterForgeContainers._guest_name(kind)
    try:
        _CinterForgeLaunchers._verify_guest_vnc(docker, env, kind, name)
        return False
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        # _activate_guest_vnc verifies the RFB handshake and HTTP endpoint
        # after restarting *only* missing helpers inside this owned container.
        _CinterForgeLaunchers._activate_guest_vnc(docker, env, kind, name)
        return True


def _wait_for_app_guests(kind: str, *, timeout_seconds: float = 1800) -> None:
    """Attach to an app-booted guest. Never provision/start a VM in a tab."""
    from runtime import CinterForgeCommands as _CinterForgeCommands
    deadline = time.monotonic() + timeout_seconds
    announced = False
    while True:
        state = _CinterForgeCommands._api('/api/lifecycle/guest-status')
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
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    if native_pid <= 1:
        raise RuntimeError('A native Terminal owner is required for managed guests')
    # `reboot server` replaces only Python/HTTP while keeping the SAME native
    # PID and already-running Colima. Its original independent watcher remains
    # responsible; starting another would deadlock on the lifetime lock.
    sdk = _CinterForgeRuntimeBootstrap.sdk_root()
    if sdk is None:
        raise RuntimeError('Unified DevTools disappeared before guest startup')
    if _live_runtime_watchdog(native_pid, _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.environment(sdk, state_root=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.state_root)):
        return
    script = (_CinterForgeSettings.ASSETS / 'CinterForgeTerminal.py').resolve(strict=True)
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

