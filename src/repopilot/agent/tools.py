"""Controlled toolset: the agent's only way to touch the workspace.

There is deliberately no shell tool and no way to pass an arbitrary command line.
Each tool is a fixed function with a validated argument schema, so the model can
only choose *which* operation to run, never the executable or its flags:

``read_file``   read a bounded window of a repository file
``search_code`` regex search over repository files (pure Python, no shell)
``apply_patch`` apply a unified diff with ``git apply``
``run_tests``   run pytest through the phase 1 sandbox
``view_diff``   show the current working tree diff

Every subprocess still goes through
:class:`~repopilot.execution.sandbox.SandboxExecutor`, which enforces the
argument list, timeout, output cap and environment allowlist rules.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.errors import CodeExecutionDeniedError, RepoPilotError, ToolError
from repopilot.execution.sandbox import SandboxCommand, SandboxExecutor
from repopilot.execution.test_run import run_pytest
from repopilot.execution.workspace import Workspace
from repopilot.logging_setup import RunLogger, redact
from repopilot.models import TestSnapshot, ToolInvocation

TOOL_NAMES: tuple[str, ...] = (
    "read_file",
    "search_code",
    "apply_patch",
    "run_tests",
    "view_diff",
)

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        "build",
        "dist",
        ".tox",
        ".eggs",
    }
)
_HUNK_FILE_RE = re.compile(r"^(\+\+\+|---)\s+(?P<path>[^\t\n]+)")
_ARGUMENT_EXCERPT_CHARS = 2_000


class ReadFileArgs(BaseModel):
    """Arguments accepted by :meth:`WorkspaceTools.read_file`."""

    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class SearchCodeArgs(BaseModel):
    """Arguments accepted by :meth:`WorkspaceTools.search_code`."""

    model_config = ConfigDict(extra="forbid")

    pattern: str
    glob: str | None = None
    ignore_case: bool = False
    max_results: int | None = Field(default=None, ge=1, le=400)


class ApplyPatchArgs(BaseModel):
    """Arguments accepted by :meth:`WorkspaceTools.apply_patch`."""

    model_config = ConfigDict(extra="forbid")

    diff: str


class RunTestsArgs(BaseModel):
    """Arguments accepted by :meth:`WorkspaceTools.run_tests`."""

    model_config = ConfigDict(extra="forbid")

    targets: list[str] = Field(default_factory=list)
    timeout_seconds: float | None = Field(default=None, gt=0)


class ViewDiffArgs(BaseModel):
    """Arguments accepted by :meth:`WorkspaceTools.view_diff`."""

    model_config = ConfigDict(extra="forbid")


@dataclass
class ToolResult:
    """Outcome of one controlled tool call."""

    tool: str
    ok: bool
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error: str | None = None
    truncated: bool = False
    output_bytes: int = 0
    duration_ms: int = 0

    def to_invocation(
        self, *, index: int, round_index: int, arguments: dict[str, Any]
    ) -> ToolInvocation:
        """Build the bounded, redacted record persisted for this call."""

        return ToolInvocation(
            index=index,
            round=round_index,
            tool=self.tool,
            ok=self.ok,
            arguments=_cap_arguments(arguments),
            output_excerpt=self.output[:2_000],
            output_bytes=self.output_bytes,
            truncated=self.truncated,
            duration_ms=self.duration_ms,
            error_code=self.error_code,
            error=self.error,
            data=redact(self.data),
        )


def _cap_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    capped: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        text = value if isinstance(value, str) else str(value)
        capped[key] = (
            text
            if len(text) <= _ARGUMENT_EXCERPT_CHARS
            else f"{text[:_ARGUMENT_EXCERPT_CHARS]}... [truncated]"
        )
    return redact(capped)


class WorkspaceTools:
    """Fixed tool implementations bound to one task workspace."""

    def __init__(
        self,
        *,
        settings: Settings,
        workspace: Workspace,
        sandbox: SandboxExecutor,
        token: CancellationToken,
        logger: RunLogger,
        max_output_bytes: int | None = None,
    ) -> None:
        self._settings = settings
        self._workspace = workspace
        self._sandbox = sandbox
        self._token = token
        self._logger = logger
        self._max_output_bytes = max_output_bytes or settings.agent_max_tool_output_bytes

    # -- helpers ---------------------------------------------------------------

    @property
    def repo_dir(self) -> Path:
        return self._workspace.repo_dir

    def _resolve(self, raw: str) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("path must not be blank")
        candidate = Path(raw.replace("\\", "/"))
        root = self.repo_dir.resolve()
        target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if target != root and root not in target.parents:
            raise ToolError(f"path escapes the workspace: {raw}")
        return target

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_dir.resolve()).as_posix()
        except ValueError:
            return path.name

    def _cap(self, text: str) -> tuple[str, bool]:
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= self._max_output_bytes:
            return text, False
        trimmed = encoded[: self._max_output_bytes].decode("utf-8", errors="replace")
        return f"{trimmed}\n... [output truncated at {self._max_output_bytes} bytes]", True

    def _git(self, args: list[str], *, purpose: str, timeout: float | None = None):
        return self._sandbox.run(
            SandboxCommand(
                argv=[self._settings.git_executable, *args],
                purpose=purpose,
                cwd=self.repo_dir,
                timeout=timeout or self._settings.command_timeout_seconds,
                executes_repo_code=False,
            ),
            self._token,
        )

    def _read_text(self, path: Path) -> str:
        size = path.stat().st_size
        if size > self._settings.agent_max_file_bytes:
            raise ToolError(
                f"file is larger than the {self._settings.agent_max_file_bytes} byte tool limit: "
                f"{self._relative(path)}"
            )
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise ToolError(f"file is not text: {self._relative(path)}")
        return raw.decode("utf-8", errors="replace")

    # -- tools -----------------------------------------------------------------

    def read_file(self, args: ReadFileArgs) -> ToolResult:
        started = time.monotonic()
        try:
            target = self._resolve(args.path)
        except ToolError as exc:
            return self._fail("read_file", exc)
        if not target.exists():
            return self._fail("read_file", ToolError(f"path does not exist: {args.path}"))
        data: dict[str, Any] = {"path": self._relative(target)}
        try:
            if target.is_dir():
                entries = sorted(
                    f"{child.name}/" if child.is_dir() else child.name for child in target.iterdir()
                )
                output = "\n".join(entries) or "(empty directory)"
                data["kind"] = "directory"
                data["entries"] = entries[:200]
                text, truncated = self._cap(output)
                return ToolResult(
                    tool="read_file",
                    ok=True,
                    output=text,
                    data=data,
                    truncated=truncated,
                    output_bytes=len(text.encode("utf-8")),
                    duration_ms=_ms(started),
                )
            text = self._read_text(target)
        except ToolError as exc:
            return self._fail("read_file", exc)
        except OSError as exc:
            return self._fail("read_file", ToolError(f"cannot read {args.path}: {exc}"))

        lines = text.splitlines()
        total = len(lines)
        start = min(args.start_line, total + 1) if total else 1
        end = min(args.end_line or total, total)
        window = lines[start - 1 : end]
        numbered = "\n".join(f"{index:>5}  {line}" for index, line in enumerate(window, start))
        body, truncated = self._cap(numbered)
        raw_window, _ = self._cap("\n".join(window))
        data.update(
            {
                "kind": "file",
                "start_line": start,
                "end_line": end,
                "total_lines": total,
                "content": raw_window,
            }
        )
        return ToolResult(
            tool="read_file",
            ok=True,
            output=body,
            data=data,
            truncated=truncated,
            output_bytes=len(body.encode("utf-8")),
            duration_ms=_ms(started),
        )

    def search_code(self, args: SearchCodeArgs) -> ToolResult:
        started = time.monotonic()
        try:
            regex = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
        except re.error as exc:
            return self._fail("search_code", ToolError(f"invalid regular expression: {exc}"))
        limit = args.max_results or self._settings.agent_max_search_results
        matches: list[dict[str, Any]] = []
        truncated = False
        for path in self._iter_files():
            if len(matches) >= limit:
                truncated = True
                break
            relative = self._relative(path)
            if args.glob and not (
                fnmatch.fnmatch(relative, args.glob) or fnmatch.fnmatch(path.name, args.glob)
            ):
                continue
            try:
                text = self._read_text(path)
            except (ToolError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append({"path": relative, "line": number, "text": line.strip()[:400]})
                    if len(matches) >= limit:
                        truncated = True
                        break
        output = "\n".join(f"{hit['path']}:{hit['line']}: {hit['text']}" for hit in matches)
        body, capped = self._cap(output or "(no matches)")
        return ToolResult(
            tool="search_code",
            ok=True,
            output=body,
            data={
                "pattern": args.pattern,
                "glob": args.glob,
                "count": len(matches),
                "matches": matches,
                "truncated": truncated,
            },
            truncated=capped or truncated,
            output_bytes=len(body.encode("utf-8")),
            duration_ms=_ms(started),
        )

    def apply_patch(self, args: ApplyPatchArgs) -> ToolResult:
        started = time.monotonic()
        diff = args.diff or ""
        if not diff.strip():
            return self._fail("apply_patch", ToolError("patch is empty"))
        diff_hash = hashlib.sha1(diff.encode("utf-8")).hexdigest()[:12]
        patch_path = self._workspace.artifacts_dir / f"candidate-{diff_hash}.diff"
        try:
            # newline="" keeps LF line endings: writing CRLF here would make every
            # context line mismatch the LF checkout and git apply would reject it.
            patch_path.write_text(
                diff if diff.endswith("\n") else f"{diff}\n",
                encoding="utf-8",
                newline="\n",
            )
        except OSError as exc:
            return self._fail("apply_patch", ToolError(f"cannot stage patch: {exc}"))

        changed = _changed_files(diff)
        check = self._git(
            ["apply", "--check", "--whitespace=nowarn", str(patch_path)],
            purpose="git apply --check",
        )
        if not check.ok:
            return ToolResult(
                tool="apply_patch",
                ok=False,
                output=check.combined_output()[:2_000],
                data={"changed_files": changed, "diff_hash": diff_hash},
                error_code="patch_apply_failed",
                error=f"git apply --check rejected the patch: {check.failure_summary(300)}",
                output_bytes=len(check.combined_output().encode("utf-8")),
                duration_ms=_ms(started),
            )
        applied = self._git(
            ["apply", "--whitespace=nowarn", str(patch_path)],
            purpose="git apply",
        )
        if not applied.ok:
            return ToolResult(
                tool="apply_patch",
                ok=False,
                output=applied.combined_output()[:2_000],
                data={"changed_files": changed, "diff_hash": diff_hash},
                error_code="patch_apply_failed",
                error=f"git apply failed after a successful check: {applied.failure_summary(300)}",
                output_bytes=len(applied.combined_output().encode("utf-8")),
                duration_ms=_ms(started),
            )
        added, removed = _diff_line_counts(diff)
        output = f"applied patch {diff_hash} to {len(changed)} file(s)"
        return ToolResult(
            tool="apply_patch",
            ok=True,
            output=output,
            data={
                "applied": True,
                "diff_hash": diff_hash,
                "changed_files": changed,
                "added_lines": added,
                "removed_lines": removed,
                "patch_path": str(patch_path),
            },
            output_bytes=len(output.encode("utf-8")),
            duration_ms=_ms(started),
        )

    def run_tests(self, args: RunTestsArgs) -> ToolResult:
        started = time.monotonic()
        for target in args.targets:
            if Path(target).is_absolute() or ".." in target.split("/"):
                return self._fail("run_tests", ToolError(f"invalid test target: {target}"))
        junit_path = self._workspace.artifacts_dir / "agent-junit.xml"
        try:
            result = run_pytest(
                self._sandbox,
                self._settings,
                self._token,
                repo_dir=self.repo_dir,
                junit_path=junit_path,
                targets=args.targets or None,
                timeout=args.timeout_seconds,
                purpose="agent pytest",
            )
        except CodeExecutionDeniedError as exc:
            return self._fail("run_tests", exc, code="code_execution_denied")
        except RepoPilotError as exc:
            return self._fail("run_tests", exc, code=exc.code)
        snapshot: TestSnapshot | None = result.snapshot
        data: dict[str, Any] = {
            "argv": result.argv,
            "error": result.error,
            "snapshot": snapshot.model_dump() if snapshot is not None else None,
        }
        if snapshot is None:
            body, capped = self._cap(result.error or "pytest did not produce a usable report")
            return ToolResult(
                tool="run_tests",
                ok=False,
                output=body,
                data=data,
                error_code="tests_unavailable",
                error=result.error,
                truncated=capped,
                output_bytes=len(body.encode("utf-8")),
                duration_ms=_ms(started),
            )
        summary = (
            f"pytest: {snapshot.passed} passed, {snapshot.failed} failed, "
            f"{snapshot.errors} error, {snapshot.skipped} skipped (exit {snapshot.exit_code})"
        )
        body, capped = self._cap(result.error or summary)
        return ToolResult(
            tool="run_tests",
            ok=snapshot.failed == 0 and snapshot.errors == 0,
            output=body,
            data=data,
            error_code=None if result.error is None else "tests_unavailable",
            error=result.error,
            truncated=capped,
            output_bytes=len(body.encode("utf-8")),
            duration_ms=_ms(started),
        )

    def view_diff(self) -> ToolResult:
        started = time.monotonic()
        diff = self._git(["diff", "--no-color"], purpose="git diff")
        status = self._git(["status", "--porcelain"], purpose="git status")
        if diff.error is not None:
            return self._fail("view_diff", ToolError(diff.error))
        body, capped = self._cap(diff.stdout or "(no working tree changes)")
        return ToolResult(
            tool="view_diff",
            ok=True,
            output=body,
            data={
                "diff": diff.stdout,
                "status": status.stdout.splitlines(),
                "changed_files": _changed_files(diff.stdout),
            },
            truncated=capped,
            output_bytes=len(body.encode("utf-8")),
            duration_ms=_ms(started),
        )

    # -- dispatch --------------------------------------------------------------

    def dispatch(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        """Validate ``arguments`` for ``tool`` and run it.

        Unknown tools and malformed arguments are returned as failed results
        rather than raised, so the loop can feed the error back to the model
        within its budget instead of aborting.
        """

        handlers: dict[str, tuple[type[BaseModel], Any]] = {
            "read_file": (ReadFileArgs, self.read_file),
            "search_code": (SearchCodeArgs, self.search_code),
            "apply_patch": (ApplyPatchArgs, self.apply_patch),
            "run_tests": (RunTestsArgs, self.run_tests),
            "view_diff": (ViewDiffArgs, lambda _args: self.view_diff()),
        }
        entry = handlers.get(tool)
        if entry is None:
            return self._fail(
                tool or "<missing>",
                ToolError(f"tool {tool!r} is not allowed; allowed tools: {list(TOOL_NAMES)}"),
                code="tool_not_allowed",
            )
        model, handler = entry
        try:
            validated = model.model_validate(arguments or {})
        except ValidationError as exc:
            return self._fail(
                tool,
                ToolError(f"invalid arguments for {tool}: {exc.errors()[:4]}"),
                code="invalid_tool_arguments",
            )
        return handler(validated)

    # -- internals -------------------------------------------------------------

    def _iter_files(self):
        repo = self.repo_dir.resolve()
        for path in sorted(repo.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in path.relative_to(repo).parts[:-1]):
                continue
            yield path

    def _fail(self, tool: str, exc: BaseException, *, code: str | None = None) -> ToolResult:
        message = exc.failure_reason() if isinstance(exc, RepoPilotError) else str(exc)
        return ToolResult(
            tool=tool,
            ok=False,
            error_code=code or getattr(exc, "code", None) or "tool_error",
            error=message,
            output=message,
            output_bytes=len(message.encode("utf-8")),
        )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1_000)


def _changed_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in (diff or "").splitlines():
        match = _HUNK_FILE_RE.match(line)
        if not match:
            continue
        raw = match.group("path").strip()
        if raw == "/dev/null":
            continue
        name = raw[2:] if raw.startswith(("a/", "b/")) else raw
        if name not in files:
            files.append(name)
    return files


def _diff_line_counts(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed
