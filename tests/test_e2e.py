"""End to end acceptance tests over the HTTP API."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repopilot.app import create_app
from repopilot.config import Settings
from repopilot.fixtures import FixtureRepo, materialise_fixture
from repopilot.models import CheckerResult, CheckerStatus
from support import wait_for_terminal

REQUIRED_ISSUE_FIELDS = (
    "issue_type",
    "severity",
    "file_path",
    "line",
    "evidence",
    "source_tool",
    "fix_suggestion",
    "origin",
    "verification",
)


class SlowChecker:
    """Checker that blocks until the run is cancelled (used to test cancellation)."""

    name = "slow"
    display_name = "Slow blocking checker"

    def is_applicable(self, context) -> bool:
        return True

    def run(self, context) -> CheckerResult:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not context.token.is_cancelled():
            time.sleep(0.02)
        return CheckerResult(
            name=self.name, display_name=self.display_name, status=CheckerStatus.not_applicable
        )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings=settings)
    with TestClient(app) as test_client:
        yield test_client


def _start(client: TestClient, payload: dict) -> str:
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def test_assessment_end_to_end_succeeds(client: TestClient, buggy_repo: FixtureRepo) -> None:
    task_id = _start(client, {"repo_url": str(buggy_repo.path)})
    final = wait_for_terminal(client, task_id)

    assert final["status"] == "succeeded", final
    assert final["resolved_commit"] == buggy_repo.head_commit
    assert final["failure_reason"] is None
    assert final["report_available"] is True

    response = client.get(f"/api/tasks/{task_id}/report")
    assert response.status_code == 200
    report = response.json()

    assert report["schema_version"]
    assert report["status"] == "succeeded"
    assert report["repo"]["resolved_commit"] == buggy_repo.head_commit
    assert report["project"]["is_python_project"] is True
    assert report["project"]["python_requires"] == ">=3.11"

    confirmed = report["confirmed_issues"]
    assert len(confirmed) >= 5
    rules = {issue["rule_id"] for issue in confirmed}
    assert {"F401", "F821", "F841"} <= rules
    assert any(issue["issue_type"] == "test_failure" for issue in confirmed)
    for issue in confirmed:
        for field in REQUIRED_ISSUE_FIELDS:
            assert issue[field] is not None, (field, issue)
        assert issue["origin"] in {"tool", "model"}
        assert issue["confirmed"] is True
        assert issue["verification"] == "confirmed"

    summary = report["summary"]
    assert summary["confirmed_issues"] == len(confirmed)
    assert summary["by_severity"]
    assert summary["by_tool"]["ruff"] >= 1
    assert summary["by_tool"]["pytest"] == 1
    assert set(summary["checkers_ran"]) == {"ruff", "pytest", "heuristics"}

    unverified = report["unverified_issues"]
    assert unverified, "the heuristic checker must report unverified potential problems"
    assert all(issue["confirmed"] is False for issue in unverified)
    assert all(issue["verification"] == "unverified" for issue in unverified)
    assert {issue["source_tool"] for issue in unverified} == {"heuristics"}

    assert report["verification"]["fully_verified"] is True
    assert report["verification"]["verification_gaps"] == []

    logs = client.get(f"/api/tasks/{task_id}/logs").json()
    assert logs["task_id"] == task_id
    assert logs["count"] >= 4
    assert all(
        event["task_id"] == task_id and event["run_id"] == final["run_id"]
        for event in logs["events"]
    )
    assert any(event["event"] == "task.finished" for event in logs["events"])


def test_pinned_base_commit_is_used(client: TestClient, buggy_repo: FixtureRepo) -> None:
    task_id = _start(
        client,
        {"repo_url": str(buggy_repo.path), "base_commit": buggy_repo.baseline_commit},
    )
    final = wait_for_terminal(client, task_id)

    assert final["status"] == "succeeded"
    assert final["resolved_commit"] == buggy_repo.baseline_commit
    report = client.get(f"/api/tasks/{task_id}/report").json()
    assert report["repo"]["resolved_commit"] == buggy_repo.baseline_commit
    assert report["repo"]["requested_base_commit"] == buggy_repo.baseline_commit


def test_base_branch_head_is_used(client: TestClient, buggy_repo: FixtureRepo) -> None:
    task_id = _start(client, {"repo_url": str(buggy_repo.path), "base_branch": "main"})
    final = wait_for_terminal(client, task_id)
    assert final["resolved_commit"] == buggy_repo.head_commit


def test_unreachable_remote_fails_gracefully(client: TestClient) -> None:
    started = time.monotonic()
    task_id = _start(client, {"repo_url": "http://127.0.0.1:1/absent.git", "base_branch": "main"})
    final = wait_for_terminal(client, task_id, timeout=60)
    elapsed = time.monotonic() - started

    assert elapsed < 60
    assert final["status"] == "failed"
    assert final["failure_code"] == "clone_failed"
    assert final["failure_reason"] and "clone_failed" in final["failure_reason"]

    report = client.get(f"/api/tasks/{task_id}/report").json()
    assert report["status"] == "failed"
    assert report["failure_code"] == "clone_failed"
    assert report["confirmed_issues"] == []
    assert report["verification"]["fully_verified"] is False


def test_untrusted_source_is_not_executed_and_needs_review(
    settings: Settings, tmp_path: Path
) -> None:
    untrusted = materialise_fixture("py_calc_buggy", tmp_path / "untrusted")
    app = create_app(settings=settings)
    with TestClient(app) as client:
        task_id = _start(client, {"repo_url": str(untrusted.path)})
        final = wait_for_terminal(client, task_id)

        assert final["status"] == "needs_review", final
        assert final["is_trusted_source"] is False
        assert final["code_execution_enabled"] is False

        report = client.get(f"/api/tasks/{task_id}/report").json()
        assert report["verification"]["fully_verified"] is False
        assert "pytest" in report["summary"]["checkers_unavailable"]
        assert any("pytest" in gap for gap in report["verification"]["verification_gaps"])
        rules = {issue["rule_id"] for issue in report["confirmed_issues"]}
        assert "F401" in rules
        assert report["summary"]["by_tool"].get("pytest") is None


def test_cancel_running_assessment_marks_cancelled(
    settings: Settings, buggy_repo: FixtureRepo
) -> None:
    app = create_app(settings=settings, checkers=[SlowChecker()])
    with TestClient(app) as client:
        task_id = _start(client, {"repo_url": str(buggy_repo.path)})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if client.get(f"/api/tasks/{task_id}").json()["status"] == "running":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("task never entered the running state")

        response = client.post(f"/api/tasks/{task_id}/cancel")
        assert response.status_code == 202

        final = wait_for_terminal(client, task_id)
        assert final["status"] == "cancelled"
        assert final["failure_code"] == "cancelled"
