"""Exclude destructive source maintenance from complete production engine cycles."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class EngineCycleSynchronization:
    """One process-local gate shared by the cycle runner and source deletion."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def hold_cycle(self) -> Iterator[None]:
        with self._lock:
            yield

    @contextmanager
    def try_hold_maintenance(self) -> Iterator[bool]:
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()
