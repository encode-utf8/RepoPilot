"""Heuristic, deterministic risk hints.

These findings are explicitly *unverified potential problems*: they are derived
from static text patterns, not from a checker that confirmed a defect. They are
always reported with ``verification=unverified`` so they can never be mistaken
for tool confirmed issues.
"""

from __future__ import annotations

import re
import time

from repopilot.checkers.base import CheckContext, issue_id, truncate
from repopilot.models import (
    CheckerResult,
    CheckerStatus,
    Issue,
    Origin,
    Severity,
    VerificationState,
)
from repopilot.scan.scanner import iter_files

_SKIP_MARKER_RE = re.compile(
    r"pytest\.mark\.(?:skip|xfail)|pytest\.(?:skip|xfail)\(|unittest\.skip|@skip\b"
)
_BARE_EXCEPT_PASS_RE = re.compile(r"except[^\n:]*:\s*\n\s*pass\b")
_MAX_PY_FILES = 400


class HeuristicsChecker:
    """Reports unverified potential problems that need human confirmation."""

    name = "heuristics"
    display_name = "Heuristic risk hints (unverified)"

    def is_applicable(self, context: CheckContext) -> bool:
        return context.project.is_python_project

    def run(self, context: CheckContext) -> CheckerResult:
        started = time.monotonic()
        limit = context.settings.max_evidence_chars
        issues: list[Issue] = []
        issues.extend(self._unpinned_requirements(context, limit))
        issues.extend(self._weak_tests(context, limit))
        issues.extend(self._error_swallowing(context, limit))
        if context.project.test_file_count == 0:
            issues.append(
                Issue(
                    issue_id=issue_id(self.name, "verification_gap", "no-tests"),
                    issue_type="verification_gap",
                    severity=Severity.medium,
                    evidence=(
                        "No test files were detected, so behaviour could not be verified by "
                        "executing the project test suite."
                    ),
                    source_tool=self.name,
                    origin=Origin.tool,
                    verification=VerificationState.unverified,
                    fix_suggestion="Add tests or point the assessment at a project with tests.",
                    metadata={"hint": "missing_tests"},
                )
            )
        return CheckerResult(
            name=self.name,
            display_name=self.display_name,
            status=CheckerStatus.findings if issues else CheckerStatus.ok,
            issues=issues,
            summary={"potential_issues": len(issues)},
            duration_ms=int((time.monotonic() - started) * 1000),
            notes=[
                "Heuristic hints are unverified potential problems; confirm them before acting.",
            ],
        )

    @staticmethod
    def _issue(
        *,
        kind: str,
        severity: Severity,
        evidence: str,
        file_path: str | None,
        line: int | None,
        suggestion: str,
        limit: int,
        metadata: dict | None = None,
    ) -> Issue:
        return Issue(
            issue_id=issue_id("heuristics", kind, file_path, line, evidence[:80]),
            issue_type=kind,
            severity=severity,
            evidence=truncate(evidence, limit),
            source_tool="heuristics",
            origin=Origin.tool,
            verification=VerificationState.unverified,
            file_path=file_path,
            line=line,
            rule_id=f"heuristics/{kind}",
            fix_suggestion=suggestion,
            metadata=metadata or {},
        )

    def _unpinned_requirements(self, context: CheckContext, limit: int) -> list[Issue]:
        issues: list[Issue] = []
        for relative in context.project.requirements_files:
            path = context.repo_dir / relative
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for index, line in enumerate(lines, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "-")):
                    continue
                if "==" in stripped or "===" in stripped:
                    continue
                issues.append(
                    self._issue(
                        kind="dependency_pinning",
                        severity=Severity.low,
                        evidence=f"unpinned dependency: {stripped}",
                        file_path=relative,
                        line=index,
                        suggestion=(
                            "Pin exact versions so assessments are reproducible; verify the "
                            "resolved version before changing it."
                        ),
                        limit=limit,
                        metadata={"hint": "unpinned_dependency"},
                    )
                )
        return issues

    def _weak_tests(self, context: CheckContext, limit: int) -> list[Issue]:
        issues: list[Issue] = []
        for path in self._python_files(context, test_like_only=True):
            text = self._read(path)
            for match in _SKIP_MARKER_RE.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                issues.append(
                    self._issue(
                        kind="weak_verification",
                        severity=Severity.medium,
                        evidence=f"test skipped or marked xfail: {match.group(0)}",
                        file_path=path,
                        line=line,
                        suggestion=(
                            "Confirm whether the skip is intentional; skipped tests do not "
                            "verify behaviour."
                        ),
                        limit=limit,
                        metadata={"hint": "skipped_test"},
                    )
                )
        return issues

    def _error_swallowing(self, context: CheckContext, limit: int) -> list[Issue]:
        issues: list[Issue] = []
        for path in self._python_files(context, test_like_only=False):
            text = self._read(path)
            for match in _BARE_EXCEPT_PASS_RE.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                issues.append(
                    self._issue(
                        kind="error_swallowing",
                        severity=Severity.medium,
                        evidence="exception is caught and silently ignored (except: pass)",
                        file_path=path,
                        line=line,
                        suggestion=(
                            "Verify whether swallowing this error hides a real failure; log or "
                            "re-raise instead."
                        ),
                        limit=limit,
                        metadata={"hint": "swallowed_exception"},
                    )
                )
        return issues

    @staticmethod
    def _read(path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            return ""

    def _python_files(self, context: CheckContext, *, test_like_only: bool):
        files = iter_files(context.repo_dir, max_files=context.settings.max_scanned_files)
        result = []
        for path in files:
            if path.suffix != ".py":
                continue
            is_test = path.name.startswith("test_") or path.name == "conftest.py"
            if test_like_only and not is_test:
                continue
            result.append(path.relative_to(context.repo_dir))
            if len(result) >= _MAX_PY_FILES:
                break
        return result
