"""Ruff static analysis checker."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from repopilot.checkers.base import CheckContext, issue_id, normalize_rel_path, truncate
from repopilot.execution.sandbox import SandboxCommand
from repopilot.models import (
    CheckerResult,
    CheckerStatus,
    Issue,
    Origin,
    Severity,
    VerificationState,
)

_SEVERITY_BY_RULE: dict[str, Severity] = {
    "E999": Severity.critical,
    "F811": Severity.high,
    "F821": Severity.high,
    "F822": Severity.high,
    "F823": Severity.high,
    "B006": Severity.medium,
    "B008": Severity.medium,
    "E711": Severity.medium,
    "E712": Severity.medium,
    "E722": Severity.medium,
    "E402": Severity.low,
    "F401": Severity.low,
    "F541": Severity.low,
    "F841": Severity.low,
}
_SEVERITY_BY_PREFIX: dict[str, Severity] = {
    "E": Severity.medium,
    "F": Severity.medium,
    "W": Severity.low,
    "B": Severity.medium,
    "C": Severity.low,
    "S": Severity.high,
    "RUF": Severity.low,
    "SIM": Severity.low,
    "UP": Severity.low,
    "I": Severity.low,
    "N": Severity.low,
}
_PREFIX_RE = re.compile(r"^[A-Z]+")


def severity_for_rule(code: str | None) -> Severity:
    if not code:
        return Severity.medium
    if code in _SEVERITY_BY_RULE:
        return _SEVERITY_BY_RULE[code]
    match = _PREFIX_RE.match(code)
    prefix = match.group(0) if match else ""
    if prefix in _SEVERITY_BY_PREFIX:
        return _SEVERITY_BY_PREFIX[prefix]
    if prefix[:3] in _SEVERITY_BY_PREFIX:
        return _SEVERITY_BY_PREFIX[prefix[:3]]
    return Severity.medium


def _source_line(repo_dir: Path, relative: str | None, row: int | None) -> str | None:
    if not relative or not row or row < 1:
        return None
    path = repo_dir / relative
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle, start=1):
                if index == row:
                    return line.rstrip("\n")
    except OSError:
        return None
    return None


class RuffChecker:
    """Runs ``ruff check --output-format json`` and maps findings to issues."""

    name = "ruff"
    display_name = "Ruff static analysis"

    def is_applicable(self, context: CheckContext) -> bool:
        return context.project.is_python_project

    def run(self, context: CheckContext) -> CheckerResult:
        started = time.monotonic()
        argv = [
            *context.settings.ruff_command,
            "check",
            "--no-cache",
            "--output-format",
            "json",
            "--",
            ".",
        ]
        command = SandboxCommand(argv=argv, purpose="ruff check", cwd=context.repo_dir)
        result = context.sandbox.run(command, context.token)
        duration_ms = int((time.monotonic() - started) * 1000)
        common = {
            "name": self.name,
            "display_name": self.display_name,
            "duration_ms": duration_ms,
            "commands": [argv],
            "exit_code": result.exit_code,
            "required": True,
        }

        if result.cancelled:
            return CheckerResult(
                status=CheckerStatus.unavailable, notes=["ruff run was cancelled"], **common
            )
        if result.timed_out:
            return CheckerResult(
                status=CheckerStatus.unavailable,
                notes=[f"ruff timed out after {result.duration_ms} ms"],
                **common,
            )
        if result.error is not None:
            return CheckerResult(
                status=CheckerStatus.unavailable, error=result.error, notes=[result.error], **common
            )

        payload = self._parse(result.stdout)
        if payload is None:
            message = truncate(result.stderr or result.stdout, 600)
            return CheckerResult(
                status=CheckerStatus.error,
                error=f"ruff produced unparsable output: {message}",
                notes=[message],
                **common,
            )

        limit = context.settings.max_evidence_chars
        issues: list[Issue] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            relative = normalize_rel_path(item.get("filename"), context.workspace)
            location = item.get("location") or {}
            row = location.get("row")
            column = location.get("column")
            message = str(item.get("message", "")).strip()
            fix = item.get("fix")
            evidence = truncate(f"{code or 'ruff'}: {message}", limit)
            suggestion = (
                "Ruff can fix this automatically: run `ruff check --fix`."
                if isinstance(fix, dict)
                else "Review the reported rule and adjust the code accordingly."
            )
            issues.append(
                Issue(
                    issue_id=issue_id(self.name, "lint", code, relative, row, column, message),
                    issue_type="lint",
                    severity=severity_for_rule(code),
                    evidence=evidence,
                    source_tool=self.name,
                    origin=Origin.tool,
                    verification=VerificationState.confirmed,
                    file_path=relative,
                    line=row if isinstance(row, int) else None,
                    column=column if isinstance(column, int) else None,
                    rule_id=code,
                    fix_suggestion=suggestion,
                    metadata={
                        "fixable": isinstance(fix, dict),
                        "source_line": _source_line(
                            context.repo_dir, relative, row if isinstance(row, int) else None
                        ),
                    },
                )
            )

        by_rule: dict[str, int] = {}
        for issue in issues:
            key = issue.rule_id or "unknown"
            by_rule[key] = by_rule.get(key, 0) + 1

        return CheckerResult(
            status=CheckerStatus.findings if issues else CheckerStatus.ok,
            issues=issues,
            summary={
                "findings": len(issues),
                "by_rule": dict(sorted(by_rule.items())),
                "exit_code": result.exit_code,
            },
            **common,
        )

    @staticmethod
    def _parse(stdout: str) -> list | None:
        text = stdout.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, list) else None
