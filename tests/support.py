"""Helpers shared by the test suite."""

from __future__ import annotations

import difflib
import json
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


# --- Phase 2 repair helpers -------------------------------------------------

FIXTURE_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
STATS_PATH = "src/statskit/stats.py"
STATS_TEST_PATH = "tests/test_stats.py"
STATS_TARGET_TEST = "tests/test_stats.py::test_mean_of_values"
_REPAIR_FIXTURE = "py_stats_repair"


def start_task(client, payload: dict) -> str:
    """Create a task over the API and return its id (asserting the 202)."""

    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def complete_task(client, payload: dict, timeout: float = 180.0) -> dict:
    """Create a task and poll until it reaches a terminal status."""

    return wait_for_terminal(client, start_task(client, payload), timeout=timeout)


def _unified_diff(old: str, new: str, path: str, *, tofile: str | None = None) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=tofile or f"b/{path}",
        )
    )


def _stats_template() -> str:
    return (FIXTURE_TEMPLATE_DIR / _REPAIR_FIXTURE / STATS_PATH).read_text(encoding="utf-8")


def _stats_test_template() -> str:
    return (FIXTURE_TEMPLATE_DIR / _REPAIR_FIXTURE / STATS_TEST_PATH).read_text(encoding="utf-8")


def stats_fix_diff() -> str:
    """The correct one line fix for ``mean``."""

    old = _stats_template()
    new = old.replace("sum(values) / (len(values) - 1)", "sum(values) / len(values)")
    assert new != old
    return _unified_diff(old, new, STATS_PATH)


def stats_comment_diff(attempt: int) -> str:
    """A patch that applies but never fixes the failing test."""

    old = _stats_template()
    lines = old.splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines) if line.startswith("def mean("))
    lines.insert(index, f"# repair attempt {attempt}\n")
    return _unified_diff(old, "".join(lines), STATS_PATH)


def delete_test_file_diff() -> str:
    """A cheating patch that deletes the failing test file."""

    old = _stats_test_template()
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            [],
            fromfile=f"a/{STATS_TEST_PATH}",
            tofile="/dev/null",
        )
    )


def weaken_assertion_diff() -> str:
    """A cheating patch that replaces the failing assertion with ``assert True``."""

    old = _stats_test_template()
    new = old.replace("    assert mean([1.0, 2.0, 3.0]) == 2.0\n", "    assert True\n")
    assert new != old
    return _unified_diff(old, new, STATS_TEST_PATH)


def repair_responder(*, patch=None, patch_factory=None, plan=None, diagnosis=None):
    """Build a phase-aware stub responder for the repair loop."""

    counter = {"patch": 0}

    def responder(request, index):
        if request.phase == "understand":
            return json.dumps(
                plan
                or {
                    "goal": "make the failing test pass",
                    "queries": ["def mean"],
                    "suspect_paths": [STATS_PATH],
                }
            )
        if request.phase == "locate":
            return json.dumps(
                {
                    "diagnosis": diagnosis
                    or {
                        "target_files": [STATS_PATH],
                        "root_cause": "mean divides by len(values) - 1",
                        "edit_intent": "divide by len(values)",
                    }
                }
            )
        if request.phase == "patch":
            attempt = counter["patch"]
            counter["patch"] += 1
            diff = patch_factory(attempt) if patch_factory is not None else (patch or "")
            return json.dumps({"diff": diff, "notes": f"attempt {attempt}"})
        return "{}"

    return responder


def repair_payload(repo_path, **overrides) -> dict:
    """Standard repair request body for the stats fixture."""

    payload = {
        "repo_url": str(repo_path),
        "mode": "repair",
        "problem": (
            "tests/test_stats.py::test_mean_of_values fails: mean([1.0, 2.0, 3.0]) "
            "returns 3.0 instead of 2.0"
        ),
        "target_tests": [STATS_TARGET_TEST],
    }
    payload.update(overrides)
    return payload
