"""Sandboxed execution primitives (workspace, process runner, git)."""

from __future__ import annotations

from repopilot.execution.git import RepoCheckout, classify_source, clone_and_pin_repo
from repopilot.execution.process import CommandResult, build_sandbox_env, run_command
from repopilot.execution.sandbox import (
    LocalProcessSandboxExecutor,
    SandboxCommand,
    SandboxExecutor,
)
from repopilot.execution.workspace import Workspace, WorkspaceManager

__all__ = [
    "CommandResult",
    "LocalProcessSandboxExecutor",
    "RepoCheckout",
    "SandboxCommand",
    "SandboxExecutor",
    "Workspace",
    "WorkspaceManager",
    "build_sandbox_env",
    "classify_source",
    "clone_and_pin_repo",
    "run_command",
]
