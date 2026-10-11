"""End to end acceptance tests for Phase 3 (Draft PR publishing, fake GitHub)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from repopilot.app import create_app
from repopilot.config import Settings
from repopilot.credentials import NullCredentialProvider
from repopilot.errors import GitHubAuthError, GitHubConflictError, GitHubUnavailableError
from repopilot.execution.process import build_sandbox_env
from repopilot.github.fake import FakeGitHubClient
from repopilot.llm.stub import StubModelClient
from repopilot.pipeline.publish import build_branch_name
from support import (
    STATS_PATH,
    complete_task,
    delete_test_file_diff,
    repair_payload,
    repair_responder,
    stats_fix_diff,
)

TOKEN = "ghp_" + "Z" * 36


def _pub_settings(settings: Settings, **overrides) -> Settings:
    base = replace(
        settings,
        enable_github_publish=True,
        publish_requires_container=False,
        github_repo_slug="acme/widget",
    )
    return replace(base, **overrides) if overrides else base


def _app(settings: Settings, *, fake: FakeGitHubClient, responder):
    return create_app(
        settings=settings,
        model_client=StubModelClient(responder=responder),
        github_client=fake,
        credential_provider=NullCredentialProvider(),
    )


# --- happy path ---------------------------------------------------------------


def test_end_to_end_creates_draft_pr(settings: Settings, repair_repo) -> None:
    fake = FakeGitHubClient()
    captured: dict[str, object] = {}
    original = fake.create_pull_request

    def spy(*args, **kwargs):
        captured["body"] = kwargs["body"]
        captured["draft"] = kwargs["draft"]
        return original(*args, **kwargs)

    fake.create_pull_request = spy  # type: ignore[assignment]

    pub = _pub_settings(settings)
    with TestClient(
        _app(pub, fake=fake, responder=repair_responder(patch=stats_fix_diff()))
    ) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] == "succeeded", final
        assert final["failure_reason"] is None

        # a deterministic, per task branch exists and a single draft PR was opened
        expected_branch = build_branch_name(pub.github_branch_prefix, final["task_id"])
        assert fake.pull_request_count == 1
        pr = fake.pull_requests[0]
        assert pr.draft is True
        assert pr.head_branch == expected_branch
        assert pr.base_branch == "main"

        # the task record carries the PR link and the publish outcome
        assert final["pull_request"] is not None
        assert final["pull_request"]["number"] == pr.number
        assert final["pull_request"]["url"].endswith(f"/pull/{pr.number}")
        assert final["pull_request"]["draft"] is True

        assert captured["draft"] is True
        body = captured["body"]
        assert STATS_PATH in body
        assert "test_mean_of_values" in body

        report = client.get(f"/api/tasks/{final['task_id']}/report").json()
        assert report["publish"]["status"] == "created"
        assert report["publish"]["branch"] == expected_branch
        assert report["publish"]["pull_request"]["number"] == pr.number


def test_repeat_publish_is_idempotent(settings: Settings, repair_repo) -> None:
    fake = FakeGitHubClient()
    pub = _pub_settings(settings)
    with TestClient(
        _app(pub, fake=fake, responder=repair_responder(patch=stats_fix_diff()))
    ) as client:
        final = complete_task(client, repair_payload(repair_repo.path))
        assert final["status"] == "succeeded"
        assert fake.pull_request_count == 1

        fake.reset()
        response = client.post(f"/api/tasks/{final['task_id']}/pull-request")
        assert response.status_code == 200, response.text
        outcome = response.json()
        assert outcome["status"] == "reused"
        assert outcome["pull_request"]["number"] == fake.pull_requests[0].number

        # a repeat trigger never creates a second branch or PR
        assert fake.writes == []
        assert fake.pull_request_count == 1


# --- gate-first: zero remote writes -------------------------------------------


def test_cheating_patch_produces_zero_remote_writes(settings: Settings, repair_repo) -> None:
    fake = FakeGitHubClient()
    pub = _pub_settings(settings)
    responder = repair_responder(patch=delete_test_file_diff())
    with TestClient(_app(pub, fake=fake, responder=responder)) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] in {"failed", "needs_review"}, final
        assert fake.calls == []
        assert fake.writes == []
        assert fake.pull_request_count == 0

        report = client.get(f"/api/tasks/{final['task_id']}/report").json()
        assert report["repair"]["quality_gate"]["passed"] is False
        publish = report["publish"]
        assert publish["status"] == "skipped"
        assert publish["attempted"] is False


# --- error handling -----------------------------------------------------------


def test_permission_denied_parks_for_review(settings: Settings, repair_repo) -> None:
    fake = FakeGitHubClient(fail_ops={"get_repo": GitHubAuthError("token lacks scope")})
    pub = _pub_settings(settings)
    with TestClient(
        _app(pub, fake=fake, responder=repair_responder(patch=stats_fix_diff()))
    ) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] == "needs_review", final
        assert final["failure_code"] == "github_permission_denied"
        assert fake.writes == []
        assert fake.pull_request_count == 0

        report = client.get(f"/api/tasks/{final['task_id']}/report").json()
        assert report["publish"]["error_code"] == "github_permission_denied"


def test_remote_conflict_parks_for_review(settings: Settings, repair_repo) -> None:
    fake = FakeGitHubClient(fail_ops={"create_ref": GitHubConflictError("branch exists")})
    pub = _pub_settings(settings)
    with TestClient(
        _app(pub, fake=fake, responder=repair_responder(patch=stats_fix_diff()))
    ) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] == "needs_review", final
        assert final["failure_code"] == "github_conflict"
        assert fake.pull_request_count == 0


def test_api_failure_rolls_back_the_branch(settings: Settings, repair_repo) -> None:
    fake = FakeGitHubClient(fail_ops={"create_pull_request": GitHubUnavailableError("github 503")})
    pub = _pub_settings(settings)
    with TestClient(
        _app(pub, fake=fake, responder=repair_responder(patch=stats_fix_diff()))
    ) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] == "needs_review", final
        assert final["failure_code"] == "github_unavailable"
        assert fake.pull_request_count == 0
        # no half finished branch is left behind
        branch = build_branch_name(pub.github_branch_prefix, final["task_id"])
        assert branch not in fake.refs


def test_publish_unknown_task_is_not_found(settings: Settings, repair_repo) -> None:
    fake = FakeGitHubClient()
    pub = _pub_settings(settings)
    with TestClient(
        _app(pub, fake=fake, responder=repair_responder(patch=stats_fix_diff()))
    ) as client:
        # unknown task ids are rejected before any publishing is attempted
        response = client.post("/api/tasks/does-not-exist/pull-request")
        assert response.status_code == 404


# --- credential hygiene -------------------------------------------------------


def test_credentials_never_reach_logs_report_or_sandbox(
    settings: Settings, repair_repo, monkeypatch
) -> None:
    monkeypatch.setenv("REPOPILOT_GITHUB_TOKEN", TOKEN)
    fake = FakeGitHubClient()
    pub = _pub_settings(settings)
    with TestClient(
        _app(pub, fake=fake, responder=repair_responder(patch=stats_fix_diff()))
    ) as client:
        final = complete_task(client, repair_payload(repair_repo.path))
        assert final["status"] == "succeeded"

        logs = client.get(f"/api/tasks/{final['task_id']}/logs").json()
        report = client.get(f"/api/tasks/{final['task_id']}/report").json()
        assert TOKEN not in json.dumps(logs)
        assert TOKEN not in json.dumps(report)

        state_file = Path(final["workspace_path"]) / "artifacts" / "repair-state.json"
        assert state_file.is_file()
        assert TOKEN not in state_file.read_text(encoding="utf-8")

        # the sandbox environment is built from an allowlist and drops credentials
        env = build_sandbox_env(pub)
        assert TOKEN not in env.values()
        assert "REPOPILOT_GITHUB_TOKEN" not in env
