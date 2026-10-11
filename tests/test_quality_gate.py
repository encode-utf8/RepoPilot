"""Quality gate (design document 3.6) behaviour."""

from __future__ import annotations

from repopilot.agent.limits import AgentLimits
from repopilot.agent.quality_gate import (
    evaluate_quality_gate,
    find_cheating,
    normalize_nodeid,
    resolve_target,
)
from repopilot.models import PatchArtifact
from repopilot.models import TestCaseOutcome as CaseOutcome
from repopilot.models import TestSnapshot as Snapshot

LIMITS = AgentLimits()


def snapshot(cases: dict[str, str]) -> Snapshot:
    outcomes = [CaseOutcome(nodeid=nodeid, outcome=outcome) for nodeid, outcome in cases.items()]
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for outcome in cases.values():
        counts[outcome] += 1
    failed = counts["failed"] + counts["error"]
    return Snapshot(
        ran=True,
        total=len(outcomes),
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["error"],
        skipped=counts["skipped"],
        exit_code=1 if failed else 0,
        cases=outcomes,
    )


def patch(files: tuple[str, ...] = ("src/a.py",), *, diff_lines: int = 2) -> PatchArtifact:
    return PatchArtifact(
        applied=True,
        diff="--- a/src/a.py\n+++ b/src/a.py\n",
        changed_files=list(files),
        added_lines=1,
        removed_lines=1,
        diff_lines=diff_lines,
        diff_hash="deadbeef",
    )


def evaluate(**overrides):
    arguments = {
        "limits": LIMITS,
        "patch": patch(),
        "baseline_tests": snapshot({"tests.test_a::test_x": "failed"}),
        "final_tests": snapshot({"tests.test_a::test_x": "passed"}),
        "target_tests": ["tests/test_a.py::test_x"],
        "located_files": ["src/a.py"],
        "allowed_paths": [],
        "baseline_sources": {},
        "final_sources": {},
    }
    arguments.update(overrides)
    return evaluate_quality_gate(**arguments)


def check_names(report) -> dict[str, bool]:
    return {check.name: check.passed for check in report.checks}


def test_normalize_nodeid_matches_junit_and_path_forms() -> None:
    assert normalize_nodeid("tests/test_x.py::test_a") == "tests.test_x::test_a"
    assert normalize_nodeid("tests.test_x::test_a") == "tests.test_x::test_a"
    assert normalize_nodeid("tests\\test_x.py::test_a") == "tests.test_x::test_a"


def test_resolve_target_prefers_a_baseline_failing_match() -> None:
    final = {"tests.test_a::test_x": "passed", "tests.test_a::test_y": "failed"}
    baseline = {"tests.test_a::test_x": "failed", "tests.test_a::test_y": "failed"}
    assert resolve_target("tests/test_a.py::test_x", final, baseline) == "tests.test_a::test_x"
    assert resolve_target("tests/test_a.py", final, baseline) == "tests.test_a::test_x"
    assert resolve_target("tests/other.py::nope", final, baseline) is None


def test_gate_passes_when_the_target_is_fixed_without_regression() -> None:
    report = evaluate(
        baseline_tests=snapshot(
            {"tests.test_a::test_x": "failed", "tests.test_a::test_y": "passed"}
        ),
        final_tests=snapshot({"tests.test_a::test_x": "passed", "tests.test_a::test_y": "passed"}),
    )
    assert report.passed is True
    assert report.primary_blocker is None
    assert all(check_names(report).values())


def test_gate_detects_a_regression() -> None:
    report = evaluate(
        baseline_tests=snapshot(
            {"tests.test_a::test_x": "failed", "tests.test_a::test_y": "passed"}
        ),
        final_tests=snapshot({"tests.test_a::test_x": "passed", "tests.test_a::test_y": "failed"}),
    )
    assert report.passed is False
    assert check_names(report)["no_regression"] is False


def test_gate_requires_the_target_test_to_be_collected() -> None:
    report = evaluate(final_tests=snapshot({"tests.test_a::test_other": "passed"}))
    assert report.passed is False
    assert check_names(report)["target_tests_pass"] is False


def test_gate_fails_when_tests_did_not_run() -> None:
    report = evaluate(final_tests=None)
    assert report.passed is False
    assert check_names(report)["tests_executed"] is False


