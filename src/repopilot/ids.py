"""Identifier helpers for tasks and runs."""

from __future__ import annotations

import uuid


def new_task_id() -> str:
    """Return a new opaque task identifier."""

    return f"task_{uuid.uuid4().hex[:24]}"


def new_run_id() -> str:
    """Return a new opaque run identifier (one execution attempt of a task)."""

    return f"run_{uuid.uuid4().hex[:24]}"
