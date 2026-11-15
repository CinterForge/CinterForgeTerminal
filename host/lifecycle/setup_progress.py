"""Visible elapsed-time progress for blocking Setup/installer operations.

The lifecycle already persists console output in Builds/Logs. A subprocess can
legitimately buffer its output for minutes, so report time elapsed without
changing the command, exit code, timeout, or captured diagnostic output.
"""
from __future__ import annotations

from contextlib import contextmanager
import threading
import time
from typing import Iterator


@contextmanager
def progress_step(activity: str, *, prefix: str = '[WINDOWS SETUP]',
                  interval_seconds: float = 15.0) -> Iterator[None]:
    """Announce a blocking operation and emit a heartbeat until it completes.

    Percentages aren't reported because Wine initialization and native MSI compilation do
    not expose reliable completion fractions. Never swallow the original error.
    """
    if not activity.strip() or interval_seconds <= 0:
        raise ValueError('Progress needs an activity and positive heartbeat interval')
    started = time.monotonic()
    finished = threading.Event()
    print(f'{prefix} START {activity}', flush=True)

    def heartbeat() -> None:
        while not finished.wait(interval_seconds):
            elapsed = int(time.monotonic() - started)
            print(f'{prefix} WORKING {activity} ({elapsed}s elapsed)', flush=True)

    ticker = threading.Thread(target=heartbeat, name='CinterSetupProgress', daemon=True)
    ticker.start()
    try:
        yield
    except BaseException:
        print(f'{prefix} FAIL {activity} ({int(time.monotonic() - started)}s elapsed)', flush=True)
        raise
    else:
        print(f'{prefix} PASS {activity} ({int(time.monotonic() - started)}s)', flush=True)
    finally:
        finished.set()
        ticker.join(timeout=1)
