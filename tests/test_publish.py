"""Unit tests for the gate-first, idempotent Draft PR publisher."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from repopilot.config import Settings
from repopilot.errors import GitHubAuthError, GitHubUnavailableError
from repopilot.github.base import GitHubPullRequest
from repopilot.github.fake import FakeGitHubClient
from repopilot.models import (
    PatchArtifact,
    ProjectProfile,
    QualityGateReport,
    RepairOutcome,
    RepoMetadata,
    Task,
    TaskRequest,
    TaskStatus,
    TerminationReason,
    utcnow,
)
from repopilot.models import (
    TestSnapshot as Snapshot,
)
from repopilot.pipeline.publish import (
    PullRequestPublisher,
    build_branch_name,
    resolve_slug,
    slug_from_repo_url,
)
from repopilot.report.builder import build_report
from repopilot.storage.memory import InMemoryTaskStore
from support import STATS_PATH, STATS_TARGET_TEST, prepare_repo

PROBLEM = "tests/test_stats.py::test_mean_of_values fails: mean returns 3.0 not 2.0"


def _publish_settings(settings: Settings, **overrides) -> Settings:
    base = replace(
        settings,
        enable_github_publish=True,
        publish_requires_container=False,
        github_repo_slug="acme/widget",
    )
    return replace(base, **overrides) if overrides else base


def _repair_outcome(*, gate_passed: bool = True, result: str = "fixed") -> RepairOutcome:
    return RepairOutcome(
        termination_reason=(
            TerminationReason.completed if gate_passed else TerminationReason.gate_failed
        ),
        result=result,
        attempts=1,
        max_rounds=4,
        patch=PatchArtifact(
            applied=gate_passed,
            changed_files=[STATS_PATH],
            added_lines=1,
            removed_lines=1,
            diff_hash="deadbeef",
        ),
        quality_gate=QualityGateReport(
            passed=gate_passed,
            primary_blocker=None if gate_passed else "regression",
            blockers=[] if gate_passed else ["regression"],
        ),
        baseline_tests=Snapshot(ran=True, total=4, passed=3, failed=1),
        final_tests=Snapshot(ran=True, total=4, passed=4, failed=0),
        target_tests=[STATS_TARGET_TEST],
        located_files=[STATS_PATH],
    )


def _build_task(
    *,
    workspace,
    repo_path: Path,
    baseline: str,
    isolation: str | None = "container",
    status: TaskStatus = TaskStatus.succeeded,
    gate_passed: bool = True,
    result: str = "fixed",
    pull_request=None,
    task_id: str = "task_pub_1",
) -> Task:
    report = build_report(
        task_id=task_id,
        run_id="run_pub_1",
        status=status,
        repo=RepoMetadata(source=str(repo_path), default_branch="main", resolved_commit=baseline),
        project=ProjectProfile(),
        checkers=[],
        repair=_repair_outcome(gate_passed=gate_passed, result=result),
    )
    return Task(
        task_id=task_id,
        run_id="run_pub_1",
        status=status,
        request=TaskRequest(
            repo_url=str(repo_path),
            mode="repair",
            problem=PROBLEM,
            target_tests=[STATS_TARGET_TEST],
        ),
        created_at=utcnow(),
        updated_at=utcnow(),
        resolved_commit=baseline,
        sandbox_isolation=isolation,
        workspace_path=str(workspace.root),
        report=report,
        pull_request=pull_request,
    )


def _dirty_workspace(settings: Settings, repair_repo_path: Path):
    """Clone the stats fixture and apply the real one line fix to the worktree."""

    prepared = prepare_repo(settings, repair_repo_path, task_id="task_pub_1")
    target = prepared.repo_dir / STATS_PATH
    fixed = target.read_text(encoding="utf-8").replace(
        "sum(values) / (len(values) - 1)", "sum(values) / len(values)"
    )
    target.write_text(fixed, encoding="utf-8", newline="\n")
    return prepared


def _publisher(settings: Settings, store: InMemoryTaskStore, client) -> PullRequestPublisher:
    return PullRequestPublisher(settings=settings, store=store, client=client)


# --- happy path ---------------------------------------------------------------


def test_publish_creates_branch_commit_and_draft_pr(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    pub = _publish_settings(settings)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
    )
    store.create(task)
    fake = FakeGitHubClient()
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)

    assert outcome.status == "created", outcome
    assert outcome.attempted is True
    assert outcome.branch == build_branch_name(pub.github_branch_prefix, task.task_id)
    assert outcome.base_branch == "main"
    assert outcome.commit_sha
    assert outcome.pull_request is not None
    assert outcome.pull_request.draft is True
    assert outcome.pull_request.url.endswith(f"/acme/widget/pull/{outcome.pull_request.number}")

    # the branch and a single draft PR exist on the remote
    assert fake.refs[outcome.branch] == outcome.commit_sha
    assert fake.pull_request_count == 1
    assert set(fake.writes) >= {
        "create_blob",
        "create_tree",
        "create_commit",
        "create_ref",
        "create_pull_request",
    }

    # the record and the report are both updated
    persisted = store.get(task.task_id)
    assert persisted is not None
    assert persisted.pull_request is not None
    assert persisted.report is not None
    assert persisted.report.publish is not None
    assert persisted.report.publish.status == "created"


def test_pr_description_matches_change_set(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    pub = _publish_settings(settings)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
    )
    store.create(task)

    captured: dict[str, str] = {}
    fake = FakeGitHubClient()

    original = fake.create_pull_request

    def spy(*args, **kwargs):
        captured["body"] = kwargs["body"]
        captured["draft"] = kwargs["draft"]
        return original(*args, **kwargs)

    fake.create_pull_request = spy  # type: ignore[assignment]
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)

    assert outcome.status == "created"
    assert captured["draft"] is True
    body = captured["body"]
    assert STATS_PATH in body
    assert PROBLEM.split(":")[0] in body
    assert "pytest" in body


# --- idempotency --------------------------------------------------------------


def test_publish_is_idempotent_per_task(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    pub = _publish_settings(settings)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
    )
    store.create(task)
    fake = FakeGitHubClient()
    publisher = _publisher(pub, store, fake)

    first = publisher.publish_task(task.task_id)
    assert first.status == "created"

    fake.reset()
    second = publisher.publish_task(task.task_id)
    assert second.status == "reused"
    assert second.pull_request is not None
    assert second.pull_request.number == first.pull_request.number
    # a repeat trigger performs no remote call at all
    assert fake.writes == []
    assert fake.calls == []
    assert fake.pull_request_count == 1


def test_existing_remote_pull_request_is_reused(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    pub = _publish_settings(settings)
    branch = build_branch_name(pub.github_branch_prefix, "task_pub_1")
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
    )
    store.create(task)
    existing = GitHubPullRequest(
        number=42,
        url="https://api.test/acme/widget/pull/42",
        title="already there",
        head_branch=branch,
        base_branch="main",
        commit_sha="abc",
    )
    fake = FakeGitHubClient(pull_requests=[existing])
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)

    assert outcome.status == "reused"
    assert outcome.pull_request is not None and outcome.pull_request.number == 42
    assert fake.writes == []


# --- gate-first: zero remote writes when ineligible ---------------------------


def test_gate_failure_produces_zero_remote_writes(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    pub = _publish_settings(settings)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
        gate_passed=False,
        task_id="task_pub_gate",
    )
    store.create(task)
    fake = FakeGitHubClient()
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)

    assert outcome.status == "skipped"
    assert outcome.attempted is False
    assert "quality gate" in outcome.reason
    assert fake.calls == [] and fake.writes == []


def test_needs_review_result_produces_zero_remote_writes(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    pub = _publish_settings(settings)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
        result="needs_review",
        task_id="task_pub_review",
    )
    store.create(task)
    fake = FakeGitHubClient()
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)

    assert outcome.status == "skipped"
    assert fake.calls == [] and fake.writes == []


def test_container_isolation_is_required_by_default(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    # default settings require container isolation and this run used the local
    # process sandbox, so publishing must be refused without touching the network
    pub = _publish_settings(settings, publish_requires_container=True)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
        isolation="local-process",
        task_id="task_pub_iso",
    )
    store.create(task)
    fake = FakeGitHubClient()
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)

    assert outcome.status == "failed"
    assert outcome.error_code == "code_execution_not_isolated"
    assert fake.calls == [] and fake.writes == []
    persisted = store.get(task.task_id)
    assert persisted is not None and persisted.status is TaskStatus.failed


def test_publishing_disabled_skips(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    pub = replace(settings, enable_github_publish=False)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
    )
    store.create(task)
    fake = FakeGitHubClient()
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)
    assert outcome.status == "skipped"
    assert fake.calls == []


def test_no_changes_is_reported_without_writes(settings: Settings, repair_repo) -> None:
    prepared = prepare_repo(settings, repair_repo.path, task_id="task_pub_clean")
    pub = _publish_settings(settings)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
        task_id="task_pub_clean",
    )
    store.create(task)
    fake = FakeGitHubClient()
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)
    assert outcome.status == "failed"
    assert outcome.error_code == "github_no_changes"
    assert fake.writes == []


# --- error handling -----------------------------------------------------------


def test_permission_denied_parks_for_review(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    pub = _publish_settings(settings)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
        task_id="task_pub_denied",
    )
    store.create(task)
    fake = FakeGitHubClient(fail_ops={"get_repo": GitHubAuthError("no scope")})
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)

    assert outcome.status == "failed"
    assert outcome.error_code == "github_permission_denied"
    assert fake.writes == []
    persisted = store.get(task.task_id)
    assert persisted is not None and persisted.status is TaskStatus.needs_review


def test_existing_branch_is_a_conflict(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    pub = _publish_settings(settings)
    branch = build_branch_name(pub.github_branch_prefix, "task_pub_conflict")
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
        task_id="task_pub_conflict",
    )
    store.create(task)
    fake = FakeGitHubClient(refs={branch: "preexisting"})
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)

    assert outcome.status == "failed"
    assert outcome.error_code == "github_conflict"
    assert fake.writes == []
    assert fake.refs[branch] == "preexisting"
    persisted = store.get(task.task_id)
    assert persisted is not None and persisted.status is TaskStatus.needs_review


def test_api_failure_rolls_back_the_branch(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path)
    pub = _publish_settings(settings)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
        task_id="task_pub_rollback",
    )
    store.create(task)
    fake = FakeGitHubClient(
        fail_ops={"create_pull_request": GitHubUnavailableError("github is down")}
    )
    outcome = _publisher(pub, store, fake).publish_task(task.task_id)

    assert outcome.status == "failed"
    assert outcome.error_code == "github_unavailable"
    # the half finished branch is removed and no PR is left behind
    assert outcome.branch not in fake.refs
    assert fake.pull_request_count == 0
    assert "delete_ref" in outcome.operations


# --- helpers ------------------------------------------------------------------


def test_slug_from_repo_url_variants() -> None:
    assert slug_from_repo_url("https://github.com/acme/widget.git") == ("acme", "widget")
    assert slug_from_repo_url("git@github.com:acme/widget.git") == ("acme", "widget")
    assert slug_from_repo_url("https://gitlab.com/acme/widget") is None
    assert slug_from_repo_url("/local/path/repo") is None


def test_resolve_slug_prefers_override(settings: Settings) -> None:
    task = Task(
        task_id="t",
        run_id="r",
        request=TaskRequest(repo_url="/local/repo", mode="repair", problem="x"),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    assert resolve_slug(task, settings) is None
    assert resolve_slug(task, replace(settings, github_repo_slug="acme/widget")) == (
        "acme",
        "widget",
    )
