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
    mode: Literal["assess", "repair"] = "assess"
    #: Free form problem statement for ``repair`` tasks (Issue text, CI failure,
    #: or an assessment finding). Required when ``mode == "repair"``.
    problem: str | None = None
    #: Optional explicit target test node ids; when empty the gate treats the
    #: tests that fail on the pinned baseline as the targets.
    target_tests: list[str] = Field(default_factory=list)
    #: Optional extra paths the patch is allowed to touch.
    allowed_paths: list[str] = Field(default_factory=list)
    #: Continue a previous repair run instead of starting from scratch.
    resume_task_id: str | None = None

    def dedupe_key(self) -> tuple[str, ...]:
        return (
            self.repo_url.rstrip("/"),
            (self.base_branch or "").strip(),
            (self.base_commit or "").strip(),
            self.mode,
            (self.problem or "").strip(),
            ",".join(sorted(self.target_tests)),
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


class PullRequestInfo(BaseModel):
    """Remote state of the Draft PR created for a task."""

    model_config = ConfigDict(extra="forbid")

    number: int
    url: str
    title: str = ""
    head_branch: str
    base_branch: str
    commit_sha: str | None = None
    draft: bool = True
    state: str = "open"
    api_url: str | None = None


class PublishOutcome(BaseModel):
    """Result of the (idempotent) remote publish step."""

    model_config = ConfigDict(extra="forbid")

    attempted: bool = False
    status: Literal["created", "reused", "skipped", "failed"] = "skipped"
    reason: str = ""
    error_code: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    isolation: str | None = None
    operations: list[str] = Field(default_factory=list)
    pull_request: PullRequestInfo | None = None


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
    repair: RepairOutcome | None = None
    publish: PublishOutcome | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    notes: list[str] = Field(default_factory=list)


class TaskMode(StrEnum):
    assess = "assess"
    repair = "repair"


class TerminationReason(StrEnum):
    """Why the finite repair loop stopped."""

    completed = "completed"
    gate_failed = "gate_failed"
    limit_max_rounds = "limit_max_rounds"
    limit_wall_clock = "limit_wall_clock"
    limit_model_calls = "limit_model_calls"
    limit_model_tokens = "limit_model_tokens"
    limit_tool_calls = "limit_tool_calls"
    no_progress = "no_progress"
    model_unavailable = "model_unavailable"
    model_output_invalid = "model_output_invalid"
    tests_unavailable = "tests_unavailable"
    cancelled = "cancelled"
    unexpected_error = "unexpected_error"


class ModelCallUsage(BaseModel):
    """Per call accounting of a single model request."""

    model_config = ConfigDict(extra="forbid")

    index: int
    phase: str
    model: str = ""
    ok: bool = True
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str | None = None
    truncated: bool = False
    cost_usd: float = 0.0
    error_code: str | None = None
    error: str | None = None


class ModelUsage(BaseModel):
    """Task level aggregation of calls, latency and inference cost."""

    model_config = ConfigDict(extra="forbid")

    provider: str = ""
    model: str = ""
    calls: int = 0
    failed_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    per_phase: dict[str, int] = Field(default_factory=dict)

    def record(self, call: ModelCallUsage) -> None:
        self.calls += 1
        if not call.ok:
            self.failed_calls += 1
        self.prompt_tokens += call.prompt_tokens
        self.completion_tokens += call.completion_tokens
        self.total_tokens += call.total_tokens
        self.latency_ms += call.latency_ms
        self.cost_usd = round(self.cost_usd + call.cost_usd, 8)
        self.per_phase[call.phase] = self.per_phase.get(call.phase, 0) + 1


class ToolInvocation(BaseModel):
    """Record of one controlled tool call, bounded and redacted."""

    model_config = ConfigDict(extra="forbid")

    index: int
    round: int
    tool: str
    ok: bool
    arguments: dict[str, Any] = Field(default_factory=dict)
    output_excerpt: str = ""
    output_bytes: int = 0
    truncated: bool = False
    duration_ms: int = 0
    error_code: str | None = None
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class TestCaseOutcome(BaseModel):
    """Single pytest case outcome."""

    model_config = ConfigDict(extra="forbid")

    nodeid: str
    outcome: Literal["passed", "failed", "error", "skipped"]
    detail: str | None = None


class TestSnapshot(BaseModel):
    """Outcome of one pytest run, used to compare baseline and final state."""

    model_config = ConfigDict(extra="forbid")

    ran: bool = False
    command: list[str] = Field(default_factory=list)
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_ms: int = 0
    exit_code: int | None = None
    error: str | None = None
    cases: list[TestCaseOutcome] = Field(default_factory=list)

    def outcome_map(self) -> dict[str, str]:
        return {case.nodeid: case.outcome for case in self.cases}

    def failing(self) -> list[str]:
        return [case.nodeid for case in self.cases if case.outcome in ("failed", "error")]

    def passing(self) -> list[str]:
        return [case.nodeid for case in self.cases if case.outcome == "passed"]


class GateCheck(BaseModel):
    """One quality gate assertion and its verdict."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    detail: str = ""


class QualityGateReport(BaseModel):
    """Design document 3.6 quality gate verdict."""

    model_config = ConfigDict(extra="forbid")

    passed: bool = False
    primary_blocker: str | None = None
    checks: list[GateCheck] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    cheating_findings: list[str] = Field(default_factory=list)


class PatchArtifact(BaseModel):
    """The candidate patch produced by the agent."""

    model_config = ConfigDict(extra="forbid")

    applied: bool = False
    diff: str = ""
    changed_files: list[str] = Field(default_factory=list)
    added_lines: int = 0
    removed_lines: int = 0
    diff_lines: int = 0
    diff_hash: str | None = None


class RepairRound(BaseModel):
    """Everything recorded for one iteration of the repair loop."""

    model_config = ConfigDict(extra="forbid")

    index: int
    phase: str
    summary: str = ""
    model_calls: list[ModelCallUsage] = Field(default_factory=list)
    tool_calls: list[ToolInvocation] = Field(default_factory=list)
    patch_hash: str | None = None
    tests: TestSnapshot | None = None
    notes: list[str] = Field(default_factory=list)
    termination_reason: TerminationReason | None = None


class RepairOutcome(BaseModel):
    """Repair result attached to the unified :class:`Report`."""

    model_config = ConfigDict(extra="forbid")

    mode: TaskMode = TaskMode.repair
    termination_reason: TerminationReason
    result: Literal["fixed", "not_fixed", "needs_review", "failed"] = "not_fixed"
    attempts: int = 0
    max_rounds: int = 0
    patch: PatchArtifact | None = None
    quality_gate: QualityGateReport = Field(default_factory=QualityGateReport)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    baseline_tests: TestSnapshot | None = None
    final_tests: TestSnapshot | None = None
    target_tests: list[str] = Field(default_factory=list)
    located_files: list[str] = Field(default_factory=list)
    rounds: list[RepairRound] = Field(default_factory=list)
    state_path: str | None = None


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
    model_usage: ModelUsage | None = None
    termination_reason: str | None = None
    rounds: int = 0
    resumed_from: str | None = None
    #: Task this one was retried from, when created via the retry endpoint.
    retried_from: str | None = None
    pull_request: PullRequestInfo | None = None
    sandbox_isolation: str | None = None

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
