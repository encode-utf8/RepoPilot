"""In-memory :class:`TaskQueue`.

Thread safe, dependency free and used by the default single-process deployment
and by every test that must not depend on an external service.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class InMemoryTaskQueue:
    """A blocking FIFO task queue backed by a ``deque`` and a condition."""

    def __init__(self) -> None:
        self._pending: deque[str] = deque()
        self._running: set[str] = set()
        self._cancel: set[str] = set()
        self._condition = threading.Condition()

    def push(self, task_id: str) -> None:
        with self._condition:
            self._pending.append(task_id)
            self._condition.notify()

    def pop(self, *, timeout: float = 1.0) -> str | None:
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while not self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            task_id = self._pending.popleft()
            self._running.add(task_id)
            return task_id

    def complete(self, task_id: str) -> None:
        with self._condition:
            self._running.discard(task_id)
            self._cancel.discard(task_id)
            self._condition.notify_all()

    def is_running(self, task_id: str) -> bool:
        with self._condition:
            return task_id in self._running

    def is_active(self, task_id: str) -> bool:
        with self._condition:
            return task_id in self._running or task_id in self._pending

    def request_cancel(self, task_id: str) -> bool:
        with self._condition:
            if task_id in self._running or task_id in self._pending:
                self._cancel.add(task_id)
                self._condition.notify_all()
                return True
            return False

    def cancel_requested(self, task_id: str) -> bool:
        with self._condition:
            return task_id in self._cancel

    def clear_cancel(self, task_id: str) -> None:
        with self._condition:
            self._cancel.discard(task_id)

    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    def close(self) -> None:
        with self._condition:
            self._condition.notify_all()
