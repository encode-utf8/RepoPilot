"""``SandboxExecutor`` boundary and its local process implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.errors import CodeExecutionDeniedError, WorkspaceError
from repopilot.execution.process import (
    CommandResult,
    build_sandbox_env,
    format_command,
    run_command,
)
from repopilot.execution.workspace import Workspace


@dataclass
class SandboxCommand:
    """One command to execute inside the sandbox."""

    argv: list[str]
    purpose: str
    cwd: Path | None = None
    timeout: float | None = None
    max_output_bytes: int | None = None
    #: True when the command may execute repository controlled code.
    executes_repo_code: bool = False


class SandboxExecutor(Protocol):
    """Isolation boundary for all subprocess execution."""

    @property
    def code_execution_enabled(self) -> bool:
        """Whether repository code may be executed for this task."""

    def run(self, command: SandboxCommand, token: CancellationToken) -> CommandResult:
        """Execute a command with timeout, output cap and environment isolation."""


class LocalProcessSandboxExecutor:
    """Local process sandbox.

    This is a hardened local runner, not a container. It never passes host
    credentials to children and refuses repository code execution unless the
    source is trusted. Container isolation is a later phase (see open questions).
    """

    def __init__(self, settings: Settings, workspace: Workspace, *, trusted_source: bool) -> None:
        self._settings = settings
        self._workspace = workspace
        self._trusted_source = trusted_source
        self._code_execution_enabled = bool(
            trusted_source or settings.allow_untrusted_code_execution
        )

    @property
    def code_execution_enabled(self) -> bool:
        return self._code_execution_enabled

    def _resolve_cwd(self, cwd: Path | None) -> Path:
        target = (cwd or self._workspace.repo_dir).resolve()
        root = self._workspace.root.resolve()
        if target != root and root not in target.parents:
            raise WorkspaceError(f"command working directory escapes workspace: {target}")
        return target

    def run(self, command: SandboxCommand, token: CancellationToken) -> CommandResult:
        if command.executes_repo_code and not self._code_execution_enabled:
            raise CodeExecutionDeniedError(
                "repository code execution is disabled for untrusted sources "
                f"(blocked: {format_command(command.argv)})"
            )
        cwd = self._resolve_cwd(command.cwd)
        requested = command.timeout or self._settings.command_timeout_seconds
        timeout = min(requested, self._settings.max_command_timeout_seconds)
        max_output_bytes = command.max_output_bytes or self._settings.max_output_bytes
        env = build_sandbox_env(
            self._settings,
            isolate_root=self._workspace.sandbox_dir if command.executes_repo_code else None,
        )
        return run_command(
            command.argv,
            cwd=cwd,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            env=env,
            token=token,
        )
