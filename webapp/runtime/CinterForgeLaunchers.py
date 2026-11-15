"""CinterForgeLaunchers — CinterForge Terminal application runtime.

PURPOSE: Shared Linux/Windows VNC desktop and GUI process launch after verification; native Mac application launching.
METHODS: _launcher_guest_env, _launcher_container, _verify_guest_vnc, _activate_guest_vnc, _launcher_start, _launcher_staged_app, _launcher_x11_window, _launcher_guest_diagnostics, _launcher_launch, guest_shell_main
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



_LAUNCHER_PORTS = {'linux': 20601, 'win': 20602}


_LAUNCHER_APPS = frozenset(('CinterForgeTerminal',))


_launcher_lock = threading.RLock()


def _launcher_guest_env(kind: str) -> list[str]:
    result = ['-e', 'DISPLAY=' + (':100' if kind == 'win' else ':99'),
              '-e', r'PS1=\u@\h \w \$ ']
    if kind == 'win':
        # Match the guest's existing MSYS2 Bash environment and prefix, not
        # the separate builder-only Wine PREFIX or a second Windows image.
        result += ['-e', 'HOME=/tmp', '-e', 'WINEPREFIX=/tmp/.wine',
                   '-e', 'CINTER_WINE_GUEST=1']
    return result


def _launcher_container(kind: str, server=None) -> tuple[str, dict[str, str], str]:
    from runtime import CinterForgeContainers as _CinterForgeContainers
    from runtime import CinterForgeGuestShells as _CinterForgeGuestShells
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    if kind not in _LAUNCHER_PORTS or platform.system() != 'Darwin':
        raise RuntimeError('Remote graphical launcher requires the managed macOS DevTools guest')
    sdk = _CinterForgeRuntimeBootstrap.sdk_root()
    if sdk is None:
        raise RuntimeError('Optional unified DevTools is not installed')
    if server is None:
        _CinterForgeGuestShells._wait_for_app_guests(kind)  # CLI children carry a trusted shell token.
    else:
        # Never allocate a browser VNC view for an unverified endpoint.
        # Guest startup happens ONLY in the app-owned supervisor; requests do
        # not wait for or start VM/container work.
        with server.guest_start_lock:
            ready = kind in server.guest_vnc_ready
            detail = server.guest_errors.get(kind, server.guest_start_error or server.guest_start_state)
        if not ready:
            raise RuntimeError('Managed ' + kind + ' VNC is not connected: ' + detail +
                               '; see Server Log')
    env = _CinterForgeRuntimeBootstrap._CINTERFORGE_VM.environment(sdk, state_root=_CinterForgeRuntimeBootstrap._CINTERFORGE_VM.state_root)
    return str(sdk / 'mac/env/bin/docker'), env, _CinterForgeContainers._guest_name(kind)


def _verify_guest_vnc(docker: str, env: dict[str, str], kind: str, name: str) -> None:
    """Verify actual RFB handshake and noVNC HTTP on the retained guest."""
    from runtime import CinterForgeContainers as _CinterForgeContainers
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
    result = _CinterForgeContainers._docker_call(docker, env, 'exec', name, 'python3', '-c', script,
                          str(rfb_port), str(web_port), timeout=90)
    if result.returncode or 'CINTER_VNC_READY' not in (result.stdout or ''):
        raise RuntimeError(kind + ' guest VNC handshake failed: ' +
                           (result.stderr or result.stdout or '').strip()[-650:])


def _activate_guest_vnc(docker: str, env: dict[str, str], kind: str, name: str) -> dict:
    from runtime import CinterForgeContainers as _CinterForgeContainers
    from runtime import CinterForgeSettings as _CinterForgeSettings
    with _launcher_lock:
        # Do not install/repair packages at runtime. All dependencies belong
        # to Setup's existing pinned guest images and the unified SDK.
        probe_script = ("for tool in Xvfb xdpyinfo openbox x11vnc websockify feh pgrep; do "
                        'command -v "$tool" >/dev/null 2>&1 || echo missing:$tool; done; '
                        "test -f /usr/share/novnc/vnc.html || echo missing:novnc; true")
        probe = _CinterForgeContainers._docker_call(docker, env, 'exec', name, '/bin/sh', '-c', probe_script,
                             timeout=30)
        if probe.returncode or (probe.stdout or '').strip():
            detail = (probe.stdout or probe.stderr or 'probe failed').strip()[-400:]
            raise RuntimeError('Guest GUI tools missing from staged ' + kind + ' image (' + detail +
                               '); rebuild DevTools in Setup, then Build/Stage')
        # The guest and host display the SAME replaceable platform PNG. Copy
        # the staged resource into the owned runtime container; never install
        # packages or write to Builds from a downloaded application.
        wallpaper_name = 'windows' if kind == 'win' else 'linux'
        wallpaper = _CinterForgeSettings.ASSETS / 'launcher/assets/backgrounds' / (wallpaper_name + '.png')
        if not wallpaper.is_file() or wallpaper.is_symlink():
            raise RuntimeError('Missing staged ' + kind + ' Launcher wallpaper')
        copied = _CinterForgeContainers._docker_call(docker, env, 'cp', str(wallpaper),
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
        result = _CinterForgeContainers._docker_call(docker, env, 'exec', *_launcher_guest_env(kind),
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
                diagnostic = _CinterForgeContainers._docker_call(
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
    from runtime import CinterForgeDevTools as _CinterForgeDevTools
    from runtime import CinterForgeSessions as _CinterForgeSessions
    from runtime import CinterForgeSettings as _CinterForgeSettings
    if kind not in ('mac', 'linux', 'win'):
        raise ValueError('Unknown CinterForge Terminal launcher platform')
    steam = _CinterForgeSessions._steam_library_for_installed_root(_CinterForgeSettings.ASSETS.parent)
    if steam is None:
        raise RuntimeError('No installed CinterForge Steam library for staged application')
    release = _CinterForgeDevTools._devtools_release()
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
    if (record.get('schema') not in (_CinterForgeSettings.FOUR_SCHEMA, 'cinterforge.terminal.unpacked-build.v1') or
            record.get('target') != kind or record.get('release') != release):
        raise RuntimeError('Staged ' + platform_folder + ' CinterForge Terminal receipt does not match ' + release)
    if record['schema'] == 'cinterforge.terminal.unpacked-build.v1' and (
            not isinstance(record.get('build_fingerprint'), str) or
            not isinstance(record.get('installed_state'), str)):
        raise RuntimeError('Staged ' + platform_folder + ' unpacked Build receipt is incomplete')
    return executable


def _launcher_x11_window(docker: str, env: dict[str, str], kind: str,
                         name: str, exit_receipt: str = '') -> tuple[str, str]:
    """Read the REAL mapped X11 window tree, not Docker's exec acknowledgment.

    The guest image already includes x11-utils (xwininfo). One window per
    platform is identified by the title set in app/src/CinterForgeTerminal.cpp;
    Linux and Wine use independent DISPLAYs in the same Docker container.
    """
    from runtime import CinterForgeContainers as _CinterForgeContainers
    script = r"""set -u
