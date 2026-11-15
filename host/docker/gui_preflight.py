#!/usr/bin/env python3
# CINTERFORGE SOURCE HEADER — host/docker/gui_preflight.py
# PURPOSE: Maintains gui preflight within CinterForgeTerminal without duplicating external ownership.
# OWNERSHIP: This authored source is part of one canonical Terminal runtime.
# STARTUP: Called or loaded by the existing lifecycle/entry point; no
#          hidden dependency provisioning or alternate VM is allowed.
# SHUTDOWN: Release only resources explicitly owned by this component.
# CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
# KEY CLASSES / METHODS:
#   _tail: Handle tail through this component's established interfaces.
#   _wait: Handle wait through this component's established interfaces.
#   _xdpyinfo: Handle xdpyinfo through this component's established interfaces.
#   _vnc_banner: Handle vnc banner through this component's established interfaces.
#   _novnc_page: Handle novnc page through this component's established interfaces.
#   main: Start the current executable and return its status.
# MAINTENANCE: Keep generated outputs in sibling Builds or the actual
# Steam library; do not change Lagomorph timings, user assets or guest
# readiness without tracing their real callers and failure boundaries.
"""Setup-only, real X11/VNC/noVNC smoke, optionally followed by a GUI command.

Runs *inside* either pinned Linux OCI image.  The optional command (Wine in
Windows Setup) runs while the exact verified Xvfb display is still alive.
No ports are published, and every process is cleaned up before this exits.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import ctypes
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

DISPLAY = ':99'
SCREEN = '1280x800x24'
BANNER = b'RFB '


def _tail(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')[-2500:]
    except OSError:
        return '<log unavailable>'


def _wait(name: str, process: subprocess.Popen, probe, *, timeout: float = 12) -> object:
    deadline = time.monotonic() + timeout
    last = 'not ready'
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'{name} exited {process.returncode} before becoming ready')
        try:
            return probe()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            last = str(exc)
        time.sleep(0.15)
    raise RuntimeError(f'{name} did not become ready within {timeout:g}s: {last}')


def _xdpyinfo() -> str:
    result = subprocess.run(['xdpyinfo', '-display', DISPLAY],
                            capture_output=True, text=True, timeout=3, check=False)
    if result.returncode:
        raise RuntimeError(f'xdpyinfo rc={result.returncode}: {result.stderr[-200:]}')
    output = result.stdout
    if not re.search(r'dimensions:\s+1280x800 pixels', output):
        raise RuntimeError('xdpyinfo did not report the configured 1280x800 screen')
    if not re.search(r'depth of root window:\s+24 planes', output):
        raise RuntimeError('xdpyinfo did not report 24-bit root-window depth')
    return output


def _vnc_banner() -> bytes:
    with socket.create_connection(('127.0.0.1', 5900), timeout=2) as sock:
        sock.settimeout(2)
        banner = b''
        while len(banner) < 12:
            part = sock.recv(12 - len(banner))
            if not part:
                raise RuntimeError('VNC connection closed before RFB protocol banner')
            banner += part
    if len(banner) != 12 or not banner.startswith(BANNER):
        raise RuntimeError(f'VNC RFB protocol banner invalid: {banner!r}')
    return banner


def _novnc_page() -> bytes:
    with urllib.request.urlopen('http://127.0.0.1:6080/vnc.html', timeout=2) as response:
        payload = response.read(2048)
        if response.status != 200 or b'<html' not in payload.lower():
            raise RuntimeError('noVNC/websockify HTTP page did not respond as HTML')
        return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('linux', 'windows'), required=True)
    parser.add_argument('--web-root', default='/usr/share/novnc',
                        help='noVNC web root (normally the image-provisioned package)')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    command = options.command
    if command and command[0] == '--':
        command = command[1:]
    processes: list[subprocess.Popen] = []
    persistent_root = os.environ.get('CINTER_GUI_LOG_DIR', '')
    if persistent_root:
        candidate = Path(persistent_root)
        if candidate.is_symlink() or not candidate.is_dir():
            raise RuntimeError('Unsafe Setup preflight log destination')
        root = candidate / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid()))
        root.mkdir(mode=0o755)
        context = nullcontext(str(root))
    else:
        context = tempfile.TemporaryDirectory(prefix='cinter-gui-preflight-')
    with context as folder:
        root = Path(folder)
        print('[GUI PREFLIGHT] Helper logs: ' + str(root), flush=True)
        streams = []
        try:
            # Both platform desktops use the same pinned Linux AMD64 image.
            # VNC passing does not prove Linux CinterForgeTerminal can link.
            # Test the actual .so runtime in the image; never install on launch.
            ctypes.CDLL('libwebkit2gtk-4.1.so.0')
            print('[GUI PREFLIGHT] PASS ' + options.platform +
                  ' native WebKitGTK 4.1 runtime loader', flush=True)

            def start(name: str, args: list[str], *, env=None) -> subprocess.Popen:
                stream = (root / f'{name}.log').open('w', encoding='utf-8')
                streams.append(stream)
                proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                        stdout=stream, stderr=subprocess.STDOUT,
                                        env=env, start_new_session=True)
                processes.append(proc)
                return proc

            env = dict(os.environ, DISPLAY=DISPLAY)
            xserver = start('xvfb', ['Xvfb', DISPLAY, '-screen', '0', SCREEN,
                                     '-nolisten', 'tcp'])
            _wait('Xvfb / xdpyinfo', xserver, _xdpyinfo)
            print(f'[GUI PREFLIGHT] PASS {options.platform} X11 Xvfb {DISPLAY} ' 
                  f'(1280x800x24, xdpyinfo connection)', flush=True)

            vnc = start('x11vnc', ['x11vnc', '-display', DISPLAY, '-rfbport', '5900',
                                   '-localhost', '-forever', '-shared', '-nopw'], env=env)
            banner = _wait('x11vnc RFB connection', vnc, _vnc_banner)
            print(f'[GUI PREFLIGHT] PASS {options.platform} VNC TCP/RFB ' 
                  f'127.0.0.1:5900 ({banner.decode("ascii").strip()})', flush=True)

            novnc = start('websockify', ['websockify', '--web', options.web_root,
                                        '6080', '127.0.0.1:5900'], env=env)
            _wait('websockify / noVNC HTTP', novnc, _novnc_page)
            print(f'[GUI PREFLIGHT] PASS {options.platform} noVNC/websockify ' 
                  '127.0.0.1:6080/vnc.html', flush=True)

            if not command:
                return 0
            print(f'[GUI PREFLIGHT] START {options.platform} verified-display command: '
                  f'{command[0]}', flush=True)
            # Inherit stdout/stderr: real Wine diagnostics reach the Builds log
            # while Xvfb/x11vnc remain alive for this exact command.
            return subprocess.run(command, env=env, check=False).returncode
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f'[GUI PREFLIGHT] FAIL {options.platform}: {exc}', file=sys.stderr, flush=True)
            for name in ('xvfb', 'x11vnc', 'websockify'):
                path = root / f'{name}.log'
                if path.is_file():
                    print(f'[GUI PREFLIGHT] {name} log:\n{_tail(path)}',
                          file=sys.stderr, flush=True)
            return 1
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
            for process in reversed(processes):
                if process.poll() is None:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            for stream in streams:
                stream.close()


if __name__ == '__main__':
    raise SystemExit(main())
