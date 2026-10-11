"""Task worker: pulls ids from the queue and runs the right pipeline.

The worker is deliberately free of business logic. It resolves a runner for the
task's mode, hands it a cancellation token that observes the queue's cross-process
cancel flag, and makes sure the task never stays ``running`` if it dies: the
supervisor calls :meth:`TaskWorker.recover_stale_tasks` on startup.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.logging_setup import RunLogger
from repopilot.models import Task, TaskStatus, utcnow
from repopilot.ops.recovery import reconcile_incomplete_tasks
from repopilot.queue.base import TaskQueue
from repopilot.storage.base import TaskStore

RunnerResolver = Callable[[Task], object]


class TaskWorker:
    """A single-threaded queue consumer."""

    def __init__(
        self,
        *,
        store: TaskStore,
        queue: TaskQueue,
        runner_for: RunnerResolver,
        settings: Settings,
        poll_timeout: float = 1.0,
        reconcile_on_start: bool = True,
    ) -> None:
        self._store = store
        self._queue = queue
        self._runner_for = runner_for
        self._settings = settings
        self._poll_timeout = poll_timeout
        self._reconcile_on_start = reconcile_on_start
        self.processed = 0

    # -- lifecycle -------------------------------------------------------------

    def recover_stale_tasks(self, *, include_pending: bool = False) -> list[str]:
        """Converge interrupted tasks left by a previous crash."""

        return reconcile_incomplete_tasks(self._store, include_pending=include_pending)

    def run_once(self, *, timeout: float | None = None) -> str | None:
        """Pop and run one task; returns its id, or ``None`` on an empty poll."""

        task_id = self._queue.pop(timeout=self._poll_timeout if timeout is None else timeout)
        if task_id is None:
            return None
        try:
            self._execute(task_id)
        finally:
            self._queue.complete(task_id)
            self.processed += 1
        return task_id

    def run_forever(
        self,
        *,
        stop: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> int:
        """Consume the queue until ``stop`` is set or ``max_iterations`` reached."""

        if self._reconcile_on_start:
            self.recover_stale_tasks()
        stop = stop or threading.Event()
        iterations = 0
        while not stop.is_set():
            self.run_once()
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
        return self.processed

    # -- execution -------------------------------------------------------------

    def _execute(self, task_id: str) -> None:
        task = self._store.get(task_id)
        if task is None:
            return
        if self._queue.cancel_requested(task_id):
            self._mark_cancelled(task)
            return
        try:
            runner = self._runner_for(task)
        except Exception as exc:  # runner resolution failed: fail, never hang
            self._fail(task, "worker_dispatch_failed", f"no runner for task: {exc!r}")
            return
        token = CancellationToken(probe=lambda: self._queue.cancel_requested(task_id))
        self._log(task, "worker.started", "worker picked up the task")
        try:
            runner.run(task_id, token)
        except Exception as exc:  # pragma: no cover - runners are defensive already
            self._log(task, "worker.crashed", repr(exc), level="error")
            self._fail(task, "worker_error", f"worker_error: {exc!r}")

    def _mark_cancelled(self, task: Task) -> None:
        task.status = TaskStatus.cancelled
        task.failure_code = "cancelled"
        task.failure_reason = "cancelled: cancellation was requested before the task started"
        task.progress = "cancelled"
        task.finished_at = utcnow()
        self._store.save(task)
        self._log(task, "worker.cancelled", task.failure_reason or "cancelled")

    def _fail(self, task: Task, code: str, reason: str) -> None:
        current = self._store.get(task.task_id) or task
        current.status = TaskStatus.failed
        current.failure_code = code
        current.failure_reason = reason
        current.finished_at = utcnow()
        self._store.save(current)

    def _log(self, task: Task, event: str, message: str, *, level: str = "info") -> None:
        RunLogger(self._settings.log_dir, task.task_id, task.run_id).event(
            event, message, level=level
        )
