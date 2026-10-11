"""Phase 2 repair pipeline: clone, pin, baseline, LangGraph loop, quality gate.

The runner owns everything the agent is *not* trusted with: the workspace, the
sandbox, the baseline test run, the loop budget, the quality gate verdict and the
persisted execution record. The agent only ever sees the controlled tools.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from repopilot.agent.graph import (
    AgentCancelled,
    AgentRuntime,
    LoopStop,
    run_repair_agent,
)
from repopilot.agent.limits import AgentLimits, LoopMeter
from repopilot.agent.tools import WorkspaceTools
from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.errors import (
    CodeExecutionDeniedError,
    InvalidRequestError,
    RepoPilotError,
    TestsUnavailableError,
)
from repopilot.execution.git import RepoCheckout, is_trusted_source
from repopilot.execution.git import clone_and_pin_repo as _clone_and_pin_repo
from repopilot.execution.sandbox import LocalProcessSandboxExecutor
from repopilot.execution.test_run import run_pytest
from repopilot.execution.workspace import Workspace, WorkspaceManager
from repopilot.llm.base import ModelClient
from repopilot.llm.registry import build_model_client
from repopilot.logging_setup import RunLogger
from repopilot.models import (
    ModelUsage,
    ProjectProfile,
    QualityGateReport,
    RepairOutcome,
    RepairRound,
    RepoMetadata,
    Task,
    TaskStatus,
    TerminationReason,
    utcnow,
)
from repopilot.report.builder import build_report, persist_report
from repopilot.scan.scanner import scan_project
from repopilot.storage.base import TaskStore

_LIMIT_REASONS = frozenset(
    {
        TerminationReason.limit_max_rounds,
        TerminationReason.limit_wall_clock,
        TerminationReason.limit_model_calls,
        TerminationReason.limit_model_tokens,
        TerminationReason.limit_tool_calls,
    }
)
_MODEL_REASONS = frozenset(
    {
        TerminationReason.model_unavailable,
        TerminationReason.model_output_invalid,
    }
)
_NEEDS_REVIEW_BLOCKERS = ("change_scope", "baseline_attributable")


class _CancellationRequestedError(Exception):
    """Internal signal that a cancellation token fired."""


def _is_test_path(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    if name == "conftest.py":
        return True
    if fnmatch.fnmatch(name, "test_*.py") or fnmatch.fnmatch(name, "*_test.py"):
        return True
    return "/tests/" in f"/{relative}" and relative.endswith(".py")


def collect_test_sources(repo_dir: Path, settings: Settings) -> dict[str, str]:
    """Read every test file (bounded), for the anti-cheating comparison."""

    sources: dict[str, str] = {}
    root = repo_dir.resolve()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if not _is_test_path(relative):
            continue
        try:
            if path.stat().st_size > settings.agent_max_file_bytes:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue
        sources[relative] = raw.decode("utf-8", errors="replace")
    return sources


class RepairRunner:
    """Executes one ``repair`` task end to end; never raises for expected failures."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: TaskStore,
        model_client: ModelClient | None = None,
        workspace_manager: WorkspaceManager | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._model_client = model_client or build_model_client(settings)
        self._workspaces = workspace_manager or WorkspaceManager(settings)

    # -- resume helpers --------------------------------------------------------

    def _load_resume_state(self, task: Task) -> dict | None:
        resume_id = task.request.resume_task_id
        if not resume_id:
            return None
        prior = self._store.get(resume_id)
        if prior is None or not prior.workspace_path:
            return None
        path = Path(prior.workspace_path) / "artifacts" / "repair-state.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _seed_from_state(
        self, state: dict | None, limits: AgentLimits
    ) -> tuple[list[RepairRound], ModelUsage, LoopMeter]:
        rounds: list[RepairRound] = []
        usage = ModelUsage()
        meter = LoopMeter(limits=limits)
        if not state:
            return rounds, usage, meter
        for raw in state.get("rounds") or []:
            try:
                rounds.append(RepairRound.model_validate(raw))
            except Exception:  # pragma: no cover - tolerate schema drift on resume
                continue
        if isinstance(state.get("usage"), dict):
            try:
                usage = ModelUsage.model_validate(state["usage"])
            except Exception:  # pragma: no cover - defensive
                usage = ModelUsage()
        meter.rounds = len(rounds)
        meter.model_calls = usage.calls
        meter.model_tokens = usage.total_tokens
        meter.tool_calls = sum(len(rnd.tool_calls) for rnd in rounds)
        return rounds, usage, meter

    # -- main entry point ------------------------------------------------------

    def run(self, task_id: str, token: CancellationToken) -> None:
        """Run the repair loop for ``task_id`` and persist the report."""

        task = self._store.get(task_id)
        if task is None:
            return
        logger = RunLogger(self._settings.log_dir, task.task_id, task.run_id)

        this_task = task.model_copy(deep=True)
        this_task.status = TaskStatus.running
        this_task.started_at = utcnow()
        this_task.progress = "starting"
        this_task.termination_reason = None
        self._store.save(this_task)
        logger.event(
            "task.started",
            "repair started",
            mode=task.request.mode,
            repo_url=task.request.repo_url,
            target_tests=task.request.target_tests,
        )

        workspace: Workspace | None = None
        project = ProjectProfile()
        checkout: RepoCheckout | None = None
        trusted = False
        runtime: AgentRuntime | None = None
        status = TaskStatus.failed
        failure_code: str | None = None
        failure_reason: str | None = None
        result = "failed"
        base_state = this_task

        try:
            if not (task.request.problem or "").strip():
                raise InvalidRequestError("repair tasks require a non blank problem statement")
            if task.request.mode != "repair":  # pragma: no cover - guarded by the API
                raise InvalidRequestError("RepairRunner only accepts mode=repair tasks")

            trusted = is_trusted_source(task.request.repo_url, self._settings)
            this_task.is_trusted_source = trusted
            this_task.code_execution_enabled = bool(
                trusted or self._settings.allow_untrusted_code_execution
            )
            workspace = self._workspaces.create(task.task_id)
            this_task.workspace_path = str(workspace.root)
            this_task.progress = "cloning"
            self._store.save(this_task)

            sandbox = LocalProcessSandboxExecutor(self._settings, workspace, trusted_source=trusted)
            if not sandbox.code_execution_enabled:
                raise CodeExecutionDeniedError(
                    "repair requires executing repository tests; this source is not a trusted "
                    "fixture root and untrusted code execution is disabled"
                )
            checkout = _clone_and_pin_repo(
                task.request.repo_url,
                workspace.repo_dir,
                base_commit=task.request.base_commit,
                base_branch=task.request.base_branch,
                settings=self._settings,
                sandbox=sandbox,
                token=token,
            )
            this_task.resolved_commit = checkout.resolved_commit
            this_task.progress = "scanning"
            self._store.save(this_task)
            logger.event(
                "repo.cloned",
                "repository cloned and baseline commit pinned",
                resolved_commit=checkout.resolved_commit,
                resolved_ref=checkout.resolved_ref,
                trusted_source=trusted,
            )

            project = scan_project(workspace.repo_dir, self._settings)
            logger.event(
                "project.scanned",
                "project scanned",
                is_python_project=project.is_python_project,
                test_frameworks=project.test_frameworks,
                test_file_count=project.test_file_count,
            )
            this_task.progress = "baseline"
            self._store.save(this_task)

            baseline = run_pytest(
                sandbox,
                self._settings,
                token,
                repo_dir=workspace.repo_dir,
                junit_path=workspace.artifacts_dir / "baseline-junit.xml",
                purpose="baseline pytest",
            )
            if not baseline.usable:
                raise TestsUnavailableError(
                    f"baseline test run was not usable: {baseline.error or 'unknown error'}"
                )
            baseline_snapshot = baseline.snapshot
            baseline_sources = collect_test_sources(workspace.repo_dir, self._settings)
            logger.event(
                "repair.baseline",
                "baseline test run captured",
                passed=baseline_snapshot.passed if baseline_snapshot else 0,
                failed=baseline_snapshot.failed if baseline_snapshot else 0,
                failing=baseline_snapshot.failing() if baseline_snapshot else [],
            )

            limits = AgentLimits.from_settings(self._settings)
            resume_state = self._load_resume_state(task)
            rounds, usage, meter = self._seed_from_state(resume_state, limits)
            resumed_from = task.request.resume_task_id if resume_state else None

            tools = WorkspaceTools(
                settings=self._settings,
                workspace=workspace,
                sandbox=sandbox,
                token=token,
                logger=logger,
            )
            runtime = AgentRuntime(
                settings=self._settings,
                client=self._model_client,
                tools=tools,
                logger=logger,
                token=token,
                problem=task.request.problem or "",
                limits=limits,
                meter=meter,
                target_tests=list(task.request.target_tests),
                allowed_paths=list(task.request.allowed_paths),
                profile_summary=_profile_summary(project),
                file_listing=_file_listing(workspace.repo_dir, self._settings),
                baseline_tests=baseline_snapshot,
                baseline_sources=baseline_sources,
                state_path=workspace.artifacts_dir / "repair-state.json",
                final_sources_provider=lambda: collect_test_sources(
                    workspace.repo_dir, self._settings
                ),
                resumed_from=resumed_from,
                rounds=rounds,
                usage=usage,
            )
            this_task.progress = "repairing"
            this_task.resumed_from = resumed_from
            this_task.rounds = len(rounds)
            self._store.save(this_task)

            run_repair_agent(runtime)
        except LoopStop as stop:  # pragma: no cover - nodes convert these first
            status = TaskStatus.failed
            failure_code = stop.reason.value
            failure_reason = f"{stop.reason.value}: repair loop stopped by budget"
        except AgentCancelled:
            status = TaskStatus.cancelled
            failure_code = "cancelled"
            failure_reason = "cancelled: repair was cancelled on request"
            logger.event("task.cancelled", failure_reason, level="warning")
        except _CancellationRequestedError:  # pragma: no cover - defensive
            status = TaskStatus.cancelled
            failure_code = "cancelled"
            failure_reason = "cancelled: repair was cancelled on request"
        except RepoPilotError as exc:
            if token.is_cancelled():
                status = TaskStatus.cancelled
                failure_code = "cancelled"
                failure_reason = "cancelled: repair was cancelled on request"
                logger.event("task.cancelled", failure_reason, level="warning")
            else:
                status = TaskStatus.failed
                failure_code = exc.code
                failure_reason = exc.failure_reason()
                logger.event("task.failed", failure_reason, level="error", code=exc.code)
        except Exception as exc:  # pragma: no cover - defensive catch all
            status = TaskStatus.failed
            failure_code = "unexpected_error"
            failure_reason = f"unexpected_error: {exc!r}"
            logger.event("task.failed", failure_reason, level="error", code=failure_code)

        outcome: RepairOutcome | None = None
        if runtime is not None:
            if status is TaskStatus.cancelled:
                runtime.termination = TerminationReason.cancelled
            elif status is TaskStatus.failed and runtime.termination is None:
                runtime.termination = _reason_for_code(failure_code)
            runtime.persist()
            status, failure_code, failure_reason, result = _classify(
                runtime, failure_code, failure_reason
            )
            outcome = _build_outcome(runtime, result)
        else:
            # The loop never started (clone, trust or baseline failure). Still emit a
            # repair block so the report always explains why nothing was verified.
            outcome = RepairOutcome(
                termination_reason=_reason_for_code(failure_code),
                result=(
                    "needs_review"
                    if status is TaskStatus.needs_review
                    else "not_fixed"
                    if status in (TaskStatus.cancelled, TaskStatus.failed)
                    else "failed"
                ),
                quality_gate=QualityGateReport(
                    passed=False,
                    primary_blocker="repair_not_executed",
                    blockers=[failure_reason or "the repair loop did not run"],
                ),
            )

        repo_metadata = _repo_metadata(task, checkout, trusted, workspace, self._settings)
        report = build_report(
            task_id=task.task_id,
            run_id=task.run_id,
            status=status,
            repo=repo_metadata,
            project=project,
            checkers=[],
            failure_code=failure_code,
            failure_reason=failure_reason,
            repair=outcome,
        )

        final = self._store.get(task_id) or base_state
        final.status = status
        final.progress = "finished"
        final.finished_at = utcnow()
        final.failure_code = failure_code
        final.failure_reason = failure_reason
        final.report = report
        final.is_trusted_source = trusted
        if runtime is not None:
            final.model_usage = runtime.usage
            final.termination_reason = runtime.termination.value if runtime.termination else None
            final.rounds = len(runtime.rounds)
        if checkout is not None:
            final.resolved_commit = checkout.resolved_commit
        if workspace is not None:
            final.workspace_path = str(workspace.root)
        self._store.save(final)

        persist_report(self._settings, report, workspace, filename="repair-report.json")
        logger.event(
            "task.finished",
            f"repair finished with status {status.value}",
            status=status.value,
            result=(outcome.result if outcome else result),
            termination_reason=failure_code,
            gate_passed=outcome.quality_gate.passed if outcome else None,
            usage=(runtime.usage.model_dump() if runtime else None),
        )
        if workspace is not None:
            self._workspaces.cleanup(workspace)


