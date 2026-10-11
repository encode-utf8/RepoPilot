"""FastAPI application factory and application context wiring."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI

from repopilot import __version__
from repopilot.api.routes import register_exception_handlers
from repopilot.api.routes import router as api_router
from repopilot.cancellation import CancellationToken
from repopilot.checkers.base import Checker
from repopilot.config import Settings, load_settings
from repopilot.ids import new_run_id, new_task_id
from repopilot.pipeline.evaluate import AssessmentRunner
from repopilot.scheduler.base import TaskScheduler
from repopilot.scheduler.thread_pool import ThreadPoolTaskScheduler
from repopilot.storage.base import TaskStore
from repopilot.storage.memory import InMemoryTaskStore


@dataclass
class AppContext:
    """Collaborators shared by all request handlers."""

    settings: Settings
    store: TaskStore
    scheduler: TaskScheduler
    runner: AssessmentRunner
    task_id_factory: Callable[[], str] = field(default=new_task_id)
    run_id_factory: Callable[[], str] = field(default=new_run_id)

    def submit(self, task_id: str):
        """Return the callable the scheduler executes for ``task_id``."""

        def _run(token: CancellationToken) -> None:
            self.runner.run(task_id, token)

        return _run


def create_app(
    *,
    settings: Settings | None = None,
    store: TaskStore | None = None,
    scheduler: TaskScheduler | None = None,
    checkers: list[Checker] | None = None,
    runner: AssessmentRunner | None = None,
) -> FastAPI:
    """Create the FastAPI application.

    Everything is injected so tests can substitute a temporary workspace, a fake
    store or a stub sandbox without patching module globals.
    """

    resolved_settings = settings or load_settings()
    resolved_store = store or InMemoryTaskStore()
    resolved_scheduler = scheduler or ThreadPoolTaskScheduler(
        max_workers=resolved_settings.scheduler_max_workers
    )
    resolved_runner = runner or AssessmentRunner(
        settings=resolved_settings, store=resolved_store, checkers=checkers
    )
    context = AppContext(
        settings=resolved_settings,
        store=resolved_store,
        scheduler=resolved_scheduler,
        runner=resolved_runner,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Iterator[None]:
        try:
            yield
        finally:
            resolved_scheduler.shutdown(wait=False)

    app = FastAPI(title="RepoPilot", version=__version__, lifespan=lifespan)
    app.state.context = context
    app.include_router(api_router)
    register_exception_handlers(app)

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
