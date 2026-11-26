"""CinterForgeIF — CinterForge Terminal application runtime.

PURPOSE: Infinite Mac/Lagomorph public media links, IF saves and authorized game catalogs; never changes emulator timing.
METHODS: _lagomorph_media_url, _verified_public_hfs_floppy, _if_catalog, _if_story, _if_save_path, _if_load_save, _if_store_save
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



def _lagomorph_media_url(builds: Path) -> dict:
    """Select public HTTPS classic HFS media for Infinite Mac.

    Infinite Mac cannot fetch an HTTP loopback image: its disk worker only
    accepts approved HTTPS origins or HTTPS/CORS-enabled sources. The private
    local ISO remains staged in Builds, but is never uploaded automatically.
    """
    from runtime import CinterForgeSettings as _CinterForgeSettings
    config = builds / 'CinterForgeTerminal/Input/Lagomorph/CDROM_URL.txt'
    value = os.environ.get('CINTER_LAGOMORPH_CDROM_URL', '').strip()
    if not value and config.is_file():
        if config.is_symlink() or config.stat().st_size > 2048:
            raise ValueError('Unsafe Lagomorph CD-ROM URL configuration')
        value = config.read_text(encoding='utf-8').strip()
    if not value:
        # Published, range/CORS-verified production disk. This object is independent
        # of the four release ZIPs, and must also work from the packaged app's
        # resource-only Builds directory (which has no local publish receipt).
        release = _CinterForgeSettings.ASSETS.parent.name
        if not __import__('re').fullmatch(r'20[0-9]{2}\.[0-9]{2}\.[0-9]{2}', release):
            release = '2026.11.23'
        value = ('https://cinterforge.ucreg.io/CinterForgeTerminal/Distribution/'
                 + release + '/Lagomorph-Quadra650.iso')
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or not host or parsed.username or parsed.password or
            parsed.fragment or any(ch.isspace() for ch in value) or
            host in ('localhost', 'localhost.localdomain') or
            host.endswith(('.localhost', '.local', '.internal')) or not parsed.path.lower().endswith(('.iso', '.img', '.toast', '.bin'))):
        raise ValueError('Lagomorph CD-ROM URL must be a public HTTPS disk-image URL')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise ValueError('Lagomorph CD-ROM URL cannot use a private or loopback address')
    # Only use the floppy pair if both have actually been published. Never
    # point Infinite Mac at inferred/stale objects. The hosted application
    # may have been staged before the public media was uploaded.
    result = {'configured': True, 'url': value}
    if host == 'cinterforge.ucreg.io' and value.endswith('/Lagomorph-Quadra650.iso'):
        names = ('Lagomorph-Application-1440K.img', 'Lagomorph-Battles-1440K.img')
        urls = [value.rsplit('/', 1)[0] + '/' + name for name in names]
        if all(_verified_public_hfs_floppy(url) for url in urls):
            result['floppies'] = urls
    return result


def _verified_public_hfs_floppy(url: str) -> bool:
    """A narrow public CORS/range check, not an arbitrary remote URL fetch."""
    from urllib.request import Request, urlopen
    from urllib.error import URLError
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'cinterforge.ucreg.io':
            return False
        request = Request(url, headers={'Range': 'bytes=1024-1025',
                                        'Origin': 'https://infinitemac.org'})
        with urlopen(request, timeout=1.5) as response:
            cors = response.headers.get('Access-Control-Allow-Origin', '')
            return (response.status == 206 and response.read(2) == b'BD' and
                    cors in ('*', 'https://infinitemac.org'))
    except (OSError, URLError, ValueError):
        return False


def _infinitemac_media_url() -> dict:
    """Return the beta app disk with the same trusted HTTPS mount path as Lagomorph.

    Publish owns the authenticated R2 read-back, public HFS signature, byte
    ranges and CORS checks. Do not make every emulator launch depend on a
    second host-side CDN request: Lagomorph supplies its validated HTTPS URL
    directly to Infinite Mac's disk_url mount as well.
    """
    return {
        'configured': True,
        'url': ('https://cinterforge.ucreg.io/CinterForgeTerminal/Distribution/'
                'beta/CinterForgeTerminal.68k.img'),
    }


def _if_catalog() -> dict:
    from runtime import CinterForgeSettings as _CinterForgeSettings
    data = json.loads((_CinterForgeSettings.ASSETS / 'data/if_titles.json').read_text(encoding='utf-8'))
    if data.get('schema') != 'cinterforge.terminal.external-if-titles.v1' or len(data.get('titles', [])) != 16:
        raise RuntimeError('Incomplete Terminal IF title manifest')
    return {Path(item['file']).stem:item for item in data['titles']}


def _if_story(server, name: str) -> bytes:
    item = _if_catalog().get(name)
    if not item or not server.bedrock or not server.bedrock.steamapps:
        raise FileNotFoundError('IF game or Steam library unavailable: '+name)
    root = server.bedrock.steamapps / 'common/CinterForge/InteractiveFiction/Titles' / name
    path = root / item['file']
    if root.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size > 32*1024*1024:
        raise FileNotFoundError('Install the game first: '+name+' install')
    story = path.read_bytes()
    if path.suffix != '.zblorb':
        if story[:1] not in (b'\x03', b'\x05', b'\x08'):
            raise ValueError('Invalid installed Z-code story')
        return story
    # Blorb FORM/IFRS: extract only an embedded ZCOD chunk for the JS VM.
    if len(story) < 12 or story[:4] != b'FORM' or story[8:12] != b'IFRS':
        raise ValueError('Invalid ZBlorb container')
    offset = 12
    while offset + 8 <= len(story):
        kind = story[offset:offset+4]
        length = int.from_bytes(story[offset+4:offset+8], 'big')
        end = offset + 8 + length
        if end > len(story):
            raise ValueError('Malformed ZBlorb chunk length')
        if kind == b'ZCOD':
            content = story[offset+8:end]
            if content[:1] not in (b'\x03',b'\x05',b'\x08'):
                raise ValueError('Unsupported ZBlorb executable')
            return content
        offset = end + (length & 1)
    raise ValueError('ZBlorb has no ZCOD executable')


def _if_save_path(server, name: str, *, screen=None) -> Path:
    if name not in _if_catalog():
        raise ValueError('Unknown IF title')
    steam = getattr(getattr(server, 'bedrock', None), 'steamapps', None)
    if not steam:
        raise RuntimeError('IF saves require a resolved host Steam library')
    root = steam / 'common/CinterForge/InteractiveFiction/Saves' / name
    if root.is_symlink() or root.parent.is_symlink():
        raise ValueError('Unsafe IF save location')
    if screen is not None:
        if not isinstance(screen, str) or not re.fullmatch(r'cinter_[0-9a-f]{32}', screen):
            raise ValueError('Invalid in-world story save scope')
        root = root / 'Bedrock' / screen
        if root.is_symlink() or root.parent.is_symlink():
            raise ValueError('Unsafe in-world IF save location')
    return root / 'autosave.conv.json'


def _if_load_save(server, name: str, *, screen=None) -> dict:
    story_hash = hashlib.sha256(_if_story(server, name)).hexdigest()
    path = _if_save_path(server, name, screen=screen)
    if path.is_symlink():
        raise ValueError('Unsafe IF autosave link')
    if not path.is_file():
        return {'found': False, 'story_sha256': story_hash}
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('IF autosave exceeds size limit')
    saved = json.loads(path.read_text(encoding='utf-8'))
    if (saved.get('schema') != 'cinterforge.conv.if.v1' or
            saved.get('story') != name or saved.get('story_sha256') != story_hash or
            not isinstance(saved.get('vm_state'), dict)):
        raise ValueError('Autosave is incompatible with the installed story')
    return {'found': True, 'story_sha256': story_hash, 'save': saved}


def _if_store_save(server, name: str, saved: dict, *, screen=None) -> None:
    path = _if_save_path(server, name, screen=screen)
    story_hash = hashlib.sha256(_if_story(server, name)).hexdigest()
    if (saved.get('schema') != 'cinterforge.conv.if.v1' or
            saved.get('card_type') != 'CONV' or saved.get('story') != name or
            saved.get('story_sha256') != story_hash or
            not isinstance(saved.get('vm_state'), dict) or
            saved['vm_state'].get('schema') != 'cinterforge.if.quetzal.v1'):
        raise ValueError('Invalid CONV IF autosave or story fingerprint')
    raw = json.dumps(saved, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('IF autosave exceeds size limit')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Unsafe IF autosave target')
    # One previous save survives a torn write or a newer incompatible save.
    candidate = path.with_name('autosave-' + uuid.uuid4().hex + '.tmp')
    with candidate.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if path.exists():
            shutil.copyfile(path, path.with_name('autosave.previous.conv.json'))
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)

