"""CinterForgeContainers — CinterForge Terminal application runtime.

PURPOSE: Owned Docker guest creation, image checks, Wine display and shell verification.
METHODS: _guest_name, _guest_host_binds, _guest_workspace, _docker_call, _guest_container_failure_evidence, _stop_owned_guests_locked, _start_guest_locked, _ensure_wine_display, _guest_verification_probe, _verify_guest_bash, _stop_owned_guests_locked_one
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



def _guest_name(kind: str) -> str:
    if kind not in ('linux', 'win'):
        raise ValueError('Invalid Terminal guest')
    return 'cinterforge-terminal-t-debian'


def _guest_host_binds(sdk: Path, kind: str, workspace: Path | None) -> list[str]:
    """All accessible Mac drives, not a sandbox/allowlist; stable startup cwd."""
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    binds = ['--mount', 'type=bind,src=/host/mac,dst=/host/mac']
    mounts = _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.mac_host_mounts()
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
    from runtime import CinterForgeSettings as _CinterForgeSettings
    for name in {_guest_name('linux'), _guest_name('win')}:
        result = _docker_call(docker, env, 'container', 'inspect', '--format',
                              '{{index .Config.Labels "io.cinterforge.terminal"}}', name)
        if result.returncode:
            continue
        if result.stdout.strip() != _CinterForgeSettings.PROFILE:
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
    from runtime import CinterForgeLaunchers as _CinterForgeLaunchers
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    from runtime import CinterForgeSettings as _CinterForgeSettings
    name = _guest_name(kind)
    tar, image = _CinterForgeRuntimeBootstrap._image_archive(sdk, kind)
    if _docker_call(docker, env, 'image', 'inspect', image).returncode:
        _CinterForgeRuntimeBootstrap._run_checked([docker, 'load', '--input', str(tar)], env,
                     title='Loading verified ' + kind + ' OCI image', timeout=1200)
    existing = _docker_call(docker, env, 'container', 'inspect', '--format',
                            '{{index .Config.Labels "io.cinterforge.terminal"}}|{{.State.Running}}', name)
    if existing.returncode == 0:
        owner, _, running = existing.stdout.strip().partition('|')
        if owner != _CinterForgeSettings.PROFILE:
            raise RuntimeError('Existing Docker container is not Terminal-owned: ' + name)
        if running == 'true':
            # A named running container is not proof Bash actually works.
            # Wine needs the guest display before it initializes its prefix.
            try:
                if kind == 'win':
                    _ensure_wine_display(docker, env, name)
                _verify_guest_bash(docker, env, kind, name)
                _CinterForgeLaunchers._activate_guest_vnc(docker, env, kind, name)
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
            '--label', 'io.cinterforge.terminal=' + _CinterForgeSettings.PROFILE,
            '--platform', 'linux/amd64',
            '--publish', f'127.0.0.1:{_CinterForgeLaunchers._LAUNCHER_PORTS["linux"]}:6080',
            '--publish', f'127.0.0.1:{_CinterForgeLaunchers._LAUNCHER_PORTS["win"]}:6081',
            *_guest_host_binds(sdk, kind, workspace),
            *environment, '--workdir', '/workspace' if workspace else '/',
            # Do not use a shell-owned background sleep/wait loop as PID 1.
            # An interactive shell is created only by an explicit docker exec;
            # this foreground keepalive has no inherited stdin or shell traps.
            '--entrypoint', '/bin/sleep', image, 'infinity']
    _CinterForgeRuntimeBootstrap._run_checked(args, env, title='Starting one reusable ' + kind + ' guest', timeout=120)
    try:
        if kind == 'win':
            _ensure_wine_display(docker, env, name)
        _verify_guest_bash(docker, env, kind, name)
        _CinterForgeLaunchers._activate_guest_vnc(docker, env, kind, name)
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
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    started = time.monotonic()
    completed = threading.Event()
    _CinterForgeRuntimeBootstrap._app_devtools_progress('Windows Bash probe START ' + stage)

    def heartbeat() -> None:
        from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
        while not completed.wait(15):
            _CinterForgeRuntimeBootstrap._app_devtools_progress('Windows Bash probe WORKING ' + stage +
                                   ' (' + str(int(time.monotonic() - started)) + 's)')

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
    _CinterForgeRuntimeBootstrap._app_devtools_progress('Windows Bash probe PASS ' + stage +
                           ' (' + str(round(time.monotonic() - started, 1)) + 's)')
    return result


def _verify_guest_bash(docker: str, env: dict[str, str], kind: str, name: str) -> None:
    from runtime import CinterForgeLaunchers as _CinterForgeLaunchers
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    if kind == 'linux':
        _CinterForgeRuntimeBootstrap._app_devtools_progress('Verifying reusable Linux guest Bash')
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
    common = ['exec', *_CinterForgeLaunchers._launcher_guest_env('win'), name]
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

