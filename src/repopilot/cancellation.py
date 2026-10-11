"""Cooperative cancellation token shared by the scheduler and the sandbox."""

from __future__ import annotations

import threading


class CancellationToken:
    """Thread safe one-shot cancellation flag."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None
        self._lock = threading.Lock()

    def cancel(self, reason: str | None = None) -> None:
        with self._lock:
            if self._reason is None:
                self._reason = reason or "cancelled"
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def is_cancelled(self) -> bool:
        return self._event.is_set()
