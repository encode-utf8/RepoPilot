"""GitHub integration boundary.

The backend's PR module depends only on :class:`GitHubClient`. The protocol is
deliberately small: it exposes exactly the operations needed to create one
Draft PR (branch -> commit -> review request) plus the lookups required for
idempotent re-entry. No implementation here can merge or push to a default
branch, because no such operation exists in the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class GitHubRepo:
    """Repository identity resolved from the GitHub API."""

    owner: str
    name: str
    default_branch: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class GitHubPullRequest:
    """A pull request as returned by the GitHub API."""

    number: int
    url: str
    title: str
    head_branch: str
    base_branch: str
    draft: bool = True
    state: str = "open"
    api_url: str | None = None
    commit_sha: str | None = None


@dataclass(frozen=True)
class TreeEntry:
    """One entry of a git tree update."""

    path: str
    mode: str
    type: str
    sha: str | None


@dataclass(frozen=True)
class FileChange:
    """A single file in the candidate commit.

    ``content`` is ``None`` for a deletion and the full new bytes otherwise.
    """

    path: str
    status: str  # "added" | "modified" | "deleted"
    content: bytes | None = None


@runtime_checkable
class GitHubClient(Protocol):
    """Write-capable GitHub client used exclusively by the PR module."""

    def get_repo(self, owner: str, name: str) -> GitHubRepo:
        """Return repository metadata, raising when access is denied."""

    def get_ref_sha(self, owner: str, name: str, branch: str) -> str | None:
        """Return the commit a branch points at, or ``None`` when it is absent."""

    def create_ref(self, owner: str, name: str, branch: str, sha: str) -> None:
        """Create ``refs/heads/<branch>`` pointing at ``sha``."""

    def delete_ref(self, owner: str, name: str, branch: str) -> None:
        """Delete ``refs/heads/<branch>`` (used to roll back a partial publish)."""

    def get_commit_tree(self, owner: str, name: str, commit_sha: str) -> str:
        """Return the tree sha of ``commit_sha``."""

    def create_blob(self, owner: str, name: str, content: bytes) -> str:
        """Upload blob content and return its sha."""

    def create_tree(self, owner: str, name: str, base_tree: str, entries: list[TreeEntry]) -> str:
        """Create a tree on top of ``base_tree`` and return its sha."""

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
        """Create a commit object and return its sha."""

    def find_pull_request(
        self, owner: str, name: str, head_branch: str, base_branch: str
    ) -> GitHubPullRequest | None:
        """Return an existing pull request for the same head branch, if any."""

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
        """Create a pull request. Callers of this code only ever pass ``draft=True``."""
