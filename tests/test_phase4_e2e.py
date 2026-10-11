"""End to end acceptance tests for Phase 4 (queue + worker + store)."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from repopilot.app import create_app
from repopilot.config import Settings
from repopilot.credentials import NullCredentialProvider
from repopilot.github.fake import FakeGitHubClient
from repopilot.llm.stub import StubModelClient
from repopilot.models import Task, TaskRequest, TaskStatus, utcnow
from repopilot.queue.memory import InMemoryTaskQueue
from repopilot.queue.scheduler import QueueTaskScheduler
from repopilot.storage.memory import InMemoryTaskStore
from support import repair_payload, repair_responder, stats_fix_diff, wait_for_terminal


def _pub_settings(settings: Settings, **overrides) -> Settings:
    base = replace(
        settings,
        enable_github_publish=True,
        publish_requires_container=False,
        github_repo_slug="acme/widget",
    )
    return replace(base, **overrides) if overrides else base


def _manual_task(task_id: str) -> Task:
    return Task(
        task_id=task_id,
        run_id=f"run_{task_id}",
        request=TaskRequest(repo_url="/tmp/repo"),
        created_at=utcnow(),
        updated_at=utcnow(),
    )


def test_queue_backed_repair_runs_to_success_and_publishes(settings: Settings, repair_repo) -> None:
    fake = FakeGitHubClient()
    store = InMemoryTaskStore()
    queue = InMemoryTaskQueue()
    app_settings = _pub_settings(
        settings,
        worker_enabled=True,
        worker_poll_timeout_seconds=0.05,
        scheduler_backend="queue",
    )
    app = create_app(
        settings=app_settings,
        store=store,
        task_queue=queue,
        model_client=StubModelClient(responder=repair_responder(patch=stats_fix_diff())),
        github_client=fake,
        credential_provider=NullCredentialProvider(),
    )
    with TestClient(app) as client:
        created = client.post("/api/tasks", json=repair_payload(repair_repo.path))
        assert created.status_code == 202, created.text
        body = created.json()
        assert body["status"] == "pending"
        task_id = body["task_id"]

        final = wait_for_terminal(client, task_id, timeout=180.0)
        assert final["status"] == "succeeded", final
        assert final["pull_request"] is not None
        assert final["pull_request"]["draft"] is True
        assert fake.pull_request_count == 1

        report = client.get(f"/api/tasks/{task_id}/report")
        assert report.status_code == 200
        assert report.json()["status"] == "succeeded"
        assert report.json()["publish"]["status"] == "created"

        usage = client.get(f"/api/tasks/{task_id}/usage").json()
        assert usage["status"] == "succeeded"
        assert usage["rounds"] >= 1
        assert usage["model_calls"] >= 1
        assert usage["duration_ms"] is not None
        assert usage["sandbox"]["backend"] == "local"
        assert usage["publish"]["status"] == "created"

        listed = client.get("/api/tasks").json()
        assert [item["task_id"] for item in listed] == [task_id]
        assert [item["task_id"] for item in client.get("/api/tasks?status=succeeded").json()] == [
            task_id
        ]
        assert client.get("/api/tasks?status=failed").json() == []

        logs = client.get(f"/api/tasks/{task_id}/logs").json()
        assert logs["task_id"] == task_id
        assert logs["run_id"] == final["run_id"]
        assert any(event["event"] == "worker.started" for event in logs["events"])


def test_endpoints_are_deterministic_without_a_worker(settings: Settings) -> None:
    store = InMemoryTaskStore()
    app = create_app(
        settings=settings,
        store=store,
        scheduler=QueueTaskScheduler(InMemoryTaskQueue()),
    )
    with TestClient(app) as client:
        created = client.post("/api/tasks", json={"repo_url": "/tmp/repo"}).json()
        task_id = created["task_id"]
        assert created["status"] == "pending"

        assert [item["task_id"] for item in client.get("/api/tasks").json()] == [task_id]

        usage = client.get(f"/api/tasks/{task_id}/usage").json()
        assert usage["status"] == "pending"
        assert usage["duration_ms"] is None

        cancelled = client.post(f"/api/tasks/{task_id}/cancel")
        assert cancelled.status_code == 202

        # retry is refused while the source task is still active
        assert client.post(f"/api/tasks/{task_id}/retry").status_code == 409

        # once terminal, retry clones the request into a fresh task
        finished = store.get(task_id)
        assert finished is not None
        finished.status = TaskStatus.failed
        store.save(finished)
        retried = client.post(f"/api/tasks/{task_id}/retry")
        assert retried.status_code == 202
        new_id = retried.json()["task_id"]
        assert new_id != task_id
        assert client.get(f"/api/tasks/{new_id}").json()["retried_from"] == task_id


def test_duplicate_submission_is_deduplicated_over_the_queue(settings: Settings) -> None:
    store = InMemoryTaskStore()
    queue = InMemoryTaskQueue()
    app = create_app(settings=settings, store=store, scheduler=QueueTaskScheduler(queue))
    payload = {"repo_url": "/tmp/repo", "base_branch": "main"}
    with TestClient(app) as client:
        first = client.post("/api/tasks", json=payload).json()
        second = client.post("/api/tasks", json=payload).json()
    assert second["task_id"] == first["task_id"]
    assert second["deduplicated"] is True
    assert len(store.list()) == 1
    assert queue.pending_count() == 1


def test_cancel_task_that_was_never_scheduled_returns_conflict(settings: Settings) -> None:
    store = InMemoryTaskStore()
    store.create(_manual_task("task_orphan"))
    app = create_app(
        settings=settings,
        store=store,
        scheduler=QueueTaskScheduler(InMemoryTaskQueue()),
    )
    with TestClient(app) as client:
        response = client.post("/api/tasks/task_orphan/cancel")
    assert response.status_code == 409
    assert response.json()["error"] == "task_conflict"
