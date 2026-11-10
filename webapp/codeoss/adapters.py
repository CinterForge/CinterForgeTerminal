"""Path/process adapters for the copied IF Code-OSS producer.

The actual producer is copied from CinterForgeServer, not rewritten here.
Only the old Server path and subprocess imports are translated for Terminal.
"""
from __future__ import annotations
import os
from pathlib import Path
import subprocess


def codeoss_root(repo_root: Path) -> Path:
    workspace = Path(repo_root).expanduser().resolve()
    if workspace.name == 'CinterForgeTerminal':
        workspace = workspace.parent
    return workspace / 'Builds' / 'InteractiveFiction' / 'CodeOSS'


def run_process(argv, *, hide_window=True, **kwargs):
    merged = dict(kwargs)
    if os.name == 'nt' and hide_window:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        merged.setdefault('startupinfo', startupinfo)
        merged.setdefault('creationflags', getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
    return subprocess.run([str(arg) for arg in argv], **merged)
