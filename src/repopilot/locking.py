"""Cross process mutual exclusion.

Two critical sections need "at most one actor" semantics across workers: creating
a task for a given dedupe key, and publishing a Draft PR for a given task. The
boundary is a tiny protocol so single-process deployments and tests can use an
in-memory implementation while production points at Redis. Nothing here stores or
logs credentials.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol, runtime_checkable


@runtime_checkable
class LockManager(Protocol):
    """A best effort distributed mutex with an owner token and a TTL."""

    def acquire(self, key: str, *, owner: str, ttl_seconds: float) -> bool:
        """Try to take ``key`` for ``owner``; returns ``False`` when already held."""

    def release(self, key: str, *, owner: str) -> None:
        """Release ``key`` when ``owner`` still holds it (otherwise a no-op)."""

    def is_locked(self, key: str) -> bool:
        """Return ``True`` while ``key`` is held by anyone."""


def new_lock_owner() -> str:
    """Return a fresh opaque owner token."""

    return uuid.uuid4().hex


@contextmanager
def hold_lock(manager: LockManager, key: str, *, ttl_seconds: float) -> Iterator[bool]:
    """Yield whether ``key`` was acquired, releasing it on the way out."""

    owner = new_lock_owner()
    acquired = manager.acquire(key, owner=owner, ttl_seconds=ttl_seconds)
    try:
        yield acquired
    finally:
        if acquired:
            manager.release(key, owner=owner)


class InMemoryLockManager:
    """Process local locks used by the default single-process deployment."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, *, owner: str, ttl_seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            current = self._held.get(key)
            if current is not None and current[1] > now and current[0] != owner:
                return False
            self._held[key] = (owner, now + max(ttl_seconds, 0.001))
            return True

    def release(self, key: str, *, owner: str) -> None:
        with self._lock:
            current = self._held.get(key)
            if current is not None and current[0] == owner:
                del self._held[key]

    def is_locked(self, key: str) -> bool:
        with self._lock:
            current = self._held.get(key)
            return current is not None and current[1] > time.monotonic()


_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


class RedisLockManager:
    """Redis ``SET NX PX`` locks; release is compare-and-delete via Lua."""

    def __init__(self, client, *, prefix: str = "repopilot:lock:") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def acquire(self, key: str, *, owner: str, ttl_seconds: float) -> bool:
        ttl_ms = max(int(ttl_seconds * 1000), 1)
        return bool(self._client.set(self._key(key), owner, nx=True, px=ttl_ms))

    def release(self, key: str, *, owner: str) -> None:
        self._client.eval(_RELEASE_SCRIPT, 1, self._key(key), owner)

    def is_locked(self, key: str) -> bool:
        return bool(self._client.exists(self._key(key)))
