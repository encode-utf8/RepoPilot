"""Project scan and Python project recognition."""

from __future__ import annotations

import sys

from repopilot.config import Settings
from repopilot.fixtures import FixtureRepo
from repopilot.scan.scanner import scan_project


def test_scans_buggy_fixture(settings: Settings, buggy_repo: FixtureRepo) -> None:
    profile = scan_project(buggy_repo.path, settings)
    assert profile.is_python_project is True
    assert profile.primary_language == "python"
    assert profile.python_requires == ">=3.11"
    assert "pyproject.toml" in profile.dependency_manifests
    assert profile.requirements_files == ["requirements.txt"]
    assert "pytest" in profile.test_frameworks
    assert profile.test_dirs == ["tests"]
    assert profile.test_file_count == 1
    assert "ruff" in profile.dev_tools
    assert profile.suggested_test_command is not None
    assert profile.suggested_test_command[0] == sys.executable
    assert "tests" in profile.suggested_test_command


def test_scans_clean_fixture(settings: Settings, clean_repo: FixtureRepo) -> None:
    profile = scan_project(clean_repo.path, settings)
    assert profile.is_python_project is True
    assert profile.requirements_files == ["requirements.txt"]
    assert profile.test_file_count == 1


def test_non_python_directory_is_reported(settings: Settings, tmp_path) -> None:
    other = tmp_path / "docs-only"
    other.mkdir()
    (other / "README.md").write_text("# docs\n", encoding="utf-8")
    profile = scan_project(other, settings)
    assert profile.is_python_project is False
    assert profile.primary_language == "unknown"
    assert profile.suggested_test_command is None
