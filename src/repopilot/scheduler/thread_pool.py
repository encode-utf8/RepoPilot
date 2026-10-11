"""Thread pool based :class:`TaskScheduler` implementation."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor

from repopilot.cancellation import CancellationToken
from repopilot.scheduler.base import TaskFunction


class ThreadPoolTaskScheduler:
    """Runs each task in a worker thread and supports cooperative cancellation."""

    def __init__(self, max_workers: int = 4, thread_name_prefix: str = "repopilot-task") -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._tokens: dict[str, CancellationToken] = {}
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.RLock()

    def submit(self, task_id: str, function: TaskFunction) -> None:
        token = CancellationToken()
        with self._lock:
            self._tokens[task_id] = token
            self._futures[task_id] = self._executor.submit(function, token)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(task_id)
        if token is None:
            return False
        token.cancel(f"cancel requested for {task_id}")
        return True

    def token_for(self, task_id: str) -> CancellationToken | None:
        with self._lock:
            return self._tokens.get(task_id)

    def is_running(self, task_id: str) -> bool:
        with self._lock:
            future = self._futures.get(task_id)
        return future is not None and not future.done()

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)
