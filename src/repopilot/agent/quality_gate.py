"""Design document section 3.6: the repair quality gate.

The gate is deliberately deterministic. It never asks the model whether a repair
succeeded; it checks the pinned baseline against the final state and refuses to
report success when anything is unconfirmed. Failing any check keeps the task out
of ``succeeded``.
"""

from __future__ import annotations

import ast
import re

from repopilot.agent.limits import AgentLimits
from repopilot.models import (
    GateCheck,
    PatchArtifact,
    QualityGateReport,
    TestSnapshot,
)

_FAILING_OUTCOMES = ("failed", "error")

#: Patterns that would fake a passing suite rather than fix it.
_CHEAT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("skip marker", re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)\b")),
    ("skip call", re.compile(r"\bpytest\.skip\s*\(")),
    ("unittest skip", re.compile(r"\bunittest\.skip\w*")),
    ("pytest ignore", re.compile(r"\bcollect_ignore\b")),
    ("lint suppression", re.compile(r"#\s*noqa")),
)
_TRIVIAL_ASSERT_RE = re.compile(r"^\s*assert\s+True\s*(?:#.*)?$")


def normalize_nodeid(value: str) -> str:
    """Normalise a pytest target so ``tests/test_x.py::case`` matches JUnit ids.

    JUnit reports ``tests.test_x::case`` while users write
    ``tests/test_x.py::case``; both must compare equal, otherwise a passing
    target looks unresolved and a passing repair looks unverified.
    """

    text = value.strip().replace("\\", "/")
    text = text.replace(".py::", "::")
    if text.endswith(".py"):
        text = text[:-3]
    return text.replace("/", ".")


def normalize_outcomes(snapshot: TestSnapshot | None) -> dict[str, str]:
    """Map a snapshot's node ids to outcomes, keyed by normalised node id."""

    if snapshot is None:
        return {}
    return {normalize_nodeid(nodeid): outcome for nodeid, outcome in snapshot.outcome_map().items()}


