"""Build the pluggable backends (task store, queue, locks) from settings.

Each backend has a dependency-free default and an optional external
implementation. The external drivers are imported lazily inside the factory
functions, so importing this module never requires ``redis`` or ``psycopg`` and
the default test suite never needs an external service.
"""

from __future__ import annotations

from repopilot.config import Settings
from repopilot.errors import InvalidRequestError
from repopilot.locking import InMemoryLockManager, LockManager, RedisLockManager
from repopilot.queue.base import TaskQueue
from repopilot.queue.memory import InMemoryTaskQueue
from repopilot.storage.base import TaskStore
from repopilot.storage.memory import InMemoryTaskStore


def build_task_store(settings: Settings) -> TaskStore:
    """Return the configured :class:`TaskStore`."""

    backend = (settings.task_store_backend or "memory").strip().lower()
    if backend == "memory":
        return InMemoryTaskStore()
    if backend == "postgres":
        from repopilot.storage.postgres import postgres_task_store

        return postgres_task_store(settings.postgres_dsn)
    raise InvalidRequestError(f"unknown task store backend {settings.task_store_backend!r}")


def build_task_queue(settings: Settings) -> TaskQueue:
    """Return the configured :class:`TaskQueue`."""

    backend = (settings.queue_backend or "memory").strip().lower()
    if backend == "memory":
        return InMemoryTaskQueue()
    if backend == "redis":
        from repopilot.queue.redis_queue import redis_task_queue

        return redis_task_queue(settings.redis_url, prefix=f"{settings.redis_key_prefix}queue:")
    raise InvalidRequestError(f"unknown queue backend {settings.queue_backend!r}")


def build_lock_manager(settings: Settings) -> LockManager:
    """Return the configured :class:`LockManager`."""

    backend = (settings.lock_backend or "memory").strip().lower()
    if backend == "memory":
        return InMemoryLockManager()
    if backend == "redis":
        # Imported on demand: redis is an optional dependency.
        import redis

        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        return RedisLockManager(client, prefix=f"{settings.redis_key_prefix}lock:")
    raise InvalidRequestError(f"unknown lock backend {settings.lock_backend!r}")
