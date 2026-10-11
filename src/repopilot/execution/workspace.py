"""Per task workspace layout."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from repopilot.config import Settings
from repopilot.errors import WorkspaceError


@dataclass(frozen=True)
class Workspace:
    """Directories reserved for a single task run."""

    task_id: str
    root: Path
    repo_dir: Path
    artifacts_dir: Path
    sandbox_dir: Path

    def relative(self, path: Path) -> str:
        """Return a POSIX style path relative to the checkout, for reports."""

        try:
            return path.resolve().relative_to(self.repo_dir.resolve()).as_posix()
        except ValueError:
            return path.name


class WorkspaceManager:
    """Creates and disposes per task workspaces under ``settings.workspace_root``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create(self, task_id: str) -> Workspace:
        root = (self._settings.workspace_root / task_id).resolve()
        repo_dir = root / "repo"
        artifacts_dir = root / "artifacts"
        sandbox_dir = root / "sandbox"
        try:
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            sandbox_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(f"cannot prepare workspace {root}: {exc}") from exc
        return Workspace(
            task_id=task_id,
            root=root,
            repo_dir=repo_dir,
            artifacts_dir=artifacts_dir,
            sandbox_dir=sandbox_dir,
        )

    def cleanup(self, workspace: Workspace, *, keep: bool | None = None) -> None:
        should_keep = self._settings.keep_workspace if keep is None else keep
        if should_keep:
            return
        shutil.rmtree(workspace.root, ignore_errors=True)
