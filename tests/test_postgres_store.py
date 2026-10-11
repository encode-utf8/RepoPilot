"""PostgreSQL TaskStore behaviour against a lightweight in-memory double.

The double understands only the handful of statements the store issues, which is
enough to prove the SQL is parameterised and that a task survives a JSONB round
trip. Real PostgreSQL is exercised by the opt-in integration test.
"""

from __future__ import annotations

import json

import pytest

from repopilot.models import Task, TaskRequest, TaskStatus, utcnow
from repopilot.storage.postgres import (
    PostgresTaskStore,
    dedupe_key_string,
    postgres_task_store,
)


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection
        self._rows: list[tuple] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._rows = self._connection.execute(sql, params)

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class _FakeConnection:
    def __init__(self) -> None:
        self.rows: dict[str, tuple] = {}
        self.statements: list[str] = []
        self.commits = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True

    def execute(self, sql: str, params: tuple) -> list[tuple]:
        self.statements.append(sql)
        text = " ".join(sql.split())
        if text.startswith("CREATE"):
            return []
        if text.startswith("INSERT INTO"):
            self.rows[params[0]] = tuple(params)
            return []
        if "WHERE task_id = %s" in text:
            row = self.rows.get(params[0])
            return [(row[-1],)] if row is not None else []
        if "WHERE dedupe_key = %s" in text:
            key = params[0]
            blocked = set(params[1:])
            matches = [row for row in self.rows.values() if row[3] == key and row[1] not in blocked]
            matches.sort(key=lambda row: row[7], reverse=True)
            return [(matches[0][-1],)] if matches else []
        if "ORDER BY created_at DESC" in text:
            rows = sorted(self.rows.values(), key=lambda row: row[7], reverse=True)
            return [(row[-1],) for row in rows]
        raise AssertionError(f"unexpected SQL: {text}")


def _task(task_id: str, *, mode: str = "assess", repo_url: str = "/tmp/repo") -> Task:
    return Task(
        task_id=task_id,
        run_id=f"run_{task_id}",
        request=TaskRequest(
            repo_url=repo_url,
            mode=mode,
            problem="x" if mode == "repair" else None,
        ),
        created_at=utcnow(),
        updated_at=utcnow(),
    )


@pytest.fixture
def store() -> PostgresTaskStore:
    connection = _FakeConnection()
    created = PostgresTaskStore(connect=lambda: connection)
    created.ensure_schema()
    return created


def test_requires_a_dsn_or_connect_callable() -> None:
    with pytest.raises(ValueError):
        PostgresTaskStore()


def test_ensure_schema_creates_table_and_indexes(store: PostgresTaskStore) -> None:
    statements = store._connection().statements
    assert any("CREATE TABLE IF NOT EXISTS" in statement for statement in statements)
    assert sum("CREATE INDEX" in statement for statement in statements) == 3


def test_create_and_get_round_trip(store: PostgresTaskStore) -> None:
    store.create(_task("task_1"))
    loaded = store.get("task_1")
    assert loaded is not None
    assert loaded.task_id == "task_1"
    assert loaded.status is TaskStatus.pending
    assert store.get("task_missing") is None


def test_insert_is_parameterised(store: PostgresTaskStore) -> None:
    connection = store._connection()
    store.create(_task("task_abc"))
    insert = next(sql for sql in connection.statements if sql.startswith("INSERT INTO"))
    assert "task_abc" not in insert
    assert "%s::jsonb" in insert
    assert insert.count("%s") == 12


def test_save_upserts_and_stamps_updated_at(store: PostgresTaskStore) -> None:
    created = store.create(_task("task_2"))
    created.status = TaskStatus.running
    saved = store.save(created)
    assert saved.status is TaskStatus.running
    assert saved.updated_at >= created.updated_at
    reloaded = store.get("task_2")
    assert reloaded is not None and reloaded.status is TaskStatus.running


def test_list_returns_newest_first(store: PostgresTaskStore) -> None:
    first = _task("task_a")
    second = _task("task_b")
    second.created_at = first.created_at.replace(year=first.created_at.year + 1)
    store.create(first)
    store.create(second)
    ids = [task.task_id for task in store.list()]
    assert ids == ["task_b", "task_a"]


def test_find_active_by_key_skips_terminal_tasks(store: PostgresTaskStore) -> None:
    task = store.create(_task("task_3"))
    key = task.request.dedupe_key()
    assert store.find_active_by_key(key) is not None

    task.status = TaskStatus.succeeded
    store.save(task)
    assert store.find_active_by_key(key) is None
    # an unrelated key never matches
    assert store.find_active_by_key(("other",)) is None


def test_dedupe_key_string_is_stable() -> None:
    assert dedupe_key_string(("a", "b")) == "a\x1fb"


def test_factory_builds_a_store_without_connecting(monkeypatch) -> None:
    built = postgres_task_store("postgresql://user:pw@localhost/db", ensure_schema=False)
    assert isinstance(built, PostgresTaskStore)


def test_payload_uses_json_serialisable_model_dump() -> None:
    task = _task("task_4", mode="repair")
    payload = json.dumps(task.model_dump(mode="json"))
    assert Task.model_validate_json(payload).task_id == "task_4"
