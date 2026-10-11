"""Task creation service: dedupe, enqueue and retry."""

from __future__ import annotations

import threading

import pytest

from repopilot.cancellation import CancellationToken
from repopilot.errors import TaskConflictError, TaskNotFoundError
from repopilot.locking import InMemoryLockManager
from repopilot.models import TaskRequest, TaskStatus
from repopilot.service import TaskService, dedupe_lock_key
from repopilot.storage.memory import InMemoryTaskStore


class RecordingScheduler:
    """Scheduler double that records enqueued ids without running anything."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, task_id: str, function) -> None:
        self.submitted.append(task_id)

    def cancel(self, task_id: str) -> bool:
        return True

    def token_for(self, task_id: str) -> CancellationToken:
        return CancellationToken()

    def is_running(self, task_id: str) -> bool:
        return False

    def shutdown(self, wait: bool = False) -> None:
        return None


class _Runner:
    def run(self, task_id: str, token: CancellationToken) -> None:
        return None


def _service(store=None, scheduler=None) -> TaskService:
    return TaskService(
        store=store or InMemoryTaskStore(),
        scheduler=scheduler or RecordingScheduler(),
        lock_manager=InMemoryLockManager(),
        runner_for=lambda mode: _Runner(),
    )


def _request(**overrides) -> TaskRequest:
    payload = {"repo_url": "https://github.com/acme/widget", "base_branch": "main"}
    payload.update(overrides)
    return TaskRequest(**payload)


def test_submit_creates_and_enqueues() -> None:
    scheduler = RecordingScheduler()
    service = _service(scheduler=scheduler)
    task, deduplicated = service.submit(_request())
    assert deduplicated is False
    assert scheduler.submitted == [task.task_id]
    assert task.status is TaskStatus.pending


def test_duplicate_submit_returns_same_task() -> None:
    store = InMemoryTaskStore()
    scheduler = RecordingScheduler()
    service = _service(store=store, scheduler=scheduler)
    first, first_dedup = service.submit(_request())
    second, second_dedup = service.submit(_request())
    assert first_dedup is False
    assert second_dedup is True
    assert second.task_id == first.task_id
    assert scheduler.submitted == [first.task_id]
    assert len(store.list()) == 1


def test_distinct_requests_are_not_deduplicated() -> None:
    service = _service()
    first, _ = service.submit(_request())
    second, _ = service.submit(_request(base_branch="develop"))
    assert first.task_id != second.task_id


def test_concurrent_submit_creates_exactly_one_task() -> None:
    store = InMemoryTaskStore()
    scheduler = RecordingScheduler()
    service = _service(store=store, scheduler=scheduler)
    barrier = threading.Barrier(8)
    results: list[tuple[str, bool]] = []
    guard = threading.Lock()

    def submit() -> None:
        barrier.wait()
        task, deduplicated = service.submit(_request())
        with guard:
            results.append((task.task_id, deduplicated))

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert len({task_id for task_id, _ in results}) == 1
    assert sum(1 for _, deduplicated in results if not deduplicated) == 1
    assert len(store.list()) == 1
    assert len(scheduler.submitted) == 1


def test_retry_requires_terminal_task() -> None:
    store = InMemoryTaskStore()
    service = _service(store=store)
    task, _ = service.submit(_request())
    with pytest.raises(TaskConflictError):
        service.retry(task.task_id)


def test_retry_clones_a_finished_task() -> None:
    store = InMemoryTaskStore()
    service = _service(store=store)
    original, _ = service.submit(_request())
    finished = store.get(original.task_id)
    assert finished is not None
    finished.status = TaskStatus.failed
    finished.failure_code = "boom"
    store.save(finished)

    retried, deduplicated = service.retry(original.task_id)
    assert deduplicated is False
    assert retried.task_id != original.task_id
    assert retried.retried_from == original.task_id
    assert retried.status is TaskStatus.pending


def test_retry_unknown_task_raises() -> None:
    service = _service()
    with pytest.raises(TaskNotFoundError):
        service.retry("task_missing")


def test_lost_dedupe_lock_conflicts_instead_of_duplicating() -> None:
    store = InMemoryTaskStore()
    lock_manager = InMemoryLockManager()
    service = TaskService(
        store=store,
        scheduler=RecordingScheduler(),
        lock_manager=lock_manager,
        runner_for=lambda mode: _Runner(),
        dedupe_wait_seconds=0.05,
        dedupe_poll_seconds=0.005,
    )
    request = _request()
    lock_manager.acquire(dedupe_lock_key(request.dedupe_key()), owner="other", ttl_seconds=30)
    with pytest.raises(TaskConflictError):
        service.submit(request)
    assert store.list() == []
