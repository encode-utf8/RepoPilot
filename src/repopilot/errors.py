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
