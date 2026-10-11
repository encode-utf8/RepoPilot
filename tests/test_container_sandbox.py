"""Tests for container isolation and the sandbox factory."""

from __future__ import annotations

from dataclasses import replace

import pytest

from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.errors import (
    CodeExecutionDeniedError,
    InvalidRequestError,
    SandboxUnavailableError,
    WorkspaceError,
)
from repopilot.execution.container import ContainerSandboxExecutor
from repopilot.execution.process import CommandResult
from repopilot.execution.sandbox import (
    LocalProcessSandboxExecutor,
    SandboxCommand,
    build_sandbox,
)
from repopilot.execution.workspace import WorkspaceManager


class RecordingRunner:
    """A ``DockerRunner`` double that records argv and never launches docker."""

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.calls: list[list[str]] = []

    def available(self) -> bool:
        return self._available

    def run(self, argv, *, cwd, timeout, max_output_bytes, token) -> CommandResult:
        self.calls.append(list(argv))
        return CommandResult(
            argv=list(argv),
            cwd=str(cwd) if cwd is not None else None,
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=1,
        )


def _workspace(settings: Settings, task_id: str = "task-container"):
    return WorkspaceManager(settings).create(task_id)


def test_build_argv_is_hardened(settings: Settings) -> None:
    workspace = _workspace(settings)
    executor = ContainerSandboxExecutor(
        settings, workspace, trusted_source=True, runner=RecordingRunner()
    )
    argv = executor.build_argv(
        SandboxCommand(
            argv=[settings.python_executable, "-m", "pytest", "-q"],
            purpose="tests",
            cwd=workspace.repo_dir,
            executes_repo_code=True,
        )
    )

    assert argv[0] == settings.docker_executable
    assert argv[1] == "run"
    for flag in ("--network=none", "--read-only", "--pids-limit", "--memory", "--cpus"):
        assert flag in argv
    assert "ALL" in argv
    assert "no-new-privileges" in argv

    assert f"{workspace.repo_dir.resolve()}:/repo:ro" in argv
    assert f"{workspace.artifacts_dir.resolve()}:/artifacts:rw" in argv
    assert f"{workspace.sandbox_dir.resolve()}:/home/repopilot:rw" in argv

    # the interpreter is rewritten to the container's python and the workdir is
    # the container mount, never the host path
    assert argv[-4:] == ["python", "-m", "pytest", "-q"]
    assert "/repo" in argv
    assert str(workspace.repo_dir.resolve()) not in argv[-4:]


def test_container_env_never_carries_host_credentials(settings: Settings, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "B" * 36)
    monkeypatch.setenv("REPOPILOT_GITHUB_TOKEN", "ghp_" + "C" * 36)
    monkeypatch.setenv("LC_ALL", "C.UTF-8")

    workspace = _workspace(settings)
    executor = ContainerSandboxExecutor(
        settings, workspace, trusted_source=True, runner=RecordingRunner()
    )
    argv = executor.build_argv(
        SandboxCommand(
            argv=[settings.python_executable, "-c", "pass"],
            purpose="x",
            cwd=workspace.repo_dir,
        )
    )
    env_pairs = [argv[i + 1] for i, token in enumerate(argv) if token == "-e"]
    joined = " ".join(env_pairs)
    assert "ghp_" not in joined
    assert "GITHUB_TOKEN" not in joined
    assert "HOME=/home/repopilot" in env_pairs
    assert "LC_ALL=C.UTF-8" in env_pairs


def test_rejected_shapes_of_path_translation(settings: Settings) -> None:
    workspace = _workspace(settings)
    executor = ContainerSandboxExecutor(
        settings, workspace, trusted_source=True, runner=RecordingRunner()
    )
    with pytest.raises(WorkspaceError):
        executor.build_argv(
            SandboxCommand(argv=[str(settings.workspace_root / "elsewhere")], purpose="x")
        )


def test_untrusted_repo_code_is_denied(settings: Settings) -> None:
    workspace = _workspace(settings)
    executor = ContainerSandboxExecutor(
        settings, workspace, trusted_source=False, runner=RecordingRunner()
    )
    with pytest.raises(CodeExecutionDeniedError):
        executor.run(
            SandboxCommand(
                argv=[settings.python_executable, "-c", "pass"],
                purpose="x",
                cwd=workspace.repo_dir,
                executes_repo_code=True,
            ),
            CancellationToken(),
        )


def test_run_translates_and_delegates_to_runner(settings: Settings) -> None:
    workspace = _workspace(settings)
    runner = RecordingRunner()
    executor = ContainerSandboxExecutor(settings, workspace, trusted_source=True, runner=runner)
    result = executor.run(
        SandboxCommand(
            argv=[settings.python_executable, "-m", "pytest"],
            purpose="tests",
            cwd=workspace.repo_dir,
            timeout=5.0,
        ),
        CancellationToken(),
    )
    assert result.exit_code == 0
    assert runner.calls and runner.calls[0][-2:] == ["python", "-m", "pytest"][-2:]


def test_build_sandbox_local_by_default(settings: Settings) -> None:
    executor = build_sandbox(settings, _workspace(settings), trusted_source=True)
    assert isinstance(executor, LocalProcessSandboxExecutor)
    assert executor.isolation_kind == "local-process"


def test_build_sandbox_container_requires_docker(settings: Settings) -> None:
    container = replace(settings, sandbox_backend="container")
    with pytest.raises(SandboxUnavailableError):
        build_sandbox(
            container,
            _workspace(settings),
            trusted_source=True,
            docker_runner=RecordingRunner(available=False),
        )


def test_build_sandbox_container_available(settings: Settings) -> None:
    container = replace(settings, sandbox_backend="container")
    executor = build_sandbox(
        container,
        _workspace(settings),
        trusted_source=True,
        docker_runner=RecordingRunner(available=True),
    )
    assert executor.isolation_kind == "container"


def test_build_sandbox_rejects_unknown_backend(settings: Settings) -> None:
    with pytest.raises(InvalidRequestError):
        build_sandbox(
            replace(settings, sandbox_backend="nope"),
            _workspace(settings),
            trusted_source=True,
        )