def _parse(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _is_raises(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Attribute) and expr.attr == "raises"


def _test_functions(source: str) -> dict[str, int]:
    """Map test function name to its assertion-like statement count."""

    tree = _parse(source)
    if tree is None:
        return {}
    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        total = 0
        for child in ast.walk(node):
            if isinstance(child, ast.Assert):
                total += 1
            elif isinstance(child, ast.With):
                total += sum(1 for item in child.items if _is_raises(item.context_expr))
        counts[node.name] = total
    return counts


def _added_lines(diff: str) -> list[str]:
    return [
        line[1:]
        for line in (diff or "").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def find_cheating(
    *,
    baseline_sources: dict[str, str],
    final_sources: dict[str, str],
    diff: str,
) -> list[str]:
    """Return human readable findings for test tampering, if any."""

    findings: list[str] = []

    for path, baseline_source in sorted(baseline_sources.items()):
        if path not in final_sources:
            findings.append(f"test file deleted: {path}")
            continue
        baseline_tests = _test_functions(baseline_source)
        final_tests = _test_functions(final_sources[path])
        removed = sorted(set(baseline_tests) - set(final_tests))
        if removed:
            findings.append(f"test function(s) removed from {path}: {', '.join(removed)}")
        for name in sorted(set(baseline_tests) & set(final_tests)):
            before, after = baseline_tests[name], final_tests[name]
            if after < before:
                findings.append(
                    f"assertions weakened in {path}::{name} ({before} -> {after} checks)"
                )

    for line in _added_lines(diff):
        for label, pattern in _CHEAT_PATTERNS:
            if pattern.search(line):
                findings.append(f"added {label}: {line.strip()[:200]}")
        if _TRIVIAL_ASSERT_RE.match(line):
            findings.append(f"added trivially true assertion: {line.strip()[:200]}")

    return findings


def resolve_target(
    target: str, final_map: dict[str, str], baseline_map: dict[str, str]
) -> str | None:
    """Resolve a loose target (path or substring) to a normalised node id."""

    target = normalize_nodeid(target)
    if target in final_map:
        return target
    candidates = [nodeid for nodeid in final_map if target in nodeid]
    if not candidates:
        return None
    failing = [nodeid for nodeid in candidates if baseline_map.get(nodeid) in _FAILING_OUTCOMES]
    pool = failing or candidates
    return sorted(pool)[0]


def evaluate_quality_gate(
    *,
    limits: AgentLimits,
    patch: PatchArtifact | None,
    baseline_tests: TestSnapshot | None,
    final_tests: TestSnapshot | None,
    target_tests: list[str],
    located_files: list[str],
    allowed_paths: list[str],
    baseline_sources: dict[str, str],
    final_sources: dict[str, str],
) -> QualityGateReport:
    """Evaluate every section 3.6 condition and return a structured verdict."""

    checks: list[GateCheck] = []
    warnings: list[str] = []
    baseline_map = normalize_outcomes(baseline_tests)
    final_map = normalize_outcomes(final_tests)
    baseline_failures = [
        node for node, outcome in baseline_map.items() if outcome in _FAILING_OUTCOMES
    ]

    targets = [normalize_nodeid(target) for target in target_tests] or list(baseline_failures)
    if not target_tests:
        warnings.append("no explicit target tests supplied; inferred from baseline failures")

    # 1. the patch must have been produced and applied
    applied = bool(patch is not None and patch.applied and patch.changed_files)
    checks.append(
        GateCheck(
            name="patch_applied",
            passed=applied,
            detail=(
                f"applied to {len(patch.changed_files)} file(s)"
                if applied and patch
                else "no applied patch was produced"
            ),
        )
    )

    # 2. the suite must actually have run
    tests_ran = bool(final_tests is not None and final_tests.ran and final_tests.total > 0)
    checks.append(
        GateCheck(
            name="tests_executed",
            passed=tests_ran,
            detail=(
                f"{final_tests.total} test case(s) collected"
                if tests_ran and final_tests
                else (
                    final_tests.error if final_tests and final_tests.error else "no usable test run"
                )
            ),
        )
    )

    # 3. unrelated baseline failures make a confident verdict impossible
    unattributed = sorted(set(baseline_failures) - set(targets))
    checks.append(
        GateCheck(
            name="baseline_attributable",
            passed=not unattributed,
            detail=(
                "all baseline failures are part of the task"
                if not unattributed
                else f"baseline has failures outside the task targets: {', '.join(unattributed)}"
            ),
        )
    )

    # 4. target tests must now pass
    if not targets:
        target_detail = "no target tests could be determined"
        targets_ok = False
    else:
        unresolved = [
            target for target in targets if resolve_target(target, final_map, baseline_map) is None
        ]
        still_failing = [
            target
            for target in targets
            if (node := resolve_target(target, final_map, baseline_map)) is not None
            and final_map.get(node) != "passed"
        ]
        targets_ok = not unresolved and not still_failing
        if targets_ok:
            target_detail = f"all {len(targets)} target test(s) pass"
        elif unresolved:
            target_detail = f"target test(s) not collected: {', '.join(unresolved)}"
        else:
            target_detail = f"target test(s) still failing: {', '.join(still_failing)}"
    checks.append(GateCheck(name="target_tests_pass", passed=targets_ok, detail=target_detail))

    # 5. no regression on tests that passed on the baseline
    regressions = sorted(
        node
        for node, outcome in baseline_map.items()
        if outcome == "passed" and final_map.get(node) != "passed"
    )
    checks.append(
        GateCheck(
            name="no_regression",
            passed=not regressions,
            detail=(
                "no previously passing test regressed"
                if not regressions
                else f"regressions: {', '.join(regressions)}"
            ),
        )
    )

    # 6. change scope must stay related to the task
    changed = list(patch.changed_files) if patch else []
    allowed = {*located_files, *allowed_paths}
    out_of_scope = [path for path in changed if path not in allowed]
    diff_lines = patch.diff_lines if patch else 0
    scope_problems: list[str] = []
    if out_of_scope:
        scope_problems.append(f"files outside the located scope: {', '.join(out_of_scope)}")
    if len(changed) > limits.max_changed_files:
        scope_problems.append(
            f"{len(changed)} changed files exceeds the limit of {limits.max_changed_files}"
        )
    if diff_lines > limits.max_diff_lines:
        scope_problems.append(
            f"{diff_lines} changed lines exceeds the limit of {limits.max_diff_lines}"
        )
    checks.append(
        GateCheck(
            name="change_scope",
            passed=not scope_problems,
            detail="; ".join(scope_problems) if scope_problems else "change is within scope",
        )
    )

    # 7. no cheating: no deleted tests, weakened assertions or skipped checks
    cheating = find_cheating(
        baseline_sources=baseline_sources,
        final_sources=final_sources,
        diff=patch.diff if patch else "",
    )
    checks.append(
        GateCheck(
            name="no_cheating",
            passed=not cheating,
            detail="; ".join(cheating[:5]) if cheating else "no test tampering detected",
        )
    )

    blockers = [f"{check.name}: {check.detail}" for check in checks if not check.passed]
    primary = next((check.name for check in checks if not check.passed), None)
    return QualityGateReport(
        passed=not blockers,
        primary_blocker=primary,
        checks=checks,
        blockers=blockers,
        warnings=warnings,
        cheating_findings=cheating,
    )
