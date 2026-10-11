"""Offline fake ``GitHubClient``.

The test suite must never touch the real GitHub API, so this in-memory double
implements the same protocol, records every operation, and lets a test inject
failures per operation. It is strict on purpose: it refuses to silently accept
an operation whose scripted behaviour would not happen on GitHub (for example
creating a ref that already exists raises a conflict).
"""

from __future__ import annotations

import hashlib
from typing import Any

from repopilot.errors import GitHubConflictError
from repopilot.github.base import GitHubPullRequest, GitHubRepo, TreeEntry

_WRITE_OPS = frozenset(
    {
        "create_ref",
        "delete_ref",
        "create_blob",
        "create_tree",
        "create_commit",
        "create_pull_request",
    }
)


def _sha(prefix: str, payload: bytes) -> str:
    return f"{prefix}{hashlib.sha1(payload).hexdigest()[:32]}"


class FakeGitHubClient:
    """Deterministic, network free ``GitHubClient``."""

    def __init__(
        self,
        *,
        owner: str = "acme",
        name: str = "repo",
        default_branch: str = "main",
        refs: dict[str, str] | None = None,
        pull_requests: list[GitHubPullRequest] | None = None,
        fail_ops: dict[str, BaseException] | None = None,
    ) -> None:
        self.owner = owner
        self.name = name
        self.default_branch = default_branch
        self.refs: dict[str, str] = dict(refs or {})
        self.pull_requests: list[GitHubPullRequest] = list(pull_requests or [])
        self.fail_ops: dict[str, BaseException] = dict(fail_ops or {})
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, str] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.writes: list[str] = []

    # -- helpers ---------------------------------------------------------------

    def _record(self, op: str) -> None:
        self.calls.append(op)
        if op in _WRITE_OPS:
            self.writes.append(op)
        failure = self.fail_ops.get(op)
        if failure is not None:
            raise failure

    def reset(self) -> None:
        self.calls.clear()
        self.writes.clear()

    @property
    def pull_request_count(self) -> int:
        return len(self.pull_requests)

    def _find_pr(self, head_branch: str, base_branch: str) -> GitHubPullRequest | None:
        for pr in self.pull_requests:
            if pr.head_branch == head_branch and pr.base_branch == base_branch:
                return pr
        return None

    # -- reads -----------------------------------------------------------------

    def get_repo(self, owner: str, name: str) -> GitHubRepo:
        self._record("get_repo")
        return GitHubRepo(owner=owner, name=name, default_branch=self.default_branch)

    def get_ref_sha(self, owner: str, name: str, branch: str) -> str | None:
        self._record("get_ref_sha")
        return self.refs.get(branch)

    def get_commit_tree(self, owner: str, name: str, commit_sha: str) -> str:
        self._record("get_commit_tree")
        return self.trees.get(commit_sha, f"tree-{commit_sha}")

    def find_pull_request(
        self, owner: str, name: str, head_branch: str, base_branch: str
    ) -> GitHubPullRequest | None:
        self._record("find_pull_request")
        return self._find_pr(head_branch, base_branch)

    # -- writes ----------------------------------------------------------------

    def create_ref(self, owner: str, name: str, branch: str, sha: str) -> None:
        self._record("create_ref")
        if branch in self.refs:
            raise GitHubConflictError(f"refs/heads/{branch} already exists")
        self.refs[branch] = sha

    def delete_ref(self, owner: str, name: str, branch: str) -> None:
        self._record("delete_ref")
        self.refs.pop(branch, None)

    def create_blob(self, owner: str, name: str, content: bytes) -> str:
        self._record("create_blob")
        sha = _sha("blob", content)
        self.blobs[sha] = content
        return sha

    def create_tree(self, owner: str, name: str, base_tree: str, entries: list[TreeEntry]) -> str:
        self._record("create_tree")
        payload = base_tree.encode("utf-8") + b"".join(
            f"{entry.path}:{entry.sha}".encode() for entry in entries
        )
        sha = _sha("tree", payload)
        self.trees[sha] = base_tree
        return sha

    def create_commit(
        self,
        owner: str,
        name: str,
        *,
        message: str,
        tree_sha: str,
        parents: list[str],
        author_name: str,
        author_email: str,
    ) -> str:
        self._record("create_commit")
        sha = _sha("commit", f"{tree_sha}:{','.join(parents)}:{message}".encode())
        self.commits[sha] = {
            "tree": tree_sha,
            "parents": list(parents),
            "message": message,
            "author": author_name,
        }
        return sha

    def create_pull_request(
        self,
        owner: str,
        name: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = True,
    ) -> GitHubPullRequest:
        self._record("create_pull_request")
        existing = self._find_pr(head, base)
        if existing is not None:
            raise GitHubConflictError(f"a pull request already exists for {head}")
        number = 1000 + len(self.pull_requests)
        pr = GitHubPullRequest(
            number=number,
            url=f"https://github.example/{owner}/{name}/pull/{number}",
            title=title,
            head_branch=head,
            base_branch=base,
            draft=draft,
            state="open",
            api_url=f"https://api.github.example/repos/{owner}/{name}/pulls/{number}",
            commit_sha=self.refs.get(head),
        )
        self.pull_requests.append(pr)
        return pr