if ! command -v xwininfo >/dev/null 2>&1; then
  echo CINTER_X11_ERROR=xwininfo-missing-from-Setup-image
  exit 0
fi
if ! xdpyinfo >/dev/null 2>&1; then
  echo CINTER_X11_ERROR=display-not-ready
  exit 0
fi
windows=$(LC_ALL=C xwininfo -root -tree 2>&1)
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "CINTER_X11_ERROR=$windows"
  exit 0
fi
ids=$(printf '%s\n' "$windows" | awk 'tolower($0) ~ /"cinterforge terminal/ && $1 ~ /^0x[0-9a-fA-F]+$/ { print $1 }')
for id in $ids; do
  detail=$(LC_ALL=C xwininfo -id "$id" 2>/dev/null) || continue
  if printf '%s\n' "$detail" | grep -q 'Map State: IsViewable'; then
    printf 'CINTER_WINDOW_VISIBLE=%s\n' "$id"
    exit 0
  fi
done
if [ -n "$1" ] && [ -f "$1" ]; then
  printf 'CINTER_LAUNCH_EXIT=%s\n' "$(cat "$1")"
fi
"""
    check = _CinterForgeContainers._docker_call(docker, env, 'exec', *_launcher_guest_env(kind), name,
                         '/bin/sh', '-c', script, 'x11-check', exit_receipt, timeout=12)
    if check.returncode:
        return '', 'Docker/X11 inspection failed: ' + (check.stderr or check.stdout or '').strip()[-320:]
    output = (check.stdout or '').strip()
    for line in output.splitlines():
        if line.startswith('CINTER_WINDOW_VISIBLE='):
            return line.partition('=')[2], ''
    for line in output.splitlines():
        if line.startswith('CINTER_LAUNCH_EXIT='):
            return '', 'Process exited with status ' + line.partition('=')[2]
        if line.startswith('CINTER_X11_ERROR='):
            return '', 'X11 inspection: ' + line.partition('=')[2]
    return '', ''


def _launcher_guest_diagnostics(docker: str, env: dict[str, str], name: str,
                                log: str, status: str) -> str:
    """Expose only this launch's captured output; never hide it inside Docker."""
    from runtime import CinterForgeContainers as _CinterForgeContainers
    script = r"""if [ -f "$2" ]; then printf 'exit='; cat "$2"; fi
if [ -f "$1" ]; then tail -n 35 "$1"; else echo 'No guest launch log was created'; fi
"""
    try:
        result = _CinterForgeContainers._docker_call(docker, env, 'exec', name, '/bin/sh', '-c',
                              script, 'launch-diagnostics', log, status, timeout=12)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 'Unable to read guest startup output: ' + str(exc)
    return (result.stdout or result.stderr or '').strip()[-1700:]


