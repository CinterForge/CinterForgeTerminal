"""CinterForgeLogging — the Python service's bounded persistent Server Log.

PURPOSE: Own the one file-backed, cursor-addressable Server tab log. The HTTP
service and VM supervisor append status here; this component never starts,
restarts or stops a guest, and does not handle authentication or HTTP routing.

CLASS AND METHODS:
- CinterForgeRuntimeLog.__init__ opens a new log under an authorized caller
  supplied directory, with an in-memory bounded ring for browser polling.
- append writes redacted/structured *caller-provided* status lines and flushes.
- since retrieves a cursor-limited batch without re-reading the whole file.
- close flushes and releases the log handle on HTTP server shutdown.

LIFECYCLE: The outer server selects the correct developer Builds or Steam Logs
root and owns this instance from HTTP startup through shutdown. Keep sensitive
secrets out of append messages; no alternate runtime-state store is created.
"""
from __future__ import annotations
from collections import deque
from pathlib import Path
import os
import secrets
import threading
import time

class CinterForgeRuntimeLog:
    """Thread-safe, bounded live Server tab with a persistent session log."""
    def __init__(self, directory: Path, *, memory_lines: int = 2048, batch_lines: int = 120):
        self.batch_lines = batch_lines
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise RuntimeError('Unsafe CinterForge Terminal log directory: ' + str(directory))
        directory.mkdir(parents=True, exist_ok=True)
        filename = time.strftime('server-%Y%m%d-%H%M%S-') + str(os.getpid()) + '-' + secrets.token_hex(4) + '.log'
        self.path = directory / filename
        self.stream = self.path.open('x', encoding='utf-8', buffering=1)
        self.lock = threading.Lock()
        self.cursor = 0
        self.lines = deque(maxlen=memory_lines)
        self.append('HTTP/Python server log opened; path=' + str(self.path))

    def append(self, message: str) -> None:
        # Never log API tokens, request bodies, cookies, or whole URL queries.
        line = time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + str(message).replace('\r', ' ').replace('\x00', '')
        with self.lock:
            for part in line.splitlines():
                self.cursor += 1
                self.lines.append((self.cursor, part))
                self.stream.write(part + '\n')
            self.stream.flush()

    def since(self, cursor: int) -> dict:
        if cursor < 0:
            raise ValueError('Invalid Server log cursor')
        with self.lock:
            values = [(i, line) for i, line in self.lines if i > cursor][:self.batch_lines]
            return {'lines': [line for _, line in values],
                    'cursor': values[-1][0] if values else max(cursor, self.cursor),
                    'log_path': str(self.path), 'read_only': True,
                    'truncated': bool(self.lines and cursor < self.lines[0][0]-1)}

    def close(self) -> None:
        with self.lock:
            if not self.stream.closed:
                self.stream.flush()
                self.stream.close()

