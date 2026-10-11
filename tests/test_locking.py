"""Cross-process lock primitives (in-memory implementation)."""

from __future__ import annotations

import threading
import time

from repopilot.locking import InMemoryLockManager, hold_lock, new_lock_owner


def test_acquire_is_exclusive_until_released() -> None:
    manager = InMemoryLockManager()
    owner = new_lock_owner()
    assert manager.acquire("k", owner=owner, ttl_seconds=30) is True
    assert manager.is_locked("k") is True
    assert manager.acquire("k", owner=new_lock_owner(), ttl_seconds=30) is False
    manager.release("k", owner=owner)
    assert manager.is_locked("k") is False
    assert manager.acquire("k", owner=new_lock_owner(), ttl_seconds=30) is True


def test_release_by_other_owner_is_a_noop() -> None:
    manager = InMemoryLockManager()
    owner = new_lock_owner()
    assert manager.acquire("k", owner=owner, ttl_seconds=30) is True
    manager.release("k", owner="someone-else")
    assert manager.is_locked("k") is True
    manager.release("k", owner=owner)
    assert manager.is_locked("k") is False


def test_expired_lock_can_be_reacquired() -> None:
    manager = InMemoryLockManager()
    assert manager.acquire("k", owner="a", ttl_seconds=0.0) is True
    time.sleep(0.02)
    assert manager.is_locked("k") is False
    assert manager.acquire("k", owner="b", ttl_seconds=30) is True


def test_hold_lock_yields_true_and_releases() -> None:
    manager = InMemoryLockManager()
    with hold_lock(manager, "k", ttl_seconds=30) as acquired:
        assert acquired is True
        assert manager.is_locked("k") is True
    assert manager.is_locked("k") is False


def test_hold_lock_loser_yields_false_without_releasing_winner() -> None:
    manager = InMemoryLockManager()
    with hold_lock(manager, "k", ttl_seconds=30) as first:
        assert first is True
        with hold_lock(manager, "k", ttl_seconds=30) as second:
            assert second is False
        # the losing context manager must not release the winner's lock
        assert manager.is_locked("k") is True
    assert manager.is_locked("k") is False


def test_concurrent_acquire_has_exactly_one_winner() -> None:
    manager = InMemoryLockManager()
    barrier = threading.Barrier(8)
    results: list[bool] = []
    guard = threading.Lock()

    def worker() -> None:
        barrier.wait()
        got = manager.acquire("k", owner=new_lock_owner(), ttl_seconds=30)
        with guard:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    assert results.count(True) == 1
