"""PostgreSQL backed :class:`TaskStore`.

The whole :class:`~repopilot.models.Task` (status, report, PR metadata, usage and
timestamps) is stored as JSONB; the columns that operators query on are lifted to
the top level so they can be indexed. The protocol is unchanged, so the pipeline,
API and worker code do not know which store they are talking to.

``psycopg`` is imported lazily: importing this module is cheap and works even when
the driver is not installed, as long as the store is never constructed.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from repopilot.models import TERMINAL_TASK_STATUSES, Task, utcnow

_TABLE = "repopilot_tasks"

_DEDUPE_SEPARATOR = "\x1f"

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    task_id           TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    mode              TEXT NOT NULL,
    dedupe_key        TEXT NOT NULL,
    resolved_commit   TEXT,
    failure_code      TEXT,
    pull_request_url  TEXT,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL,
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    payload           JSONB NOT NULL
)
"""

_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {_TABLE}_dedupe_idx ON {_TABLE} (dedupe_key)",
    f"CREATE INDEX IF NOT EXISTS {_TABLE}_status_idx ON {_TABLE} (status)",
    f"CREATE INDEX IF NOT EXISTS {_TABLE}_created_idx ON {_TABLE} (created_at DESC)",
)

_COLUMNS = (
    "task_id, status, mode, dedupe_key, resolved_commit, failure_code, "
    "pull_request_url, created_at, updated_at, started_at, finished_at, payload"
)
_PLACEHOLDERS = "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb"

_TERMINAL_VALUES = tuple(status.value for status in TERMINAL_TASK_STATUSES)


def dedupe_key_string(key: tuple[str, ...]) -> str:
    """Join a request dedupe key into a single indexed string."""

    return _DEDUPE_SEPARATOR.join(key)


def _row_values(task: Task) -> tuple[Any, ...]:
    pull_request_url = task.pull_request.url if task.pull_request is not None else None
    return (
        task.task_id,
        task.status.value,
        task.request.mode,
        dedupe_key_string(task.request.dedupe_key()),
        task.resolved_commit,
        task.failure_code,
        pull_request_url,
        task.created_at,
        task.updated_at,
        task.started_at,
        task.finished_at,
        json.dumps(task.model_dump(mode="json")),
    )


def _decode_payload(payload: Any) -> Task:
    if isinstance(payload, str):
        return Task.model_validate_json(payload)
    return Task.model_validate(payload)


class PostgresTaskStore:
    """Durable task storage on PostgreSQL using one connection per thread."""

    def __init__(self, dsn: str | None = None, *, connect=None) -> None:
        if connect is None and dsn is None:
            raise ValueError("PostgresTaskStore needs a DSN or a connect callable")
        self._dsn = dsn
        self._connect = connect
        self._local = threading.local()

    # -- connection ------------------------------------------------------------

    def _connection(self):
        if self._connect is not None:
            return self._connect()
        conn = getattr(self._local, "conn", None)
        if conn is None or getattr(conn, "closed", False):
            # Imported on demand: psycopg is an optional dependency.
            import psycopg

            conn = psycopg.connect(self._dsn)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close the thread's connection, if any."""

        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def ensure_schema(self) -> None:
        """Create the table and indexes when missing."""

        conn = self._connection()
        with conn.cursor() as cursor:
            cursor.execute(_SCHEMA_SQL)
            for statement in _INDEX_SQL:
                cursor.execute(statement)
        conn.commit()

    # -- TaskStore protocol ----------------------------------------------------

    def create(self, task: Task) -> Task:
        conn = self._connection()
        with conn.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {_TABLE} ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
                _row_values(task),
            )
        conn.commit()
        return task.model_copy(deep=True)

    def save(self, task: Task) -> Task:
        stored = task.model_copy(deep=True)
        stored.updated_at = utcnow()
        assignments = ", ".join(
            f"{column} = EXCLUDED.{column}"
            for column in _COLUMNS.split(", ")
            if column != "task_id"
        )
        conn = self._connection()
        with conn.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {_TABLE} ({_COLUMNS}) VALUES ({_PLACEHOLDERS}) "
                f"ON CONFLICT (task_id) DO UPDATE SET {assignments}",
                _row_values(stored),
            )
        conn.commit()
        return stored.model_copy(deep=True)

    def get(self, task_id: str) -> Task | None:
        conn = self._connection()
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT payload FROM {_TABLE} WHERE task_id = %s", (task_id,))
            row = cursor.fetchone()
        return _decode_payload(row[0]) if row is not None else None

    def list(self) -> list[Task]:
        conn = self._connection()
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT payload FROM {_TABLE} ORDER BY created_at DESC")
            rows = cursor.fetchall()
        return [_decode_payload(row[0]) for row in rows]

    def find_active_by_key(self, key: tuple[str, ...]) -> Task | None:
        placeholders = ", ".join(["%s"] * len(_TERMINAL_VALUES))
        conn = self._connection()
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT payload FROM {_TABLE} "
                f"WHERE dedupe_key = %s AND status NOT IN ({placeholders}) "
                "ORDER BY created_at DESC LIMIT 1",
                (dedupe_key_string(key), *_TERMINAL_VALUES),
            )
            row = cursor.fetchone()
        return _decode_payload(row[0]) if row is not None else None


def postgres_task_store(dsn: str, *, ensure_schema: bool = True) -> PostgresTaskStore:
    """Build a store from a DSN and (by default) create the schema."""

    store = PostgresTaskStore(dsn)
    if ensure_schema:
        store.ensure_schema()
    return store
