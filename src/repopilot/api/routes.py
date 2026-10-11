"""Task API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from repopilot.api.schemas import (
    CancelTaskResponse,
    CreateTaskRequest,
    CreateTaskResponse,
    LogResponse,
    TaskUsageView,
    TaskView,
)
from repopilot.errors import (
    InvalidRequestError,
    RepoPilotError,
    ReportNotReadyError,
    TaskConflictError,
    TaskNotFoundError,
)
from repopilot.logging_setup import RunLogger
from repopilot.models import (
    TERMINAL_TASK_STATUSES,
    PublishOutcome,
    Report,
    Task,
    TaskStatus,
)

router = APIRouter(prefix="/api", tags=["tasks"])


def get_context(request: Request):
    """Return the application context wired up in :func:`repopilot.app.create_app`."""

    context = getattr(request.app.state, "context", None)
    if context is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return context


def _load_task(context, task_id: str) -> Task:
    task = context.store.get(task_id)
    if task is None:
        raise TaskNotFoundError(f"unknown task id: {task_id}")
    return task


def _service(context):
    """Return the task service or fail loudly when the app was misconfigured."""

    service = getattr(context, "service", None)
    if service is None:  # pragma: no cover - defensive
        raise InvalidRequestError("task service is not configured on this server")
    return service


@router.post("/tasks", status_code=status.HTTP_202_ACCEPTED, response_model=CreateTaskResponse)
def create_task(payload: CreateTaskRequest, context=Depends(get_context)) -> CreateTaskResponse:
    """Create an assessment or repair task and enqueue it in the background."""

    task_request = payload.to_task_request()
    # Validate the mode up front so an unsupported mode is a 400, not a task that
    # is created only to fail dispatch later.
    context.runner_for(task_request.mode)
    stored, deduplicated = _service(context).submit(task_request)
    return CreateTaskResponse(
        task_id=stored.task_id,
        run_id=stored.run_id,
        status=stored.status,
        deduplicated=deduplicated,
    )


@router.get("/tasks", response_model=list[TaskView])
def list_tasks(
    status_filter: TaskStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=1000),
    context=Depends(get_context),
) -> list[TaskView]:
    """List known tasks (newest first) with an optional status filter."""

    tasks = context.store.list()
    if status_filter is not None:
        tasks = [task for task in tasks if task.status is status_filter]
    return [TaskView.from_task(task) for task in tasks[:limit]]


@router.post(
    "/tasks/{task_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateTaskResponse,
)
def retry_task(task_id: str, context=Depends(get_context)) -> CreateTaskResponse:
    """Re-run a finished task as a fresh task; 409 while it is still active."""

    stored, deduplicated = _service(context).retry(task_id)
    return CreateTaskResponse(
        task_id=stored.task_id,
        run_id=stored.run_id,
        status=stored.status,
        deduplicated=deduplicated,
    )


@router.get("/tasks/{task_id}/usage", response_model=TaskUsageView)
def get_usage(task_id: str, context=Depends(get_context)) -> TaskUsageView:
    """Return the resource and progress view for a task."""

    return TaskUsageView.from_task(_load_task(context, task_id), context.settings)


@router.get("/tasks/{task_id}", response_model=TaskView)
def get_task(task_id: str, context=Depends(get_context)) -> TaskView:
    """Return the current state of a task."""

    return TaskView.from_task(_load_task(context, task_id))


@router.get("/tasks/{task_id}/report", response_model=Report)
def get_report(task_id: str, context=Depends(get_context)) -> Report:
    """Return the assessment report; 409 until the task reaches a terminal state."""

    task = _load_task(context, task_id)
    if task.report is None:
        raise ReportNotReadyError(
            f"report is not available yet for task {task_id} (status: {task.status.value})"
        )
    return task.report


@router.get("/tasks/{task_id}/logs", response_model=LogResponse)
def get_logs(
    task_id: str,
    tail: int | None = Query(default=None, ge=1, le=5000),
    context=Depends(get_context),
) -> LogResponse:
    """Return the structured JSONL event log for a task."""

    task = _load_task(context, task_id)
    logger = RunLogger(context.settings.log_dir, task.task_id, task.run_id)
    events = logger.events(tail=tail)
    return LogResponse(task_id=task.task_id, run_id=task.run_id, count=len(events), events=events)


@router.post(
    "/tasks/{task_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CancelTaskResponse,
)
def cancel_task(task_id: str, context=Depends(get_context)) -> CancelTaskResponse:
    """Request cooperative cancellation of a running task."""

    task = _load_task(context, task_id)
    if task.status in TERMINAL_TASK_STATUSES:
        reason = (
            f"task {task_id} already finished with status "
            f"{task.status.value} and cannot be cancelled"
        )
        raise TaskConflictError(reason)
    if not context.scheduler.cancel(task_id):
        raise TaskConflictError(f"task {task_id} is not scheduled and cannot be cancelled")
    updated = context.store.get(task_id) or task
    updated.progress = "cancel requested"
    context.store.save(updated)
    return CancelTaskResponse(**TaskView.from_task(updated).model_dump())


@router.post("/tasks/{task_id}/pull-request", response_model=PublishOutcome)
def publish_pull_request(task_id: str, context=Depends(get_context)) -> PublishOutcome:
    """Publish a verified repair as a Draft PR (idempotent per task)."""

    task = _load_task(context, task_id)
    if not task.is_terminal:
        raise TaskConflictError(
            f"task {task_id} is still {task.status.value} and cannot be published yet"
        )
    if context.publisher is None:
        raise InvalidRequestError("pull request publishing is not configured on this server")
    return context.publisher.publish_task(task_id)


def register_exception_handlers(app) -> None:
    """Map domain errors onto HTTP status codes."""

    @app.exception_handler(TaskNotFoundError)
    async def _not_found(_request: Request, exc: TaskNotFoundError):
        return _json_error(status.HTTP_404_NOT_FOUND, exc)

    @app.exception_handler(ReportNotReadyError)
    async def _not_ready(_request: Request, exc: ReportNotReadyError):
        return _json_error(status.HTTP_409_CONFLICT, exc)

    @app.exception_handler(TaskConflictError)
    async def _conflict(_request: Request, exc: TaskConflictError):
        return _json_error(status.HTTP_409_CONFLICT, exc)

    @app.exception_handler(RepoPilotError)
    async def _domain_error(_request: Request, exc: RepoPilotError):
        return _json_error(status.HTTP_400_BAD_REQUEST, exc)


def _json_error(status_code: int, exc: RepoPilotError):
    return JSONResponse(
        status_code=status_code,
        content={"error": exc.code, "code": exc.code, "detail": exc.message},
    )
