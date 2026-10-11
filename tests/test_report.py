"""Unified report assembly: confirmed versus unverified findings."""

from __future__ import annotations

import json

from repopilot.models import (
    CheckerResult,
    CheckerStatus,
    Issue,
    Origin,
    ProjectProfile,
    RepoMetadata,
    Severity,
    TaskStatus,
    VerificationState,
    sort_issues,
)
from repopilot.report.builder import build_report


def _issue(
    issue_id: str,
    *,
    tool: str,
    severity: Severity,
    verification: VerificationState,
    issue_type: str = "lint",
) -> Issue:
    return Issue(
        issue_id=issue_id,
        issue_type=issue_type,
        severity=severity,
        evidence=f"evidence for {issue_id}",
        source_tool=tool,
        origin=Origin.tool,
        verification=verification,
        file_path="src/a.py",
        line=3,
        rule_id="F401",
        fix_suggestion="fix it",
    )


def _checker(name: str, status: CheckerStatus, issues: list[Issue], *, required: bool = True):
    return CheckerResult(
        name=name,
        display_name=name,
        status=status,
        required=required,
        issues=issues,
    )


def test_report_separates_confirmed_and_unverified() -> None:
    checkers = [
        _checker(
            "ruff",
            CheckerStatus.findings,
            [
                _issue(
                    "i1",
                    tool="ruff",
                    severity=Severity.low,
                    verification=VerificationState.confirmed,
                ),
                _issue(
                    "i2",
                    tool="ruff",
                    severity=Severity.high,
                    verification=VerificationState.confirmed,
                ),
            ],
        ),
        _checker(
            "heuristics",
            CheckerStatus.findings,
            [
                _issue(
                    "i3",
                    tool="heuristics",
                    severity=Severity.medium,
                    verification=VerificationState.unverified,
                    issue_type="dependency_pinning",
                )
            ],
            required=False,
        ),
        _checker("pytest", CheckerStatus.unavailable, []),
    ]

    report = build_report(
        task_id="task_x",
        run_id="run_x",
        status=TaskStatus.needs_review,
        repo=RepoMetadata(source="repo", code_execution_enabled=False),
        project=ProjectProfile(is_python_project=True, primary_language="python"),
        checkers=checkers,
    )

    assert report.summary.confirmed_issues == 2
    assert report.summary.unverified_issues == 1
    assert report.summary.total_issues == 3
    assert report.summary.by_severity == {"high": 1, "low": 1}
    assert report.summary.by_tool == {"ruff": 2}
    assert report.summary.unverified_by_severity == {"medium": 1}
    assert report.summary.unverified_by_tool == {"heuristics": 1}
    assert report.summary.checkers_unavailable == ["pytest"]
    assert report.summary.checkers_ran == ["ruff", "heuristics"]
    assert report.verification.verification_gaps == ["pytest: not executed"]
    assert report.verification.fully_verified is False
    assert [issue.issue_id for issue in report.confirmed_issues] == ["i2", "i1"]
    assert [issue.issue_id for issue in report.unverified_issues] == ["i3"]
    assert all(issue.confirmed is True for issue in report.confirmed_issues)
    assert all(issue.confirmed is False for issue in report.unverified_issues)


def test_report_is_json_serialisable_with_required_fields() -> None:
    report = build_report(
        task_id="task_y",
        run_id="run_y",
        status=TaskStatus.succeeded,
        repo=RepoMetadata(source="repo", resolved_commit="abc", code_execution_enabled=True),
        project=ProjectProfile(is_python_project=True),
        checkers=[
            _checker(
                "ruff",
                CheckerStatus.findings,
                [
                    _issue(
                        "i1",
                        tool="ruff",
                        severity=Severity.low,
                        verification=VerificationState.confirmed,
                    )
                ],
            )
        ],
    )
    payload = json.loads(report.model_dump_json())
    assert report.verification.fully_verified is True
    for field in (
        "issue_type",
        "severity",
        "file_path",
        "line",
        "evidence",
        "source_tool",
        "fix_suggestion",
        "origin",
        "verification",
        "confirmed",
    ):
        assert field in payload["confirmed_issues"][0]
    assert payload["confirmed_issues"][0]["origin"] == "tool"


def test_sort_issues_orders_by_severity_then_location() -> None:
    issues = [
        _issue("b", tool="ruff", severity=Severity.low, verification=VerificationState.confirmed),
        _issue(
            "a", tool="ruff", severity=Severity.critical, verification=VerificationState.confirmed
        ),
        _issue("c", tool="pytest", severity=Severity.low, verification=VerificationState.confirmed),
    ]
    # critical first, then the two low issues ordered by tool name (pytest < ruff).
    assert [issue.issue_id for issue in sort_issues(issues)] == ["a", "c", "b"]