def _reason_for_code(code: str | None) -> TerminationReason:
    for member in TerminationReason:
        if member.value == code:
            return member
    return (
        TerminationReason.unexpected_error
        if code == "unexpected_error"
        else TerminationReason.gate_failed
    )


def _classify(
    runtime: AgentRuntime, code: str | None, reason: str | None
) -> tuple[TaskStatus, str | None, str | None, str]:
    """Map the gate verdict and termination reason onto a task status.

    Nothing here can turn an unverified repair into ``succeeded``.
    """

    gate = runtime.gate
    termination = runtime.termination or TerminationReason.gate_failed

    if termination is TerminationReason.cancelled:
        return (
            TaskStatus.cancelled,
            "cancelled",
            "cancelled: repair was cancelled on request",
            "not_fixed",
        )

    if termination is TerminationReason.tests_unavailable:
        return (
            TaskStatus.failed,
            "tests_unavailable",
            "tests_unavailable: the test suite could not be run or parsed",
            "failed",
        )
    if termination in _MODEL_REASONS:
        return (
            TaskStatus.failed,
            termination.value,
            f"{termination.value}: the model was unavailable or its answer was unusable",
            "failed",
        )
    if gate.cheating_findings:
        detail = gate.cheating_findings[0]
        return (
            TaskStatus.failed,
            "cheating_detected",
            f"cheating_detected: {detail}",
            "failed",
        )
    if termination in _LIMIT_REASONS or termination is TerminationReason.no_progress:
        return (
            TaskStatus.failed,
            termination.value,
            f"{termination.value}: the repair loop stopped without a verified fix",
            "not_fixed",
        )

    if gate.passed and termination is TerminationReason.completed:
        return TaskStatus.succeeded, None, None, "fixed"

    blockers = list(gate.blockers)
    needs_review = any(
        blocker.startswith(name) for blocker in blockers for name in _NEEDS_REVIEW_BLOCKERS
    )
    if needs_review:
        return (
            TaskStatus.needs_review,
            "quality_gate_needs_review",
            f"quality_gate_needs_review: {'; '.join(blockers) or 'manual review required'}",
            "needs_review",
        )
    if blockers:
        return (
            TaskStatus.failed,
            "quality_gate_failed",
            f"quality_gate_failed: {'; '.join(blockers)}",
            "not_fixed",
        )
    return (
        TaskStatus.needs_review,
        "unverified",
        "unverified: the repair result could not be confirmed",
        "needs_review",
    )


