"""Task API endpoint behaviour."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from repopilot.app import create_app
from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.models import Task, TaskRequest, TaskStatus, utcnow
from repopilot.storage.memory import InMemoryTaskStore


class NullScheduler:
    """Scheduler stub that records submissions but never executes them."""

    def __init__(self) -> None:
        self.tokens: dict[str, CancellationToken] = {}

    def submit(self, task_id: str, function) -> None:
        self.tokens[task_id] = CancellationToken()

    def cancel(self, task_id: str) -> bool:
        token = self.tokens.get(task_id)
        if token is None:
            return False
        token.cancel("test cancel")
        return True

    def token_for(self, task_id: str) -> CancellationToken | None:
        return self.tokens.get(task_id)

    def is_running(self, task_id: str) -> bool:
        return False

    def shutdown(self, wait: bool = False) -> None:
        return None


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings=settings, scheduler=NullScheduler())
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_task_returns_immediately(client: TestClient) -> None:
    response = client.post("/api/tasks", json={"repo_url": "https://example.com/org/repo.git"})
    assert response.status_code == 202
    body = response.json()
    assert body["task_id"].startswith("task_")
    assert body["run_id"].startswith("run_")
    assert body["status"] == "pending"
    assert body["deduplicated"] is False

    fetched = client.get(f"/api/tasks/{body['task_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "pending"
    assert fetched.json()["report_available"] is False


def test_blank_repo_url_is_rejected(client: TestClient) -> None:
    assert client.post("/api/tasks", json={"repo_url": "  "}).status_code == 422
    assert client.post("/api/tasks", json={}).status_code == 422


def test_unknown_task_returns_404(client: TestClient) -> None:
    response = client.get("/api/tasks/task_missing")
    assert response.status_code == 404
    assert response.json()["error"] == "task_not_found"


def test_report_is_conflict_until_terminal(client: TestClient) -> None:
    task_id = client.post(
        "/api/tasks", json={"repo_url": "https://example.com/org/repo.git"}
    ).json()["task_id"]
    response = client.get(f"/api/tasks/{task_id}/report")
    assert response.status_code == 409
    assert response.json()["error"] == "report_not_ready"


def test_logs_endpoint_returns_events(client: TestClient) -> None:
    task_id = client.post(
        "/api/tasks", json={"repo_url": "https://example.com/org/repo.git"}
    ).json()["task_id"]
    response = client.get(f"/api/tasks/{task_id}/logs")
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == task_id
    assert body["count"] == len(body["events"])


def test_duplicate_submission_is_deduplicated(client: TestClient) -> None:
    payload = {"repo_url": "https://example.com/org/repo.git", "base_branch": "main"}
    first = client.post("/api/tasks", json=payload).json()
    second = client.post("/api/tasks", json=payload).json()
    assert second["task_id"] == first["task_id"]
    assert second["deduplicated"] is True


def test_cancel_unknown_task_returns_404(client: TestClient) -> None:
    assert client.post("/api/tasks/task_missing/cancel").status_code == 404


def test_cancel_terminal_task_returns_conflict(settings: Settings) -> None:
    store = InMemoryTaskStore()
    store.create(
        Task(
            task_id="task_done",
            run_id="run_done",
            status=TaskStatus.succeeded,
            request=TaskRequest(repo_url="https://example.com/org/repo.git"),
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    app = create_app(settings=settings, store=store, scheduler=NullScheduler())
    with TestClient(app) as client:
        response = client.post("/api/tasks/task_done/cancel")
    assert response.status_code == 409
    assert response.json()["error"] == "task_conflict"


def test_cancel_pending_task_requests_cancellation(settings: Settings) -> None:
    scheduler = NullScheduler()
    app = create_app(settings=settings, scheduler=scheduler)
    with TestClient(app) as client:
        task_id = client.post(
            "/api/tasks", json={"repo_url": "https://example.com/org/repo.git"}
        ).json()["task_id"]
        response = client.post(f"/api/tasks/{task_id}/cancel")
    assert response.status_code == 202
    assert response.json()["cancel_requested"] is True
    token = scheduler.token_for(task_id)
    assert token is not None and token.cancelled is True
