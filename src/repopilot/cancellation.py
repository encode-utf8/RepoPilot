"""Cooperative cancellation token shared by the scheduler and the sandbox."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class CancellationToken:
    """Thread safe one-shot cancellation flag.

    An optional ``probe`` lets a token observe cancellation requested in another
    process (for example a Redis flag set by a different API worker). The probe is
    rate limited so hot polling loops do not hammer the backing store.
    """

    def __init__(
        self,
        probe: Callable[[], bool] | None = None,
        *,
        probe_interval: float = 0.5,
    ) -> None:
        self._event = threading.Event()
        self._reason: str | None = None
        self._lock = threading.Lock()
        self._probe = probe
        self._probe_interval = probe_interval
        self._last_probe = 0.0

    def cancel(self, reason: str | None = None) -> None:
        with self._lock:
            if self._reason is None:
                self._reason = reason or "cancelled"
        self._event.set()

    def _probe_cancelled(self) -> bool:
        if self._probe is None:
            return False
        now = time.monotonic()
        if now - self._last_probe < self._probe_interval:
            return False
        self._last_probe = now
        try:
            if self._probe():
                self.cancel("cancel requested by another worker")
        except Exception:  # pragma: no cover - a flaky store must not break a run
            return self._event.is_set()
        return self._event.is_set()

    def is_cancelled(self) -> bool:
        if self._event.is_set():
            return True
        return self._probe_cancelled()

    @property
    def cancelled(self) -> bool:
        return self.is_cancelled()

    @property
    def reason(self) -> str | None:
        return self._reason
