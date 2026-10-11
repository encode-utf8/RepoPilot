"""End to end repair acceptance tests over the HTTP API (deterministic stub model)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from repopilot.app import create_app
from repopilot.config import Settings
from repopilot.errors import ModelUnavailableError
from repopilot.fixtures import FixtureRepo
from repopilot.llm.stub import StubModelClient
from repopilot.storage.memory import InMemoryTaskStore
from support import (
    STATS_PATH,
    STATS_TARGET_TEST,
    complete_task,
    delete_test_file_diff,
    repair_payload,
    repair_responder,
    start_task,
    stats_comment_diff,
    stats_fix_diff,
    wait_for_terminal,
    weaken_assertion_diff,
)


def make_client(settings: Settings, responder=None) -> TestClient:
    app = create_app(settings=settings, model_client=StubModelClient(responder=responder))
    return TestClient(app)


def test_repair_end_to_end_with_stub_model(settings: Settings, repair_repo: FixtureRepo) -> None:
    """fixture repo + explicit problem -> locate, patch, test, gate, record."""

    with make_client(settings, repair_responder(patch=stats_fix_diff())) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] == "succeeded", final
        assert final["failure_reason"] is None
        assert final["rounds"] == 1
        assert final["model_usage"]["calls"] >= 3
        assert final["model_usage"]["total_tokens"] > 0

        report = client.get(f"/api/tasks/{final['task_id']}/report").json()
        repair = report["repair"]
        assert repair["result"] == "fixed"
        assert repair["termination_reason"] == "completed"
        assert repair["quality_gate"]["passed"] is True
        assert repair["quality_gate"]["primary_blocker"] is None
        assert repair["target_tests"] == [STATS_TARGET_TEST]

        assert repair["patch"]["applied"] is True
        assert repair["patch"]["changed_files"] == [STATS_PATH]
        assert repair["patch"]["diff_hash"]

        assert repair["baseline_tests"]["failed"] == 1
        assert repair["final_tests"]["failed"] == 0
        assert repair["final_tests"]["passed"] == 4

        assert repair["located_files"] == [STATS_PATH]
        assert repair["usage"]["calls"] == sum(repair["usage"]["per_phase"].values())
        assert repair["usage"]["per_phase"] == {"understand": 1, "locate": 1, "patch": 1}

        # complete execution record: per round model calls and tool calls
        assert [rnd["index"] for rnd in repair["rounds"]] == [1]
        tools_used = {call["tool"] for call in repair["rounds"][0]["tool_calls"]}
        assert {"apply_patch", "run_tests", "view_diff"} <= tools_used
        assert all(call["ok"] for call in repair["rounds"][0]["tool_calls"])

        assert report["verification"]["fully_verified"] is True
        assert report["verification"]["verification_gaps"] == []

        # the run state is persisted on disk for later inspection / resume
        state_file = Path(final["workspace_path"]) / "artifacts" / "repair-state.json"
        assert state_file.is_file()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["termination_reason"] == "completed"
        assert state["rounds"][0]["tool_calls"]

        logs = client.get(f"/api/tasks/{final['task_id']}/logs").json()
        assert logs["count"] >= 8
        assert all(
            event["task_id"] == final["task_id"] and event["run_id"] == final["run_id"]
            for event in logs["events"]
        )
        events = {event["event"] for event in logs["events"]}
        assert {
            "agent.started",
            "agent.model_call",
            "agent.tool_call",
            "agent.gate_evaluated",
            "task.finished",
        } <= events


def test_deleting_the_failing_test_is_rejected(
    settings: Settings, repair_repo: FixtureRepo
) -> None:
    with make_client(settings, repair_responder(patch=delete_test_file_diff())) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] in {"failed", "needs_review"}
        report = client.get(f"/api/tasks/{final['task_id']}/report").json()
        repair = report["repair"]
        assert repair["quality_gate"]["passed"] is False
        assert any("deleted" in finding for finding in repair["quality_gate"]["cheating_findings"])
        assert repair["result"] in {"failed", "not_fixed", "needs_review"}
        assert report["verification"]["fully_verified"] is False


def test_weakening_the_assertion_is_rejected(settings: Settings, repair_repo: FixtureRepo) -> None:
    with make_client(settings, repair_responder(patch=weaken_assertion_diff())) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] == "failed", final
        assert final["failure_code"] == "cheating_detected"
        report = client.get(f"/api/tasks/{final['task_id']}/report").json()
        findings = report["repair"]["quality_gate"]["cheating_findings"]
        assert any("trivially true" in finding for finding in findings)


def test_unavailable_model_fails_without_faking_success(
    settings: Settings, repair_repo: FixtureRepo
) -> None:
    client_stub = StubModelClient([ModelUnavailableError("vllm is down")])
    app = create_app(settings=settings, model_client=client_stub)
    with TestClient(app) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] == "failed", final
        assert final["failure_code"] == "model_unavailable"
        assert "model_unavailable" in (final["failure_reason"] or "")
        report = client.get(f"/api/tasks/{final['task_id']}/report").json()
        assert report["status"] == "failed"
        assert report["repair"]["quality_gate"]["passed"] is False
        assert report["verification"]["fully_verified"] is False


def test_unparseable_model_output_fails(settings: Settings, repair_repo: FixtureRepo) -> None:
    garbage = StubModelClient(responder=lambda request, index: "not json at all")
    app = create_app(settings=settings, model_client=garbage)
    with TestClient(app) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] == "failed", final
        assert final["failure_code"] == "model_output_invalid"
        assert final["model_usage"]["failed_calls"] == 1


def test_repair_requires_a_problem_statement(settings: Settings) -> None:
    with make_client(settings, repair_responder(patch=stats_fix_diff())) as client:
        response = client.post(
            "/api/tasks",
            json={"repo_url": "https://example.com/org/repo.git", "mode": "repair"},
        )
        assert response.status_code == 422


def test_untrusted_source_is_not_executed_for_repair(
    settings: Settings, repair_repo: FixtureRepo
) -> None:
    untrusted = replace(settings, trusted_source_roots=())
    with make_client(untrusted, repair_responder(patch=stats_fix_diff())) as client:
        final = complete_task(client, repair_payload(repair_repo.path))

        assert final["status"] == "failed", final
        assert final["failure_code"] == "code_execution_denied"
        report = client.get(f"/api/tasks/{final['task_id']}/report").json()
        assert report["repair"]["quality_gate"]["passed"] is False
        assert report["repair"]["termination_reason"] != "completed"


def test_repair_resumes_from_saved_state(settings: Settings, repair_repo: FixtureRepo) -> None:
    """A failed run keeps its state, and a follow up run continues the round count."""

    store = InMemoryTaskStore()
    limited = replace(settings, agent_max_rounds=1)
    first_app = create_app(
        settings=limited,
        store=store,
        model_client=StubModelClient(responder=repair_responder(patch_factory=stats_comment_diff)),
    )
    with TestClient(first_app) as client:
        first = complete_task(client, repair_payload(repair_repo.path))
        assert first["status"] == "failed", first
        assert first["rounds"] == 1

    second_app = create_app(
        settings=replace(settings, agent_max_rounds=2),
        store=store,
        model_client=StubModelClient(responder=repair_responder(patch_factory=stats_comment_diff)),
    )
    with TestClient(second_app) as client:
        resumed = complete_task(
            client, repair_payload(repair_repo.path, resume_task_id=first["task_id"])
        )
        assert resumed["resumed_from"] == first["task_id"]
        assert resumed["rounds"] == 2

        report = client.get(f"/api/tasks/{resumed['task_id']}/report").json()
        assert [rnd["index"] for rnd in report["repair"]["rounds"]] == [1, 2]
        logs = client.get(f"/api/tasks/{resumed['task_id']}/logs").json()
        started = [event for event in logs["events"] if event["event"] == "agent.started"]
        assert started and started[0]["data"]["resumed_from"] == first["task_id"]


def test_cancel_endpoint_remains_available_for_repair(
    settings: Settings, repair_repo: FixtureRepo
) -> None:
    with make_client(settings, repair_responder(patch=stats_fix_diff())) as client:
        task_id = start_task(client, repair_payload(repair_repo.path))
        final = wait_for_terminal(client, task_id)
        assert final["status"] == "succeeded"
        # cancelling a finished task stays a conflict, exactly like phase 1
        assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 409
