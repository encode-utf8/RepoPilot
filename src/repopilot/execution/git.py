"""Repository cloning and deterministic baseline commit pinning."""

from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.errors import CloneError, CommitResolveError, RepoPilotError, RepoSourceError
from repopilot.execution.process import CommandResult
from repopilot.execution.sandbox import SandboxCommand, SandboxExecutor

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SCP_LIKE_RE = re.compile(r"^[^/\\@]+@[^/\\:]+:")


@dataclass(frozen=True)
class SourceInfo:
    """Classification of the user supplied repository source."""

    raw: str
    is_local: bool
    is_remote: bool
    local_path: Path | None


def classify_source(source: str) -> SourceInfo:
    """Classify a source as a local path or a remote URL."""

    raw = (source or "").strip()
    if not raw:
        raise RepoSourceError("repository source must not be empty")
    if raw.startswith("file://"):
        parsed = urllib.parse.urlparse(raw)
        local = Path(urllib.request.url2pathname(parsed.path))
        return SourceInfo(raw=raw, is_local=True, is_remote=False, local_path=local)
    if _WINDOWS_DRIVE_RE.match(raw) or raw.startswith("\\\\"):
        return SourceInfo(raw=raw, is_local=True, is_remote=False, local_path=Path(raw))
    if "://" in raw or raw.startswith("git@") or _SCP_LIKE_RE.match(raw):
        return SourceInfo(raw=raw, is_local=False, is_remote=True, local_path=None)
    return SourceInfo(raw=raw, is_local=True, is_remote=False, local_path=Path(raw))


def is_trusted_source(source: str, settings: Settings) -> bool:
    """Return ``True`` when ``source`` is a controlled fixture under an allowed root."""

    try:
        info = classify_source(source)
    except RepoPilotError:
        return False
    if not info.is_local or info.local_path is None:
        return False
    try:
        resolved = info.local_path.resolve(strict=True)
    except OSError:
        return False
    for root in settings.trusted_source_roots:
        try:
            root_resolved = root.resolve(strict=True)
        except OSError:
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            return True
    return False


@dataclass
class RepoCheckout:
    """Repository checkout with a pinned, reproducible revision."""

    source: SourceInfo
    repo_dir: Path
    resolved_commit: str
    resolved_ref: str | None
    default_branch: str | None
    head_commit: str | None
    requested_base_commit: str | None
    requested_base_branch: str | None
    clone_duration_ms: int


def _git(
    sandbox: SandboxExecutor,
    token: CancellationToken,
    settings: Settings,
    args: list[str],
    *,
    cwd: Path,
    purpose: str,
    timeout: float | None = None,
) -> CommandResult:
    command = SandboxCommand(
        argv=[settings.git_executable, *args],
        purpose=purpose,
        cwd=cwd,
        timeout=timeout or settings.command_timeout_seconds,
    )
    return sandbox.run(command, token)


def _ensure_ok(result: CommandResult, error_type: type[RepoPilotError], what: str) -> None:
    if result.cancelled:
        raise error_type(f"{what} cancelled")
    if result.timed_out:
        raise error_type(f"{what} timed out after {result.duration_ms} ms")
    if result.error is not None:
        raise error_type(f"{what} could not start: {result.error}")
    if result.exit_code != 0:
        raise error_type(f"{what} failed (exit {result.exit_code}): {result.failure_summary()}")


def _probe_commit(
    sandbox: SandboxExecutor,
    token: CancellationToken,
    settings: Settings,
    repo_dir: Path,
    revision: str,
) -> str | None:
    """Best effort revision resolution; returns ``None`` instead of raising."""

    result = _git(
        sandbox,
        token,
        settings,
        ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
        cwd=repo_dir,
        purpose=f"git rev-parse {revision}",
    )
    if result.exit_code != 0 or result.error is not None or result.cancelled:
        return None
    resolved = result.stdout.strip()
    return resolved or None