def _launcher_launch(kind: str, app: str, server=None) -> dict:
    from runtime import CinterForgeContainers as _CinterForgeContainers
    if app not in _LAUNCHER_APPS or kind not in ('mac', 'linux', 'win'):
        raise ValueError('Unknown CinterForge launcher application')
    staged = _launcher_staged_app(kind)
    if kind == 'win':
        # Windows WebView2 is an actual runtime dependency, not merely SDK
        # headers. Check the staged official loader and Fixed Runtime before
        # waiting for Wine/X11, so the user sees a useful immediate error.
        required = (staged.parent / 'WebView2Loader.dll',
                    staged.parent / 'Builds/CinterForgeTerminal/Dependencies/WebView2FixedRuntime/msedgewebview2.exe')
        for path in required:
            valid = path.is_file() and not path.is_symlink()
            if valid:
                with path.open('rb') as binary:
                    valid = binary.read(2) == b'MZ'
            if not valid:
                raise RuntimeError('Staged Windows CinterForge Terminal is missing a valid ' +
                                   path.name + '; rerun Setup, Build and Stage to restore ' +
                                   'the pinned Microsoft WebView2 runtime')
    if kind == 'mac':
        # Launch/focus the real staged .app on the host, never in the WebView.
        if platform.system() != 'Darwin':
            raise RuntimeError('The staged Mac application requires a macOS host')
        app_bundle = staged.parents[2]   # .../CinterForge Terminal.app
        started = subprocess.run(['/usr/bin/open', '-a', str(app_bundle)],
                                 capture_output=True, text=True, timeout=20, check=False)
        if started.returncode:
            raise RuntimeError('Cannot open staged Mac CinterForge Terminal: ' +
                               started.stderr.strip()[-400:])
        return {'opened': True, 'platform': kind, 'external': True}

    # The only GUI processes run in the existing shared Debian container.
    # VNC was already activated by the Launcher tab; do not install a runtime,
    # provision another container, or start another VM on an app-icon click.
    docker, env, name = _launcher_container(kind, server)
    with _launcher_lock:
        window, error = _launcher_x11_window(docker, env, kind, name)
        if window:
            return {'opened': True, 'platform': kind, 'external': False,
                    'window_id': window, 'message': 'CinterForge Terminal window already visible'}
        if error:
            raise RuntimeError(kind + ' guest display is unavailable: ' + error)
        guest_executable = '/host/mac' + str(staged)
        present = _CinterForgeContainers._docker_call(docker, env, 'exec', name, 'test', '-f', guest_executable,
                               timeout=20)
        if present.returncode:
            raise RuntimeError('Staged ' + kind + ' build is not visible through the managed guest mount: ' +
                               str(staged))
        if kind == 'linux':
            # Read-only ELF dependency check in the already-provisioned image.
            # Build/Stage cannot fix missing system libraries and app runtime
            # must never call apt, download or mutate the guest toolchain.
            binary = '/host/mac' + str(staged.parent / 'CinterForge Terminal')
            probe = _CinterForgeContainers._docker_call(docker, env, 'exec', name, '/bin/sh', '-c',
                                 'ldd "$1" 2>&1', 'linux-library-check', binary,
                                 timeout=25)
            dependency_output = (probe.stdout or '') + (probe.stderr or '')
            missing = [line.strip() for line in dependency_output.splitlines()
                       if 'not found' in line]
            if probe.returncode or missing:
                raise RuntimeError('Staged Linux executable cannot load its native libraries: ' +
                                   ('; '.join(missing) or dependency_output.strip()[-500:]) +
                                   '. This is a missing Setup image dependency, not a VNC failure.')

        # Separate logs and completion receipts for Linux and Windows, even
        # when both are started in the one shared container.  The wrapper
        # records the ACTUAL native process exit, which docker exec -d cannot.
        ident = secrets.token_hex(6)
        base = '/tmp/cinterforge-launch-' + kind + '-' + ident
        logfile, statusfile = base + '.log', base + '.exit'
        win_path = 'Z:' + guest_executable.replace('/', '\\') if kind == 'win' else ''
        script = r"""set -u
platform=$1
launcher=$2
windows_path=$3
log=$4
status=$5
(
  cd "$(dirname "$launcher")" || exit 126
  printf '[CinterForge Terminal %s] DISPLAY=%s launcher=%s\n' "$platform" "$DISPLAY" "$launcher"
  if [ "$platform" = linux ]; then
    /bin/sh "$launcher"
  else
    # Use the genuine packaged .cmd: it resolves the Windows Python runtime,
    # worker, WebView2 fixed browser and required resource paths.
    wine cmd.exe /c "$windows_path"
  fi
) >"$log" 2>&1
rc=$?
printf '%s\n' "$rc" >"$status.tmp"
mv -f "$status.tmp" "$status"
exit "$rc"
"""
        if server is not None:
            server.log_event('Staged ' + kind + ' GUI launching on DISPLAY=' +
                             (':99' if kind == 'linux' else ':100') +
                             ' in managed container ' + name + '; guest log=' + logfile)
        launched = _CinterForgeContainers._docker_call(docker, env, 'exec', '-d', *_launcher_guest_env(kind),
                                name, '/bin/sh', '-c', script, 'cinterforge-launch',
                                kind, guest_executable, win_path, logfile, statusfile,
                                timeout=45)
        if launched.returncode:
            raise RuntimeError('Could not execute staged ' + kind + ' CinterForge Terminal: ' +
                               (launched.stderr or launched.stdout or '').strip()[-400:])
        # Linux WebKitGTK and Wine WebView2 can take different times to map.
        # Check the live X server; a successful Docker exec is NOT success.
        deadline = time.monotonic() + (180 if kind == 'win' else 45)
        last_error = ''
        last_heartbeat = time.monotonic()
        while time.monotonic() < deadline:
            if server is not None and server._stopping:
                raise RuntimeError('Terminal quit while launching ' + kind + ' GUI')
            window, error = _launcher_x11_window(docker, env, kind, name, statusfile)
            if window:
                if server is not None:
                    server.log_event('Staged ' + kind + ' GUI window VISIBLE on X11: ' + window)
                return {'opened': True, 'platform': kind, 'external': False,
                        'window_id': window, 'message': 'CinterForge Terminal window visible'}
            if error:
                last_error = error
                if error.startswith('Process exited with status ') or error.startswith('X11 inspection:'):
                    break
            if server is not None and time.monotonic() - last_heartbeat >= 10:
                server.log_event('Waiting for staged ' + kind + ' X11 window; guest log=' + logfile)
                last_heartbeat = time.monotonic()
            time.sleep(0.6)
        details = _launcher_guest_diagnostics(docker, env, name, logfile, statusfile)
        explanation = last_error or 'No mapped CinterForge Terminal X11 window before launch deadline'
        failure = ('Staged ' + kind + ' GUI failed: ' + explanation +
                   '; guest output:\n' + details)
        if server is not None:
            server.log_event(failure)
        raise RuntimeError(failure)


