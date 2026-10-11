"""Shared pytest execution and JUnit parsing.

The phase 1 checker and the phase 2 agent tools both need to run pytest and turn
the JUnit XML into structured outcomes, so the logic lives here once. Everything
goes through the :class:`~repopilot.execution.sandbox.SandboxExecutor`, which
means the argument list, timeout, output cap and environment allowlist rules are
enforced on this path too.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.execution.process import CommandResult
from repopilot.execution.sandbox import SandboxCommand, SandboxExecutor
from repopilot.models import TestCaseOutcome, TestSnapshot

_TRACEBACK_LOCATION_RE = re.compile(r"([\w./\\-]+\.py):(\d+)")
_ERROR_EXCERPT_CHARS = 600


@dataclass
class JunitCase:
    """One parsed JUnit ``testcase`` element."""

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
class JunitSummary:
    """Parsed JUnit document: counts plus per case detail."""

    counts: dict[str, int] = field(default_factory=dict)
    cases: list[JunitCase] = field(default_factory=list)


@dataclass
class TestRunResult:
    """Outcome of one pytest invocation."""

    argv: list[str]
    command: CommandResult | None
    snapshot: TestSnapshot | None
    error: str | None = None

    @property
    def usable(self) -> bool:
        """``True`` when a parseable, non empty test run was produced."""

        return self.snapshot is not None and self.snapshot.total > 0


def _excerpt(text: str, limit: int = _ERROR_EXCERPT_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def build_pytest_argv(
    settings: Settings,
    *,
    junit_path: Path,
    repo_dir: Path,
    targets: list[str] | None = None,
) -> list[str]:
    """Build the deterministic pytest argument list used everywhere."""

    return [
        *settings.pytest_command,
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "-q",
        f"--junit-xml={junit_path}",
        f"--rootdir={repo_dir}",
        *(targets or ["."]),
    ]


def parse_junit_xml(path: Path) -> JunitSummary | None:
    """Parse a JUnit XML file, or return ``None`` when it is unusable."""

    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None

    counts = {"total": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0}
    cases: list[JunitCase] = []
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
            JunitCase(
                name=case.get("name") or "<unknown>",
                classname=case.get("classname") or "",
                outcome=outcome,
                detail=detail.strip(),
                file=file_value,
                line=line,
            )
        )
    return JunitSummary(counts=counts, cases=cases)


def snapshot_from_summary(
    summary: JunitSummary,
    *,
    command: list[str],
    exit_code: int | None,
    duration_ms: int,
) -> TestSnapshot:
    """Convert a parsed JUnit summary into the persisted snapshot model."""

    cases = [
        TestCaseOutcome(
            nodeid=case.nodeid,
            outcome=("failed" if case.outcome == "failure" else case.outcome),  # type: ignore[arg-type]
            detail=_excerpt(case.detail, 400) or None,
        )
        for case in summary.cases
    ]
    total = summary.counts.get("total", 0)
    return TestSnapshot(
        ran=total > 0,
        command=list(command),
        total=total,
        passed=summary.counts.get("passed", 0),
        failed=summary.counts.get("failures", 0),
        errors=summary.counts.get("errors", 0),
        skipped=summary.counts.get("skipped", 0),
        duration_ms=duration_ms,
        exit_code=exit_code,
        cases=cases,
    )


def run_pytest(
    sandbox: SandboxExecutor,
    settings: Settings,
    token: CancellationToken,
    *,
    repo_dir: Path,
    junit_path: Path,
    targets: list[str] | None = None,
    timeout: float | None = None,
    purpose: str = "pytest",
) -> TestRunResult:
    """Run pytest through the sandbox and return a structured snapshot."""

    junit_path.unlink(missing_ok=True)
    argv = build_pytest_argv(settings, junit_path=junit_path, repo_dir=repo_dir, targets=targets)
    command = SandboxCommand(
        argv=argv,
        purpose=purpose,
        cwd=repo_dir,
        timeout=timeout or settings.command_timeout_seconds,
        executes_repo_code=True,
    )
    result = sandbox.run(command, token)
    if result.cancelled:
        return TestRunResult(argv=argv, command=result, snapshot=None, error="pytest was cancelled")
    if result.timed_out:
        return TestRunResult(
            argv=argv,
            command=result,
            snapshot=None,
            error=f"pytest timed out after {result.duration_ms} ms",
        )
    if result.error is not None:
        return TestRunResult(argv=argv, command=result, snapshot=None, error=result.error)

    summary = parse_junit_xml(junit_path)
    if summary is None:
        detail = _excerpt(result.stderr or result.stdout)
        if result.exit_code == 5:
            reason = "pytest collected no tests; test coverage is unverified"
        else:
            reason = f"pytest output could not be parsed: {detail}"
        return TestRunResult(argv=argv, command=result, snapshot=None, error=reason)

    snapshot = snapshot_from_summary(
        summary, command=argv, exit_code=result.exit_code, duration_ms=result.duration_ms
    )
    if snapshot.total == 0:
        return TestRunResult(
            argv=argv,
            command=result,
            snapshot=snapshot,
            error="pytest reported zero test cases",
        )
    return TestRunResult(argv=argv, command=result, snapshot=snapshot, error=None)
