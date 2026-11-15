"""CinterForgeApplication — CinterForge Terminal application runtime.

PURPOSE: Public command-mode dispatch from the thin native Python service entry point.
METHODS: main
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



def main(argv: list[str] | None = None) -> int:
    """Select an explicit mode without importing or spawning extra source files.

    No arguments: the staged HTTP readiness smoke exercise.
    --terminal-cli: user-facing shell commands (help, games, Bedrock, etc.).
    --guest-shell: legacy guest status helper; never starts a duplicate Bash.
    --vm-watchdog: private Bedrock VM lifetime owner (never a server launcher).
    """
    from runtime import CinterForgeBedrock as _CinterForgeBedrock
    from runtime import CinterForgeCommands as _CinterForgeCommands
    from runtime import CinterForgeLaunchers as _CinterForgeLaunchers
    from runtime import CinterForgeRuntimeWatchdog as _CinterForgeRuntimeWatchdog
    from runtime import CinterForgeService as _CinterForgeService
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == '--terminal-cli':
        return _CinterForgeCommands.terminal_cli_main(arguments[1:])
    if arguments and arguments[0] == '--guest-shell':
        if len(arguments) != 2:
            raise ValueError('Usage: --guest-shell linux|win')
        return _CinterForgeLaunchers.guest_shell_main(arguments[1])
    if arguments and arguments[0] == '--vm-watchdog':
        return _CinterForgeBedrock._vm_watchdog_main(arguments[1:])
    if arguments and arguments[0] == '--runtime-watchdog':
        return _CinterForgeRuntimeWatchdog._runtime_watchdog_main(arguments[1:])
    return _CinterForgeService.bootstrap_smoke_main(arguments)

