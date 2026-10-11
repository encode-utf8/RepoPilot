"""Redis queue and lock behaviour against a fake Redis client."""

from __future__ import annotations

from repopilot.locking import RedisLockManager
from repopilot.queue.redis_queue import RedisTaskQueue


class _Pipeline:
    def __init__(self, client: FakeRedis) -> None:
        self._client = client
        self._ops: list[tuple] = []

    def rpush(self, key: str, value: str) -> None:
        self._ops.append(("rpush", key, value))

    def sadd(self, key: str, value: str) -> None:
        self._ops.append(("sadd", key, value))

    def srem(self, key: str, value: str) -> None:
        self._ops.append(("srem", key, value))

    def execute(self) -> None:
        for op in self._ops:
            name, key, value = op
            if name == "rpush":
                self._client.rpush(key, value)
            elif name == "sadd":
                self._client.sadd(key, value)
            elif name == "srem":
                self._client.srem(key, value)
        self._ops.clear()


class FakeRedis:
    """The subset of redis-py the backends actually call."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.values: dict[str, str] = {}
        self.closed = False

    def pipeline(self) -> _Pipeline:
        return _Pipeline(self)

    def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    def blpop(self, keys: list[str], timeout: int = 1):
        for key in keys:
            items = self.lists.get(key)
            if items:
                return (key, items.pop(0))
        return None

    def sadd(self, key: str, value: str) -> None:
        self.sets.setdefault(key, set()).add(value)

    def srem(self, key: str, value: str) -> None:
        self.sets.setdefault(key, set()).discard(value)

    def sismember(self, key: str, value: str) -> bool:
        return value in self.sets.get(key, set())

    def scard(self, key: str) -> int:
        return len(self.sets.get(key, set()))

    def exists(self, key: str) -> int:
        return 1 if key in self.values else 0

    def set(self, key: str, value: str, *, nx: bool = False, px: int | None = None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def eval(self, script: str, numkeys: int, key: str, owner: str) -> int:
        if self.values.get(key) == owner:
            del self.values[key]
            return 1
        return 0

    def close(self) -> None:
        self.closed = True


def test_queue_push_pop_running_and_complete() -> None:
    queue = RedisTaskQueue(FakeRedis())
    queue.push("a")
    queue.push("b")
    assert queue.pending_count() == 2
    assert queue.pop(timeout=1) == "a"
    assert queue.is_running("a") is True
    assert queue.is_active("a") is True
    assert queue.pop(timeout=1) == "b"
    assert queue.pop(timeout=1) is None
    assert queue.pending_count() == 0
    queue.complete("a")
    assert queue.is_running("a") is False
    assert queue.is_active("a") is False


def test_queue_cancel_only_when_active() -> None:
    queue = RedisTaskQueue(FakeRedis())
    assert queue.request_cancel("ghost") is False
    queue.push("p")
    assert queue.request_cancel("p") is True
    assert queue.cancel_requested("p") is True
    queue.clear_cancel("p")
    assert queue.cancel_requested("p") is False
    queue.pop(timeout=1)
    assert queue.is_active("p") is True
    queue.complete("p")
    assert queue.is_active("p") is False


def test_queue_close_only_closes_owned_clients() -> None:
    client = FakeRedis()
    RedisTaskQueue(client, owns_client=False).close()
    assert client.closed is False
    RedisTaskQueue(client, owns_client=True).close()
    assert client.closed is True


def test_lock_acquire_and_release() -> None:
    manager = RedisLockManager(FakeRedis())
    assert manager.acquire("k", owner="a", ttl_seconds=30) is True
    assert manager.is_locked("k") is True
    assert manager.acquire("k", owner="b", ttl_seconds=30) is False
    manager.release("k", owner="a")
    assert manager.is_locked("k") is False
    assert manager.acquire("k", owner="b", ttl_seconds=30) is True


def test_lock_release_by_other_owner_is_a_noop() -> None:
    manager = RedisLockManager(FakeRedis())
    manager.acquire("k", owner="a", ttl_seconds=30)
    manager.release("k", owner="someone-else")
    assert manager.is_locked("k") is True
