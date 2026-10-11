"""Phase 3: publish a verified repair as a branch plus a Draft PR.

This module is the *only* place that performs remote writes. It:

* refuses to touch the network unless the task's quality gate passed, so a
  cheating or unverified repair produces strictly zero remote operations;
* refuses to publish unless code execution ran inside a container (design
  requirement: turn on isolation before adding write side effects);
* is idempotent per task: the branch name is derived from the task id, an
  existing pull request is reused, and a pre-existing branch is treated as a
  conflict instead of being clobbered;
* rolls the branch back when the pull request cannot be created, so a failed
  publish never leaves a half finished branch behind.

The Pull Request description is generated from the *same* change set that is
pushed, so it can never disagree with the actual diff.
"""

from __future__ import annotations

import re
from pathlib import Path

from repopilot.config import Settings
from repopilot.errors import (
    GitHubError,
    RepoPilotError,
    TaskNotFoundError,
)
from repopilot.execution.process import CommandResult, build_sandbox_env, run_command
from repopilot.github.base import FileChange, GitHubClient, GitHubPullRequest, TreeEntry
from repopilot.locking import InMemoryLockManager, LockManager, hold_lock
from repopilot.logging_setup import RunLogger
from repopilot.models import PublishOutcome, PullRequestInfo, Task, TaskStatus, utcnow
from repopilot.storage.base import TaskStore

