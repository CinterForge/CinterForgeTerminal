"""Path/process adapters for the copied IF Code-OSS producer.

Long-running maintainer commands must stream progress and have a bounded idle
period. This is exclusively a Setup-time process adapter, not a new producer.
"""
from __future__ import annotations
import codecs
from collections import deque
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time


def codeoss_root(repo_root: Path) -> Path:
    workspace = Path(repo_root).expanduser().resolve()
    if workspace.name == 'CinterForgeTerminal':
        workspace = workspace.parent
    return workspace / 'Builds' / 'InteractiveFiction' / 'CodeOSS'


def _stop_owned_process(process: subprocess.Popen) -> None:
    """Stop the command and its children, not another user's processes."""
    if os.name == 'nt' and process.poll() is not None:
        return
    if os.name == 'nt':
        # Windows does not have POSIX process groups. Kill only this process tree.
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       capture_output=True, check=False, timeout=10)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_with_progress(command, *, label: str, timeout: float,
                       inactivity_timeout: float, heartbeat: float,
                       hide_window: bool, **kwargs):
    """Show real command output plus periodic health signals; fail closed on stalls.

    Use a reader thread rather than readline/select: Git uses carriage-return
    progress, and selectors cannot wait on Windows anonymous subprocess pipes.
    A separate process group permits cleanup of Git/npm children on interrupt.
    """
    kwargs.pop('capture_output', None)
    kwargs.pop('text', None)
    kwargs.pop('encoding', None)
    kwargs.pop('errors', None)
    kwargs.pop('stdout', None)
    kwargs.pop('stderr', None)
    kwargs.pop('check', None)
    kwargs.pop('timeout', None)
    if os.name == 'nt':
        flags = kwargs.pop('creationflags', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        if hide_window:
            flags |= getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
        kwargs['creationflags'] = flags
        if hide_window and 'startupinfo' not in kwargs:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            kwargs['startupinfo'] = startupinfo
    else:
        kwargs['start_new_session'] = True

    print(f'[codeoss] START {label} (max={int(timeout)}s, idle-limit={int(inactivity_timeout)}s)', flush=True)
    start = last_output = last_report = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, bufsize=0, **kwargs)
    chunks = queue.Queue(maxsize=64)

    def reader() -> None:
        try:
            while True:
                block = os.read(process.stdout.fileno(), 4096)
                if not block:
                    break
                chunks.put(block)
        finally:
            chunks.put(None)

    threading.Thread(target=reader, daemon=True, name='codeoss-progress').start()
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    pending = ''
    tail = deque(maxlen=120)
    eof = False
    exit_observed = None

    def output_line(line: str) -> None:
        text = line.strip()
        if text:
            tail.append(text[-2000:])
            print(f'[codeoss:{label}] {text[-2000:]}', flush=True)

    try:
        while True:
            now = time.monotonic()
            elapsed, quiet = now - start, now - last_output
            if elapsed >= timeout or (not eof and quiet >= inactivity_timeout):
                why = f'hard timeout {int(timeout)}s' if elapsed >= timeout else f'no output for {int(quiet)}s'
                detail = ' | '.join(tail)[-2000:]
                raise subprocess.TimeoutExpired(command, elapsed, output=detail)
            try:
                block = chunks.get(timeout=0.5)
            except queue.Empty:
                block = b''
            if block is None:
                eof = True
                pending += decoder.decode(b'', final=True)
            elif block:
                last_output = time.monotonic()
                pending += decoder.decode(block).replace('\r', '\n')

            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                output_line(line)
            if len(pending) > 8192:
                output_line(pending[:8192])
                pending = pending[8192:]

            now = time.monotonic()
            if now - last_report >= heartbeat:
                print(f'[codeoss] RUNNING {label} elapsed={int(now-start)}s '
                      f'since-output={int(now-last_output)}s', flush=True)
                last_report = now
            rc = process.poll()
            if rc is not None:
                if exit_observed is None:
                    exit_observed = now
                if eof:
                    break
                # A forked child may retain stdout after the main command exits.
                # Never let it hold Setup indefinitely.
                if now - exit_observed >= 3:
                    raise RuntimeError(f'{label} exited but a child kept its output pipe open')
        if pending.strip():
            output_line(pending)
        print(f'[codeoss] {"PASS" if rc == 0 else "FAIL"} {label} '
              f'exit={rc} elapsed={int(time.monotonic()-start)}s', flush=True)
        return subprocess.CompletedProcess(command, rc, stdout='\n'.join(tail), stderr='')
    except BaseException:
        _stop_owned_process(process)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()


def run_process(argv, *, hide_window=True, live=False, label=None,
                inactivity_timeout=300, heartbeat=15, **kwargs):
    command = [str(arg) for arg in argv]
    if live:
        limit = float(kwargs.pop('timeout', 3600))
        return _run_with_progress(command, label=label or Path(command[0]).name,
                                  timeout=limit,
                                  inactivity_timeout=float(inactivity_timeout),
                                  heartbeat=float(heartbeat),
                                  hide_window=hide_window, **kwargs)
    merged = dict(kwargs)
    if os.name == 'nt' and hide_window:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        merged.setdefault('startupinfo', startupinfo)
        merged.setdefault('creationflags', getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
    return subprocess.run(command, **merged)
