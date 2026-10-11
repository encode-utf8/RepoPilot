"""Task creation service.

Centralises the two things that must be atomic across workers:

* de-duplication: two identical submissions (same dedupe key) must not start two
  runs, even when they arrive at different processes at the same moment;
* enqueueing: the task record is persisted first, then the id is pushed to the
  queue, so a worker can always resolve the record it is handed.

De-duplication is guarded by a distributed lock (see :mod:`repopilot.locking`):
the winner of the lock performs the lookup-then-insert, and a loser waits briefly
for the winner to persist before returning that same record. A loser never
creates a second task, so ``POST /api/tasks`` stays idempotent per dedupe key.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

from repopilot.errors import TaskConflictError, TaskNotFoundError
from repopilot.ids import new_run_id, new_task_id
from repopilot.locking import LockManager, hold_lock
from repopilot.models import Task, TaskRequest, TaskStatus, utcnow
from repopilot.scheduler.base import TaskScheduler
from repopilot.storage.base import TaskStore

RunnerResolver = Callable[[str], object]


def dedupe_lock_key(key: tuple[str, ...]) -> str:
    """Return a short, filesystem-safe lock key for a request dedupe key."""

    digest = hashlib.sha1("\x1f".join(key).encode("utf-8")).hexdigest()
    return f"dedupe:{digest}"


class TaskService:
    """Creates, de-duplicates and enqueues tasks."""

    def __init__(
        self,
        *,
        store: TaskStore,
        scheduler: TaskScheduler,
        lock_manager: LockManager,
        runner_for: RunnerResolver,
        task_id_factory: Callable[[], str] = new_task_id,
        run_id_factory: Callable[[], str] = new_run_id,
        dedupe_lock_ttl_seconds: float = 30.0,
        dedupe_wait_seconds: float = 5.0,
        dedupe_poll_seconds: float = 0.02,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._lock_manager = lock_manager
        self._runner_for = runner_for
        self._task_id_factory = task_id_factory
        self._run_id_factory = run_id_factory
        self._dedupe_lock_ttl = dedupe_lock_ttl_seconds
        self._dedupe_wait = max(dedupe_wait_seconds, 0.0)
        self._dedupe_poll = max(dedupe_poll_seconds, 0.001)

    def submit(self, request: TaskRequest) -> tuple[Task, bool]:
        """Create and enqueue ``request``; returns ``(task, deduplicated)``."""

        key = request.dedupe_key()
        with hold_lock(
            self._lock_manager, dedupe_lock_key(key), ttl_seconds=self._dedupe_lock_ttl
        ) as acquired:
            if acquired:
                return self._create_once(request, key)
            # Another worker is creating the same task. Wait briefly for it to
            # persist, then hand back that record; never create a second one.
            existing = self._wait_for_active(key)
            if existing is not None:
                return existing, True
            raise TaskConflictError(
                "an identical task is already being submitted by another worker"
            )

    def _create_once(self, request: TaskRequest, key: tuple[str, ...]) -> tuple[Task, bool]:
        existing = self._store.find_active_by_key(key)
        if existing is not None:
            return existing, True
        task = self._new_task(request)
        stored = self._store.create(task)
        self._enqueue(stored, request)
        return stored, False

    def _wait_for_active(self, key: tuple[str, ...]) -> Task | None:
        deadline = time.monotonic() + self._dedupe_wait
        while True:
            existing = self._store.find_active_by_key(key)
            if existing is not None:
                return existing
            if time.monotonic() >= deadline:
                return None
            time.sleep(self._dedupe_poll)

    def retry(self, task_id: str) -> tuple[Task, bool]:
        """Re-run a finished task as a new task, returning ``(new_task, deduplicated)``."""

        original = self._store.get(task_id)
        if original is None:
            raise TaskNotFoundError(f"unknown task id: {task_id}")
        if not original.is_terminal:
            raise TaskConflictError(
                f"task {task_id} is still {original.status.value} and cannot be retried"
            )
        request = original.request.model_copy(deep=True)
        request.resume_task_id = None
        task = self._new_task(request)
        task.retried_from = original.task_id
        stored = self._store.create(task)
        self._enqueue(stored, request)
        return stored, False

    # -- internals -------------------------------------------------------------

    def _new_task(self, request: TaskRequest) -> Task:
        now = utcnow()
        return Task(
            task_id=self._task_id_factory(),
            run_id=self._run_id_factory(),
            status=TaskStatus.pending,
            request=request,
            created_at=now,
            updated_at=now,
        )

    def _enqueue(self, task: Task, request: TaskRequest) -> None:
        runner = self._runner_for(request.mode)

        def _run(token) -> None:
            runner.run(task.task_id, token)

        self._scheduler.submit(task.task_id, _run)
