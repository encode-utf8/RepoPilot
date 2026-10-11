"""External interaction adapters (Dify)."""

from __future__ import annotations

from repopilot.integrations.dify import render_task
from repopilot.integrations.dify import router as dify_router

__all__ = ["dify_router", "render_task"]
