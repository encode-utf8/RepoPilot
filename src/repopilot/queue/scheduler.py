"""Queue backed :class:`TaskScheduler`.

``submit`` enqueues the task id and returns immediately; a :class:`TaskWorker`
(same process or a different one) pulls it and runs the pipeline. The compatibility
surface is identical to :class:`ThreadPoolTaskScheduler` so the API layer does not
change.
"""

from __future__ import annotations

import threading

from repopilot.cancellation import CancellationToken
from repopilot.queue.base import TaskQueue
from repopilot.scheduler.base import TaskFunction


class QueueTaskScheduler:
    """Enqueues tasks for one or more workers; supports cooperative cancellation."""

    def __init__(self, queue: TaskQueue) -> None:
        self._queue = queue
        self._tokens: dict[str, CancellationToken] = {}
        self._lock = threading.RLock()

    def submit(self, task_id: str, function: TaskFunction) -> None:
        # ``function`` is intentionally unused: the worker resolves the pipeline
        # from the persisted task record, which is what allows cross-process runs.
        del function
        token = CancellationToken(probe=lambda: self._queue.cancel_requested(task_id))
        with self._lock:
            self._tokens[task_id] = token
        self._queue.push(task_id)

    def cancel(self, task_id: str) -> bool:
        if not self._queue.request_cancel(task_id):
            return False
        token = self.token_for(task_id)
        if token is not None:
            token.cancel(f"cancel requested for {task_id}")
        return True

    def token_for(self, task_id: str) -> CancellationToken | None:
        with self._lock:
            return self._tokens.get(task_id)

    def is_running(self, task_id: str) -> bool:
        return self._queue.is_running(task_id)

    def shutdown(self, wait: bool = False) -> None:
        del wait
        with self._lock:
            self._tokens.clear()
        self._queue.close()
