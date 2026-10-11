"""``Checker`` boundary shared by every deterministic checker.

Adding a checker (or replacing one) only requires implementing this protocol and
registering it; the pipeline and report layers stay unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.execution.sandbox import SandboxExecutor
from repopilot.execution.workspace import Workspace
from repopilot.logging_setup import RunLogger
from repopilot.models import CheckerResult, ProjectProfile


@dataclass
class CheckContext:
    """Everything a checker needs to do its job."""

    task_id: str
    run_id: str
    workspace: Workspace
    project: ProjectProfile
    sandbox: SandboxExecutor
    token: CancellationToken
    settings: Settings
    logger: RunLogger
    trusted_source: bool = False

    @property
    def repo_dir(self) -> Path:
        return self.workspace.repo_dir

    @property
    def code_execution_enabled(self) -> bool:
        return self.sandbox.code_execution_enabled


class Checker(Protocol):
    """Interface implemented by Ruff, pytest, dependency and heuristic checkers."""

    name: str
    display_name: str

    def is_applicable(self, context: CheckContext) -> bool:
        """Whether this checker should run for the scanned project."""

    def run(self, context: CheckContext) -> CheckerResult:
        """Execute the checker and return a structured result."""


def issue_id(tool: str, kind: str, *parts: object) -> str:
    """Deterministic identifier so repeated runs produce comparable reports."""

    payload = "|".join([tool, kind, *(str(part) for part in parts)])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"{tool}:{kind}:{digest[:12]}"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def normalize_rel_path(value: str | None, workspace: Workspace) -> str | None:
    """Normalise a tool reported path into a repo relative POSIX path."""

    if not value:
        return None
    raw = value.replace("\\", "/")
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(workspace.repo_dir.resolve()).as_posix()
        except ValueError:
            return candidate.name
    if raw.startswith("./"):
        return raw[2:]
    return raw