def test_gate_flags_changes_outside_the_located_scope() -> None:
    report = evaluate(patch=patch(("src/elsewhere.py",)))
    assert report.passed is False
    assert check_names(report)["change_scope"] is False
    assert "outside the located scope" in report.checks[-2].detail


def test_gate_flags_unattributable_baseline_failures() -> None:
    report = evaluate(
        baseline_tests=snapshot(
            {"tests.test_a::test_x": "failed", "tests.test_b::test_z": "failed"}
        ),
        final_tests=snapshot({"tests.test_a::test_x": "passed", "tests.test_b::test_z": "passed"}),
    )
    assert report.passed is False
    assert check_names(report)["baseline_attributable"] is False


def test_gate_fails_when_no_target_can_be_determined() -> None:
    report = evaluate(
        baseline_tests=snapshot({"tests.test_a::test_y": "passed"}),
        final_tests=snapshot({"tests.test_a::test_y": "passed"}),
        target_tests=[],
    )
    assert report.passed is False
    assert check_names(report)["target_tests_pass"] is False


def test_gate_blocks_a_diff_larger_than_the_limit() -> None:
    report = evaluate(patch=patch(diff_lines=LIMITS.max_diff_lines + 1))
    assert report.passed is False
    assert check_names(report)["change_scope"] is False


def test_gate_blocks_too_many_changed_files() -> None:
    files = tuple(f"src/f{index}.py" for index in range(LIMITS.max_changed_files + 1))
    report = evaluate(patch=patch(files), located_files=list(files))
    assert report.passed is False
    assert check_names(report)["change_scope"] is False


def test_gate_rejects_a_deleted_test_file() -> None:
    findings = find_cheating(
        baseline_sources={"tests/test_a.py": "def test_x():\n    assert 1 == 1\n"},
        final_sources={},
        diff="--- a/tests/test_a.py\n+++ /dev/null\n",
    )
    assert any("deleted" in finding for finding in findings)


def test_gate_rejects_a_removed_test_function() -> None:
    findings = find_cheating(
        baseline_sources={"tests/test_a.py": "def test_x():\n    assert 1 == 1\n"},
        final_sources={"tests/test_a.py": "def helper():\n    return 1\n"},
        diff="",
    )
    assert any("removed" in finding for finding in findings)


def test_gate_rejects_weakened_assertions() -> None:
    baseline = "def test_x():\n    assert 1 == 1\n    assert 2 == 2\n"
    final = "def test_x():\n    assert 1 == 1\n"
    findings = find_cheating(
        baseline_sources={"tests/test_a.py": baseline},
        final_sources={"tests/test_a.py": final},
        diff="",
    )
    assert any("weakened" in finding for finding in findings)


def test_gate_rejects_added_skip_and_noqa_markers() -> None:
    diff = (
        "--- a/tests/test_a.py\n"
        "+++ b/tests/test_a.py\n"
        "@@ -1,1 +1,2 @@\n"
        " import pytest\n"
        "+@pytest.mark.skip(reason='flaky')\n"
        "+    x = 1  # noqa\n"
    )
    findings = find_cheating(baseline_sources={}, final_sources={}, diff=diff)
    labels = " ".join(findings)
    assert "skip marker" in labels
    assert "lint suppression" in labels


def test_gate_rejects_a_trivially_true_assertion() -> None:
    diff = (
        "--- a/tests/test_a.py\n"
        "+++ b/tests/test_a.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-    assert value == 2\n"
        "+    assert True\n"
    )
    findings = find_cheating(
        baseline_sources={},
        final_sources={},
        diff=diff,
    )
    assert any("trivially true" in finding for finding in findings)


def test_source_only_patch_has_no_cheating_findings() -> None:
    findings = find_cheating(
        baseline_sources={"tests/test_a.py": "def test_x():\n    assert 1 == 1\n"},
        final_sources={"tests/test_a.py": "def test_x():\n    assert 1 == 1\n"},
        diff="--- a/src/a.py\n+++ b/src/a.py\n@@ -1,1 +1,1 @@\n-    return 1\n+    return 2\n",
    )
    assert findings == []


def test_cheating_findings_block_the_gate() -> None:
    report = evaluate(
        baseline_sources={"tests/test_a.py": "def test_x():\n    assert 1 == 1\n"},
        final_sources={},
    )
    assert report.passed is False
    assert check_names(report)["no_cheating"] is False
    assert report.cheating_findings
