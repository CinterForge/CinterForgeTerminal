"""CinterForgeSettings — CinterForge Terminal application runtime.

PURPOSE: Source-controlled runtime configuration, HTTP asset mapping, port policies and release metadata. No downloads or VM operations.
METHODS: 
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



"""CinterForgeTerminal — portable Python backend for the native/web UI.

PUBLIC ENTRY POINTS (see ``main()`` immediately below configuration):
    no mode           Staged HTTP bootstrap/smoke service
    --terminal-cli    Human-facing text commands, game catalog, install actions
    --guest-shell     Legacy helper (guest Bash lives exclusively in Code-OSS)
    --vm-watchdog     Private VM lifetime guardian (never starts Bedrock)

COMPONENTS
    Session objects   PTY/ConPTY input/output, resize and lifecycle
    HTTP service      Authenticated loopback origin for webapp/ assets and API
    Guest supervisor  CinterForgeGuestSupervisor (in runtime/CinterForgeGuest.py)
    VM bootstrap      CinterForgeVM (in runtime/CinterForgeVM.py); owned t profile,
                      Docker context and QEMU configuration/reuse decisions
    CinterForgeBedrockManager    EULA, server state, world preservation and console
    DevTools          Optional consented SDK installation / guest shell setup
    Commands          help, debugger, bedrock, minecraft, stories and games

AUTHORING/LIFECYCLE CONTRACT
    The browser UI is ``webapp/CinterForgeTerminal.js``. The native C++ host owns the
    process lifetime, and ``Cinter`` owns all build/setup/publish commands.
    Runtime files must work *without* the original source checkout installed.
    Steam installs keep runtime state in their selected Steam library; direct
    downloads use standard OS user-support directories without requiring Steam.
    An explicitly consented DevTools install creates/uses the selected developer
    Builds tree. Repository lifecycle runs use sibling Builds. Do not repurpose
    or terminate another process's managed VM.
    Do not change the Infinite Mac guest-input automation in this backend.

Configuration and externally callable dispatch are at the beginning of this
file. Supporting implementations follow, grouped by responsibility.
"""


if os.name != 'nt':
    import fcntl
    import pty
    import select
    import struct
    import termios


MAX_BODY = 65_536                 # bytes per HTTP request


MAX_SESSIONS = 6                  # concurrent terminal tabs


MAX_OUTPUT = 1024 * 1024         # maximum buffered PTY output bytes


SERVER_LOG_MEMORY_LINES = 2048     # rolling Server tab backlog


SERVER_LOG_BATCH_LINES = 120       # bounded HTTP response per poll


ASSETS = Path(__file__).resolve().parent.parent  # bundled webapp/ (not cwd)


PROFILE = 't'


CINTERFORGE_TERMINAL_RELEASE = '2026.11.23'


MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024


EULA_URL = 'https://www.minecraft.net/en-us/eula'


PRIVACY_URL = 'https://www.minecraft.net/en-us/privacy'


DOWNLOAD_URL = 'https://www.minecraft.net/en-us/download/server/bedrock'


METADATA_ROOT = 'https://raw.githubusercontent.com/EndstoneMC/bedrock-server-data/v2/'


PUBLIC_DISTRIBUTION = 'https://cinterforge.ucreg.io/CinterForgeTerminal/Distribution'


FOUR_SCHEMA = 'cinterforge.terminal.four-artifact-release.v1'


VERSION_PATTERN = re.compile(r'^\d+\.\d+\.\d+(?:\.\d+)?$')


APP_PATTERN = re.compile(r'^[a-zA-Z][a-zA-Z0-9_-]{0,55}$')


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


CLIENT_SCRIPTS = ('client/CinterForgeAPI.js', 'client/CinterForgeTerminals.js', 'client/CinterForgeCapabilities.js', 'client/CinterForgeClientState.js', 'client/CinterForgeCodeOSS.js', 'client/CinterForgeEmulators.js', 'client/CinterForgeSessions.js', 'client/CinterForgeLaunchers.js', 'client/CinterForgeDiscord.js', 'client/CinterForgeLAN.js', 'client/CinterForgeIF.js', 'client/CinterForgeSessionIO.js', 'client/CinterForgeInput.js', 'client/CinterForgeMenus.js', 'client/CinterForgeLayout.js', 'client/CinterForgeKeyboard.js', 'client/CinterForgeBedrock.js', 'client/CinterForgeGuestStatus.js', 'client/CinterForgeCommands.js', 'CinterForgeTerminal.js')

STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/CinterForgeTerminal.js': ('CinterForgeTerminal.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeAPI.js': ('client/CinterForgeAPI.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeTerminals.js': ('client/CinterForgeTerminals.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeCapabilities.js': ('client/CinterForgeCapabilities.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeClientState.js': ('client/CinterForgeClientState.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeCodeOSS.js': ('client/CinterForgeCodeOSS.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeEmulators.js': ('client/CinterForgeEmulators.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeSessions.js': ('client/CinterForgeSessions.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeLaunchers.js': ('client/CinterForgeLaunchers.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeDiscord.js': ('client/CinterForgeDiscord.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeLAN.js': ('client/CinterForgeLAN.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeIF.js': ('client/CinterForgeIF.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeSessionIO.js': ('client/CinterForgeSessionIO.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeInput.js': ('client/CinterForgeInput.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeMenus.js': ('client/CinterForgeMenus.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeLayout.js': ('client/CinterForgeLayout.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeKeyboard.js': ('client/CinterForgeKeyboard.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeBedrock.js': ('client/CinterForgeBedrock.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeGuestStatus.js': ('client/CinterForgeGuestStatus.js', 'text/javascript; charset=utf-8'),
    '/client/CinterForgeCommands.js': ('client/CinterForgeCommands.js', 'text/javascript; charset=utf-8'),
    '/terminal.css': ('terminal.css', 'text/css; charset=utf-8'),
    '/terminal_styles.json': ('data/terminal_styles.json', 'application/json; charset=utf-8'),
    '/mac_games.json': ('data/mac_games.json', 'application/json; charset=utf-8'),
    '/vendor/xterm.js': ('vendor/xterm.js', 'text/javascript; charset=utf-8'),
    '/vendor/xterm.css': ('vendor/xterm.css', 'text/css; charset=utf-8'),
    '/vendor/addon-fit.js': ('vendor/addon-fit.js', 'text/javascript; charset=utf-8'),
}