def clone_and_pin_repo(
    source: str,
    repo_dir: Path,
    *,
    base_commit: str | None,
    base_branch: str | None,
    settings: Settings,
    sandbox: SandboxExecutor,
    token: CancellationToken,
) -> RepoCheckout:
    """Clone ``source`` and pin a deterministic baseline commit.

    A caller supplied ``base_commit`` always wins. Otherwise the HEAD of
    ``base_branch`` (or the remote default branch) is resolved. The resolved
    commit is checked out in detached HEAD state and verified by reading it back.
    """

    info = classify_source(source)
    if info.is_local and info.local_path is not None and not info.local_path.exists():
        raise RepoSourceError(f"local repository path does not exist: {source}")

    repo_dir = repo_dir.resolve()
    if repo_dir.exists():
        raise CloneError(f"clone destination already exists: {repo_dir}")

    started = time.monotonic()
    clone = _git(
        sandbox,
        token,
        settings,
        ["clone", "--no-checkout", "--quiet", "--", source, str(repo_dir)],
        cwd=repo_dir.parent,
        purpose="git clone",
        timeout=settings.clone_timeout_seconds,
    )
    _ensure_ok(clone, CloneError, f"git clone {source}")
    clone_duration_ms = int((time.monotonic() - started) * 1000)

    default_branch = _probe_default_branch(sandbox, token, settings, repo_dir)
    head_commit = _probe_commit(sandbox, token, settings, repo_dir, "HEAD")

    resolved_ref: str | None = None
    resolved: str | None
    if base_commit:
        resolved = _probe_commit(sandbox, token, settings, repo_dir, base_commit)
        if resolved is None:
            # Some servers allow fetching a specific SHA; ignore failures so the
            # caller receives a precise "commit not found" error below.
            _git(
                sandbox,
                token,
                settings,
                ["fetch", "--quiet", "origin", base_commit],
                cwd=repo_dir,
                purpose=f"git fetch {base_commit}",
            )
            resolved = _probe_commit(sandbox, token, settings, repo_dir, base_commit)
        if resolved is None:
            raise CommitResolveError(
                f"base commit could not be resolved in {source}: {base_commit}"
            )
    elif base_branch:
        resolved = None
        for candidate in (f"refs/remotes/origin/{base_branch}", base_branch):
            resolved = _probe_commit(sandbox, token, settings, repo_dir, candidate)
            if resolved is not None:
                break
        if resolved is None:
            raise CommitResolveError(f"base branch not found in {source}: {base_branch}")
        resolved_ref = base_branch
    else:
        resolved = None
        if default_branch:
            resolved = _probe_commit(
                sandbox, token, settings, repo_dir, f"refs/remotes/origin/{default_branch}"
            )
        if resolved is None:
            resolved = head_commit
        if resolved is None:
            raise CommitResolveError(f"unable to resolve a baseline commit for {source}")
        resolved_ref = default_branch

    checkout = _git(
        sandbox,
        token,
        settings,
        ["checkout", "--detach", "--quiet", resolved],
        cwd=repo_dir,
        purpose=f"git checkout {resolved}",
    )
    _ensure_ok(checkout, CommitResolveError, f"git checkout {resolved}")

    verified = _probe_commit(sandbox, token, settings, repo_dir, "HEAD")
    if verified != resolved:
        raise CommitResolveError(
            f"checked out commit {verified!r} does not match resolved commit {resolved!r}"
        )

    return RepoCheckout(
        source=info,
        repo_dir=repo_dir,
        resolved_commit=resolved,
        resolved_ref=resolved_ref,
        default_branch=default_branch,
        head_commit=head_commit,
        requested_base_commit=base_commit,
        requested_base_branch=base_branch,
        clone_duration_ms=clone_duration_ms,
    )


def _probe_default_branch(
    sandbox: SandboxExecutor,
    token: CancellationToken,
    settings: Settings,
    repo_dir: Path,
) -> str | None:
    result = _git(
        sandbox,
        token,
        settings,
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo_dir,
        purpose="git symbolic-ref origin/HEAD",
    )
    if result.exit_code != 0 or result.error is not None or result.cancelled:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    return value.split("/", 1)[1] if value.startswith("origin/") else value
