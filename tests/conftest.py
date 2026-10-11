"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from repopilot.config import Settings
from repopilot.fixtures import FixtureRepo, materialise_fixture


@pytest.fixture
def trusted_root(tmp_path: Path) -> Path:
    root = tmp_path / "trusted"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Fast, offline friendly settings: dependency audit is opt-in per test."""

    return Settings(
        workspace_root=tmp_path / "workspaces",
        log_dir=tmp_path / "logs",
        enable_dependency_audit=False,
        trusted_source_roots=(tmp_path / "trusted",),
        clone_timeout_seconds=60.0,
        command_timeout_seconds=120.0,
        max_command_timeout_seconds=300.0,
        dependency_audit_timeout_seconds=30.0,
    )


@pytest.fixture
def buggy_repo(trusted_root: Path) -> FixtureRepo:
    return materialise_fixture("py_calc_buggy", trusted_root)


@pytest.fixture
def clean_repo(trusted_root: Path) -> FixtureRepo:
    return materialise_fixture("py_lib_clean", trusted_root)
