"""Cloning and deterministic baseline commit pinning."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.errors import CloneError, CommitResolveError, RepoSourceError
from repopilot.execution.git import classify_source, clone_and_pin_repo, is_trusted_source
from repopilot.execution.sandbox import LocalProcessSandboxExecutor
from repopilot.execution.workspace import WorkspaceManager
from repopilot.fixtures import FixtureRepo


def _clone(settings: Settings, source: str, *, task_id: str = "task_git", **kwargs):
    workspace = WorkspaceManager(settings).create(task_id)
    sandbox = LocalProcessSandboxExecutor(settings, workspace, trusted_source=True)
    checkout = clone_and_pin_repo(
        source,
        workspace.repo_dir,
        settings=settings,
        sandbox=sandbox,
        token=CancellationToken(),
        **kwargs,
    )
    return workspace, checkout


def test_explicit_base_commit_is_pinned(settings: Settings, buggy_repo: FixtureRepo) -> None:
    workspace, checkout = _clone(
        settings,
        str(buggy_repo.path),
        base_commit=buggy_repo.baseline_commit,
        base_branch=None,
    )
    assert checkout.resolved_commit == buggy_repo.baseline_commit
    assert checkout.resolved_ref is None
    assert (workspace.repo_dir / "pyproject.toml").is_file()
    assert not (workspace.repo_dir / "CHANGELOG.md").exists()


def test_branch_head_is_resolved(settings: Settings, buggy_repo: FixtureRepo) -> None:
    _, checkout = _clone(settings, str(buggy_repo.path), base_commit=None, base_branch="main")
    assert checkout.resolved_commit == buggy_repo.head_commit
    assert checkout.resolved_ref == "main"


def test_default_branch_head_is_resolved(settings: Settings, buggy_repo: FixtureRepo) -> None:
    _, checkout = _clone(settings, str(buggy_repo.path), base_commit=None, base_branch=None)
    assert checkout.resolved_commit == buggy_repo.head_commit
    assert checkout.default_branch == "main"


def test_unknown_commit_raises(settings: Settings, buggy_repo: FixtureRepo) -> None:
    with pytest.raises(CommitResolveError):
        _clone(settings, str(buggy_repo.path), base_commit="0" * 40, base_branch=None)


def test_unknown_branch_raises(settings: Settings, buggy_repo: FixtureRepo) -> None:
    with pytest.raises(CommitResolveError):
        _clone(settings, str(buggy_repo.path), base_commit=None, base_branch="no-such-branch")


def test_missing_local_path_raises(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(RepoSourceError):
        _clone(settings, str(tmp_path / "not-a-repo"), base_commit=None, base_branch=None)


def test_blank_source_raises(settings: Settings) -> None:
    with pytest.raises(RepoSourceError):
        _clone(settings, "   ", base_commit=None, base_branch=None)


def test_unreachable_remote_fails_fast(settings: Settings) -> None:
    started = time.monotonic()
    with pytest.raises(CloneError) as excinfo:
        _clone(settings, "http://127.0.0.1:1/nope.git", base_commit=None, base_branch="main")
    assert time.monotonic() - started < 30
    assert "clone_failed" in excinfo.value.failure_reason()


def test_classify_source_variants() -> None:
    assert classify_source("https://github.com/org/repo.git").is_remote
    assert classify_source("git@github.com:org/repo.git").is_remote
    assert classify_source("E:/projects/repo").is_local
    assert classify_source("relative/path").is_local
    assert classify_source("file:///tmp/repo").is_local


def test_is_trusted_source(settings: Settings, buggy_repo: FixtureRepo, tmp_path: Path) -> None:
    assert is_trusted_source(str(buggy_repo.path), settings) is True
    outside = tmp_path / "outside" / "repo"
    outside.mkdir(parents=True)
    assert is_trusted_source(str(outside), settings) is False
    assert is_trusted_source("https://github.com/org/repo.git", settings) is False