_GITHUB_URL_RE = re.compile(
    r"github\.com[:/]+(?P<owner>[^/\s]+)/(?P<name>[^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: Codes a human can act on; the task is parked for review rather than failed.
_REVIEW_CODES = frozenset(
    {
        "github_not_configured",
        "github_permission_denied",
        "github_rate_limited",
        "github_conflict",
        "github_unavailable",
        "github_api_error",
        "github_no_changes",
        "github_patch_too_large",
    }
)
#: Codes that mean the deployment is misconfigured and publishing is forbidden.
_POLICY_CODES = frozenset({"code_execution_not_isolated"})


def slug_from_repo_url(repo_url: str) -> tuple[str, str] | None:
    """Extract ``(owner, name)`` from a GitHub URL, or ``None`` when it is not one."""

    match = _GITHUB_URL_RE.search((repo_url or "").strip())
    if match is None:
        return None
    return match.group("owner"), match.group("name")


def resolve_slug(task: Task, settings: Settings) -> tuple[str, str] | None:
    """Resolve the target ``owner/name``, honouring the local mirror override."""

    override = (settings.github_repo_slug or "").strip()
    if override:
        parts = override.split("/", 1)
        if len(parts) == 2 and all(part.strip() for part in parts):
            return parts[0].strip(), parts[1].strip()
    return slug_from_repo_url(task.request.repo_url)


def build_branch_name(prefix: str, task_id: str) -> str:
    """Deterministic per task branch name, so a retry cannot fork a second branch."""

    tail = _ID_SAFE_RE.sub("-", task_id).strip("-") or "task"
    return f"{prefix.rstrip('/')}{tail}"


def _git(settings: Settings, args: list[str], *, cwd: Path) -> CommandResult:
    # Plumbing only: no repository code is executed and the environment is the
    # credential free sandbox allowlist.
    return run_command(
        [settings.git_executable, *args],
        cwd=cwd,
        timeout=settings.command_timeout_seconds,
        max_output_bytes=settings.max_output_bytes,
        env=build_sandbox_env(settings),
    )


def _parse_status_entries(raw: str) -> list[tuple[str, str]]:
    parts = [part for part in raw.split("\0") if part]
    entries: list[tuple[str, str]] = []
    for index in range(0, len(parts) - 1, 2):
        entries.append((parts[index].strip(), parts[index + 1]))
    return entries


def collect_patch_files(
    settings: Settings, repo_dir: Path, baseline_commit: str
) -> list[FileChange]:
    """Read the working-tree change set relative to ``baseline_commit``.

    Uses only read-only git plumbing, so no index mutation and no clean/smudge
    filter is triggered.
    """

    tracked = _git(
        settings,
        ["diff", "--name-status", "-z", "--no-renames", baseline_commit],
        cwd=repo_dir,
    )
    if tracked.exit_code != 0:
        raise RepoPilotError(f"git diff failed: {tracked.failure_summary(300)}")
    untracked = _git(
        settings,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        cwd=repo_dir,
    )
    if untracked.exit_code != 0:
        raise RepoPilotError(f"git ls-files failed: {untracked.failure_summary(300)}")

    statuses: dict[str, str] = {}
    for code, path in _parse_status_entries(tracked.stdout):
        normalized = code[:1].upper()
        if normalized == "D":
            statuses[path] = "deleted"
        elif normalized in {"A", "T"}:
            statuses[path] = "added"
        else:
            statuses[path] = "modified"
    for path in untracked.stdout.split("\0"):
        if path:
            statuses[path] = "added"

    changes: list[FileChange] = []
    total_bytes = 0
    for path in sorted(statuses):
        status = statuses[path]
        if status == "deleted":
            changes.append(FileChange(path=path, status=status, content=None))
            continue
        if path.startswith("/") or ".." in Path(path).parts:
            continue
        file_path = repo_dir / path
        try:
            content = file_path.read_bytes()
        except OSError:
            continue
        total_bytes += len(content)
        if total_bytes > settings.github_max_commit_bytes:
            raise RepoPilotError(
                f"the change set exceeds REPOPILOT_GITHUB_MAX_COMMIT_BYTES "
                f"({settings.github_max_commit_bytes} bytes)"
            )
        if len(changes) >= settings.github_max_changed_files:
            raise RepoPilotError(
                f"the change set exceeds REPOPILOT_GITHUB_MAX_CHANGED_FILES "
                f"({settings.github_max_changed_files} files)"
            )
        changes.append(FileChange(path=path, status=status, content=content))
    return changes


def build_commit_message(task: Task) -> str:
    problem = " ".join((task.request.problem or "").split())
    subject = problem[:72] or "automated repair"
    return f"fix: {subject}\n\nGenerated by RepoPilot for task {task.task_id}."


def build_pr_title(task: Task) -> str:
    problem = " ".join((task.request.problem or "").split())
    subject = problem[:68] or "automated repair"
    if len(problem) > 68:
        subject = f"{subject}..."
    return f"fix: {subject}"


def _result_summary(label: str, snapshot) -> str:
    if snapshot is None:
        return f"| {label} | - | - | - | - |"
    return (
        f"| {label} | {snapshot.passed} | {snapshot.failed} | {snapshot.errors} | "
        f"{snapshot.skipped} |"
    )


def build_pr_description(
    *,
    task: Task,
    files: list[FileChange],
    base_branch: str,
    test_command: str,
    located_files: list[str],
) -> str:
    """Render the Draft PR body from the same change set that is pushed."""

    repair = task.report.repair if task.report else None
    problem = (task.request.problem or "(no problem statement)").strip()
    targets = task.request.target_tests or []
    file_lines = "\n".join(f"- `{change.path}` ({change.status})" for change in files) or "- (none)"
    located = ", ".join(f"`{path}`" for path in located_files) or "(none)"

    lines = [
        "本 PR 由 RepoPilot 自动生成, **仅创建 Draft PR, 不会自动合并**。",
        "",
        "## 问题背景",
        "",
        problem,
        "",
        "## 修改说明",
        "",
        "- 定位文件: " + located,
        f"- 改动文件数: {len(files)}",
    ]
    if repair is not None:
        patch = repair.patch
        if patch is not None:
            lines.append(f"- 增删行数: +{patch.added_lines} / -{patch.removed_lines}")
        lines.append(f"- 修复轮数: {repair.attempts} / 上限 {repair.max_rounds}")
    lines += [
        "- 质量门禁: 全部通过(补丁可应用、目标测试通过、无回归、范围相关、无作弊)",
        "",
        "## 影响文件",
        "",
        file_lines,
        "",
        "## 测试命令",
        "",
        f"```bash\n{test_command}\n```",
        "",
        "## 实际测试结果",
        "",
        "| 阶段 | 通过 | 失败 | 错误 | 跳过 |",
        "|---|---|---|---|---|",
        _result_summary("基线", repair.baseline_tests if repair else None),
        _result_summary("修复后", repair.final_tests if repair else None),
        "",
        f"目标测试: {', '.join(targets) if targets else '(baseline failures)'}",
        "",
        "---",
        f"base branch: `{base_branch}` / task: `{task.task_id}` / run: `{task.run_id}`",
    ]
    return "\n".join(lines)


def _repair_test_command(task: Task) -> str:
    targets = task.request.target_tests or []
    suffix = " ".join(targets) if targets else "."
    return f"pytest -p no:cacheprovider -o addopts= -q {suffix}"


def _to_pull_request_info(pr: GitHubPullRequest) -> PullRequestInfo:
    return PullRequestInfo(
        number=pr.number,
        url=pr.url,
        title=pr.title,
        head_branch=pr.head_branch,
        base_branch=pr.base_branch,
        commit_sha=pr.commit_sha,
        draft=pr.draft,
        state=pr.state,
        api_url=pr.api_url,
    )


class PullRequestPublisher:
    """Idempotent, gate-first publisher for one task's verified repair."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: TaskStore,
        client: GitHubClient | None,
        lock_manager: LockManager | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._client = client
        self._lock_manager: LockManager = lock_manager or InMemoryLockManager()

    # -- public API ------------------------------------------------------------

    def publish_task(self, task_id: str, *, require_terminal: bool = True) -> PublishOutcome:
        """Publish ``task_id`` if eligible, persist the outcome and return it.

        ``require_terminal`` is ``True`` for API initiated publishes, which only
        accept a finished task. The repair runner calls with ``False`` while it
        still holds the task open, so that the record is only marked terminal once
        publishing has finished.
        """

        task = self._store.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"unknown task id: {task_id}")
        logger = RunLogger(self._settings.log_dir, task.task_id, task.run_id)

        with hold_lock(
            self._lock_manager,
            f"publish:{task_id}",
            ttl_seconds=self._settings.publish_lock_ttl_seconds,
        ) as acquired:
            if not acquired:
                # Another worker holds the lock. Do not persist anything: the
                # winner owns the record, and clobbering it would hide a
                # successful publish.
                logger.event(
                    "pr.locked",
                    "another worker is already publishing this task",
                    level="warning",
                )
                return PublishOutcome(
                    attempted=False,
                    status="skipped",
                    error_code="publish_in_progress",
                    reason="another worker is already publishing this task",
                    isolation=task.sandbox_isolation,
                )
            outcome = self._publish(task, require_terminal=require_terminal)
        if outcome.status in {"created", "reused"} and outcome.pull_request is not None:
            logger.event(
                f"pr.{outcome.status}",
                f"draft pull request {outcome.status}",
                number=outcome.pull_request.number,
                url=outcome.pull_request.url,
                branch=outcome.branch,
                base_branch=outcome.base_branch,
                commit_sha=outcome.commit_sha,
                operations=outcome.operations,
            )
        elif outcome.status == "skipped":
            logger.event("pr.skipped", outcome.reason, level="info")
        else:
            logger.event(
                "pr.failed",
                outcome.reason,
                level="error",
                error_code=outcome.error_code,
                operations=outcome.operations,
            )

        self._persist(task_id, outcome)
        return outcome

    # -- eligibility -----------------------------------------------------------

    def _eligibility(self, task: Task, *, require_terminal: bool) -> str | None:
        if not self._settings.enable_github_publish:
            return "github publishing is disabled"
        if require_terminal and task.status is not TaskStatus.succeeded:
            return f"task status is {task.status.value}, not succeeded"
        repair = task.report.repair if task.report else None
        if repair is None:
            return "the task has no repair outcome"
        if not repair.quality_gate.passed:
            return "the quality gate did not pass"
        if repair.result != "fixed":
            return f"repair result is {repair.result}, not fixed"
        return None

    # -- core ------------------------------------------------------------------

    def _publish(self, task: Task, *, require_terminal: bool = True) -> PublishOutcome:
        skip = self._eligibility(task, require_terminal=require_terminal)
        if skip is not None:
            return PublishOutcome(attempted=False, status="skipped", reason=skip)

        isolation = task.sandbox_isolation
        if self._settings.publish_requires_container and isolation != "container":
            return PublishOutcome(
                attempted=False,
                status="failed",
                error_code="code_execution_not_isolated",
                reason=(
                    "publishing requires container isolation; the task ran with "
                    f"isolation={isolation!r}. Set REPOPILOT_SANDBOX_BACKEND=container."
                ),
                isolation=isolation,
            )

        if task.pull_request is not None:
            return PublishOutcome(
                attempted=False,
                status="reused",
                reason="a pull request is already recorded for this task",
                branch=task.pull_request.head_branch,
                base_branch=task.pull_request.base_branch,
                commit_sha=task.pull_request.commit_sha,
                isolation=isolation,
                pull_request=task.pull_request,
            )

        if self._client is None:
            return PublishOutcome(
                attempted=False,
                status="failed",
                error_code="github_not_configured",
                reason="no GitHub client is configured for this server",
                isolation=isolation,
            )

        slug = resolve_slug(task, self._settings)
        if slug is None:
            return PublishOutcome(
                attempted=False,
                status="failed",
                error_code="github_not_configured",
                reason=(
                    "cannot resolve owner/repo from the repository source; set "
                    "REPOPILOT_GITHUB_REPO_SLUG for local mirrors"
                ),
                isolation=isolation,
            )
        owner, name = slug

        baseline = task.resolved_commit
        if not baseline or not task.workspace_path:
            return PublishOutcome(
                attempted=False,
                status="failed",
                error_code="github_not_configured",
                reason="the task has no pinned commit or workspace to publish from",
                isolation=isolation,
            )

        repo_dir = Path(task.workspace_path) / "repo"
        try:
            files = collect_patch_files(self._settings, repo_dir, baseline)
        except RepoPilotError as exc:
            code = "github_patch_too_large" if "exceeds" in exc.message else "github_no_changes"
            return PublishOutcome(
                attempted=False,
                status="failed",
                error_code=code,
                reason=exc.failure_reason(),
                isolation=isolation,
            )
        if not files:
            return PublishOutcome(
                attempted=False,
                status="failed",
                error_code="github_no_changes",
                reason="the workspace has no changes to publish",
                isolation=isolation,
            )

        branch = build_branch_name(self._settings.github_branch_prefix, task.task_id)
        base_branch = self._default_base_branch(task)
        return self._create(task, owner, name, branch, base_branch, baseline, files, isolation)

    def _default_base_branch(self, task: Task) -> str:
        if task.report is not None and task.report.repo.default_branch:
            return task.report.repo.default_branch
        return self._settings.github_base_branch

    def _create(
        self,
        task: Task,
        owner: str,
        name: str,
        branch: str,
        base_branch: str,
        baseline: str,
        files: list[FileChange],
        isolation: str | None,
    ) -> PublishOutcome:
        assert self._client is not None  # guarded by the caller
        client = self._client
        operations: list[str] = []
        outcome_meta = {
            "attempted": True,
            "branch": branch,
            "base_branch": base_branch,
            "isolation": isolation,
        }
        try:
            repo = client.get_repo(owner, name)
            operations.append("get_repo")
            if repo.default_branch:
                base_branch = repo.default_branch

            existing = client.find_pull_request(owner, name, branch, base_branch)
            operations.append("find_pull_request")
            if existing is not None:
                return PublishOutcome(
                    status="reused",
                    reason="a pull request already exists for this branch",
                    commit_sha=existing.commit_sha,
                    operations=operations,
                    pull_request=_to_pull_request_info(existing),
                    **outcome_meta,
                )

            ref = client.get_ref_sha(owner, name, branch)
            operations.append("get_ref_sha")
            if ref is not None:
                return PublishOutcome(
                    status="failed",
                    error_code="github_conflict",
                    reason=f"branch {branch!r} already exists on the remote; refusing to overwrite",
                    commit_sha=ref,
                    operations=operations,
                    **outcome_meta,
                )

            base_tree = client.get_commit_tree(owner, name, baseline)
            operations.append("get_commit_tree")
            entries: list[TreeEntry] = []
            for change in files:
                if change.content is None:
                    entries.append(TreeEntry(change.path, "100644", "blob", None))
                    continue
                blob_sha = client.create_blob(owner, name, change.content)
                operations.append("create_blob")
                entries.append(TreeEntry(change.path, "100644", "blob", blob_sha))
            tree_sha = client.create_tree(owner, name, base_tree, entries)
            operations.append("create_tree")
            commit_sha = client.create_commit(
                owner,
                name,
                message=build_commit_message(task),
                tree_sha=tree_sha,
                parents=[baseline],
                author_name=self._settings.github_commit_author_name,
                author_email=self._settings.github_commit_author_email,
            )
            operations.append("create_commit")
            client.create_ref(owner, name, branch, commit_sha)
            operations.append("create_ref")

            try:
                pr = client.create_pull_request(
                    owner,
                    name,
                    title=build_pr_title(task),
                    body=build_pr_description(
                        task=task,
                        files=files,
                        base_branch=base_branch,
                        test_command=_repair_test_command(task),
                        located_files=task.report.repair.located_files
                        if task.report and task.report.repair
                        else [],
                    ),
                    head=branch,
                    base=base_branch,
                    draft=True,
                )
            except GitHubError:
                # Do not leave a half finished branch behind.
                operations.extend(self._rollback(client, owner, name, branch))
                raise
            operations.append("create_pull_request")
            return PublishOutcome(
                status="created",
                reason="draft pull request created",
                commit_sha=commit_sha,
                operations=operations,
                pull_request=_to_pull_request_info(pr),
                **outcome_meta,
            )
        except GitHubError as exc:
            return PublishOutcome(
                status="failed",
                error_code=exc.code,
                reason=exc.failure_reason(),
                operations=operations,
                **outcome_meta,
            )

    @staticmethod
    def _rollback(client: GitHubClient, owner: str, name: str, branch: str) -> list[str]:
        try:
            client.delete_ref(owner, name, branch)
        except GitHubError:
            return ["delete_ref_failed"]
        return ["delete_ref"]

    # -- persistence -----------------------------------------------------------

    def _persist(self, task_id: str, outcome: PublishOutcome) -> None:
        task = self._store.get(task_id)
        if task is None:  # pragma: no cover - the task existed a moment ago
            return
        if outcome.pull_request is not None:
            task.pull_request = outcome.pull_request
        if task.report is not None:
            task.report.publish = outcome
        if outcome.status == "failed":
            if outcome.error_code in _POLICY_CODES:
                task.status = TaskStatus.failed
            elif outcome.error_code in _REVIEW_CODES:
                task.status = TaskStatus.needs_review
            if outcome.error_code is not None:
                task.failure_code = outcome.error_code
                task.failure_reason = outcome.reason
                if task.report is not None:
                    task.report.status = task.status
                    task.report.failure_code = outcome.error_code
                    task.report.failure_reason = outcome.reason
                    task.report.verification.fully_verified = False
                    task.report.verification.verification_gaps = [
                        *task.report.verification.verification_gaps,
                        f"publish: {outcome.reason}",
                    ]
        task.updated_at = utcnow()
        self._store.save(task)
