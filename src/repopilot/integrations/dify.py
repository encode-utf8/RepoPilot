"""Dify integration: task trigger, parameter passing and result display.

Dify is the interaction and task-entry layer (design doc section 2.1). It never
executes repository code, never manages permissions and never touches GitHub
credentials. It only calls this thin adapter, which maps a Dify tool payload onto
the existing task API and renders the result back in a tool-friendly shape.

The router is only mounted when ``REPOPILOT_ENABLE_DIFY=true`` so a Dify outage or
absence can never affect the core service or the test suite.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError

from repopilot.api.schemas import CreateTaskRequest, TaskView
from repopilot.errors import InvalidRequestError, TaskNotFoundError
from repopilot.models import Task, TaskStatus

router = APIRouter(prefix="/api/integrations/dify", tags=["dify"])

#: Dify passes every input as a string, so these need coercion back to a list.
_LIST_FIELDS = ("target_tests", "allowed_paths")
#: Empty strings from Dify mean "not provided".
_NULLABLE_FIELDS = ("base_branch", "base_commit", "problem", "resume_task_id")


def _context(request: Request):
    context = getattr(request.app.state, "context", None)
    if context is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return context


def normalise_dify_payload(payload: dict) -> dict:
    """Extract the task body from a Dify tool request and coerce its fields."""

    body = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else payload
    body = {key: value for key, value in body.items() if key in CreateTaskRequest.model_fields}
    for field in _LIST_FIELDS:
        value = body.get(field)
        if isinstance(value, str):
            items = value.replace("\n", ",").split(",")
            body[field] = [item.strip() for item in items if item.strip()]
    for field in _NULLABLE_FIELDS:
        if isinstance(body.get(field), str) and not body[field].strip():
            body[field] = None
    return body


def _text_summary(task: Task) -> str:
    if not task.is_terminal:
        return f"task {task.task_id} is {task.status.value}"
    if task.status is TaskStatus.succeeded:
        suffix = f", draft PR: {task.pull_request.url}" if task.pull_request else ""
        return f"task succeeded: {task.task_id}{suffix}"
    return f"task {task.status.value}: {task.failure_reason or 'no reason recorded'}"


def render_task(task: Task) -> dict:
    """Render a task for a Dify workflow node to display or pass downstream."""

    view = TaskView.from_task(task)
    return {
        "task_id": task.task_id,
        "run_id": task.run_id,
        "status": task.status.value,
        "terminal": task.is_terminal,
        "progress": task.progress,
        "text": _text_summary(task),
        "result": {
            "failure_code": task.failure_code,
            "failure_reason": task.failure_reason,
            "resolved_commit": task.resolved_commit,
            "pull_request_url": task.pull_request.url if task.pull_request else None,
            "report_available": task.report is not None,
            "summary": view.summary.model_dump() if view.summary is not None else None,
        },
    }


@router.post("/tasks", status_code=status.HTTP_202_ACCEPTED)
def create_dify_task(payload: dict, context=Depends(_context)) -> dict:
    """Trigger a task from a Dify tool node."""

    body = normalise_dify_payload(payload)
    try:
        request = CreateTaskRequest(**body).to_task_request()
    except ValidationError as exc:
        raise InvalidRequestError(f"invalid dify payload: {exc.errors()}") from exc
    task, deduplicated = context.service.submit(request)
    rendered = render_task(task)
    rendered["deduplicated"] = deduplicated
    return rendered


@router.get("/tasks/{task_id}")
def get_dify_task(task_id: str, context=Depends(_context)) -> dict:
    """Return task progress and result for a Dify workflow node."""

    task = context.store.get(task_id)
    if task is None:
        raise TaskNotFoundError(f"unknown task id: {task_id}")
    return render_task(task)


@router.post("/tasks/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_dify_task(task_id: str, context=Depends(_context)) -> dict:
    """Request cancellation from a Dify workflow node."""

    from repopilot.api.routes import cancel_task

    return cancel_task(task_id, context=context).model_dump()
