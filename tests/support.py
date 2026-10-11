"""Helpers shared by the test suite."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from repopilot.cancellation import CancellationToken
from repopilot.checkers.base import CheckContext
from repopilot.config import Settings
from repopilot.execution.git import RepoCheckout, clone_and_pin_repo
from repopilot.execution.process import CommandResult
from repopilot.execution.sandbox import LocalProcessSandboxExecutor, SandboxCommand
from repopilot.execution.workspace import Workspace, WorkspaceManager
from repopilot.logging_setup import RunLogger
from repopilot.scan.scanner import scan_project

TERMINAL_STATUSES = {"succeeded", "failed", "needs_review", "cancelled"}


class FakeSandbox:
    """Sandbox stub returning canned results so checkers can be tested offline."""

    def __init__(
        self,
        responder: Callable[[SandboxCommand], CommandResult],
        *,
        code_execution_enabled: bool = True,
    ) -> None:
        self._responder = responder
        self._code_execution_enabled = code_execution_enabled
        self.commands: list[SandboxCommand] = []

    @property
    def code_execution_enabled(self) -> bool:
        return self._code_execution_enabled

    def run(self, command: SandboxCommand, token: CancellationToken) -> CommandResult:
        self.commands.append(command)
        return self._responder(command)


def command_result(
    argv: list[str],
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    error: str | None = None,
) -> CommandResult:
    return CommandResult(
        argv=list(argv),
        cwd=None,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
        error=error,
    )


@dataclass
class PreparedRepo:
    workspace: Workspace
    checkout: RepoCheckout
    sandbox: LocalProcessSandboxExecutor
    project: object
    context: CheckContext

    @property
    def repo_dir(self) -> Path:
        return self.workspace.repo_dir


def prepare_repo(
    settings: Settings,
    source: str | Path,
    *,
    task_id: str = "task_support",
    trusted: bool = True,
    sandbox=None,
    base_commit: str | None = None,
) -> PreparedRepo:
    """Clone ``source`` into a fresh workspace and build a check context."""

    workspace = WorkspaceManager(settings).create(task_id)
    token = CancellationToken()
    real_sandbox = LocalProcessSandboxExecutor(settings, workspace, trusted_source=trusted)
    checkout = clone_and_pin_repo(
        str(source),
        workspace.repo_dir,
        base_commit=base_commit,
        base_branch=None,
        settings=settings,
        sandbox=real_sandbox,
        token=token,
    )
    project = scan_project(workspace.repo_dir, settings)
    logger = RunLogger(settings.log_dir, task_id, "run_support")
    context = CheckContext(
        task_id=task_id,
        run_id="run_support",
        workspace=workspace,
        project=project,
        sandbox=sandbox or real_sandbox,
        token=token,
        settings=settings,
        logger=logger,
        trusted_source=trusted,
    )
    return PreparedRepo(
        workspace=workspace,
        checkout=checkout,
        sandbox=real_sandbox,
        project=project,
        context=context,
    )


def wait_for_terminal(client, task_id: str, timeout: float = 120.0) -> dict:
    """Poll ``GET /api/tasks/{id}`` until the task reaches a terminal status."""

    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in TERMINAL_STATUSES:
            return last
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish within {timeout}s; last={last}")
