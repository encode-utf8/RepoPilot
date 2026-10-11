"""Unit tests for the GitHub integration boundary (fully offline)."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from repopilot.config import Settings
from repopilot.credentials import (
    EnvCredentialProvider,
    GitHubCredentials,
    Secret,
    StaticCredentialProvider,
)
from repopilot.errors import (
    GitHubApiError,
    GitHubAuthError,
    GitHubConflictError,
    GitHubNotConfiguredError,
    GitHubRateLimitError,
    GitHubUnavailableError,
)
from repopilot.github.fake import FakeGitHubClient
from repopilot.github.rest import RestGitHubClient

TOKEN = "ghp_" + "A" * 36


def _settings(**overrides) -> Settings:
    base = Settings(
        github_api_base_url="https://api.test",
        github_max_response_bytes=4096,
    )
    return replace(base, **overrides) if overrides else base


def _client(handler, *, token: str | None = TOKEN, **overrides) -> RestGitHubClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    credentials = GitHubCredentials(token=Secret(token), source="test") if token else None
    return RestGitHubClient(_settings(**overrides), credentials=credentials, client=http)


# --- rest client --------------------------------------------------------------


def test_get_repo_parses_metadata_and_sends_bearer() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "owner": {"login": "acme"},
                "name": "widget",
                "default_branch": "trunk",
            },
        )

    client = _client(handler)
    repo = client.get_repo("acme", "widget")
    assert repo.full_name == "acme/widget"
    assert repo.default_branch == "trunk"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["url"].endswith("/repos/acme/widget")


def test_get_ref_sha_returns_none_when_branch_absent() -> None:
    client = _client(lambda request: httpx.Response(404, json={"message": "Not Found"}))
    assert client.get_ref_sha("acme", "widget", "repopilot/fix-1") is None


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (401, {}, GitHubAuthError),
        (403, {"x-ratelimit-remaining": "0"}, GitHubRateLimitError),
        (403, {}, GitHubAuthError),
        (409, {}, GitHubConflictError),
        (422, {}, GitHubConflictError),
        (429, {}, GitHubRateLimitError),
        (500, {}, GitHubUnavailableError),
        (502, {}, GitHubUnavailableError),
    ],
)
def test_error_status_mapping(status, headers, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, text="service says no")

    with pytest.raises(expected):
        _client(handler).get_repo("acme", "widget")


def test_timeout_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow")

    with pytest.raises(GitHubUnavailableError):
        _client(handler).get_repo("acme", "widget")


def test_missing_token_is_not_configured() -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    client = RestGitHubClient(Settings(), provider=StaticCredentialProvider(None), client=http)
    with pytest.raises(GitHubNotConfiguredError):
        client.get_repo("acme", "widget")


def test_oversized_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"padding": "x" * 5000})

    with pytest.raises(GitHubApiError):
        _client(handler, github_max_response_bytes=1000).get_repo("acme", "widget")


def test_create_pull_request_is_always_draft() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "number": 7,
                "html_url": "https://api.test/acme/widget/pull/7",
                "title": "fix",
                "head": {"ref": "repopilot/fix-1", "sha": "abc"},
                "base": {"ref": "main"},
                "draft": True,
                "state": "open",
                "url": "https://api.test/repos/acme/widget/pulls/7",
            },
        )

    pr = _client(handler).create_pull_request(
        "acme", "widget", title="fix", body="body", head="repopilot/fix-1", base="main"
    )
    assert seen["body"]["draft"] is True
    assert pr.number == 7
    assert pr.draft is True
    assert pr.head_branch == "repopilot/fix-1"
    assert pr.commit_sha == "abc"


# --- credential hygiene -------------------------------------------------------


def test_token_never_leaks_through_errors_or_repr() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(401, text="bad credentials")

    client = _client(handler)
    with pytest.raises(GitHubAuthError) as excinfo:
        client.get_repo("acme", "widget")

    assert seen["auth"] == f"Bearer {TOKEN}"  # it was really used
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in repr(client)

    credentials = GitHubCredentials(token=Secret(TOKEN), source="env:TEST")
    assert TOKEN not in repr(credentials)
    assert TOKEN not in str(credentials.token)
    assert Secret(TOKEN).get_secret_value() == TOKEN


def test_env_provider_reads_lazily(monkeypatch) -> None:
    monkeypatch.delenv("REPOPILOT_GITHUB_TOKEN", raising=False)
    provider = EnvCredentialProvider(_settings())
    assert provider.credentials() is None

    monkeypatch.setenv("REPOPILOT_GITHUB_TOKEN", TOKEN)
    credentials = provider.credentials()
    assert credentials is not None
    assert credentials.token.get_secret_value() == TOKEN
    assert TOKEN not in repr(credentials)


# --- fake client --------------------------------------------------------------


def test_fake_separates_reads_from_writes() -> None:
    fake = FakeGitHubClient()
    assert fake.get_repo("acme", "repo").default_branch == "main"
    assert fake.get_ref_sha("acme", "repo", "missing") is None
    assert fake.writes == []
    assert set(fake.calls) == {"get_repo", "get_ref_sha"}


def test_fake_create_ref_conflict() -> None:
    fake = FakeGitHubClient(refs={"repopilot/fix-1": "sha"})
    with pytest.raises(GitHubConflictError):
        fake.create_ref("acme", "repo", "repopilot/fix-1", "other")


def test_fake_pull_request_is_draft_and_reuses() -> None:
    fake = FakeGitHubClient()
    fake.create_ref("acme", "repo", "br", "c0ffee")
    pr = fake.create_pull_request(
        "acme", "repo", title="t", body="b", head="br", base="main", draft=True
    )
    assert pr.draft is True
    assert pr.commit_sha == "c0ffee"
    found = fake.find_pull_request("acme", "repo", "br", "main")
    assert found is not None and found.number == pr.number
    assert "create_pull_request" in fake.writes
