"""Host-only interface for verifying native Terminal HTTP ownership.

PURPOSE
    The repository command dispatcher can send an explicit reboot request to
    the running application without importing the Python HTTP application.

METHODS
    existing_native_owner(builds): verify a private owner receipt, native PID,
    and instance-specific /healthz challenge on the loopback service. Return
    None for stale/foreign owners. Does not start, kill, or mutate app services.

BOUNDARY
    This duplicates the narrowly scoped runtime protocol; only the local HTTP
    API and existing receipt on disk couple the two independently owned layers.
    The default developer receipt remains under Builds/CinterForgeTerminal/Config.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen

LAN_HTTP_PORT = 20300


def existing_native_owner(builds: Path) -> dict | None:
    path = Path(builds) / 'CinterForgeTerminal/Config/http-owner.json'
    try:
        if (path.parent.is_symlink() or path.is_symlink() or not path.is_file() or
                path.stat().st_size > 2048 or
                (os.name != 'nt' and path.stat().st_mode & 0o077)):
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
        if (record.get('schema') != 'cinterforge.terminal.http-owner.v1' or
                record.get('port') != LAN_HTTP_PORT or
                not re.fullmatch(r'[0-9a-f]{32}', str(record.get('instance', ''))) or
                not re.fullmatch(r'[A-Za-z0-9_-]{40,128}', str(record.get('native_grant', '')))):
            return None
        native_pid = record.get('native_pid')
        if type(native_pid) is not int or native_pid <= 1:
            return None
        os.kill(native_pid, 0)
        req = Request(f'http://127.0.0.1:{LAN_HTTP_PORT}/healthz',
                      headers={'X-Cinter-Owner': record['native_grant']})
        with urlopen(req, timeout=1.5) as response:
            status = json.loads(response.read(2048))
        if status.get('ready') and status.get('instance') == record['instance']:
            return record
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return None
