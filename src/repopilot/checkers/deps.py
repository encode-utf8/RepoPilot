"""Dependency risk scanning checker.

Tool selection: ``pip-audit`` is preferred over the alternatives because it is
maintained under the PyPA umbrella, queries the public OSV/PyPI advisory data
directly (no account or API token needed, so no credential ever enters the
sandbox), emits machine readable JSON, and can audit a requirements file without
mutating the environment. ``safety`` needs a paid/authenticated advisory API for
full results, ``osv-scanner`` is a separate Go binary, and ``pip list --outdated``
is not a vulnerability database at all.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from repopilot.checkers.base import CheckContext, issue_id, truncate
from repopilot.execution.sandbox import SandboxCommand
from repopilot.models import (
    CheckerResult,
    CheckerStatus,
    Issue,
    Origin,
    Severity,
    VerificationState,
    dedupe_issues,
)

_NETWORK_HINTS = (
    "connection",
    "could not connect",
    "temporary failure in name resolution",
    "name or service not known",
    "no matching distribution",
    "proxy",
    "ssl",
    "timed out",
    "timeout",
    "failed to resolve",
    "network",
)
_NAME_SPLIT_RE = re.compile(r"[<>=!~;\[\s]")


def _parse(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _explain_failure(stderr: str, stdout: str) -> str:
    combined = f"{stderr}\n{stdout}".strip()
    lowered = combined.lower()
    if any(hint in lowered for hint in _NETWORK_HINTS):
        return (
            "pip-audit could not reach the vulnerability database "
            "(offline or restricted network); dependency risk is unverified"
        )
    return truncate(combined, 400) or "pip-audit produced no usable output"


def _requirement_line(path: Path, package: str) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    needle = package.lower()
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        name = _NAME_SPLIT_RE.split(stripped, maxsplit=1)[0].strip().lower()
        if name == needle:
            return index
    return None


class DependencyAuditChecker:
    """Runs ``pip-audit`` against detected dependency manifests."""

    name = "dependency-audit"
    display_name = "Dependency vulnerability audit (pip-audit)"

    def is_applicable(self, context: CheckContext) -> bool:
        return bool(context.project.requirements_files) or (
            "pyproject.toml" in context.project.dependency_manifests
        )

    def run(self, context: CheckContext) -> CheckerResult:
        started = time.monotonic()
        targets: list[tuple[Path, str | None]] = [
            (context.repo_dir / relative, relative)
            for relative in context.project.requirements_files
        ]
        if not targets:
            generated = self._generate_requirements(context)
            if generated is None:
                return CheckerResult(
                    name=self.name,
                    display_name=self.display_name,
                    status=CheckerStatus.not_applicable,
                    applicable=False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    notes=["no requirements file or project dependencies were found"],
                )
            targets = [(generated, None)]

        required = context.settings.require_dependency_audit
        issues: list[Issue] = []
        commands: list[list[str]] = []
        unavailable: list[str] = []
        exit_code: int | None = None
        limit = context.settings.max_evidence_chars

        for target, label in targets:
            argv = [
                *context.settings.pip_audit_command,
                "-r",
                str(target),
                "--format",
                "json",
                "--progress-spinner",
                "off",
            ]
            if not context.trusted_source:
                # Do not resolve (and therefore download) transitive dependencies
                # for repositories we do not control.
                argv.append("--no-deps")
            commands.append(argv)
            command = SandboxCommand(
                argv=argv,
                purpose=f"pip-audit {target.name}",
                cwd=context.repo_dir,
                timeout=context.settings.dependency_audit_timeout_seconds,
            )
            result = context.sandbox.run(command, context.token)
            exit_code = result.exit_code
            if result.cancelled:
                unavailable.append("pip-audit was cancelled")
                break
            if result.timed_out:
                unavailable.append(f"pip-audit timed out after {result.duration_ms} ms")
                continue
            if result.error is not None:
                unavailable.append(result.error)
                continue
            payload = _parse(result.stdout)
            if payload is None:
                unavailable.append(_explain_failure(result.stderr, result.stdout))
                continue
            issues.extend(self._issues_from_payload(payload, target, label, limit))

        common = {
            "name": self.name,
            "display_name": self.display_name,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "commands": commands,
            "exit_code": exit_code,
            "required": required,
        }
        if unavailable and not issues:
            return CheckerResult(
                status=CheckerStatus.unavailable,
                notes=unavailable,
                error="; ".join(unavailable),
                summary={"unavailable": True},
                **common,
            )
        notes = [f"partial audit: {'; '.join(unavailable)}"] if unavailable else []
        return CheckerResult(
            status=CheckerStatus.findings if issues else CheckerStatus.ok,
            issues=issues,
            notes=notes,
            summary={"vulnerabilities": len(issues), "audited_files": len(targets)},
            **common,
        )

    def _issues_from_payload(
        self,
        payload: dict,
        target: Path,
        label: str | None,
        limit: int,
    ) -> list[Issue]:
        issues: list[Issue] = []
        for dependency in payload.get("dependencies") or []:
            if not isinstance(dependency, dict):
                continue
            name = str(dependency.get("name") or "unknown")
            version = str(dependency.get("version") or "unknown")
            for vuln in dependency.get("vulns") or []:
                if not isinstance(vuln, dict):
                    continue
                vuln_id = str(vuln.get("id") or "unknown-vulnerability")
                fix_versions = [str(item) for item in (vuln.get("fix_versions") or [])]
                description = " ".join(str(vuln.get("description") or "").split())
                evidence = f"{name} {version} is affected by {vuln_id}"
                if description:
                    evidence = f"{evidence}: {description}"
                suggestion = (
                    f"Upgrade {name} to >= {', '.join(fix_versions)} and re-run tests."
                    if fix_versions
                    else f"No fixed version is published for {vuln_id}; review the advisory."
                )
                issues.append(
                    Issue(
                        issue_id=issue_id(self.name, "vulnerability", name, version, vuln_id),
                        issue_type="dependency_vulnerability",
                        severity=Severity.high,
                        evidence=truncate(evidence, limit),
                        source_tool=self.name,
                        origin=Origin.tool,
                        verification=VerificationState.confirmed,
                        file_path=label,
                        line=_requirement_line(target, name) if label else None,
                        rule_id=vuln_id,
                        fix_suggestion=suggestion,
                        metadata={
                            "package": name,
                            "installed_version": version,
                            "fix_versions": fix_versions,
                            "aliases": [str(item) for item in (vuln.get("aliases") or [])],
                            "severity_source": "default-high; the advisory feed exposes no CVSS",
                        },
                    )
                )
        return dedupe_issues(issues)

    @staticmethod
    def _generate_requirements(context: CheckContext) -> Path | None:
        pyproject = context.repo_dir / "pyproject.toml"
        if not pyproject.is_file():
            return None
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        project = data.get("project")
        if not isinstance(project, dict):
            return None
        requirements: list[str] = [str(item) for item in (project.get("dependencies") or [])]
        for extra in (project.get("optional-dependencies") or {}).values():
            requirements.extend(str(item) for item in (extra or []))
        if not requirements:
            return None
        path = context.workspace.artifacts_dir / "generated-requirements.txt"
        path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        return path
