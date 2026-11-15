"""CinterForgeDiscord — CinterForge Terminal application runtime.

PURPOSE: Discord provider credentials, named channel lookup and the remote message API.
METHODS: _discord_provider, _discord_api, _discord_resolve
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



def _discord_provider() -> str:
    """Read the existing external discord_dev provider; never ship credentials."""
    explicit = os.environ.get('CINTERFORGE_CONFIG', '').strip()
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        try:
            import pwd
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError):
            home = Path(os.environ.get('USERPROFILE') or Path.home())
        candidates = [home / 'cfdev/config.json', home / 'dev/config.json']
    for path in candidates:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        provider = config.get('discord_dev')
        if not isinstance(provider, dict):
            provider = config.get('providers', {}).get('discord_dev')
        if not isinstance(provider, dict):
            continue
        env = provider.get('env', {})
        if not isinstance(env, dict):
            env = {}
        enabled = os.environ.get('CINTERFORGE_DISCORD_DEV_ENABLED',
                  env.get('CINTERFORGE_DISCORD_DEV_ENABLED', '1' if provider.get('enabled') else '0'))
        if str(enabled).lower() not in ('1', 'true', 'yes'):
            raise RuntimeError('Discord Dev is disabled in the existing external config.json')
        token = os.environ.get('CINTERFORGE_DISCORD_DEV_BOT_TOKEN') or env.get('CINTERFORGE_DISCORD_DEV_BOT_TOKEN')
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError('Discord Dev Bot token is missing from the external provider')
        return token.strip()
    raise RuntimeError('Discord Dev provider not found in the existing external config.json')


def _discord_api(path: str, method: str = 'GET', payload: dict | None = None):
    """Discord REST runs only in the trusted Python service, never in the page."""
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    token = _discord_provider()
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = Request('https://discord.com/api/v10' + path, method=method, data=data,
                      headers={'Authorization': 'Bot ' + token,
                               'User-Agent': 'CinterForgeTerminal (development tool)',
                               'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise RuntimeError('Discord response too large')
            return json.loads(raw) if raw else {}
    except HTTPError as error:
        # Never echo Authorization; Discord's status is sufficient to diagnose.
        raise RuntimeError('Discord API HTTP ' + str(error.code) + ' (verify bot membership, scopes and channel permissions)') from None
    except (URLError, TimeoutError) as error:
        raise RuntimeError('Discord connection failed: ' + type(error).__name__) from None


def _discord_resolve(guild_name: str, channel_name: str) -> dict:
    if not (0 < len(guild_name) <= 120 and 0 < len(channel_name) <= 120):
        raise ValueError('Discord guild and channel names required')
    identity = _discord_api('/users/@me')
    guilds = _discord_api('/users/@me/guilds')
    if not isinstance(guilds, list):
        raise RuntimeError('Discord did not return bot guilds')
    guild = next((g for g in guilds if g.get('name', '').casefold() == guild_name.casefold()), None)
    if not guild or not str(guild.get('id', '')).isdigit():
        raise RuntimeError('Discord bot is not a member of server: ' + guild_name)
    channels = _discord_api('/guilds/' + guild['id'] + '/channels')
    if not isinstance(channels, list):
        raise RuntimeError('Discord did not return channels')
    channel = next((c for c in channels if c.get('name', '').casefold() == channel_name.casefold()
                    and c.get('type') in (0, 5)), None)
    if not channel or not str(channel.get('id', '')).isdigit():
        raise RuntimeError('Discord bot cannot see text channel: ' + channel_name)
    return {'guild': guild.get('name', guild_name), 'channel': channel.get('name', channel_name),
            'channel_id': channel['id'],
            'bot': identity.get('global_name') or identity.get('username') or 'Discord bot'}

