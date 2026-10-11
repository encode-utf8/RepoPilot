"""Controlled fixture repositories.

Phase 1 requires repositories with known, detectable problems so the pipeline can
be verified end to end. Templates live under ``fixtures/``; this module turns a
template into a real git repository with a stable baseline commit (and a second
commit, so that pinning a specific baseline can be exercised).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"

_GIT_IDENTITY_ARGS: tuple[str, ...] = (
    "-c",
    "user.name=RepoPilot Fixture",
    "-c",
    "user.email=fixtures@repopilot.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "init.defaultBranch=main",
    "-c",
    "core.autocrlf=false",
)
_GIT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class FixtureRepo:
    """A materialised fixture repository."""

    name: str
    path: Path
    baseline_commit: str
    head_commit: str


def available_fixtures() -> list[str]:
    """Return the names of the fixture templates shipped with the project."""

    return sorted(
        entry.name
        for entry in FIXTURES_DIR.iterdir()
        if entry.is_dir() and (entry / "pyproject.toml").is_file()
    )


def _git(args: list[str], cwd: Path) -> str:
    """Run git plumbing on a controlled fixture template.

    Development tooling only: it never runs repository code, the templates are
    first party, and the hard timeout bounds every call. The product execution
    path (``repopilot.execution.process.run_command``) additionally enforces an
    output byte cap, which is unnecessary for these small git calls.
    """

    result = subprocess.run(
        ["git", *_GIT_IDENTITY_ARGS, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result.stdout.strip()


def materialise_fixture(
    name: str,
    destination: str | Path,
    *,
    include_followup_commit: bool = True,
) -> FixtureRepo:
    """Copy a fixture template into ``destination`` and initialise a git repository."""

    template = FIXTURES_DIR / name
    if not template.is_dir():
        raise FileNotFoundError(f"unknown fixture template: {name}")
    dest = Path(destination).resolve() / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(template, dest)

    _git(["init", "-q"], dest)
    _git(["add", "-A"], dest)
    _git(["commit", "-q", "-m", "fixture baseline"], dest)
    baseline_commit = _git(["rev-parse", "HEAD"], dest)

    if include_followup_commit:
        (dest / "CHANGELOG.md").write_text(
            "# Changelog\n\n- fixture follow-up commit\n", encoding="utf-8", newline="\n"
        )
        _git(["add", "-A"], dest)
        _git(["commit", "-q", "-m", "fixture follow-up"], dest)

    return FixtureRepo(
        name=name,
        path=dest,
        baseline_commit=baseline_commit,
        head_commit=_git(["rev-parse", "HEAD"], dest),
    )
