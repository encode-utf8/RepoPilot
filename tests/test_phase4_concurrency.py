"""Concurrent publishing is serialised by the distributed lock (Phase 4)."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from repopilot.config import Settings
from repopilot.github.fake import FakeGitHubClient
from repopilot.locking import InMemoryLockManager
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
from repopilot.models import TestSnapshot as Snapshot
from repopilot.pipeline.publish import PullRequestPublisher, build_branch_name
from repopilot.report.builder import build_report
from repopilot.storage.memory import InMemoryTaskStore
from support import STATS_PATH, STATS_TARGET_TEST, prepare_repo

PROBLEM = "tests/test_stats.py::test_mean_of_values fails: mean returns 3.0 not 2.0"


class _BlockingGitHub(FakeGitHubClient):
    """Fake client that parks inside ``create_pull_request`` until released."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def create_pull_request(self, *args, **kwargs):
        self.entered.set()
        self.release.wait(timeout=15.0)
        return super().create_pull_request(*args, **kwargs)


def _pub_settings(settings: Settings) -> Settings:
    return replace(
        settings,
        enable_github_publish=True,
        publish_requires_container=False,
        github_repo_slug="acme/widget",
    )


def _repair_outcome() -> RepairOutcome:
    return RepairOutcome(
        termination_reason=TerminationReason.completed,
        result="fixed",
        attempts=1,
        max_rounds=4,
        patch=PatchArtifact(
            applied=True,
            changed_files=[STATS_PATH],
            added_lines=1,
            removed_lines=1,
            diff_hash="deadbeef",
        ),
        quality_gate=QualityGateReport(passed=True),
        baseline_tests=Snapshot(ran=True, total=4, passed=3, failed=1),
        final_tests=Snapshot(ran=True, total=4, passed=4, failed=0),
        target_tests=[STATS_TARGET_TEST],
        located_files=[STATS_PATH],
    )


def _build_task(*, workspace, repo_path: Path, baseline: str, task_id: str) -> Task:
    report = build_report(
        task_id=task_id,
        run_id="run_pub",
        status=TaskStatus.succeeded,
        repo=RepoMetadata(source=str(repo_path), default_branch="main", resolved_commit=baseline),
        project=ProjectProfile(),
        checkers=[],
        repair=_repair_outcome(),
    )
    return Task(
        task_id=task_id,
        run_id="run_pub",
        status=TaskStatus.succeeded,
        request=TaskRequest(
            repo_url=str(repo_path),
            mode="repair",
            problem=PROBLEM,
            target_tests=[STATS_TARGET_TEST],
        ),
        created_at=utcnow(),
        updated_at=utcnow(),
        resolved_commit=baseline,
        sandbox_isolation="container",
        workspace_path=str(workspace.root),
        report=report,
    )


def _dirty_workspace(settings: Settings, repo_path: Path, *, task_id: str):
    prepared = prepare_repo(settings, repo_path, task_id=task_id)
    target = prepared.repo_dir / STATS_PATH
    fixed = target.read_text(encoding="utf-8").replace(
        "sum(values) / (len(values) - 1)", "sum(values) / len(values)"
    )
    target.write_text(fixed, encoding="utf-8", newline="\n")
    return prepared


def test_concurrent_publish_creates_exactly_one_pr(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path, task_id="task_lock")
    pub = _pub_settings(settings)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
        task_id="task_lock",
    )
    store.create(task)

    fake = _BlockingGitHub()
    lock_manager = InMemoryLockManager()
    publisher = PullRequestPublisher(
        settings=pub, store=store, client=fake, lock_manager=lock_manager
    )

    winner: dict[str, object] = {}

    def run_winner() -> None:
        winner["outcome"] = publisher.publish_task(task.task_id)

    thread = threading.Thread(target=run_winner)
    thread.start()
    try:
        # the winner owns the lock while it is inside create_pull_request
        assert fake.entered.wait(timeout=15.0)
        loser = publisher.publish_task(task.task_id)
    finally:
        fake.release.set()
        thread.join(timeout=15.0)

    assert not thread.is_alive()
    assert winner["outcome"].status == "created"
    assert loser.status == "skipped"
    assert loser.error_code == "publish_in_progress"
    assert loser.attempted is False

    # exactly one branch and one PR, and the winner's record was not clobbered
    expected_branch = build_branch_name(pub.github_branch_prefix, task.task_id)
    assert list(fake.refs) == [expected_branch]
    assert fake.pull_request_count == 1
    persisted = store.get(task.task_id)
    assert persisted is not None
    assert persisted.pull_request is not None
    assert persisted.pull_request.number == fake.pull_requests[0].number


def test_sequential_publish_is_idempotent(settings: Settings, repair_repo) -> None:
    prepared = _dirty_workspace(settings, repair_repo.path, task_id="task_idem")
    pub = _pub_settings(settings)
    store = InMemoryTaskStore()
    task = _build_task(
        workspace=prepared.workspace,
        repo_path=repair_repo.path,
        baseline=prepared.checkout.resolved_commit,
        task_id="task_idem",
    )
    store.create(task)
    fake = FakeGitHubClient()
    publisher = PullRequestPublisher(
        settings=pub, store=store, client=fake, lock_manager=InMemoryLockManager()
    )

    first = publisher.publish_task(task.task_id)
    second = publisher.publish_task(task.task_id)
    assert first.status == "created"
    assert second.status == "reused"
    assert fake.pull_request_count == 1
    assert len(fake.refs) == 1
