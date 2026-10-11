"""Dify adapter: payload normalisation, rendering and trigger endpoints."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from repopilot.app import create_app
from repopilot.config import Settings
from repopilot.integrations.dify import normalise_dify_payload, render_task
from repopilot.models import PullRequestInfo, Task, TaskRequest, TaskStatus, utcnow
from repopilot.queue.memory import InMemoryTaskQueue
from repopilot.queue.scheduler import QueueTaskScheduler


def _client(settings: Settings, *, enable_dify: bool) -> TestClient:
    app = create_app(
        settings=replace(settings, enable_dify=enable_dify),
        scheduler=QueueTaskScheduler(InMemoryTaskQueue()),
    )
    return TestClient(app)


def _task(**overrides) -> Task:
    payload = {
        "task_id": "task_1",
        "run_id": "run_1",
        "status": TaskStatus.succeeded,
        "request": TaskRequest(repo_url="/tmp/repo"),
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    payload.update(overrides)
    return Task(**payload)


# --- normalisation ------------------------------------------------------------


def test_normalise_unwraps_inputs_and_coerces_lists() -> None:
    payload = {
        "inputs": {
            "repo_url": "/tmp/repo",
            "mode": "repair",
            "problem": "fix the failing test",
            "target_tests": "tests/a.py::t1, tests/b.py::t2",
            "allowed_paths": "src/\nlib/",
            "base_branch": "   ",
            "unknown": "dropped",
        }
    }
    body = normalise_dify_payload(payload)
    assert body["target_tests"] == ["tests/a.py::t1", "tests/b.py::t2"]
    assert body["allowed_paths"] == ["src/", "lib/"]
    assert body["base_branch"] is None
    assert "unknown" not in body


def test_normalise_accepts_a_bare_payload() -> None:
    body = normalise_dify_payload({"repo_url": "/tmp/repo", "base_commit": ""})
    assert body["base_commit"] is None
    assert body["repo_url"] == "/tmp/repo"


# --- rendering ----------------------------------------------------------------


def test_render_running_task() -> None:
    rendered = render_task(_task(status=TaskStatus.running))
    assert rendered["terminal"] is False
    assert "running" in rendered["text"]
    assert rendered["result"]["report_available"] is False


def test_render_succeeded_task_includes_pr_link() -> None:
    pr = PullRequestInfo(
        number=7,
        url="https://github.example/acme/widget/pull/7",
        head_branch="repopilot/fix-task_1",
        base_branch="main",
    )
    rendered = render_task(_task(pull_request=pr))
    assert rendered["terminal"] is True
    assert str(pr.url) in rendered["text"]
    assert rendered["result"]["pull_request_url"] == pr.url


def test_render_failed_task_exposes_reason() -> None:
    rendered = render_task(
        _task(status=TaskStatus.failed, failure_code="clone_failed", failure_reason="no network")
    )
    assert "no network" in rendered["text"]
    assert rendered["result"]["failure_code"] == "clone_failed"


# --- endpoints ----------------------------------------------------------------


def test_dify_trigger_query_and_cancel(settings: Settings) -> None:
    with _client(settings, enable_dify=True) as client:
        response = client.post(
            "/api/integrations/dify/tasks",
            json={"inputs": {"repo_url": "/tmp/repo", "mode": "assess"}},
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["deduplicated"] is False
        task_id = body["task_id"]

        fetched = client.get(f"/api/integrations/dify/tasks/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["task_id"] == task_id

        cancelled = client.post(f"/api/integrations/dify/tasks/{task_id}/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["cancel_requested"] is True


def test_dify_rejects_invalid_payload(settings: Settings) -> None:
    with _client(settings, enable_dify=True) as client:
        response = client.post(
            "/api/integrations/dify/tasks",
            json={"inputs": {"repo_url": "   ", "mode": "repair"}},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"


def test_dify_unknown_task_is_not_found(settings: Settings) -> None:
    with _client(settings, enable_dify=True) as client:
        assert client.get("/api/integrations/dify/tasks/missing").status_code == 404


def test_dify_routes_are_absent_when_disabled(settings: Settings) -> None:
    with _client(settings, enable_dify=False) as client:
        assert client.post("/api/integrations/dify/tasks", json={}).status_code == 404
        assert client.get("/api/integrations/dify/tasks/x").status_code == 404
