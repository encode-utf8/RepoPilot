"""Task worker: polling, dispatch, failure and crash convergence."""

from __future__ import annotations

import contextlib
import threading

from repopilot.config import Settings
from repopilot.models import Task, TaskRequest, TaskStatus, utcnow
from repopilot.ops.recovery import STALE_FAILURE_CODE, reconcile_incomplete_tasks
from repopilot.queue.memory import InMemoryTaskQueue
from repopilot.queue.worker import TaskWorker
from repopilot.storage.memory import InMemoryTaskStore


class _StoreRunner:
    """Runner double that records runs and marks the task terminal."""

    def __init__(self, store, *, status: TaskStatus = TaskStatus.succeeded, error=None) -> None:
        self._store = store
        self._status = status
        self._error = error
        self.runs: list[str] = []

    def run(self, task_id: str, token) -> None:
        self.runs.append(task_id)
        if self._error is not None:
            raise self._error
        current = self._store.get(task_id)
        if current is not None:
            current.status = self._status
            self._store.save(current)


def _task(task_id: str) -> Task:
    return Task(
        task_id=task_id,
        run_id=f"run_{task_id}",
        request=TaskRequest(repo_url="/tmp/repo"),
        created_at=utcnow(),
        updated_at=utcnow(),
    )


def _worker(store, queue, settings: Settings, runner_for, **kwargs) -> TaskWorker:
    return TaskWorker(
        store=store,
        queue=queue,
        runner_for=runner_for,
        settings=settings,
        poll_timeout=0.02,
        **kwargs,
    )


def test_run_once_executes_and_marks_running_then_terminal(settings: Settings) -> None:
    store = InMemoryTaskStore()
    queue = InMemoryTaskQueue()
    runner = _StoreRunner(store)
    worker = _worker(store, queue, settings, lambda task: runner)
    store.create(_task("task_1"))
    queue.push("task_1")

    assert worker.run_once() == "task_1"
    assert worker.processed == 1
    assert runner.runs == ["task_1"]
    final = store.get("task_1")
    assert final is not None and final.status is TaskStatus.succeeded
    assert queue.is_running("task_1") is False


def test_run_once_returns_none_when_queue_is_empty(settings: Settings) -> None:
    store = InMemoryTaskStore()
    queue = InMemoryTaskQueue()
    worker = _worker(store, queue, settings, lambda task: _StoreRunner(store))
    assert worker.run_once(timeout=0.01) is None
    assert worker.processed == 0


def test_runner_failure_marks_task_failed(settings: Settings) -> None:
    store = InMemoryTaskStore()
    queue = InMemoryTaskQueue()
    runner = _StoreRunner(store, error=RuntimeError("boom"))
    worker = _worker(store, queue, settings, lambda task: runner)
    store.create(_task("task_2"))
    queue.push("task_2")

    assert worker.run_once() == "task_2"
    final = store.get("task_2")
    assert final is not None
    assert final.status is TaskStatus.failed
    assert final.failure_code == "worker_error"


def test_missing_runner_marks_task_failed(settings: Settings) -> None:
    store = InMemoryTaskStore()
    queue = InMemoryTaskQueue()

    def runner_for(task) -> object:
        raise LookupError("repair is not configured")

    worker = _worker(store, queue, settings, runner_for)
    store.create(_task("task_3"))
    queue.push("task_3")

    worker.run_once()
    final = store.get("task_3")
    assert final is not None
    assert final.status is TaskStatus.failed
    assert final.failure_code == "worker_dispatch_failed"


def test_cancel_before_start_skips_the_runner(settings: Settings) -> None:
    store = InMemoryTaskStore()
    queue = InMemoryTaskQueue()
    runner = _StoreRunner(store)
    worker = _worker(store, queue, settings, lambda task: runner)
    store.create(_task("task_4"))
    queue.push("task_4")
    assert queue.request_cancel("task_4") is True

    worker.run_once()
    assert runner.runs == []
    final = store.get("task_4")
    assert final is not None and final.status is TaskStatus.cancelled
    assert final.failure_code == "cancelled"


def test_unknown_task_id_is_dropped_without_crashing(settings: Settings) -> None:
    store = InMemoryTaskStore()
    queue = InMemoryTaskQueue()
    worker = _worker(store, queue, settings, lambda task: _StoreRunner(store))
    queue.push("task_ghost")
    assert worker.run_once() == "task_ghost"
    assert worker.processed == 1


def test_run_forever_drains_the_queue(settings: Settings) -> None:
    store = InMemoryTaskStore()
    queue = InMemoryTaskQueue()
    runner = _StoreRunner(store)
    worker = _worker(store, queue, settings, lambda task: runner)
    for index in range(3):
        store.create(_task(f"task_{index}"))
        queue.push(f"task_{index}")

    processed = worker.run_forever(max_iterations=3)
    assert processed == 3
    assert len(runner.runs) == 3


def test_killed_worker_leaves_running_task_that_restart_converges(
    settings: Settings,
) -> None:
    store = InMemoryTaskStore()
    queue = InMemoryTaskQueue()

    class CrashingRunner:
        def run(self, task_id: str, token) -> None:
            current = store.get(task_id)
            assert current is not None
            current.status = TaskStatus.running
            current.started_at = utcnow()
            store.save(current)
            # The process dies mid-run: ``SystemExit`` escapes the worker's
            # ``except Exception`` handler, exactly like SIGKILL would.
            raise SystemExit(1)

    worker = _worker(store, queue, settings, lambda task: CrashingRunner())
    store.create(_task("task_crash"))
    queue.push("task_crash")

    def run() -> None:
        with contextlib.suppress(SystemExit):
            worker.run_once()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    interrupted = store.get("task_crash")
    assert interrupted is not None and interrupted.status is TaskStatus.running

    # a fresh worker reconciles the interrupted task on startup
    recovered = reconcile_incomplete_tasks(store)
    assert recovered == ["task_crash"]
    final = store.get("task_crash")
    assert final is not None
    assert final.status is TaskStatus.failed
    assert final.failure_code == STALE_FAILURE_CODE
