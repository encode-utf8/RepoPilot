"""Durable task queue boundary.

The queue is what makes Phase 4 asynchronous: ``POST /api/tasks`` pushes a task
id and returns immediately, and one or more workers pop ids and run the pipeline.
Keeping this behind a protocol lets the default deployment (and the whole test
suite) use an in-memory queue while production uses Redis.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TaskQueue(Protocol):
    """A FIFO of task ids with cooperative cancellation and a running set."""

    def push(self, task_id: str) -> None:
        """Append ``task_id`` to the queue."""

    def pop(self, *, timeout: float = 1.0) -> str | None:
        """Block up to ``timeout`` seconds for the next task id, or ``None``."""

    def complete(self, task_id: str) -> None:
        """Mark ``task_id`` as finished and clear its cancellation flag."""

    def is_running(self, task_id: str) -> bool:
        """Return ``True`` while ``task_id`` has been popped but not completed."""

    def is_active(self, task_id: str) -> bool:
        """Return ``True`` while ``task_id`` is pending or running."""

    def request_cancel(self, task_id: str) -> bool:
        """Flag ``task_id`` for cancellation; ``False`` when it is not active."""

    def cancel_requested(self, task_id: str) -> bool:
        """Return ``True`` when cancellation was requested for ``task_id``."""

    def clear_cancel(self, task_id: str) -> None:
        """Drop the cancellation flag for ``task_id``."""

    def pending_count(self) -> int:
        """Return how many task ids are waiting to be picked up."""

    def close(self) -> None:
        """Release queue resources."""
