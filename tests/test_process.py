"""Process runner hardening: no shell, timeout, output cap, env allowlist, cancel."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.execution.process import build_sandbox_env, run_command


def _python(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def _run(tmp_path: Path, argv: list[str], **kwargs):
    env = kwargs.pop("env", build_sandbox_env(Settings()))
    return run_command(
        argv,
        cwd=tmp_path,
        timeout=kwargs.pop("timeout", 30.0),
        max_output_bytes=kwargs.pop("max_output_bytes", 100_000),
        env=env,
        **kwargs,
    )


def test_output_is_captured(tmp_path: Path) -> None:
    result = _run(tmp_path, _python("print('hello stdout')"))
    assert result.exit_code == 0
    assert result.ok
    assert "hello stdout" in result.stdout


def test_output_is_capped(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _python("import sys; sys.stdout.write('x' * 50_000)"),
        max_output_bytes=1_000,
    )
    assert result.truncated is True
    assert len(result.stdout) == 1_000


def test_timeout_terminates_the_process(tmp_path: Path) -> None:
    started = time.monotonic()
    result = _run(tmp_path, _python("import time; time.sleep(30)"), timeout=1.0)
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert not result.ok
    assert elapsed < 20


def test_arguments_are_not_shell_interpreted(tmp_path: Path) -> None:
    payload = "; echo pwned && echo oops"
    result = _run(
        tmp_path,
        [*_python("import sys; sys.stdout.write(sys.argv[1])"), payload],
    )
    assert result.exit_code == 0
    assert result.stdout == payload


def test_env_allowlist_drops_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 30)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret-value")
    env = build_sandbox_env(Settings())
    env["REPOPILOT_TEST_KEEP"] = "kept"
    result = _run(
        tmp_path,
        _python("import json, os; print(json.dumps(dict(os.environ)))"),
        env=env,
    )
    assert result.exit_code == 0
    observed = json.loads(result.stdout)
    assert "GITHUB_TOKEN" not in observed
    assert "AWS_SECRET_ACCESS_KEY" not in observed
    assert observed["GIT_TERMINAL_PROMPT"] == "0"
    assert observed["REPOPILOT_TEST_KEEP"] == "kept"


def test_cancellation_kills_a_running_command(tmp_path: Path) -> None:
    token = CancellationToken()
    timer = threading.Timer(0.6, token.cancel, args=("test cancellation",))
    timer.start()
    try:
        started = time.monotonic()
        result = _run(tmp_path, _python("import time; time.sleep(30)"), token=token)
    finally:
        timer.cancel()
    assert result.cancelled is True
    assert time.monotonic() - started < 20


def test_missing_executable_reports_error(tmp_path: Path) -> None:
    result = _run(tmp_path, ["definitely-not-a-real-binary-repopilot"])
    assert result.error is not None
    assert result.exit_code is None
    assert not result.ok


def test_empty_argv_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _run(tmp_path, [])
