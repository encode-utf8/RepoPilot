"""Small text helpers."""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")
_NON_SLUG_RE = re.compile(r"[^a-z0-9\-]+")


def slugify(value: str) -> str:
    """Return a lowercase, hyphen separated slug."""

    collapsed = _WHITESPACE_RE.sub("-", value.strip().lower())
    return _NON_SLUG_RE.sub("", collapsed)


def truncate_text(value: str, limit: int) -> str:
    """Truncate ``value`` to ``limit`` characters, appending an ellipsis."""

    if limit < 0:
        raise ValueError("limit must not be negative")
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."
