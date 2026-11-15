"""CinterForgeDiscovery — CinterForge Terminal application runtime.

PURPOSE: Native DNS-SD/mDNS C API, service advertisement and LAN peer retrieval.
METHODS: _DNSService, CinterForgeDiscovery
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



CINTERFORGE_PORT_FIRST = 20300


CINTERFORGE_PORT_LAST = 20399


DISCOVERY_TYPE = b'_http._tcp.local.'


CINTERFORGE_SERVICE_PORTS = {20300: 'terminal', 20310: 'converter',
                             20320: 'os', 20380: 'webapp'}


class _DNSService(ctypes.Structure):
    """Exact CinterZeroconf.h callback ABI; callback strings expire on return."""
    _fields_ = [('name', ctypes.c_char_p), ('type', ctypes.c_char_p),
                ('host', ctypes.c_char_p), ('address', ctypes.c_char_p),
                ('port', ctypes.c_uint16), ('txt', ctypes.c_char_p)]


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