def guest_shell_main(kind: str) -> int:
    from runtime import CinterForgeDevTools as _CinterForgeDevTools
    from runtime import CinterForgeGuestShells as _CinterForgeGuestShells
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    if kind not in ('win', 'linux') or platform.system() != 'Darwin':
        raise RuntimeError('DevTools shell bridge is only for Windows/Linux tabs on macOS')
    sdk = _CinterForgeRuntimeBootstrap.sdk_root()
    if sdk is None:
        print(f'{kind.upper()} Bash needs the optional unified DevTools package.', flush=True)
        if _CinterForgeDevTools.install_devtools() != 0:
            return 1
        sdk = _CinterForgeRuntimeBootstrap.sdk_root()
        if sdk is None:
            raise RuntimeError('DevTools installer completed without usable SDK')
        # The verified installer already requested a native app relaunch.
        # Do not open a PTY or race that owner's pending shutdown/restart.
        print('[DEVTOOLS] The Terminal app is relaunching; reopen the guest Bash in Code-OSS.', flush=True)
        return 0
    _CinterForgeGuestShells._wait_for_app_guests(kind)
    print('[DEVTOOLS] ' + kind + ' Bash is already connected. Open/focus its existing '
          'Code-OSS integrated terminal; Session does not create guest Bash.', flush=True)
    return 0

