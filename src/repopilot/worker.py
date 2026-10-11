"""Dedicated worker process: ``python -m repopilot.worker``.

Builds the configured store and queue, reconciles tasks left ``running`` by a
previous crash, then consumes the queue until interrupted. This is the process a
deployment scales horizontally when ``REPOPILOT_SCHEDULER_BACKEND=queue``.
"""

from __future__ import annotations

import threading

from repopilot.bootstrap import build_lock_manager, build_task_queue, build_task_store
from repopilot.config import Settings, load_settings
from repopilot.credentials import EnvCredentialProvider
from repopilot.github.rest import RestGitHubClient
from repopilot.llm.registry import build_model_client
from repopilot.pipeline.evaluate import AssessmentRunner
from repopilot.pipeline.publish import PullRequestPublisher
from repopilot.pipeline.repair import RepairRunner
from repopilot.queue.worker import TaskWorker


def build_worker(settings: Settings, *, reconcile_on_start: bool = False) -> TaskWorker:
    """Assemble a worker from the configured backends."""

    store = build_task_store(settings)
    queue = build_task_queue(settings)
    lock = build_lock_manager(settings)
    provider = EnvCredentialProvider(settings)
    github = (
        RestGitHubClient(settings, provider=provider) if settings.enable_github_publish else None
    )
    publisher = PullRequestPublisher(
        settings=settings, store=store, client=github, lock_manager=lock
    )
    runners = {
        "assess": AssessmentRunner(settings=settings, store=store),
        "repair": RepairRunner(
            settings=settings,
            store=store,
            model_client=build_model_client(settings),
            publisher=publisher,
        ),
    }
    return TaskWorker(
        store=store,
        queue=queue,
        runner_for=lambda task: runners[task.request.mode],
        settings=settings,
        poll_timeout=settings.worker_poll_timeout_seconds,
        reconcile_on_start=reconcile_on_start,
    )


def main() -> None:
    settings = load_settings()
    worker = build_worker(settings)
    affected = worker.recover_stale_tasks(include_pending=settings.reconcile_pending_on_startup)
    if affected:
        print(f"reconciled {len(affected)} interrupted task(s): {', '.join(affected)}")
    stop = threading.Event()
    try:
        worker.run_forever(stop=stop)
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        stop.set()
        print("worker stopped")


if __name__ == "__main__":
    main()
