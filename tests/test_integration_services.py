"""Opt-in integration tests against real Redis and PostgreSQL.

Skipped by default so the suite never needs an external service. Point them at
live instances to run the real backends (``docker-compose.yml`` starts both)::

    REPOPILOT_TEST_REDIS_URL=redis://127.0.0.1:6379/0 \
    REPOPILOT_TEST_POSTGRES_DSN=postgresql://repopilot:repopilot@127.0.0.1:5432/repopilot \
    pytest tests/test_integration_services.py
"""

from __future__ import annotations

import os
import uuid

import pytest

REDIS_URL = os.environ.get("REPOPILOT_TEST_REDIS_URL")
POSTGRES_DSN = os.environ.get("REPOPILOT_TEST_POSTGRES_DSN")


@pytest.mark.skipif(not REDIS_URL, reason="set REPOPILOT_TEST_REDIS_URL to run")
def test_real_redis_queue_and_lock_round_trip() -> None:
    import redis

    from repopilot.locking import RedisLockManager
    from repopilot.queue.redis_queue import RedisTaskQueue

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    prefix = f"repopilot:test:{uuid.uuid4().hex}:"
    queue = RedisTaskQueue(client, prefix=prefix, owns_client=False)
    try:
        queue.push("t1")
        assert queue.pending_count() == 1
        assert queue.pop(timeout=2) == "t1"
        assert queue.is_running("t1") is True
        assert queue.request_cancel("t1") is True
        assert queue.cancel_requested("t1") is True
        queue.complete("t1")
        assert queue.is_active("t1") is False

        lock = RedisLockManager(client, prefix=f"{prefix}lock:")
        assert lock.acquire("k", owner="a", ttl_seconds=30) is True
        assert lock.acquire("k", owner="b", ttl_seconds=30) is False
        lock.release("k", owner="a")
        assert lock.is_locked("k") is False
    finally:
        queue.close()


@pytest.mark.skipif(not POSTGRES_DSN, reason="set REPOPILOT_TEST_POSTGRES_DSN to run")
def test_real_postgres_store_round_trip() -> None:
    import psycopg

    from repopilot.models import Task, TaskRequest, TaskStatus, utcnow
    from repopilot.storage.postgres import postgres_task_store

    store = postgres_task_store(POSTGRES_DSN)
    task_id = f"task_it_{uuid.uuid4().hex[:10]}"
    try:
        store.create(
            Task(
                task_id=task_id,
                run_id="run_it",
                request=TaskRequest(repo_url="/tmp/repo"),
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        loaded = store.get(task_id)
        assert loaded is not None and loaded.task_id == task_id

        loaded.status = TaskStatus.running
        store.save(loaded)
        stored = store.get(task_id)
        assert stored is not None and stored.status is TaskStatus.running
        assert store.find_active_by_key(stored.request.dedupe_key()) is not None
        assert any(item.task_id == task_id for item in store.list())
    finally:
        with psycopg.connect(POSTGRES_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM repopilot_tasks WHERE task_id = %s", (task_id,))
        store.close()
