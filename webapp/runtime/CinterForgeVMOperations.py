"""CinterForgeVMOperations — CinterForge Terminal application runtime.

PURPOSE: App-owned Colima/QEMU teardown, safe VM reboot and Docker ownership checks.
METHODS: boot_runtime_guests, _owned_qemu_pids, _verify_owned_qemu_stopped, stop_runtime_vm, reboot_runtime_guests
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



def boot_runtime_guests(*, cancel_event: threading.Event | None = None,
                        on_display_ready: Callable[[str], None] | None = None) -> dict:
    """On app startup: one VM, one persistent Debian guest, two desktops."""
    from runtime import CinterForgeContainers as _CinterForgeContainers
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    if platform.system() != 'Darwin':
        return {'platform': platform.system(), 'guests': 'native'}
    sdk = _CinterForgeRuntimeBootstrap.sdk_root()
    if sdk is None:
        return {'installed': False, 'guests': 'not-started'}
    env = _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.environment(sdk, state_root=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.state_root)
    workspace = _CinterForgeContainers._guest_workspace()
    with _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.managed_vm_lock(env):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError('Guest boot cancelled: native app exited')
        docker = _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.ensure_docker_locked(
            sdk, env, workspace, cancel_event=cancel_event,
            host_mounts=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.mac_host_mounts,
            configure_host_mounts=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.configure_colima_host_mounts,
            configure_runtime_emulation=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.configure_colima_runtime_emulation,
            run_checked=_CinterForgeRuntimeBootstrap._run_checked, progress=_CinterForgeRuntimeBootstrap._app_devtools_progress,
            guest_name=_CinterForgeContainers._guest_name, stop_owned_guests_locked=_CinterForgeContainers._stop_owned_guests_locked)
        ready, failures = [], {}
        _CinterForgeRuntimeBootstrap._app_devtools_progress('Shared Colima t guest booted; Docker daemon and host mounts verified')
        for kind in ('linux', 'win'):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError('Guest boot cancelled: native app exited')
            _CinterForgeRuntimeBootstrap._app_devtools_progress('Connecting ' + kind + ' Docker guest, Bash and VNC')
            try:
                _CinterForgeContainers._start_guest_locked(sdk, env, docker, kind, workspace)
                ready.append(kind)
                if on_display_ready is not None:
                    on_display_ready(kind)
                _CinterForgeRuntimeBootstrap._app_devtools_progress(kind + ' guest Bash and VNC handshake PASS')
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                failures[kind] = str(error)
                _CinterForgeRuntimeBootstrap._app_devtools_progress(kind + ' guest FAILED: ' + str(error))
        if not ready:
            # No verified capability depends on this container. Capture of
            # each startup failure has already happened before this cleanup.
            # Do not keep a completely unusable guest alive until app exit.
            # A cleanup failure must NOT mask the platform-specific cause.
            try:
                _CinterForgeContainers._stop_owned_guests_locked(docker, env)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as cleanup_error:
                failures['cleanup'] = str(cleanup_error)
                print('[DEVTOOLS] Unusable shared guest cleanup FAILED: ' +
                      str(cleanup_error), flush=True)
    return {'installed': True, 'guests': ready, 'errors': failures,
            'docker': 'one-managed-engine'}


def _owned_qemu_pids(env: dict[str, str], sdk: Path) -> list[int]:
    """Find ONLY QEMU belonging to our named Lima t VM, never host QEMU jobs.

    Require both the bundled binary and the app's private Lima state path in
    the full command line. Inconclusive or failed inspection must not be
    treated as evidence that a VM has stopped.
    """
    from runtime import CinterForgeSettings as _CinterForgeSettings
    binary = str(sdk / 'mac/qemu/bin/qemu-system-x86_64')
    state = str(Path(env['LIMA_HOME']) / ('colima-' + _CinterForgeSettings.PROFILE))
    listing = subprocess.run(['/bin/ps', '-ww', '-axo', 'pid=,command='],
                             check=False, text=True, capture_output=True, timeout=15)
    if listing.returncode:
        raise RuntimeError('Cannot inspect owned QEMU processes: ' + listing.stderr[-300:])
    found = []
    for row in listing.stdout.splitlines():
        pid, sep, command = row.strip().partition(' ')
        if sep and pid.isdigit() and command.lstrip().startswith(binary + " ") and state in command:
            found.append(int(pid))
    return found


def _verify_owned_qemu_stopped(env: dict[str, str], sdk: Path) -> None:
    """Stop a still-live, positively identified t QEMU after Colima exits.

    A normal `colima stop` is preferred. Its status can be wrong during an
    interrupted `colima start`; then use the owned Lima instance's force stop.
    If Lima also fails, terminate ONLY the exact bundled QEMU whose command
    references this private t-profile state. Never use pkill/qemu-wide kills.
    """
    from runtime import CinterForgeSettings as _CinterForgeSettings
    pids = _owned_qemu_pids(env, sdk)
    if not pids:
        return
    lima = str(sdk / 'mac/lima/bin/limactl')
    result = subprocess.run([lima, 'stop', '--force', 'colima-' + _CinterForgeSettings.PROFILE],
                            env=env, check=False, text=True, capture_output=True, timeout=60)
    if result.returncode:
        print('[DEVTOOLS] Lima force-stop t returned ' + str(result.returncode) + ': ' +
              (result.stderr or result.stdout).strip()[-500:], flush=True)
    for _ in range(10):
        if not _owned_qemu_pids(env, sdk):
            return
        time.sleep(0.5)
    # The VM is known by BOTH its bundled executable and its private state.
    # Revalidate each PID immediately before signalling; never kill other VMs.
    for pid in set(pids).intersection(_owned_qemu_pids(env, sdk)):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(16):
        if not _owned_qemu_pids(env, sdk):
            return
        time.sleep(0.5)
    for pid in _owned_qemu_pids(env, sdk):
        if pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for _ in range(10):
        if not _owned_qemu_pids(env, sdk):
            return
        time.sleep(0.5)
    raise RuntimeError('Owned t-profile QEMU is still alive after Colima/Lima cleanup: ' +
                       ', '.join(map(str, _owned_qemu_pids(env, sdk))))


def stop_runtime_vm(*, setup_sdk: Path | None = None) -> None:
    """Shut down only the managed t VM, including interrupted startup.

    Colima `status` returning nonzero DOES NOT mean QEMU is gone: the status
    command fails during some VM starts/crashes. Do not return on that error.
    Preserve any foreign Docker workloads when they can be inspected, and
    never delete a VM disk or unrelated Lima/QEMU process.
    """
    from runtime import CinterForgeContainers as _CinterForgeContainers
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    from runtime import CinterForgeSettings as _CinterForgeSettings
    if platform.system() != 'Darwin':
        return
    sdk = setup_sdk if setup_sdk is not None else _CinterForgeRuntimeBootstrap.sdk_root()
    if sdk is None:
        return
    env = _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.environment(sdk, state_root=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.state_root)
    colima = str(sdk / 'mac/env/bin/colima')
    docker = str(sdk / 'mac/env/bin/docker')
    with _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.managed_vm_lock(env):
        status = subprocess.run([colima, 'status', _CinterForgeSettings.PROFILE], env=env,
                                check=False, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=25)
        if status.returncode == 0:
            engine = _CinterForgeContainers._docker_call(docker, env, 'info', '--format', '{{.ServerVersion}}', timeout=20)
            if engine.returncode == 0:
                _CinterForgeContainers._stop_owned_guests_locked(docker, env)
                foreign = _CinterForgeContainers._docker_call(docker, env, 'ps', '--quiet', timeout=25)
                if foreign.returncode or foreign.stdout.strip():
                    raise RuntimeError('Leaving VM running: unowned Docker containers may be active')
            else:
                # Before stopping a running VM with an unavailable host socket,
                # check its Docker containers through the dedicated Lima SSH.
                listing = subprocess.run(
                    [colima, 'ssh', '--profile', _CinterForgeSettings.PROFILE, '--', 'docker', 'ps',
                     '--format', '{{.Names}}|{{.Label "io.cinterforge.terminal"}}'],
                    env=env, check=False, text=True, capture_output=True, timeout=35)
                if listing.returncode == 0:
                    for row in listing.stdout.splitlines():
                        name, sep, owner = row.partition('|')
                        if not sep or name != _CinterForgeContainers._guest_name('linux') or owner != _CinterForgeSettings.PROFILE:
                            raise RuntimeError('Leaving VM running: unowned Docker container ' + name)
                    for row in listing.stdout.splitlines():
                        name = row.partition('|')[0]
                        removed = subprocess.run(
                            [colima, 'ssh', '--profile', _CinterForgeSettings.PROFILE, '--',
                             'docker', 'stop', '--time', '5', name],
                            env=env, check=False, text=True, capture_output=True, timeout=45)
                        if removed.returncode:
                            raise RuntimeError('Cannot stop owned container ' + name +
                                               ': ' + (removed.stderr or removed.stdout).strip()[:240])
                elif not _owned_qemu_pids(env, sdk):
                    # Docker and SSH are both unavailable, but no QEMU has a
                    # confirmed owner. Do not interfere with another runtime.
                    return
                else:
                    print('[DEVTOOLS] Owned t QEMU is running with unavailable Docker/SSH; '
                          'stopping private t profile rather than orphaning it', flush=True)
        elif not _owned_qemu_pids(env, sdk) and not (
                Path(env['COLIMA_HOME']) / _CinterForgeSettings.PROFILE / 'colima.yaml').is_file():
            # No named profile and no QEMU belongs to us. Nothing to stop.
            return
        # Colima can report status=1 while its child QEMU is still starting.
        # Always ask the named profile to stop, then verify the OWNED process.
        try:
            _CinterForgeRuntimeBootstrap._run_checked([colima, 'stop', _CinterForgeSettings.PROFILE], env,
                         title='Stopping app-owned guest VM', timeout=120)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            print('[DEVTOOLS] Colima t stop incomplete: ' + str(error), flush=True)
        _verify_owned_qemu_stopped(env, sdk)


def reboot_runtime_guests(component: str) -> dict:
    """Never recompile, download, install, Stage, ZIP or touch Setup VMs."""
    from runtime import CinterForgeContainers as _CinterForgeContainers
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    from runtime import CinterForgeSettings as _CinterForgeSettings
    if component not in ('docker', 'colima'):
        raise ValueError('Usage: reboot docker|colima')
    sdk = _CinterForgeRuntimeBootstrap.sdk_root()
    if sdk is None:
        raise RuntimeError('Install DevTools before rebooting Docker or Colima')
    env = _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.environment(sdk, state_root=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.state_root)
    colima = str(sdk / 'mac/env/bin/colima')
    docker = str(sdk / 'mac/env/bin/docker')
    workspace = _CinterForgeContainers._guest_workspace()
    with _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.managed_vm_lock(env):
        _CinterForgeContainers._stop_owned_guests_locked(docker, env)
        if component == 'colima':
            foreign = _CinterForgeContainers._docker_call(docker, env, 'ps', '--quiet')
            if foreign.returncode or foreign.stdout.strip():
                raise RuntimeError('Cannot reboot Colima while unowned containers are active')
            _CinterForgeRuntimeBootstrap._run_checked([colima, 'stop', _CinterForgeSettings.PROFILE], env, title='Rebooting owned Colima', timeout=120)
            docker = _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.ensure_docker_locked(
            sdk, env, workspace,
            host_mounts=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.mac_host_mounts,
            configure_host_mounts=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.configure_colima_host_mounts,
            configure_runtime_emulation=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.configure_colima_runtime_emulation,
            run_checked=_CinterForgeRuntimeBootstrap._run_checked, progress=_CinterForgeRuntimeBootstrap._app_devtools_progress,
            guest_name=_CinterForgeContainers._guest_name, stop_owned_guests_locked=_CinterForgeContainers._stop_owned_guests_locked)
        else:
            # Colima's default Linux guest uses OpenRC; tolerate systemd-based
            # images only when their actual service command exists. Never
            # silently substitute a destructive VM restart for docker reboot.
            services = (('sudo', 'rc-service', 'docker', 'restart'),
                        ('sudo', 'systemctl', 'restart', 'docker'),
                        ('sudo', 'service', 'docker', 'restart'))
            restarted = False
            for service in services:
                check = subprocess.run([colima, 'ssh', '--profile', _CinterForgeSettings.PROFILE, '--', *service],
                                       env=env, text=True, capture_output=True,
                                       timeout=120, check=False)
                if check.returncode == 0:
                    restarted = True
                    break
            if not restarted:
                raise RuntimeError('Could not restart managed Docker daemon in the existing Colima VM')
            for attempt in range(30):
                ping = _CinterForgeContainers._docker_call(docker, env, 'info', '--format', '{{.ServerVersion}}',
                                    timeout=10)
                if ping.returncode == 0:
                    break
                if attempt == 29:
                    raise RuntimeError('Managed Docker did not become ready after daemon restart')
                time.sleep(2)
        for kind in ('linux', 'win'):
            _CinterForgeContainers._start_guest_locked(sdk, env, docker, kind, workspace)
    return {'rebooted': component, 'guests': ['linux', 'win'], 'ready': True}

