"""In-memory :class:`TaskStore` implementation."""

from __future__ import annotations

import threading

from repopilot.models import TERMINAL_TASK_STATUSES, Task, utcnow


class InMemoryTaskStore:
    """Thread safe process local task store.

    Stored objects are detached copies, so callers can never mutate state behind
    the store's back.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()

    def create(self, task: Task) -> Task:
        with self._lock:
            stored = task.model_copy(deep=True)
            self._tasks[stored.task_id] = stored
            self._order.append(stored.task_id)
            return stored.model_copy(deep=True)

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.model_copy(deep=True) if task is not None else None

    def save(self, task: Task) -> Task:
        with self._lock:
            if task.task_id not in self._tasks:
                self._order.append(task.task_id)
            stored = task.model_copy(deep=True)
            stored.updated_at = utcnow()
            self._tasks[stored.task_id] = stored
            return stored.model_copy(deep=True)

    def list(self) -> list[Task]:
        with self._lock:
            return [self._tasks[key].model_copy(deep=True) for key in reversed(self._order)]

    def find_active_by_key(self, key: tuple[str, ...]) -> Task | None:
        with self._lock:
            for task in self._tasks.values():
                if task.status in TERMINAL_TASK_STATUSES:
                    continue
                if task.request.dedupe_key() == key:
                    return task.model_copy(deep=True)
            return None
