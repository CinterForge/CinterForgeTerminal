"""Install and verify the actual Microsoft WebView2 *browser runtime* on Windows.

The upstream webview/webview CMake target uses WebView2 SDK headers at compile
 time; those headers do not supply the browser engine needed at run time.
Evergreen bootstrap acquisition is for native Windows first-run setup ONLY.
The offline universal distribution still requires its own sealed runtime closure.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import time
import urllib.request

# Microsoft's public WebView2 runtime bootstrapper link, not SDK headers.
MICROSOFT_BOOTSTRAPPER = 'https://go.microsoft.com/fwlink/p/?linkid=2124703'
EDGE_CLIENT = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
REGISTRY_LOCATIONS = (
    'SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\' + EDGE_CLIENT,
    'SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\' + EDGE_CLIENT,
)


def installed_version() -> str | None:
    """Return a real installed version, never infer from a checked-in SDK."""
    if platform.system() != 'Windows':
        return None
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for location in REGISTRY_LOCATIONS:
            try:
                with winreg.OpenKey(hive, location) as key:
                    value, _kind = winreg.QueryValueEx(key, 'pv')
                if isinstance(value, str) and value.strip() not in ('', '0.0.0.0'):
                    return value.strip()
            except OSError:
                continue
    return None


def ensure(builds: Path) -> dict:
    """Windows uses the self-contained browser staged under Builds, not registry."""
    if platform.system() != 'Windows':
        raise RuntimeError('Native Windows runtime acceptance requires Windows')
    from host.windows_fixed_runtime import ensure as provision_fixed, VERSION
    folder = provision_fixed(builds)
    if not (folder / 'msedgewebview2.exe').is_file():
        raise RuntimeError('Microsoft WebView2 Fixed Runtime is not actually staged')
    os.environ['WEBVIEW2_BROWSER_EXECUTABLE_FOLDER'] = str(folder)
    log = builds / 'Logs/CinterForgeTerminal/windows-webview2-runtime.json'
    log.parent.mkdir(parents=True, exist_ok=True)
    result = {'schema': 'cinterforge.terminal.windows-webview2-fixed.v2',
              'status': 'passed', 'version': VERSION, 'runtime_path': str(folder),
              'execution': 'installed-files-verified-not-GUI-acceptance'}
    log.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return result
