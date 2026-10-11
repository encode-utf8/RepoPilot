"""GitHub REST client.

Only the operations required to publish a single Draft PR are implemented.
Credentials are resolved per request from a provider and used solely to build an
``Authorization`` header; the token is never stored on the client, logged, or
placed in an exception message.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx

from repopilot.config import Settings
from repopilot.credentials import GitHubCredentialProvider, GitHubCredentials
from repopilot.errors import (
    GitHubApiError,
    GitHubAuthError,
    GitHubConflictError,
    GitHubNotConfiguredError,
    GitHubRateLimitError,
    GitHubUnavailableError,
)
from repopilot.github.base import GitHubPullRequest, GitHubRepo, TreeEntry

_API_VERSION = "2022-11-28"
_USER_AGENT = "RepoPilot"


class RestGitHubClient:
    """``GitHubClient`` implementation backed by the public REST API (v3)."""

    def __init__(
        self,
        settings: Settings,
        *,
        credentials: GitHubCredentials | None = None,
        provider: GitHubCredentialProvider | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._base = settings.github_api_base_url.rstrip("/")
        self._credentials = credentials
        self._provider = provider
        self._client = client
        self._owns_client = client is None

    # -- transport -------------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._settings.github_api_timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _token(self) -> str:
        credentials = self._credentials
        if credentials is None and self._provider is not None:
            credentials = self._provider.credentials()
        if credentials is None or not credentials.token:
            raise GitHubNotConfiguredError(
                "no GitHub token is configured; set the environment variable named by "
                "REPOPILOT_GITHUB_TOKEN_ENV"
            )
        return credentials.token.get_secret_value()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": _USER_AGENT,
        }
        url = f"{self._base}{path}"
        try:
            response = self._http().request(method, url, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise GitHubUnavailableError(f"github api timed out for {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(
                f"github api request failed for {method} {path}: {type(exc).__name__}"
            ) from exc
        if len(response.content) > self._settings.github_max_response_bytes:
            raise GitHubApiError(f"github api response too large for {method} {path}")
        if response.status_code == 404 and allow_404:
            return response
        if response.is_success:
            return response
        raise self._map_error(method, path, response)

    @staticmethod
    def _map_error(method: str, path: str, response: httpx.Response) -> Exception:
        status = response.status_code
        body = response.text[:300]
        context = f"{method} {path} -> {status}"
        if status == 401:
            return GitHubAuthError(f"{context}: the token was rejected")
        if status == 403:
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0" or "rate limit" in body.lower():
                return GitHubRateLimitError(f"{context}: rate limit exceeded")
            return GitHubAuthError(f"{context}: the token lacks the required scope")
        if status in (409, 422):
            return GitHubConflictError(f"{context}: {body or 'conflict'}")
        if status == 429:
            return GitHubRateLimitError(f"{context}: rate limit exceeded")
        if status >= 500:
            return GitHubUnavailableError(f"{context}: server error")
        return GitHubApiError(f"{context}: {body or 'unexpected response'}")

    # -- reads -----------------------------------------------------------------

    def get_repo(self, owner: str, name: str) -> GitHubRepo:
        response = self._request("GET", f"/repos/{owner}/{name}")
        payload = response.json()
        return GitHubRepo(
            owner=payload.get("owner", {}).get("login", owner),
            name=payload.get("name", name),
            default_branch=payload.get("default_branch") or self._settings.github_base_branch,
        )

    def get_ref_sha(self, owner: str, name: str, branch: str) -> str | None:
        response = self._request(
            "GET",
            f"/repos/{owner}/{name}/git/ref/heads/{branch}",
            allow_404=True,
        )
        if response.status_code == 404:
            return None
        return response.json().get("object", {}).get("sha")

    def get_commit_tree(self, owner: str, name: str, commit_sha: str) -> str:
        response = self._request("GET", f"/repos/{owner}/{name}/git/commits/{commit_sha}")
        return response.json()["tree"]["sha"]

    def find_pull_request(
        self, owner: str, name: str, head_branch: str, base_branch: str
    ) -> GitHubPullRequest | None:
        head = quote(f"{owner}:{head_branch}", safe="")
        base = quote(base_branch, safe="")
        response = self._request(
            "GET",
            f"/repos/{owner}/{name}/pulls?state=all&head={head}&base={base}",
        )
        items = response.json()
        if not isinstance(items, list) or not items:
            return None
        return self._to_pull_request(items[0])

    # -- writes ----------------------------------------------------------------

    def create_ref(self, owner: str, name: str, branch: str, sha: str) -> None:
        self._request(
            "POST",
            f"/repos/{owner}/{name}/git/refs",
            json_body={"ref": f"refs/heads/{branch}", "sha": sha},
        )

    def delete_ref(self, owner: str, name: str, branch: str) -> None:
        # Best effort rollback: a missing ref is not an error.
        self._request(
            "DELETE",
            f"/repos/{owner}/{name}/git/refs/heads/{branch}",
            allow_404=True,
        )

    def create_blob(self, owner: str, name: str, content: bytes) -> str:
        response = self._request(
            "POST",
            f"/repos/{owner}/{name}/git/blobs",
            json_body={
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
        )
        return response.json()["sha"]

    def create_tree(self, owner: str, name: str, base_tree: str, entries: list[TreeEntry]) -> str:
        response = self._request(
            "POST",
            f"/repos/{owner}/{name}/git/trees",
            json_body={
                "base_tree": base_tree,
                "tree": [
                    {
                        "path": entry.path,
                        "mode": entry.mode,
                        "type": entry.type,
                        "sha": entry.sha,
                    }
                    for entry in entries
                ],
            },
        )
        return response.json()["sha"]

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
        identity = {"name": author_name, "email": author_email}
        response = self._request(
            "POST",
            f"/repos/{owner}/{name}/git/commits",
            json_body={
                "message": message,
                "tree": tree_sha,
                "parents": parents,
                "author": identity,
                "committer": identity,
            },
        )
        return response.json()["sha"]

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
        response = self._request(
            "POST",
            f"/repos/{owner}/{name}/pulls",
            json_body={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "draft": draft,
            },
        )
        return self._to_pull_request(response.json())

    @staticmethod
    def _to_pull_request(payload: dict[str, Any]) -> GitHubPullRequest:
        head = payload.get("head") or {}
        base = payload.get("base") or {}
        return GitHubPullRequest(
            number=int(payload.get("number", 0)),
            url=payload.get("html_url", ""),
            title=payload.get("title", ""),
            head_branch=head.get("ref", ""),
            base_branch=base.get("ref", ""),
            draft=bool(payload.get("draft", False)),
            state=payload.get("state", "open"),
            api_url=payload.get("url"),
            commit_sha=head.get("sha"),
        )
