"""GitHub integration package.

``base`` defines the write-capable client protocol, ``rest`` is the production
implementation and ``fake`` is the offline double used by the test suite. Only
``repopilot.pipeline.publish`` is allowed to depend on this package.
"""

from __future__ import annotations

from repopilot.github.base import (
    FileChange,
    GitHubClient,
    GitHubPullRequest,
    GitHubRepo,
    TreeEntry,
)
from repopilot.github.fake import FakeGitHubClient
from repopilot.github.rest import RestGitHubClient

__all__ = [
    "FakeGitHubClient",
    "FileChange",
    "GitHubClient",
    "GitHubPullRequest",
    "GitHubRepo",
    "RestGitHubClient",
    "TreeEntry",
]
