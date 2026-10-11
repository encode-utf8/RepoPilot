"""pytest execution checker driven by JUnit XML output."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from repopilot.checkers.base import CheckContext, issue_id, normalize_rel_path, truncate
from repopilot.errors import CodeExecutionDeniedError
from repopilot.execution.sandbox import SandboxCommand
from repopilot.models import (
    CheckerResult,
    CheckerStatus,
    Issue,
    Origin,
    Severity,
    VerificationState,
)

_TRACEBACK_LOCATION_RE = re.compile(r"([\w./\\-]+\.py):(\d+)")


@dataclass
class _JunitCase:
    name: str
    classname: str
    outcome: str
    detail: str
    file: str | None = None
    line: int | None = None

    @property
    def nodeid(self) -> str:
        return f"{self.classname}::{self.name}" if self.classname else self.name


@dataclass
class _JunitSummary:
    counts: dict[str, int] = field(default_factory=dict)
    cases: list[_JunitCase] = field(default_factory=list)


def _parse_junit(path: Path) -> _JunitSummary | None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None

    counts = {"total": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0}
    cases: list[_JunitCase] = []
    for case in root.iter("testcase"):
        counts["total"] += 1
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        outcome = "passed"
        detail = ""
        node = None
        if failure is not None:
            outcome, node = "failure", failure
        elif error is not None:
            outcome, node = "error", error
        elif skipped is not None:
            outcome, node = "skipped", skipped
        if node is not None:
            detail = (node.get("message") or "") + "\n" + (node.text or "")
        counter_key = {"failure": "failures", "error": "errors", "skipped": "skipped"}.get(
            outcome, "passed"
        )
        counts[counter_key] += 1

        file_value = case.get("file")
        line_value = case.get("line")
        line: int | None = None
        if line_value and line_value.isdigit():
            line = int(line_value)
        if file_value is None:
            match = _TRACEBACK_LOCATION_RE.search(detail)
            if match:
                file_value = match.group(1)
                line = int(match.group(2))
        cases.append(
            _JunitCase(
                name=case.get("name") or "<unknown>",
                classname=case.get("classname") or "",
                outcome=outcome,
                detail=detail.strip(),
                file=file_value,
                line=line,
            )
        )
    return _JunitSummary(counts=counts, cases=cases)


class PytestChecker:
    """Runs the repository test suite and reports failing tests as confirmed issues."""

    name = "pytest"
    display_name = "pytest test execution"

    def is_applicable(self, context: CheckContext) -> bool:
        return "pytest" in context.project.test_frameworks and context.project.test_file_count > 0

    def run(self, context: CheckContext) -> CheckerResult:
        started = time.monotonic()
        junit_path = context.workspace.artifacts_dir / "pytest-junit.xml"
        if junit_path.exists():
            junit_path.unlink()

        targets = [
            directory for directory in context.project.test_dirs if directory not in ("", ".")
        ]
        argv = [
            *context.settings.pytest_command,
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-q",
            f"--junit-xml={junit_path}",
            f"--rootdir={context.repo_dir}",
            *(targets or ["."]),
        ]
        required = context.settings.require_pytest_for_detected_tests
        common = {
            "name": self.name,
            "display_name": self.display_name,
            "commands": [argv],
            "required": required,
            "artifacts": {"junit_xml": str(junit_path)},
        }
        command = SandboxCommand(
            argv=argv,
            purpose="pytest",
            cwd=context.repo_dir,
            timeout=context.settings.command_timeout_seconds,
            executes_repo_code=True,
        )
        try:
            result = context.sandbox.run(command, context.token)
        except CodeExecutionDeniedError as exc:
            return CheckerResult(
                status=CheckerStatus.unavailable,
                error=exc.message,
                notes=[
                    "repository code execution is disabled for this source, so pytest "
                    "was not executed and test results could not be verified",
                ],
                duration_ms=int((time.monotonic() - started) * 1000),
                **common,
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        common["duration_ms"] = duration_ms
        common["exit_code"] = result.exit_code

        if result.cancelled:
            return CheckerResult(
                status=CheckerStatus.unavailable, notes=["pytest run was cancelled"], **common
            )
        if result.timed_out:
            return CheckerResult(
                status=CheckerStatus.unavailable,
                notes=[f"pytest timed out after {result.duration_ms} ms"],
                **common,
            )
        if result.error is not None:
            return CheckerResult(
                status=CheckerStatus.unavailable, error=result.error, notes=[result.error], **common
            )

        summary = _parse_junit(junit_path)
        if summary is None:
            message = truncate(result.stderr or result.stdout, 600)
            if result.exit_code == 5:
                return CheckerResult(
                    status=CheckerStatus.unavailable,
                    notes=["pytest collected no tests, coverage of the suite is unverified"],
                    **common,
                )
            return CheckerResult(
                status=CheckerStatus.error,
                error=f"pytest output could not be parsed: {message}",
                notes=[message],
                **common,
            )

        limit = context.settings.max_evidence_chars
        issues: list[Issue] = []
        for case in summary.cases:
            if case.outcome not in ("failure", "error"):
                continue
            issue_type = "test_failure" if case.outcome == "failure" else "test_error"
            relative = normalize_rel_path(case.file, context.workspace)
            nodeid = case.nodeid
            issues.append(
                Issue(
                    issue_id=issue_id(self.name, issue_type, nodeid, relative, case.line),
                    issue_type=issue_type,
                    severity=Severity.high,
                    evidence=truncate(f"{nodeid}: {case.detail or 'no details reported'}", limit),
                    source_tool=self.name,
                    origin=Origin.tool,
                    verification=VerificationState.confirmed,
                    file_path=relative,
                    line=case.line,
                    rule_id=f"pytest-{case.outcome}",
                    fix_suggestion=(
                        "Reproduce with the repository test command, then fix the code or "
                        "the test expectation. A failing baseline is not a passing result."
                    ),
                    metadata={"test": nodeid, "outcome": case.outcome},
                )
            )

        notes: list[str] = []
        if summary.counts.get("skipped"):
            skipped = summary.counts["skipped"]
            notes.append(f"{skipped} test(s) skipped; skipped tests do not verify behaviour")
        if summary.counts.get("total") == 0:
            return CheckerResult(
                status=CheckerStatus.unavailable,
                notes=[*notes, "pytest reported zero test cases"],
                summary=summary.counts,
                issues=issues,
                **common,
            )

        return CheckerResult(
            status=CheckerStatus.findings if issues else CheckerStatus.ok,
            issues=issues,
            summary={**summary.counts, "exit_code": result.exit_code},
            notes=notes,
            **common,
        )
