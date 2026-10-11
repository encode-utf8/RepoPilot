"""End to end assessment pipeline: clone, pin, scan, check, report."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

from repopilot.cancellation import CancellationToken
from repopilot.checkers.base import CheckContext, Checker
from repopilot.checkers.registry import enabled_checkers
from repopilot.config import Settings
from repopilot.errors import RepoPilotError
from repopilot.execution.git import RepoCheckout, is_trusted_source
from repopilot.execution.git import clone_and_pin_repo as _clone_and_pin_repo
from repopilot.execution.sandbox import LocalProcessSandboxExecutor
from repopilot.execution.workspace import Workspace, WorkspaceManager
from repopilot.logging_setup import RunLogger
from repopilot.models import (
    CheckerResult,
    CheckerStatus,
    ProjectProfile,
    RepoMetadata,
    Task,
    TaskStatus,
    utcnow,
)
from repopilot.report.builder import build_report
from repopilot.scan.scanner import scan_project
from repopilot.storage.base import TaskStore


class _CancellationRequestedError(Exception):
    """Internal signal that a cancellation token fired."""


class AssessmentRunner:
    """Executes one assessment task; safe to run concurrently across tasks."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: TaskStore,
        checkers: list[Checker] | None = None,
        workspace_manager: WorkspaceManager | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._checkers = checkers if checkers is not None else enabled_checkers(settings)
        self._workspaces = workspace_manager or WorkspaceManager(settings)

    def run(self, task_id: str, token: CancellationToken) -> None:
        """Run the assessment for ``task_id``. Never raises for expected failures."""

        task = self._store.get(task_id)
        if task is None:
            return
        logger = RunLogger(self._settings.log_dir, task.task_id, task.run_id)

        this_task = task.model_copy(deep=True)
        this_task.status = TaskStatus.running
        this_task.started_at = utcnow()
        this_task.progress = "starting"
        self._store.save(this_task)
        logger.event(
            "task.started",
            "assessment started",
            repo_url=task.request.repo_url,
            base_branch=task.request.base_branch,
            base_commit=task.request.base_commit,
        )

        workspace: Workspace | None = None
        project = ProjectProfile()
        results: list[CheckerResult] = []
        checkout: RepoCheckout | None = None
        trusted = False
        failure_code: str | None = None
        failure_reason: str | None = None
        status = TaskStatus.failed
        notes: list[str] = []

        try:
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
                default_branch=checkout.default_branch,
                head_commit=checkout.head_commit,
                trusted_source=trusted,
                clone_duration_ms=checkout.clone_duration_ms,
            )

            project = scan_project(workspace.repo_dir, self._settings)
            logger.event(
                "project.scanned",
                "project scanned",
                is_python_project=project.is_python_project,
                python_requires=project.python_requires,
                dependency_manifests=project.dependency_manifests,
                test_frameworks=project.test_frameworks,
                test_file_count=project.test_file_count,
                dev_tools=project.dev_tools,
            )
            if not project.is_python_project:
                notes.append(
                    "No Python project markers were detected; Python checks may be limited."
                )

            context = CheckContext(
                task_id=task.task_id,
                run_id=task.run_id,
                workspace=workspace,
                project=project,
                sandbox=sandbox,
                token=token,
                settings=self._settings,
                logger=logger,
                trusted_source=trusted,
            )
            results = self._run_checkers(context, logger, token)
            status = self._decide_status(results, token)
        except _CancellationRequestedError:
            status = TaskStatus.cancelled
            failure_code = "cancelled"
            failure_reason = "cancelled: task was cancelled on request"
            logger.event("task.cancelled", "assessment cancelled", level="warning")
        except RepoPilotError as exc:
            if token.is_cancelled():
                # A cancelled run often surfaces as a domain error (for example a
                # git clone killed mid-flight); cancellation always wins.
                status = TaskStatus.cancelled
                failure_code = "cancelled"
                failure_reason = "cancelled: task was cancelled on request"
                logger.event("task.cancelled", failure_reason, level="warning")
            else:
                status = TaskStatus.failed
                failure_code = exc.code
                failure_reason = exc.failure_reason()
                logger.event("task.failed", failure_reason, level="error", code=exc.code)
        except Exception as exc:  # pragma: no cover - defensive catch all
            if token.is_cancelled():
                status = TaskStatus.cancelled
                failure_code = "cancelled"
                failure_reason = "cancelled: task was cancelled on request"
                logger.event("task.cancelled", failure_reason, level="warning")
            else:
                status = TaskStatus.failed
                failure_code = "unexpected_error"
                failure_reason = f"unexpected_error: {exc!r}"
                logger.event("task.failed", failure_reason, level="error", code=failure_code)

        repo_metadata = self._repo_metadata(task, checkout, trusted, workspace)
        report = build_report(
            task_id=task.task_id,
            run_id=task.run_id,
            status=status,
            repo=repo_metadata,
            project=project,
            checkers=results,
            failure_code=failure_code,
            failure_reason=failure_reason,
            notes=notes,
        )

        final = self._store.get(task_id) or this_task
        final.status = status
        final.progress = "finished"
        final.finished_at = utcnow()
        final.failure_code = failure_code
        final.failure_reason = failure_reason
        final.report = report
        final.is_trusted_source = trusted
        final.code_execution_enabled = bool(
            trusted or self._settings.allow_untrusted_code_execution
        )
        if checkout is not None:
            final.resolved_commit = checkout.resolved_commit
        if workspace is not None:
            final.workspace_path = str(workspace.root)
        self._store.save(final)

        self._persist_report(report, workspace)
        logger.event(
            "task.finished",
            f"assessment finished with status {status.value}",
            status=status.value,
            confirmed_issues=report.summary.confirmed_issues,
            unverified_issues=report.summary.unverified_issues,
            verification_gaps=report.verification.verification_gaps,
        )
        if workspace is not None:
            self._workspaces.cleanup(workspace)

    def _run_checkers(
        self, context: CheckContext, logger: RunLogger, token: CancellationToken
    ) -> list[CheckerResult]:
        results: list[CheckerResult] = []
        for checker in self._checkers:
            if token.is_cancelled():
                raise _CancellationRequestedError
            if not checker.is_applicable(context):
                results.append(
                    CheckerResult(
                        name=checker.name,
                        display_name=checker.display_name,
                        status=CheckerStatus.not_applicable,
                        applicable=False,
                    )
                )
                logger.event("checker.skipped", f"{checker.name} not applicable")
                continue
            started = time.monotonic()
            logger.event("checker.started", f"{checker.name} started")
            try:
                result = checker.run(context)
            except RepoPilotError as exc:
                result = CheckerResult(
                    name=checker.name,
                    display_name=checker.display_name,
                    status=CheckerStatus.error,
                    error=exc.failure_reason(),
                    notes=[exc.failure_reason()],
                    required=True,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:  # pragma: no cover - defensive catch all
                result = CheckerResult(
                    name=checker.name,
                    display_name=checker.display_name,
                    status=CheckerStatus.error,
                    error=f"{type(exc).__name__}: {exc}",
                    notes=["checker raised an unexpected error"],
                    required=True,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            results.append(result)
            logger.event(
                "checker.finished",
                f"{checker.name} finished with status {result.status.value}",
                checker=checker.name,
                status=result.status.value,
                issues=len(result.issues),
                duration_ms=result.duration_ms,
                exit_code=result.exit_code,
            )
        return results

    @staticmethod
    def _decide_status(results: list[CheckerResult], token: CancellationToken) -> TaskStatus:
        if token.is_cancelled():
            raise _CancellationRequestedError
        if any(result.status is CheckerStatus.error for result in results):
            return TaskStatus.failed
        if any(result.required and not result.ran for result in results):
            return TaskStatus.needs_review
        return TaskStatus.succeeded

    def _repo_metadata(
        self,
        task: Task,
        checkout: RepoCheckout | None,
        trusted: bool,
        workspace: Workspace | None,
    ) -> RepoMetadata:
        if checkout is not None:
            return RepoMetadata(
                source=checkout.source.raw,
                is_local_source=checkout.source.is_local,
                is_trusted_source=trusted,
                code_execution_enabled=bool(
                    trusted or self._settings.allow_untrusted_code_execution
                ),
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
            code_execution_enabled=bool(trusted or self._settings.allow_untrusted_code_execution),
            requested_base_commit=task.request.base_commit,
            requested_base_branch=task.request.base_branch,
            resolved_commit=task.resolved_commit,
        )

    def _persist_report(self, report, workspace: Workspace | None) -> None:
        payload = report.model_dump_json(indent=2)
        if workspace is not None:
            with contextlib.suppress(OSError):
                (workspace.artifacts_dir / "assessment-report.json").write_text(
                    payload, encoding="utf-8"
                )
        reports_dir = Path(self._settings.log_dir) / "reports"
        with contextlib.suppress(OSError):
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / f"{report.task_id}.json").write_text(payload, encoding="utf-8")
