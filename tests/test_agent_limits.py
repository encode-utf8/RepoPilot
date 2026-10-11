"""Finite loop control: every budget terminates with an explicit reason."""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from repopilot.agent.limits import AgentLimits, LoopMeter
from repopilot.app import create_app
from repopilot.config import Settings
from repopilot.fixtures import FixtureRepo
from repopilot.llm.stub import StubModelClient
from repopilot.models import TerminationReason
from support import (
    complete_task,
    repair_payload,
    repair_responder,
    stats_comment_diff,
    stats_fix_diff,
)


def run_repair(settings: Settings, repo: FixtureRepo, responder) -> tuple[dict, dict]:
    app = create_app(settings=settings, model_client=StubModelClient(responder=responder))
    with TestClient(app) as client:
        final = complete_task(client, repair_payload(repo.path))
        report = client.get(f"/api/tasks/{final['task_id']}/report").json()
    return final, report


def test_meter_reports_each_budget_without_side_effects() -> None:
    limits = AgentLimits(
        max_rounds=1,
        max_wall_seconds=10.0,
        max_model_calls=1,
        max_model_tokens=10,
        max_tool_calls=1,
    )
    meter = LoopMeter(limits=limits)
    assert meter.check() is None
    meter.note_model_call(10)
    assert meter.check() is TerminationReason.limit_model_calls
    meter.model_calls = 0
    meter.model_tokens = 0
    meter.note_tool_call()
    assert meter.check() is TerminationReason.limit_tool_calls
    meter.tool_calls = 0
    meter.note_round()
    assert meter.check() is None
    assert meter.check_round() is TerminationReason.limit_max_rounds


def test_meter_reports_wall_clock_first() -> None:
    limits = AgentLimits(max_wall_seconds=0.0)
    meter = LoopMeter(limits=limits)
    assert meter.check() is TerminationReason.limit_wall_clock


def test_loop_stops_at_max_rounds(settings: Settings, repair_repo: FixtureRepo) -> None:
    limited = replace(settings, agent_max_rounds=2)
    final, report = run_repair(
        limited, repair_repo, repair_responder(patch_factory=stats_comment_diff)
    )

    assert final["status"] == "failed", final
    assert final["termination_reason"] == TerminationReason.limit_max_rounds.value
    assert final["rounds"] == 2
    assert report["repair"]["termination_reason"] == "limit_max_rounds"
    assert report["repair"]["quality_gate"]["passed"] is False
    assert report["verification"]["fully_verified"] is False
    assert "limit_max_rounds" in (final["failure_reason"] or "")


def test_loop_stops_at_model_call_budget(settings: Settings, repair_repo: FixtureRepo) -> None:
    limited = replace(settings, agent_max_model_calls=3, agent_max_rounds=6)
    final, report = run_repair(
        limited, repair_repo, repair_responder(patch_factory=stats_comment_diff)
    )

    assert final["status"] == "failed", final
    assert final["termination_reason"] == TerminationReason.limit_model_calls.value
    assert report["repair"]["usage"]["calls"] == 3
    assert report["repair"]["result"] == "not_fixed"


def test_loop_stops_on_wall_clock_budget_without_calling_the_model(
    settings: Settings, repair_repo: FixtureRepo
) -> None:
    limited = replace(settings, agent_max_wall_seconds=0.0)
    final, report = run_repair(limited, repair_repo, repair_responder(patch=stats_fix_diff()))

    assert final["status"] == "failed", final
    assert final["termination_reason"] == TerminationReason.limit_wall_clock.value
    assert report["repair"]["usage"]["calls"] == 0


def test_repeating_the_same_patch_stops_as_no_progress(
    settings: Settings, repair_repo: FixtureRepo
) -> None:
    limited = replace(settings, agent_max_rounds=6)
    final, report = run_repair(
        limited,
        repair_repo,
        repair_responder(patch_factory=lambda attempt: stats_comment_diff(0)),
    )

    assert final["status"] == "failed", final
    assert final["termination_reason"] == TerminationReason.no_progress.value
    assert report["repair"]["quality_gate"]["passed"] is False


def test_limit_termination_never_reports_success(
    settings: Settings, repair_repo: FixtureRepo
) -> None:
    limited = replace(settings, agent_max_rounds=1)
    final, report = run_repair(
        limited, repair_repo, repair_responder(patch_factory=stats_comment_diff)
    )
    assert final["status"] != "succeeded"
    assert report["status"] != "succeeded"
    assert report["repair"]["result"] in {"not_fixed", "needs_review", "failed"}
