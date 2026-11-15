"""CinterForgeRuntimeWatchdog — CinterForge Terminal application runtime.

PURPOSE: Native process lifetime watchdog for application-owned Linux/Windows VM cleanup.
METHODS: _runtime_watchdog_main
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



def _runtime_watchdog_main(argv: list[str]) -> int:
    """Detached app lifetime owner; serializes sequential native owners.

    The exclusive lifetime lock is held while the native PID exists and until
    its Colima cleanup completes. A new owner cannot start Colima before this
    lock is released; the pipe signals that takeover is complete. The lock
    protects only the dedicated t profile, never Setup's w/c or other VMs.
    """
    from runtime import CinterForgeGuestShells as _CinterForgeGuestShells
    from runtime import CinterForgeRuntimeBootstrap as _CinterForgeRuntimeBootstrap
    from runtime import CinterForgeVMOperations as _CinterForgeVMOperations
    if (len(argv) not in (1, 2) or not argv[0].isdigit() or
            int(argv[0]) <= 1 or (len(argv) == 2 and not argv[1].isdigit())):
        raise ValueError('Invalid native Terminal owner')
    native_pid = int(argv[0])
    ready_fd = int(argv[1]) if len(argv) == 2 else None
    import fcntl as local_fcntl
    if _CinterForgeRuntimeBootstrap.sdk_root() is None or platform.system() != 'Darwin':
        return 1 if ready_fd is not None else 0
    env = _env(_CinterForgeRuntimeBootstrap.sdk_root())
    lock_path = Path(env['COLIMA_HOME']) / 'CinterForgeTerminalLifetime.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        # Blocking is deliberate. A previous crashing owner's cleanup *must*
        # finish before the next owner starts this same profile.
        local_fcntl.flock(fd, local_fcntl.LOCK_EX)
        owner = _CinterForgeGuestShells._runtime_watchdog_owner(env)
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
            _CinterForgeVMOperations.stop_runtime_vm()
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            print('[DEVTOOLS WATCHDOG] Guest cleanup FAILED: ' + str(error), flush=True)
            return 1
        return 0
    finally:
        # Only our own acknowledged guardian may remove its receipt. Another
        # app might already be launching once the lock is released.
        owner = _CinterForgeGuestShells._runtime_watchdog_owner(env)
        try:
            record = json.loads(owner.read_text(encoding='utf-8'))
            if record.get('watchdog_pid') == os.getpid() and record.get('native_pid') == native_pid:
                owner.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            pass
        local_fcntl.flock(fd, local_fcntl.LOCK_UN)
        os.close(fd)

