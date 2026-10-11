"""Structured JSONL logging correlated by ``task_id`` and ``run_id``.

Every record is one JSON object per line. Values are redacted before they are
written, so credentials can never reach the log file even by accident.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from repopilot.models import utcnow

REDACTED = "***REDACTED***"

_SECRET_KEY_RE = re.compile(
    r"token|secret|password|passwd|api[_-]?key|authorization|credential|private[_-]?key",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secret looking keys and values."""

    if key is not None and _SECRET_KEY_RE.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in _SECRET_VALUE_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text
    return value


class RunLogger:
    """Append only JSONL logger for a single task run."""

    def __init__(self, log_dir: Path, task_id: str, run_id: str) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.log_dir = Path(log_dir)
        self.path = self.log_dir / f"{task_id}.jsonl"
        self._lock = threading.Lock()
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def event(
        self,
        event: str,
        message: str = "",
        *,
        level: str = "info",
        **data: Any,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": utcnow().isoformat(),
            "level": level,
            "event": event,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "message": message,
        }
        if data:
            record["data"] = redact(data)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{line}\n")
        return record

    def events(self, tail: int | None = None) -> list[dict[str, Any]]:
        """Read back the persisted events, optionally the last ``tail`` records."""

        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    records.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        if tail is not None and tail >= 0:
            return records[-tail:]
        return records
