"""Setup-only verified GUI preflights for both retained Linux/Windows images.

Run independently of Wine, fail closed, and save durable per-platform logs.
Build, Stage, Test and the installed application never provision GUI packages.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

IMAGE_SCRIPT = '/usr/local/lib/cinterforge/gui_preflight.py'
SOURCE_SCRIPT = Path(__file__).resolve().parent / 'docker/gui_preflight.py'
MARKERS = ('X11 Xvfb', 'VNC TCP/RFB', 'noVNC/websockify')


def verify_guest_gui(builds: Path, *, guest: str, docker: str | None,
                     image: str | None, timeout: int = 50) -> dict:
    """Prove X11, real RFB handshake and browser endpoint before Wine starts."""
    if guest not in ('linux', 'windows'):
        raise ValueError('GUI preflight only supports Linux and Windows guests')
    logs = builds / 'Logs/CinterForgeTerminal'
    logs.mkdir(parents=True, exist_ok=True)
    logfile = logs / f'{guest}-gui-preflight.log'
    receipt = logs / f'{guest}-gui-preflight.json'
    if docker and image:
        command = [str(docker), 'run', '--rm', '--platform', 'linux/amd64',
                   '--network', 'none', str(image), 'python3', '-u', IMAGE_SCRIPT,
                   '--platform', guest]
    elif not docker and not image and sys.platform.startswith('linux'):
        command = [sys.executable, '-u', str(SOURCE_SCRIPT), '--platform', guest]
    else:
        raise RuntimeError(f'{guest} GUI preflight requires a provisioned guest image')
    result = {'schema': 'cinterforge.terminal.gui-preflight.v1', 'guest': guest,
              'status': 'failed', 'image': image, 'timestamp': time.time(),
              'checks': {'x11': 'failed', 'vnc': 'failed', 'novnc': 'failed'},
              'log': str(logfile)}
    try:
        with logfile.open('w', encoding='utf-8') as output:
            output.write('$ ' + ' '.join(command) + '\n')
            output.flush()
            try:
                proc = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                      timeout=timeout, check=False)
                result['exit_code'] = proc.returncode
            except subprocess.TimeoutExpired:
                result['error'] = f'GUI preflight timed out after {timeout}s'
        transcript = logfile.read_text(encoding='utf-8', errors='replace')
        for key, marker in zip(result['checks'], MARKERS):
            if f'[GUI PREFLIGHT] PASS {guest} {marker}' in transcript:
                result['checks'][key] = 'passed'
        if result.get('exit_code') != 0 or any(v != 'passed' for v in result['checks'].values()):
            result.setdefault('error', 'X11/VNC/noVNC readiness failed: ' + transcript[-1200:])
            raise RuntimeError(result['error'] + f'; see {logfile}')
        result['status'] = 'passed'
        print(f'[GUI PREFLIGHT] PASS {guest}: X11 + VNC RFB + noVNC ({logfile})', flush=True)
        return result
    finally:
        receipt.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')
