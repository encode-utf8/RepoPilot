"""Container isolated sandbox executor.

Remote writes (creating a branch and a Draft PR) are only allowed after the
repository's own code has run inside a container. This executor builds a
``docker run`` invocation with:

* ``--network=none`` (no egress for untrusted code),
* a read-only bind mount of the checkout, with separate writable mounts for the
  result artifacts and the isolated ``HOME``/``TMP``,
* ``--read-only`` root filesystem, ``--tmpfs /tmp``, ``--cap-drop ALL``,
  ``no-new-privileges``, a non root ``--user`` and memory/CPU/PID limits,
* an explicit, credential free environment (never the host environment).

Only the small, well known set of paths the pipeline itself constructs is
translated into the container namespace; anything else is rejected, so a command
can never reach outside the mounted workspace.
"""

from __future__ import annotations

import os
import shutil
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
from repopilot.execution.sandbox import SandboxCommand
from repopilot.execution.workspace import Workspace

MOUNT_REPO = "/repo"
MOUNT_ARTIFACTS = "/artifacts"
MOUNT_HOME = "/home/repopilot"

#: Non sensitive host variables worth forwarding when present.
_FORWARDED_ENV = ("LANG", "LC_ALL", "LC_CTYPE")
#: Deterministic variables injected into every container process.
_FIXED_ENV = {
    "HOME": MOUNT_HOME,
    "TMPDIR": "/tmp",
    "TEMP": "/tmp",
    "TMP": "/tmp",
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/bin",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "NO_COLOR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    "PIP_NO_COLOR": "1",
}


class DockerRunner(Protocol):
    """Executes a fully built ``docker`` argv list on the host."""

    def available(self) -> bool:
        """Return ``True`` when the container runtime can be invoked."""

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None,
        timeout: float,
        max_output_bytes: int,
        token: CancellationToken,
    ) -> CommandResult:
        """Run the docker CLI with timeout, output cap and cancellation."""


class SubprocessDockerRunner:
    """Runs the ``docker`` CLI as a hardened local subprocess."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        return shutil.which(self._settings.docker_executable) is not None

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None,
        timeout: float,
        max_output_bytes: int,
        token: CancellationToken,
    ) -> CommandResult:
        # The docker CLI inherits only the allowlisted host env, so the
        # container runtime never sees GitHub credentials either.
        env = build_sandbox_env(self._settings)
        return run_command(
            argv,
            cwd=cwd,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            env=env,
            token=token,
        )


@dataclass(frozen=True)
class _Mount:
    host: Path
    container: str
    read_only: bool


class ContainerSandboxExecutor:
    """``SandboxExecutor`` that runs repository code inside a container."""

    isolation_kind = "container"

    def __init__(
        self,
        settings: Settings,
        workspace: Workspace,
        *,
        trusted_source: bool,
        runner: DockerRunner | None = None,
    ) -> None:
        self._settings = settings
        self._workspace = workspace
        self._trusted_source = trusted_source
        self._code_execution_enabled = bool(
            trusted_source or settings.allow_untrusted_code_execution
        )
        self._runner: DockerRunner = runner or SubprocessDockerRunner(settings)
        self._mounts = (
            _Mount(workspace.repo_dir.resolve(), MOUNT_REPO, True),
            _Mount(workspace.artifacts_dir.resolve(), MOUNT_ARTIFACTS, False),
            _Mount(workspace.sandbox_dir.resolve(), MOUNT_HOME, False),
        )

    # -- properties ------------------------------------------------------------

    @property
    def code_execution_enabled(self) -> bool:
        return self._code_execution_enabled

    def available(self) -> bool:
        return self._runner.available()

    # -- path translation ------------------------------------------------------

    def _translate(self, host: str | Path) -> str:
        candidate = Path(os.path.abspath(str(host)))
        for mount in self._mounts:
            if candidate == mount.host or mount.host in candidate.parents:
                relative = candidate.relative_to(mount.host)
                if not relative.parts:
                    return mount.container
                return f"{mount.container}/{relative.as_posix()}"
        raise WorkspaceError(f"path escapes the container mounts: {host}")

    def _translate_token(self, token: str) -> str:
        interpreter = self._settings.python_executable
        if interpreter and token == interpreter:
            return "python"
        if interpreter and token.startswith(f"{interpreter} "):  # pragma: no cover
            return token.replace(interpreter, "python", 1)
        if token.startswith("-") and "=" in token:
            flag, _, value = token.partition("=")
            if os.path.isabs(value):
                return f"{flag}={self._translate(value)}"
            return token
        if os.path.isabs(token):
            return self._translate(token)
        return token

    # -- argv construction -----------------------------------------------------

    def _container_env(self) -> dict[str, str]:
        env = dict(_FIXED_ENV)
        for name in _FORWARDED_ENV:
            value = os.environ.get(name)
            if value:
                env[name] = value
        return env

    def build_argv(self, command: SandboxCommand) -> list[str]:
        """Build the ``docker run`` argument list for ``command`` (no execution)."""

        settings = self._settings
        cwd = self._translate(command.cwd or self._workspace.repo_dir)
        argv: list[str] = [
            settings.docker_executable,
            "run",
            "--rm",
            "--init",
            "--network=none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            settings.container_user,
            "--memory",
            settings.container_memory,
            "--cpus",
            settings.container_cpus,
            "--pids-limit",
            str(settings.container_pids_limit),
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={settings.container_tmp_size}",
        ]
        for mount in self._mounts:
            mode = "ro" if mount.read_only else "rw"
            argv += ["-v", f"{mount.host}:{mount.container}:{mode}"]
        for name, value in self._container_env().items():
            argv += ["-e", f"{name}={value}"]
        argv += ["-w", cwd, settings.docker_image]
        argv += [self._translate_token(token) for token in command.argv]
        return argv

    # -- execution -------------------------------------------------------------

    def run(self, command: SandboxCommand, token: CancellationToken) -> CommandResult:
        if command.executes_repo_code and not self._code_execution_enabled:
            raise CodeExecutionDeniedError(
                "repository code execution is disabled for untrusted sources "
                f"(blocked: {format_command(command.argv)})"
            )
        argv = self.build_argv(command)
        requested = command.timeout or self._settings.command_timeout_seconds
        timeout = min(requested, self._settings.container_timeout_seconds)
        max_output_bytes = command.max_output_bytes or self._settings.max_output_bytes
        return self._runner.run(
            argv,
            cwd=self._workspace.root,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            token=token,
        )
