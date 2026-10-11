"""Startup crash recovery converges interrupted tasks to ``failed``."""

from __future__ import annotations

from repopilot.models import (
    ProjectProfile,
    RepoMetadata,
    Task,
    TaskRequest,
    TaskStatus,
    utcnow,
)
from repopilot.ops.recovery import STALE_FAILURE_CODE, reconcile_incomplete_tasks
from repopilot.report.builder import build_report
from repopilot.storage.memory import InMemoryTaskStore


def _task(task_id: str, *, status: TaskStatus, with_report: bool = False) -> Task:
    task = Task(
        task_id=task_id,
        run_id=f"run_{task_id}",
        status=status,
        request=TaskRequest(repo_url="/tmp/repo"),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    if with_report:
        task.report = build_report(
            task_id=task_id,
            run_id=task.run_id,
            status=status,
            repo=RepoMetadata(source="/tmp/repo"),
            project=ProjectProfile(),
            checkers=[],
        )
    return task


def test_running_tasks_are_converged_to_failed() -> None:
    store = InMemoryTaskStore()
    store.create(_task("task_running", status=TaskStatus.running, with_report=True))
    store.create(_task("task_pending", status=TaskStatus.pending))
    store.create(_task("task_done", status=TaskStatus.succeeded))

    affected = reconcile_incomplete_tasks(store)

    assert affected == ["task_running"]
    recovered = store.get("task_running")
    assert recovered is not None
    assert recovered.status is TaskStatus.failed
    assert recovered.failure_code == STALE_FAILURE_CODE
    assert recovered.finished_at is not None
    # a reconciled task must never advertise a fully verified success
    assert recovered.report is not None
    assert recovered.report.status is TaskStatus.failed
    assert recovered.report.verification.fully_verified is False
    assert recovered.report.verification.verification_gaps
    # pending and terminal tasks are untouched by the default pass
    pending = store.get("task_pending")
    done = store.get("task_done")
    assert pending is not None and pending.status is TaskStatus.pending
    assert done is not None and done.status is TaskStatus.succeeded


def test_include_pending_converges_pending_tasks() -> None:
    store = InMemoryTaskStore()
    store.create(_task("task_pending", status=TaskStatus.pending))
    affected = reconcile_incomplete_tasks(store, include_pending=True)
    assert affected == ["task_pending"]
    reclaimed = store.get("task_pending")
    assert reclaimed is not None and reclaimed.status is TaskStatus.failed


def test_recovery_is_idempotent() -> None:
    store = InMemoryTaskStore()
    store.create(_task("task_running", status=TaskStatus.running))
    assert reconcile_incomplete_tasks(store) == ["task_running"]
    assert reconcile_incomplete_tasks(store) == []
