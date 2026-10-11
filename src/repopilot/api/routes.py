"""Task API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from repopilot.api.schemas import (
    CancelTaskResponse,
    CreateTaskRequest,
    CreateTaskResponse,
    LogResponse,
    TaskView,
)
from repopilot.errors import (
    RepoPilotError,
    ReportNotReadyError,
    TaskConflictError,
    TaskNotFoundError,
)
from repopilot.logging_setup import RunLogger
from repopilot.models import TERMINAL_TASK_STATUSES, Report, Task, TaskStatus, utcnow

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


@router.post("/tasks", status_code=status.HTTP_202_ACCEPTED, response_model=CreateTaskResponse)
def create_task(payload: CreateTaskRequest, context=Depends(get_context)) -> CreateTaskResponse:
    """Create an assessment or repair task and start it in the background."""

    task_request = payload.to_task_request()
    runner = context.runner_for(task_request.mode)
    existing = context.store.find_active_by_key(task_request.dedupe_key())
    if existing is not None:
        return CreateTaskResponse(
            task_id=existing.task_id,
            run_id=existing.run_id,
            status=existing.status,
            deduplicated=True,
        )

    task = Task(
        task_id=context.task_id_factory(),
        run_id=context.run_id_factory(),
        status=TaskStatus.pending,
        request=task_request,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    stored = context.store.create(task)
    context.scheduler.submit(stored.task_id, context.submit(stored.task_id, runner))
    return CreateTaskResponse(task_id=stored.task_id, run_id=stored.run_id, status=stored.status)


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
