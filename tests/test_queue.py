"""In-memory task queue semantics."""

from __future__ import annotations

import threading
import time

from repopilot.queue.memory import InMemoryTaskQueue


def test_fifo_order_and_running_set() -> None:
    queue = InMemoryTaskQueue()
    queue.push("a")
    queue.push("b")
    assert queue.pending_count() == 2
    assert queue.pop(timeout=0.01) == "a"
    assert queue.is_running("a") is True
    assert queue.is_active("a") is True
    assert queue.pop(timeout=0.01) == "b"
    assert queue.pop(timeout=0.01) is None
    assert queue.pending_count() == 0


def test_timeout_returns_none_without_side_effects() -> None:
    queue = InMemoryTaskQueue()
    start = time.monotonic()
    assert queue.pop(timeout=0.05) is None
    assert time.monotonic() - start >= 0.03
    assert queue.pending_count() == 0


def test_blocking_pop_wakes_up_on_push() -> None:
    queue = InMemoryTaskQueue()
    results: list[str | None] = []

    def consume() -> None:
        results.append(queue.pop(timeout=2.0))

    thread = threading.Thread(target=consume)
    thread.start()
    time.sleep(0.05)
    queue.push("late")
    thread.join(timeout=3.0)
    assert results == ["late"]


def test_complete_clears_running_and_cancel_flag() -> None:
    queue = InMemoryTaskQueue()
    queue.push("a")
    assert queue.pop(timeout=0.01) == "a"
    assert queue.request_cancel("a") is True
    assert queue.cancel_requested("a") is True
    queue.complete("a")
    assert queue.is_running("a") is False
    assert queue.is_active("a") is False
    assert queue.cancel_requested("a") is False


def test_request_cancel_only_for_active_tasks() -> None:
    queue = InMemoryTaskQueue()
    assert queue.request_cancel("ghost") is False
    queue.push("p")
    assert queue.request_cancel("p") is True
    assert queue.cancel_requested("p") is True
    queue.clear_cancel("p")
    assert queue.cancel_requested("p") is False
    # a pending task is still active until it is popped and completed
    assert queue.is_active("p") is True
    assert queue.pop(timeout=0.01) == "p"
    assert queue.is_active("p") is True
    queue.complete("p")
    assert queue.is_active("p") is False