def _build_outcome(runtime: AgentRuntime, result: str) -> RepairOutcome:
    return RepairOutcome(
        termination_reason=runtime.termination or TerminationReason.gate_failed,
        result=result,  # type: ignore[arg-type]
        attempts=len(runtime.rounds),
        max_rounds=runtime.limits.max_rounds,
        patch=runtime.patch,
        quality_gate=runtime.gate,
        usage=runtime.usage,
        baseline_tests=runtime.baseline_tests,
        final_tests=runtime.final_tests,
        target_tests=runtime.target_tests,
        located_files=runtime.located_files,
        rounds=runtime.rounds,
        state_path=str(runtime.state_path) if runtime.state_path else None,
    )


def _profile_summary(project: ProjectProfile) -> str:
    return (
        f"python_project={project.is_python_project} "
        f"python_requires={project.python_requires} "
        f"test_frameworks={project.test_frameworks} "
        f"test_dirs={project.test_dirs} "
        f"dev_tools={project.dev_tools}"
    )


def _file_listing(repo_dir: Path, settings: Settings, *, limit: int = 400) -> str:
    root = repo_dir.resolve()
    entries: list[str] = []
    skip = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in skip for part in path.relative_to(root).parts[:-1]):
            continue
        entries.append(path.relative_to(root).as_posix())
        if len(entries) >= limit:
            break
    return "\n".join(entries)


def _repo_metadata(
    task: Task,
    checkout: RepoCheckout | None,
    trusted: bool,
    workspace: Workspace | None,
    settings: Settings,
) -> RepoMetadata:
    if checkout is not None:
        return RepoMetadata(
            source=checkout.source.raw,
            is_local_source=checkout.source.is_local,
            is_trusted_source=trusted,
            code_execution_enabled=True,
            resolved_commit=checkout.resolved_commit,
            resolved_ref=checkout.resolved_ref,
            default_branch=checkout.default_branch,
            head_commit=checkout.head_commit,
            requested_base_commit=task.request.base_commit,
            requested_base_branch=task.request.base_branch,
            clone_duration_ms=checkout.clone_duration_ms,
        )
    return RepoMetadata(
        source=task.request.repo_url,
        is_trusted_source=trusted,
        code_execution_enabled=bool(
            trusted or (workspace is not None and settings.allow_untrusted_code_execution)
        ),
        requested_base_commit=task.request.base_commit,
        requested_base_branch=task.request.base_branch,
        resolved_commit=task.resolved_commit,
    )
