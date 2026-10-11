"""Request and response schemas for the task API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from repopilot.config import Settings
from repopilot.models import (
    ModelUsage,
    PublishOutcome,
    PullRequestInfo,
    ReportSummary,
    Task,
    TaskMode,
    TaskRequest,
    TaskStatus,
    utcnow,
)


class CreateTaskRequest(BaseModel):
    """Body of ``POST /api/tasks``."""

    model_config = ConfigDict(extra="forbid")

    repo_url: str = Field(description="Git URL or local path of the repository to assess.")
    base_branch: str | None = Field(
        default=None, description="Branch whose HEAD is pinned when base_commit is absent."
    )
    base_commit: str | None = Field(
        default=None, description="Exact commit to pin; takes precedence over base_branch."
    )
    mode: Literal["assess", "repair"] = Field(
        default="assess", description="assess runs the checkers; repair runs the agent loop."
    )
    problem: str | None = Field(
        default=None, description="Required for repair tasks: the problem to fix."
    )
    target_tests: list[str] = Field(
        default_factory=list,
        description="Optional pytest node ids that must pass after the repair.",
    )
    allowed_paths: list[str] = Field(
        default_factory=list,
        description="Optional extra paths the repair patch is allowed to touch.",
    )
    resume_task_id: str | None = Field(
        default=None, description="Continue a previous repair run from its saved state."
    )

    @field_validator("repo_url")
    @classmethod
    def _repo_url_not_blank(cls, value: str) -> str:
        if not value.strip():
            msg = "repo_url must not be blank"
            raise ValueError(msg)
        return value.strip()

    @model_validator(mode="after")
    def _repair_requires_problem(self) -> CreateTaskRequest:
        if self.mode == "repair" and not (self.problem or "").strip():
            msg = "problem is required when mode is 'repair'"
            raise ValueError(msg)
        return self

    def to_task_request(self) -> TaskRequest:
        return TaskRequest(
            repo_url=self.repo_url,
            base_branch=self.base_branch,
            base_commit=self.base_commit,
            mode=self.mode,
            problem=self.problem,
            target_tests=list(self.target_tests),
            allowed_paths=list(self.allowed_paths),
            resume_task_id=self.resume_task_id,
        )


class CreateTaskResponse(BaseModel):
    """Immediate acknowledgement returned by ``POST /api/tasks``."""

    task_id: str
    run_id: str
    status: TaskStatus
    deduplicated: bool = False


class TaskView(BaseModel):
    """Public projection of a task record."""

    task_id: str
    run_id: str
    status: TaskStatus
    request: TaskRequest
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: str | None = None
    resolved_commit: str | None = None
    is_trusted_source: bool = False
    code_execution_enabled: bool = False
    failure_code: str | None = None
    failure_reason: str | None = None
    workspace_path: str | None = None
    report_available: bool = False
    summary: ReportSummary | None = None
    rounds: int = 0
    termination_reason: str | None = None
    resumed_from: str | None = None
    retried_from: str | None = None
    model_usage: ModelUsage | None = None
    sandbox_isolation: str | None = None
    pull_request: PullRequestInfo | None = None

    @classmethod
    def from_task(cls, task: Task) -> TaskView:
        return cls(
            task_id=task.task_id,
            run_id=task.run_id,
            status=task.status,
            request=task.request,
            created_at=task.created_at,
            updated_at=task.updated_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            progress=task.progress,
            resolved_commit=task.resolved_commit,
            is_trusted_source=task.is_trusted_source,
            code_execution_enabled=task.code_execution_enabled,
            failure_code=task.failure_code,
            failure_reason=task.failure_reason,
            workspace_path=task.workspace_path,
            report_available=task.report is not None,
            summary=task.report.summary if task.report is not None else None,
            rounds=task.rounds,
            termination_reason=task.termination_reason,
            resumed_from=task.resumed_from,
            retried_from=task.retried_from,
            model_usage=task.model_usage,
            sandbox_isolation=task.sandbox_isolation,
            pull_request=task.pull_request,
        )


class CancelTaskResponse(TaskView):
    """Result of ``POST /api/tasks/{task_id}/cancel``."""

    cancel_requested: bool = True


class LogResponse(BaseModel):
    """Result of ``GET /api/tasks/{task_id}/logs``."""

    task_id: str
    run_id: str
    count: int
    events: list[dict]


class SandboxUsage(BaseModel):
    """Resource envelope applied while a task's commands execute."""

    model_config = ConfigDict(extra="forbid")

    backend: str
    isolation: str | None = None
    memory: str | None = None
    cpus: str | None = None
    pids_limit: int | None = None
    timeout_seconds: float | None = None


def _duration_ms(task: Task) -> int | None:
    started = task.started_at
    if started is None:
        return None
    finished = task.finished_at or utcnow()
    return max(int((finished - started).total_seconds() * 1000), 0)


class TaskUsageView(BaseModel):
    """Resource and progress view for one task (Phase 4 observability)."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    run_id: str
    mode: TaskMode
    status: TaskStatus
    progress: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    rounds: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    model_usage: ModelUsage | None = None
    sandbox: SandboxUsage | None = None
    publish: PublishOutcome | None = None
    failure_code: str | None = None

    @classmethod
    def from_task(cls, task: Task, settings: Settings) -> TaskUsageView:
        report = task.report
        repair = report.repair if report is not None else None
        rounds = repair.rounds if repair is not None else []
        tool_calls = sum(len(round_.tool_calls) for round_ in rounds)
        model_calls = sum(len(round_.model_calls) for round_ in rounds)
        if model_calls == 0 and repair is not None:
            model_calls = repair.usage.calls
        return cls(
            task_id=task.task_id,
            run_id=task.run_id,
            mode=task.request.mode,
            status=task.status,
            progress=task.progress,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            duration_ms=_duration_ms(task),
            rounds=task.rounds or len(rounds),
            model_calls=model_calls,
            tool_calls=tool_calls,
            model_usage=task.model_usage or (repair.usage if repair is not None else None),
            sandbox=SandboxUsage(
                backend=settings.sandbox_backend,
                isolation=task.sandbox_isolation,
                memory=settings.container_memory,
                cpus=settings.container_cpus,
                pids_limit=settings.container_pids_limit,
                timeout_seconds=settings.container_timeout_seconds,
            ),
            publish=report.publish if report is not None else None,
            failure_code=task.failure_code,
        )
