"""Domain models shared by the API, pipeline and report layers."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    """Timezone aware UTC timestamp used everywhere in persisted records."""

    return datetime.now(UTC)


class TaskStatus(StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    needs_review = "needs_review"
    cancelled = "cancelled"


TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.succeeded,
        TaskStatus.failed,
        TaskStatus.needs_review,
        TaskStatus.cancelled,
    }
)


class Severity(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.critical: 0,
    Severity.high: 1,
    Severity.medium: 2,
    Severity.low: 3,
    Severity.info: 4,
}


class Origin(StrEnum):
    """Who produced an issue: a deterministic tool or a model."""

    tool = "tool"
    model = "model"


class VerificationState(StrEnum):
    """Tool confirmed findings versus unverified potential problems."""

    confirmed = "confirmed"
    unverified = "unverified"


class CheckerStatus(StrEnum):
    ok = "ok"
    findings = "findings"
    not_applicable = "not_applicable"
    unavailable = "unavailable"
    error = "error"

    @property
    def ran(self) -> bool:
        return self in (CheckerStatus.ok, CheckerStatus.findings)


class Issue(BaseModel):
    """A single problem record, confirmed or unverified."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    issue_type: str
    severity: Severity
    evidence: str
    source_tool: str
    origin: Origin = Origin.tool
    verification: VerificationState = VerificationState.confirmed
    confirmed: bool = True
    file_path: str | None = None
    line: int | None = None
    column: int | None = None
    rule_id: str | None = None
    fix_suggestion: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sync_confirmed(self) -> Issue:
        self.confirmed = self.verification is VerificationState.confirmed
        return self


class CheckerResult(BaseModel):
    """Outcome of one deterministic checker run."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    status: CheckerStatus
    required: bool = False
    applicable: bool = True
    issues: list[Issue] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    commands: list[list[str]] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)

    @property
    def ran(self) -> bool:
        return self.status.ran


class TaskRequest(BaseModel):
    """User supplied assessment request."""

    model_config = ConfigDict(extra="forbid")

    repo_url: str
    base_branch: str | None = None
    base_commit: str | None = None
    mode: Literal["assess"] = "assess"

    def dedupe_key(self) -> tuple[str, str, str]:
        return (
            self.repo_url.rstrip("/"),
            (self.base_branch or "").strip(),
            (self.base_commit or "").strip(),
        )


class RepoMetadata(BaseModel):
    """Repository identity and pinned revision."""

    model_config = ConfigDict(extra="forbid")

    source: str
    is_local_source: bool = False
    is_trusted_source: bool = False
    code_execution_enabled: bool = False
    resolved_commit: str | None = None
    resolved_ref: str | None = None
    default_branch: str | None = None
    head_commit: str | None = None
    requested_base_commit: str | None = None
    requested_base_branch: str | None = None
    clone_duration_ms: int | None = None


class ProjectProfile(BaseModel):
    """Result of the deterministic project scan."""

    model_config = ConfigDict(extra="forbid")

    is_python_project: bool = False
    primary_language: str = "unknown"
    python_requires: str | None = None
    python_version_files: list[str] = Field(default_factory=list)
    dependency_manifests: list[str] = Field(default_factory=list)
    requirements_files: list[str] = Field(default_factory=list)
    test_frameworks: list[str] = Field(default_factory=list)
    test_dirs: list[str] = Field(default_factory=list)
    test_file_count: int = 0
    suggested_test_command: list[str] | None = None
    dev_tools: list[str] = Field(default_factory=list)
    detectable_markers: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)


class ReportSummary(BaseModel):
    """Aggregations over the issue list and the executed checkers."""

    model_config = ConfigDict(extra="forbid")

    total_issues: int = 0
    confirmed_issues: int = 0
    unverified_issues: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_tool: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)
    unverified_by_severity: dict[str, int] = Field(default_factory=dict)
    unverified_by_tool: dict[str, int] = Field(default_factory=dict)
    checkers_ran: list[str] = Field(default_factory=list)
    checkers_unavailable: list[str] = Field(default_factory=list)
    checkers_not_applicable: list[str] = Field(default_factory=list)


class VerificationInfo(BaseModel):
    """Explicit statement about what could and could not be verified."""

    model_config = ConfigDict(extra="forbid")

    code_execution_enabled: bool
    fully_verified: bool
    verification_gaps: list[str] = Field(default_factory=list)
    unverified_reasons: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Report(BaseModel):
    """Unified assessment report."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    task_id: str
    run_id: str
    status: TaskStatus
    generated_at: datetime
    repo: RepoMetadata
    project: ProjectProfile
    checkers: list[CheckerResult] = Field(default_factory=list)
    confirmed_issues: list[Issue] = Field(default_factory=list)
    unverified_issues: list[Issue] = Field(default_factory=list)
    summary: ReportSummary = Field(default_factory=ReportSummary)
    verification: VerificationInfo
    failure_code: str | None = None
    failure_reason: str | None = None
    notes: list[str] = Field(default_factory=list)


class Task(BaseModel):
    """Persisted task record; the API never exposes execution internals directly."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    run_id: str
    status: TaskStatus = TaskStatus.pending
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
    report: Report | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES


def sort_issues(issues: list[Issue]) -> list[Issue]:
    """Sort by severity, then tool, then file/line for stable reports."""

    return sorted(
        issues,
        key=lambda issue: (
            SEVERITY_ORDER[issue.severity],
            issue.source_tool,
            issue.file_path or "",
            issue.line or 0,
            issue.issue_id,
        ),
    )


def dedupe_issues(issues: list[Issue]) -> list[Issue]:
    """Drop duplicate records (same ``issue_id``), preserving input order.

    Advisory feeds occasionally report the same identifier twice, so callers
    normalise here instead of relying on each tool.
    """

    seen: set[str] = set()
    unique: list[Issue] = []
    for issue in issues:
        if issue.issue_id in seen:
            continue
        seen.add(issue.issue_id)
        unique.append(issue)
    return unique
