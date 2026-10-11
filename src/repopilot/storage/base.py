"""``TaskStore`` boundary.

Phase 1 ships an in-memory implementation. Phase 4 replaces it with PostgreSQL
without touching the pipeline, because everything depends on this protocol only.
"""

from __future__ import annotations

from typing import Protocol

from repopilot.models import Task


class TaskStore(Protocol):
    """Storage boundary for task records."""

    def create(self, task: Task) -> Task:
        """Persist a new task and return the stored copy."""

    def get(self, task_id: str) -> Task | None:
        """Return the task or ``None`` when unknown."""

    def save(self, task: Task) -> Task:
        """Persist an updated task and return the stored copy."""

    def list(self) -> list[Task]:
        """Return all known tasks, newest first."""

    def find_active_by_key(self, key: tuple[str, str, str]) -> Task | None:
        """Return a non terminal task matching the request dedupe key."""
