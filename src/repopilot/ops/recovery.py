"""Startup crash recovery.

A worker that is killed mid-run leaves its task stuck in ``running`` in the
store. On restart the operator (or the worker supervisor) calls
:func:`reconcile_incomplete_tasks` to converge those records to a terminal
``failed`` state with a stable failure code, so nothing stays permanently
``running`` and nothing is ever reported as a fake success.
"""

from __future__ import annotations

from repopilot.models import Task, TaskStatus, utcnow
from repopilot.storage.base import TaskStore

#: Failure code attached to tasks that were interrupted by a worker crash.
STALE_FAILURE_CODE = "worker_lost"

_STALE_REASON = (
    "worker_lost: the task was interrupted before it finished and was reconciled "
    "on startup; re-run or retry it to get a fresh result"
)


def _converge(task: Task) -> None:
    task.status = TaskStatus.failed
    task.failure_code = STALE_FAILURE_CODE
    task.failure_reason = _STALE_REASON
    task.progress = "failed (reconciled at startup)"
    task.finished_at = utcnow()
    report = task.report
    if report is not None:
        report.status = TaskStatus.failed
        report.failure_code = STALE_FAILURE_CODE
        report.failure_reason = _STALE_REASON
        verification = report.verification
        verification.fully_verified = False
        if _STALE_REASON not in verification.verification_gaps:
            verification.verification_gaps = [*verification.verification_gaps, _STALE_REASON]


def reconcile_incomplete_tasks(
    store: TaskStore,
    *,
    include_pending: bool = False,
) -> list[str]:
    """Converge interrupted tasks to ``failed`` and return the affected ids.

    ``running`` tasks are always reconciled (they can no longer be picked up).
    ``pending`` tasks are only reconciled when ``include_pending`` is set, which
    the caller should do only when it knows no worker still holds the queue.
    """

    affected: list[str] = []
    for task in store.list():
        if task.status is TaskStatus.running or (
            include_pending and task.status is TaskStatus.pending
        ):
            _converge(task)
            store.save(task)
            affected.append(task.task_id)
    return affected
