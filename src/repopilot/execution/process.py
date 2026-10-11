"""Hardened subprocess execution primitives.

Rules enforced here, once, for every external command in the system:

* commands are always passed as an argument list (``shell=True`` is never used);
* every command has a timeout and a hard output size cap;
* the child environment is an allowlist, so host credentials never leak in;
* a cancellation token kills the process tree within one poll interval.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from repopilot.cancellation import CancellationToken
from repopilot.config import Settings

_GIT_CONFIG_LOCK = threading.Lock()
_GIT_CONFIG_PATH: Path | None = None

#: Deterministic, credential free variables injected into every child process.
_INJECTED_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_PAGER": "cat",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_INPUT": "1",
    "PIP_NO_COLOR": "1",
    "PIP_PROGRESS_BAR": "off",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
    "TERM": "dumb",
}


def _empty_git_config() -> Path:
    """Return a path to an empty gitconfig, used to neutralise host config."""

    global _GIT_CONFIG_PATH
    with _GIT_CONFIG_LOCK:
        if _GIT_CONFIG_PATH is None or not _GIT_CONFIG_PATH.exists():
            directory = Path(tempfile.mkdtemp(prefix="repopilot-gitconfig-"))
            path = directory / "empty.gitconfig"
            path.write_text("", encoding="utf-8")
            _GIT_CONFIG_PATH = path
        return _GIT_CONFIG_PATH


def build_sandbox_env(
    settings: Settings,
    *,
    isolate_root: Path | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the child environment from an allowlist.

    ``isolate_root`` redirects ``HOME``/``TEMP`` into the task workspace, which is
    used for commands that execute repository controlled code.
    """

    env = {name: os.environ[name] for name in settings.env_allowlist if name in os.environ}
    env.update(_INJECTED_ENV)
    env["GIT_CONFIG_GLOBAL"] = str(_empty_git_config())
    if isolate_root is not None:
        home = isolate_root / "home"
        temp = isolate_root / "tmp"
        home.mkdir(parents=True, exist_ok=True)
        temp.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["TEMP"] = str(temp)
        env["TMP"] = str(temp)
        env["TMPDIR"] = str(temp)
    if extra:
        env.update({key: str(value) for key, value in extra.items()})
    return env


def format_command(argv: list[str]) -> str:
    """Render a command for logs without ever invoking a shell."""

    return " ".join(f'"{part}"' if (" " in part or not part) else part for part in argv)


@dataclass
class CommandResult:
    """Outcome of one executed command."""

    argv: list[str]
    cwd: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    output_bytes: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.error is None and not self.timed_out and not self.cancelled and self.exit_code == 0
        )

    def command_line(self) -> str:
        return format_command(self.argv)

    def combined_output(self) -> str:
        if self.stdout and self.stderr:
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout or self.stderr

    def failure_summary(self, limit: int = 400) -> str:
        if self.error:
            return self.error
        if self.timed_out:
            return f"command timed out after {self.duration_ms} ms"
        if self.cancelled:
            return "command cancelled"
        text = (self.stderr or self.stdout).strip()
        if len(text) > limit:
            return f"{text[:limit]}..."
        return text or f"exit code {self.exit_code}"


def _terminate(process: subprocess.Popen[bytes], grace_seconds: float = 10.0) -> None:
    """Kill the process and its children, escalating if the kill is ignored."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
            shell=False,
        )
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()


def _read_capped(handle: IO[bytes], limit: int) -> tuple[str, bool, int]:
    handle.seek(0)
    data = handle.read(limit + 1)
    truncated = len(data) > limit
    if truncated:
        data = data[:limit]
    return data.decode("utf-8", errors="replace"), truncated, len(data)


def run_command(
    argv: list[str],
    *,
    cwd: Path | None,
    timeout: float,
    max_output_bytes: int,
    env: dict[str, str],
    token: CancellationToken | None = None,
    poll_interval: float = 0.05,
) -> CommandResult:
    """Run ``argv`` without a shell, enforcing timeout, output cap and cancellation."""

    if not argv:
        msg = "argv must not be empty"
        raise ValueError(msg)

    started = time.monotonic()
    cwd_str = str(cwd) if cwd is not None else None
    creationflags = 0
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    # The handles are closed in the ``finally`` block below; the descriptor is
    # deliberately anonymous so nothing is left behind on disk.
    out_handle = tempfile.TemporaryFile()  # noqa: SIM115
    err_handle = tempfile.TemporaryFile()  # noqa: SIM115
    try:
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd_str,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out_handle,
                stderr=err_handle,
                shell=False,
                creationflags=creationflags,
                close_fds=True,
                **popen_kwargs,
            )
        except FileNotFoundError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return CommandResult(
                argv=list(argv),
                cwd=cwd_str,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=duration_ms,
                error=f"executable not found: {argv[0]} ({exc})",
            )
        except OSError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return CommandResult(
                argv=list(argv),
                cwd=cwd_str,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=duration_ms,
                error=f"failed to start command: {exc}",
            )

        deadline = started + timeout if timeout and timeout > 0 else None
        timed_out = False
        cancelled = False
        while True:
            if process.poll() is not None:
                break
            if token is not None and token.is_cancelled():
                cancelled = True
                _terminate(process)
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                _terminate(process)
                break
            time.sleep(poll_interval)

        exit_code = process.poll()
        stdout, out_truncated, out_bytes = _read_capped(out_handle, max_output_bytes)
        stderr, err_truncated, err_bytes = _read_capped(err_handle, max_output_bytes)
        duration_ms = int((time.monotonic() - started) * 1000)
        return CommandResult(
            argv=list(argv),
            cwd=cwd_str,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            cancelled=cancelled,
            truncated=out_truncated or err_truncated,
            output_bytes=out_bytes + err_bytes,
        )
    finally:
        out_handle.close()
        err_handle.close()
