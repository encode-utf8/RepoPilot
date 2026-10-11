"""Task persistence abstractions."""

from __future__ import annotations

from repopilot.storage.base import TaskStore
from repopilot.storage.memory import InMemoryTaskStore

__all__ = ["InMemoryTaskStore", "TaskStore"]
