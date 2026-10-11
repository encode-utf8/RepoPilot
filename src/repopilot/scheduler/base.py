"""``TaskScheduler`` boundary.

Phase 1 runs tasks on a local thread pool. Phase 4 swaps in a Redis backed queue
by implementing this protocol; the API layer never changes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from repopilot.cancellation import CancellationToken

TaskFunction = Callable[[CancellationToken], None]


class TaskScheduler(Protocol):
    """Schedules background task executions."""

    def submit(self, task_id: str, function: TaskFunction) -> None:
        """Start ``function`` in the background for ``task_id``."""

    def cancel(self, task_id: str) -> bool:
        """Request cancellation; returns ``False`` when the task is not tracked."""

    def token_for(self, task_id: str) -> CancellationToken | None:
        """Return the cancellation token for a task, if any."""

    def is_running(self, task_id: str) -> bool:
        """Return ``True`` while a submitted function has not finished."""

    def shutdown(self, wait: bool = False) -> None:
        """Release scheduler resources."""
