"""Checker behaviour against controlled fixtures and canned tool output."""

from __future__ import annotations

import json
from dataclasses import replace

from repopilot.checkers.deps import DependencyAuditChecker
from repopilot.checkers.heuristics import HeuristicsChecker
from repopilot.checkers.pytest_checker import PytestChecker
from repopilot.checkers.ruff_checker import RuffChecker
from repopilot.config import Settings
from repopilot.fixtures import FixtureRepo
from repopilot.models import CheckerStatus, VerificationState
from support import FakeSandbox, command_result, prepare_repo


def test_ruff_reports_known_rules(settings: Settings, buggy_repo: FixtureRepo) -> None:
    prepared = prepare_repo(settings, buggy_repo.path)
    result = RuffChecker().run(prepared.context)

    assert result.status is CheckerStatus.findings
    assert result.ran is True
    rules = {issue.rule_id for issue in result.issues}
    assert {"F401", "F821", "F841", "E711", "B006"} <= rules
    for issue in result.issues:
        assert issue.origin.value == "tool"
        assert issue.verification is VerificationState.confirmed
        assert issue.confirmed is True
        assert issue.evidence
        assert issue.fix_suggestion

    undefined_name = next(issue for issue in result.issues if issue.rule_id == "F821")
    assert undefined_name.severity.value == "high"
    assert undefined_name.file_path == "src/calc/core.py"
    assert undefined_name.line is not None
    assert result.summary["findings"] == len(result.issues)


def test_ruff_clean_fixture_has_no_findings(settings: Settings, clean_repo: FixtureRepo) -> None:
    prepared = prepare_repo(settings, clean_repo.path)
    result = RuffChecker().run(prepared.context)
    assert result.status is CheckerStatus.ok
    assert result.issues == []


def test_pytest_reports_the_intentional_failure(
    settings: Settings, buggy_repo: FixtureRepo
) -> None:
    prepared = prepare_repo(settings, buggy_repo.path)
    result = PytestChecker().run(prepared.context)

    assert result.status is CheckerStatus.findings
    failures = [issue for issue in result.issues if issue.issue_type == "test_failure"]
    assert len(failures) == 1
    assert "test_accumulate_known_failure" in failures[0].evidence
    assert failures[0].file_path == "tests/test_core.py"
    assert failures[0].severity.value == "high"
    assert result.summary["failures"] == 1
    assert result.summary["total"] >= 5


def test_pytest_clean_fixture_passes_with_skip(settings: Settings, clean_repo: FixtureRepo) -> None:
    prepared = prepare_repo(settings, clean_repo.path)
    result = PytestChecker().run(prepared.context)

    assert result.status is CheckerStatus.ok
    assert result.issues == []
    assert result.summary["failures"] == 0
    assert result.summary["skipped"] == 1
    assert any("skipped" in note for note in result.notes)


def test_pytest_is_denied_for_untrusted_source(settings: Settings, buggy_repo: FixtureRepo) -> None:
    prepared = prepare_repo(settings, buggy_repo.path, trusted=False)
    result = PytestChecker().run(prepared.context)

    assert result.status is CheckerStatus.unavailable
    assert result.ran is False
    assert result.required is True
    assert any("code execution" in note for note in result.notes)


def test_dependency_checker_parses_json(settings: Settings, buggy_repo: FixtureRepo) -> None:
    prepared = prepare_repo(settings, buggy_repo.path)
    payload = {
        "dependencies": [
            {
                "name": "requests",
                "version": "2.31.0",
                "vulns": [
                    {
                        "id": "GHSA-9wx4-h78v-vm56",
                        "fix_versions": ["2.32.0"],
                        "description": "certificate verification bypass",
                        "aliases": ["CVE-2024-35195"],
                    }
                ],
            },
            {"name": "jinja2", "version": "3.1.4", "vulns": []},
        ]
    }
    sandbox = FakeSandbox(
        lambda command: command_result(command.argv, stdout=json.dumps(payload), exit_code=1)
    )
    prepared.context.sandbox = sandbox

    result = DependencyAuditChecker().run(prepared.context)

    assert result.status is CheckerStatus.findings
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.rule_id == "GHSA-9wx4-h78v-vm56"
    assert issue.issue_type == "dependency_vulnerability"
    assert issue.file_path == "requirements.txt"
    assert issue.line == 1
    assert issue.verification is VerificationState.confirmed
    assert "2.32.0" in (issue.fix_suggestion or "")
    assert issue.metadata["aliases"] == ["CVE-2024-35195"]


def test_dependency_checker_reports_unavailable_when_offline(
    settings: Settings, buggy_repo: FixtureRepo
) -> None:
    prepared = prepare_repo(settings, buggy_repo.path)
    sandbox = FakeSandbox(
        lambda command: command_result(
            command.argv,
            stderr="ERROR: Could not connect to pypi.org (network is unreachable)",
            exit_code=1,
        )
    )
    prepared.context.sandbox = sandbox

    result = DependencyAuditChecker().run(prepared.context)

    assert result.status is CheckerStatus.unavailable
    assert result.ran is False
    assert result.required is False
    assert result.issues == []
    assert any("unverified" in note for note in result.notes)


def test_dependency_checker_can_be_required(settings: Settings, buggy_repo: FixtureRepo) -> None:
    required_settings = replace(settings, require_dependency_audit=True)
    prepared = prepare_repo(required_settings, buggy_repo.path)
    sandbox = FakeSandbox(
        lambda command: command_result(command.argv, stderr="no network", exit_code=1)
    )
    prepared.context.sandbox = sandbox

    result = DependencyAuditChecker().run(prepared.context)
    assert result.required is True


def test_heuristics_are_always_unverified(settings: Settings, buggy_repo: FixtureRepo) -> None:
    prepared = prepare_repo(settings, buggy_repo.path)
    result = HeuristicsChecker().run(prepared.context)

    assert result.status is CheckerStatus.findings
    assert result.issues
    assert all(issue.verification is VerificationState.unverified for issue in result.issues)
    assert all(issue.confirmed is False for issue in result.issues)
    assert all(issue.origin.value == "tool" for issue in result.issues)
    kinds = {issue.issue_type for issue in result.issues}
    assert "dependency_pinning" in kinds
