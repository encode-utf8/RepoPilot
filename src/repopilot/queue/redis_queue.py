"""Redis backed :class:`TaskQueue`.

Only plain Redis data types are used (a list for the FIFO, sets for the running
and cancellation state), so the same client works for a single node or a managed
instance. Importing this module never imports ``redis``; the client is injected
and :func:`redis_task_queue` is the only place that needs the driver.
"""

from __future__ import annotations


class RedisTaskQueue:
    """A durable FIFO task queue and cancellation set on top of Redis."""

    def __init__(
        self,
        client,
        *,
        prefix: str = "repopilot:queue:",
        owns_client: bool = False,
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._queue_key = f"{prefix}pending"
        self._pending_set = f"{prefix}pending_set"
        self._running_set = f"{prefix}running"
        self._cancel_set = f"{prefix}cancel"
        self._owns_client = owns_client

    def push(self, task_id: str) -> None:
        pipe = self._client.pipeline()
        pipe.rpush(self._queue_key, task_id)
        pipe.sadd(self._pending_set, task_id)
        pipe.execute()

    def pop(self, *, timeout: float = 1.0) -> str | None:
        # redis-py requires a whole-number timeout for BLPOP.
        seconds = max(int(timeout), 1)
        item = self._client.blpop([self._queue_key], timeout=seconds)
        if not item:
            return None
        _, task_id = item
        if isinstance(task_id, bytes):  # pragma: no cover - decode_responses is set
            task_id = task_id.decode("utf-8")
        pipe = self._client.pipeline()
        pipe.srem(self._pending_set, task_id)
        pipe.sadd(self._running_set, task_id)
        pipe.execute()
        return task_id

    def complete(self, task_id: str) -> None:
        pipe = self._client.pipeline()
        pipe.srem(self._running_set, task_id)
        pipe.srem(self._cancel_set, task_id)
        pipe.execute()

    def is_running(self, task_id: str) -> bool:
        return bool(self._client.sismember(self._running_set, task_id))

    def is_active(self, task_id: str) -> bool:
        if self._client.sismember(self._running_set, task_id):
            return True
        return bool(self._client.sismember(self._pending_set, task_id))

    def request_cancel(self, task_id: str) -> bool:
        if not self.is_active(task_id):
            return False
        self._client.sadd(self._cancel_set, task_id)
        return True

    def cancel_requested(self, task_id: str) -> bool:
        return bool(self._client.sismember(self._cancel_set, task_id))

    def clear_cancel(self, task_id: str) -> None:
        self._client.srem(self._cancel_set, task_id)

    def pending_count(self) -> int:
        return int(self._client.scard(self._pending_set))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def redis_task_queue(
    url: str,
    *,
    prefix: str = "repopilot:queue:",
) -> RedisTaskQueue:
    """Build a queue from a Redis URL (imports ``redis`` lazily)."""

    # Imported on demand: redis is an optional dependency.
    import redis

    client = redis.Redis.from_url(url, decode_responses=True)
    return RedisTaskQueue(client, prefix=prefix, owns_client=True)
