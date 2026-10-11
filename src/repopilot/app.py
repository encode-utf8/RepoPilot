"""FastAPI application factory and application context wiring."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI

from repopilot import __version__
from repopilot.api.routes import register_exception_handlers
from repopilot.api.routes import router as api_router
from repopilot.bootstrap import build_lock_manager, build_task_queue, build_task_store
from repopilot.cancellation import CancellationToken
from repopilot.checkers.base import Checker
from repopilot.config import Settings, load_settings
from repopilot.credentials import EnvCredentialProvider, GitHubCredentialProvider
from repopilot.errors import InvalidRequestError
from repopilot.github.base import GitHubClient
from repopilot.github.rest import RestGitHubClient
from repopilot.ids import new_run_id, new_task_id
from repopilot.integrations.dify import router as dify_router
from repopilot.llm.base import ModelClient
from repopilot.locking import LockManager
from repopilot.pipeline.evaluate import AssessmentRunner
from repopilot.pipeline.publish import PullRequestPublisher
from repopilot.pipeline.repair import RepairRunner
from repopilot.queue.base import TaskQueue
from repopilot.queue.scheduler import QueueTaskScheduler
from repopilot.queue.worker import TaskWorker
from repopilot.scheduler.base import TaskScheduler
from repopilot.scheduler.thread_pool import ThreadPoolTaskScheduler
from repopilot.service import TaskService
from repopilot.storage.base import TaskStore


@dataclass
class AppContext:
    """Collaborators shared by all request handlers."""

    settings: Settings
    store: TaskStore
    scheduler: TaskScheduler
    runner: AssessmentRunner
    lock_manager: LockManager
    #: Wired after construction so it can reuse :meth:`runner_for`.
    service: TaskService | None = None
    repair_runner: RepairRunner | None = None
    model_client: ModelClient | None = None
    publisher: PullRequestPublisher | None = None
    worker: TaskWorker | None = None
    task_id_factory: Callable[[], str] = field(default=new_task_id)
    run_id_factory: Callable[[], str] = field(default=new_run_id)

    def runner_for(self, mode: str):
        """Return the pipeline that handles ``mode`` or raise a 400-mapped error."""

        if mode == "repair":
            if self.repair_runner is None:
                raise InvalidRequestError("repair mode is not configured on this server")
            return self.repair_runner
        return self.runner

    def submit(self, task_id: str, runner=None):
        """Return the callable the scheduler executes for ``task_id``."""

        resolved = runner or self.runner

        def _run(token: CancellationToken) -> None:
            resolved.run(task_id, token)

        return _run


def create_app(
    *,
    settings: Settings | None = None,
    store: TaskStore | None = None,
    scheduler: TaskScheduler | None = None,
    checkers: list[Checker] | None = None,
    runner: AssessmentRunner | None = None,
    model_client: ModelClient | None = None,
    repair_runner: RepairRunner | None = None,
    github_client: GitHubClient | None = None,
    credential_provider: GitHubCredentialProvider | None = None,
    publisher: PullRequestPublisher | None = None,
    sandbox_factory=None,
    lock_manager: LockManager | None = None,
    task_queue: TaskQueue | None = None,
    service: TaskService | None = None,
    worker: TaskWorker | None = None,
) -> FastAPI:
    """Create the FastAPI application.

    Everything is injected so tests can substitute a temporary workspace, an
    in-memory or fake store, or a stub sandbox without patching module globals.
    """

    resolved_settings = settings or load_settings()
    resolved_store = store or build_task_store(resolved_settings)
    resolved_lock = lock_manager or build_lock_manager(resolved_settings)

    queue = task_queue
    if queue is None and scheduler is None and resolved_settings.scheduler_backend == "queue":
        queue = build_task_queue(resolved_settings)
    if scheduler is not None:
        resolved_scheduler = scheduler
    elif queue is not None:
        resolved_scheduler = QueueTaskScheduler(queue)
    else:
        resolved_scheduler = ThreadPoolTaskScheduler(
            max_workers=resolved_settings.scheduler_max_workers
        )

    resolved_runner = runner or AssessmentRunner(
        settings=resolved_settings, store=resolved_store, checkers=checkers
    )
    resolved_provider = credential_provider or EnvCredentialProvider(resolved_settings)
    resolved_github = github_client
    if resolved_github is None and resolved_settings.enable_github_publish:
        resolved_github = RestGitHubClient(resolved_settings, provider=resolved_provider)
    resolved_publisher = publisher or PullRequestPublisher(
        settings=resolved_settings,
        store=resolved_store,
        client=resolved_github,
        lock_manager=resolved_lock,
    )
    resolved_repair_runner = repair_runner or RepairRunner(
        settings=resolved_settings,
        store=resolved_store,
        model_client=model_client,
        publisher=resolved_publisher,
        sandbox_factory=sandbox_factory,
    )
    context = AppContext(
        settings=resolved_settings,
        store=resolved_store,
        scheduler=resolved_scheduler,
        runner=resolved_runner,
        lock_manager=resolved_lock,
        repair_runner=resolved_repair_runner,
        model_client=model_client,
        publisher=resolved_publisher,
    )
    context.service = service or TaskService(
        store=resolved_store,
        scheduler=resolved_scheduler,
        lock_manager=resolved_lock,
        runner_for=context.runner_for,
        task_id_factory=context.task_id_factory,
        run_id_factory=context.run_id_factory,
        dedupe_lock_ttl_seconds=resolved_settings.dedupe_lock_ttl_seconds,
    )

    resolved_worker = worker
    if resolved_worker is None and resolved_settings.worker_enabled and queue is not None:
        resolved_worker = TaskWorker(
            store=resolved_store,
            queue=queue,
            runner_for=lambda task: context.runner_for(task.request.mode),
            settings=resolved_settings,
            poll_timeout=resolved_settings.worker_poll_timeout_seconds,
            reconcile_on_start=resolved_settings.reconcile_running_on_startup,
        )
    context.worker = resolved_worker

    worker_stop = threading.Event()
    worker_thread: threading.Thread | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Iterator[None]:
        nonlocal worker_thread
        if resolved_worker is not None:
            worker_thread = threading.Thread(
                target=resolved_worker.run_forever,
                kwargs={"stop": worker_stop},
                name="repopilot-worker",
                daemon=True,
            )
            worker_thread.start()
        try:
            yield
        finally:
            worker_stop.set()
            if worker_thread is not None:
                worker_thread.join(
                    timeout=max(resolved_settings.worker_poll_timeout_seconds * 3, 1.0)
                )
            resolved_scheduler.shutdown(wait=False)
            close = getattr(resolved_store, "close", None)
            if callable(close):
                close()

    app = FastAPI(title="RepoPilot", version=__version__, lifespan=lifespan)
    app.state.context = context
    app.include_router(api_router)
    if resolved_settings.enable_dify:
        app.include_router(dify_router)
    register_exception_handlers(app)

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
