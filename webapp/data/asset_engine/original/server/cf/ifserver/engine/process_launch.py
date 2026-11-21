# =============================================================================
# CINTERFORGE IF FILE PURPOSE
# One cross-platform subprocess contract for every IFPlayer child/helper launch.
# Keep Windows GUI-process hiding and packaged-Python requirements here, while
# keeping macOS/Linux venv launches free of embedded-Python environment leakage.
# =============================================================================

"""Centralized process launching for CinterForge IFPlayer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence



_PYTHON_DFROTZ_PROCESS_RE = re.compile(
    r"^(?:python(?:w|[0-9.]*)?|dfrotz)(?:\.exe)?$",
    re.IGNORECASE,
)


def _system_process_rows() -> list[tuple[int, int, str]]:
    """Return ``(pid, ppid, executable-name)`` for visible host processes.

    IFPlayer launch cleanup intentionally has no psutil dependency because this
    code must run in the packaged Mac, Windows, and Linux Python environments.
    """
    rows: list[tuple[int, int, str]] = []
    if os.name == "nt":
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json -Compress",
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=10,
                **platform_process_kwargs(),
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return rows
            payload = json.loads(completed.stdout)
            if isinstance(payload, dict):
                payload = [payload]
            for item in payload if isinstance(payload, list) else []:
                if not isinstance(item, dict):
                    continue
                try:
                    rows.append((
                        int(item.get("ProcessId") or 0),
                        int(item.get("ParentProcessId") or 0),
                        str(item.get("Name") or "").strip(),
                    ))
                except (TypeError, ValueError):
                    continue
        except Exception:
            return rows
        return rows

    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,comm="],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return rows
    if completed.returncode != 0:
        return rows
    for line in completed.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), Path(parts[2]).name))
        except ValueError:
            continue
    return rows


def kill_all_other_python_dfrotz_processes() -> list[int]:
    """Aggressively clear stale Python processes before IFPlayer launch.

    Pete explicitly requested launch-time cleanup of every Python
    process.  The current IFPlayer process and its ancestor chain are the sole
    unavoidable exception: killing either would terminate CinterForge/IFPlayer
    before the clean launch could occur.  Every other matching process is
    terminated, then forcibly killed if still present.
    """
    rows = _system_process_rows()
    if not rows:
        print("[ifplayer cleanup] process enumeration unavailable; nothing killed", flush=True)
        return []

    by_pid = {pid: (ppid, name) for pid, ppid, name in rows if pid > 0}
    protected: set[int] = {os.getpid()}
    cursor = os.getpid()
    while cursor in by_pid:
        parent = by_pid[cursor][0]
        if parent <= 0 or parent in protected:
            break
        protected.add(parent)
        cursor = parent

    targets = sorted(
        pid for pid, _ppid, name in rows
        if pid > 0 and pid not in protected and _PYTHON_DFROTZ_PROCESS_RE.match(Path(name).name)
    )
    if not targets:
        print("[ifplayer cleanup] no other python processes found", flush=True)
        return []

    print(
        f"[ifplayer cleanup] terminating all other python processes count={len(targets)} "
        f"pids={targets}",
        flush=True,
    )
    terminated: list[int] = []
    for pid in targets:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(pid), "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, **platform_process_kwargs(),
                )
            else:
                os.kill(pid, signal.SIGTERM)
            terminated.append(pid)
        except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
            continue

    if os.name != "nt" and terminated:
        deadline = time.monotonic() + 1.0
        survivors = set(terminated)
        while survivors and time.monotonic() < deadline:
            for pid in tuple(survivors):
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError, OSError):
                    survivors.discard(pid)
            if survivors:
                time.sleep(0.05)
        for pid in sorted(survivors):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    print(f"[ifplayer cleanup] cleanup complete attempted={terminated}", flush=True)
    return terminated

def platform_process_kwargs(*, hide_window: bool = True) -> dict[str, object]:
    """Return platform-specific Popen/run flags.

    Windows IFPlayer runs under the GUI application, so every console helper and
    Python child must be created without a transient console window.  POSIX
    platforms require no corresponding startup flags.
    """
    if os.name != "nt" or not hide_window:
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    }


def process_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a mutable process environment without changing platform semantics."""
    return dict(os.environ if base is None else base)


def python_child_environment(
    module_root: str | Path,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the environment for an IFPlayer Python child.

    The parent/supervisor interpreter is already the interpreter selected by the
    CinterForge worker launcher.  Children must use that same interpreter.

    On Windows the staged pythonw.exe requires the parent's packaged PYTHONHOME
    and DLL/PATH contract, so those values are deliberately preserved.

    On macOS/Linux, PYTHONHOME/PYTHONEXECUTABLE can leak from an embedded/native
    host and corrupt a venv child.  Strip them and let the selected venv Python
    establish its own prefix.  This is the same split used by normal CinterForge
    workers and prevents Windows-specific bootstrap settings from breaking Mac.
    """
    env = process_environment(base)
    root = str(Path(module_root).resolve())
    inherited = str(env.get("PYTHONPATH", "") or "").strip()
    env["PYTHONPATH"] = root if not inherited else os.pathsep.join((root, inherited))
    env["PYTHONNOUSERSITE"] = "1"
    if os.name != "nt":
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONEXECUTABLE", None)
    return env


def launch_process(
    argv: Sequence[str | os.PathLike[str]],
    *,
    hide_window: bool = True,
    **kwargs: Any,
) -> subprocess.Popen:
    """Launch one IFPlayer process using the shared cross-platform contract."""
    merged = dict(kwargs)
    for key, value in platform_process_kwargs(hide_window=hide_window).items():
        merged.setdefault(key, value)
    return subprocess.Popen([str(value) for value in argv], **merged)


def run_process(
    argv: Sequence[str | os.PathLike[str]],
    *,
    hide_window: bool = True,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run one IFPlayer process to completion using the shared contract."""
    merged = dict(kwargs)
    for key, value in platform_process_kwargs(hide_window=hide_window).items():
        merged.setdefault(key, value)
    return subprocess.run([str(value) for value in argv], **merged)


def launch_python_module(
    module: str,
    args: Sequence[str],
    *,
    module_root: str | Path,
    env: Mapping[str, str] | None = None,
    hide_window: bool = True,
    **kwargs: Any,
) -> subprocess.Popen:
    """Launch a Python module with the *same interpreter as this supervisor*.

    Never rediscover Python independently in nested IFPlayer code.  The outer
    CinterForge worker launcher owns interpreter selection; all-mode children
    simply inherit that choice through ``sys.executable`` plus the normalized
    environment above.
    """
    root = Path(module_root).resolve()
    command = [sys.executable, "-m", module, *[str(value) for value in args]]
    child_env = python_child_environment(root, base=env)
    kwargs.setdefault("cwd", str(root))
    kwargs["env"] = child_env
    return launch_process(command, hide_window=hide_window, **kwargs)
