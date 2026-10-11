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
from repopilot.credentials import EnvCredentialProvider, GitHubCredentialProvider
from repopilot.errors import InvalidRequestError
from repopilot.github.base import GitHubClient
from repopilot.github.rest import RestGitHubClient
from repopilot.ids import new_run_id, new_task_id
from repopilot.llm.base import ModelClient
from repopilot.pipeline.evaluate import AssessmentRunner
from repopilot.pipeline.publish import PullRequestPublisher
from repopilot.pipeline.repair import RepairRunner
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
    repair_runner: RepairRunner | None = None
    model_client: ModelClient | None = None
    publisher: PullRequestPublisher | None = None
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
    resolved_provider = credential_provider or EnvCredentialProvider(resolved_settings)
    resolved_github = github_client
    if resolved_github is None and resolved_settings.enable_github_publish:
        resolved_github = RestGitHubClient(resolved_settings, provider=resolved_provider)
    resolved_publisher = publisher or PullRequestPublisher(
        settings=resolved_settings, store=resolved_store, client=resolved_github
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
        repair_runner=resolved_repair_runner,
        model_client=model_client,
        publisher=resolved_publisher,
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
