"""Domain errors carrying stable machine readable failure codes."""

from __future__ import annotations


class RepoPilotError(Exception):
    """Base class for every RepoPilot domain error."""

    code = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def failure_reason(self) -> str:
        """Human readable reason persisted on the task record."""

        return f"{self.code}: {self.message}"


class InvalidRequestError(RepoPilotError):
    code = "invalid_request"


class TaskNotFoundError(RepoPilotError):
    code = "task_not_found"


class TaskConflictError(RepoPilotError):
    code = "task_conflict"


class ReportNotReadyError(RepoPilotError):
    code = "report_not_ready"


class WorkspaceError(RepoPilotError):
    code = "workspace_error"


class RepoSourceError(RepoPilotError):
    code = "source_invalid"


class CloneError(RepoPilotError):
    code = "clone_failed"


class CommitResolveError(RepoPilotError):
    code = "commit_unresolved"


class CodeExecutionDeniedError(RepoPilotError):
    code = "code_execution_denied"


class CommandTimeoutError(RepoPilotError):
    code = "command_timeout"


class CheckerError(RepoPilotError):
    code = "checker_error"


class ToolError(RepoPilotError):
    """A controlled agent tool rejected its input or could not complete."""

    code = "tool_error"


class PatchApplyError(ToolError):
    code = "patch_apply_failed"


class ModelError(RepoPilotError):
    """Base class for model client failures."""

    code = "model_error"


class ModelUnavailableError(ModelError):
    """The model endpoint is unreachable, timed out or returned a server error."""

    code = "model_unavailable"


class ModelRequestError(ModelError):
    """The model endpoint rejected the request (bad request, auth, rate limit)."""

    code = "model_request_failed"


class ModelOutputError(ModelError):
    """The model answered, but the answer could not be used as specified."""

    code = "model_output_invalid"


class ModelBudgetExceededError(ModelError):
    """A model call would exceed the per task call or token budget."""

    code = "model_budget_exceeded"


class TestsUnavailableError(RepoPilotError):
    """The baseline test run could not be executed or parsed."""

    code = "tests_unavailable"
