"""Central GitHub credential management.

Credentials live here and *only* here. They are read from the environment (or an
explicit provider) at the moment of a remote write, wrapped in :class:`Secret`
so an accidental ``repr``/``str``/log of the object can never reveal the value,
and are handed straight to the GitHub REST client. Nothing in this module writes
the secret to a task record, a report, a structured log or a subprocess
environment.

The agent never imports this module: remote writes are performed exclusively by
``repopilot.pipeline.publish`` on behalf of the backend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from repopilot.config import Settings

_MASK = "***REDACTED***"


class Secret:
    """A string that refuses to reveal itself in ``repr`` or ``str``.

    Call :meth:`get_secret_value` at the exact point of use; never store the
    unwrapped value on a settings object, a model or a report.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        """Return the raw secret. The only sanctioned way to read it."""

        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return f"Secret({_MASK})"

    def __str__(self) -> str:
        return _MASK


@dataclass(frozen=True)
class GitHubCredentials:
    """A token plus a human readable, non sensitive description of its origin."""

    token: Secret
    source: str = "unknown"

    def __repr__(self) -> str:
        # Intentionally omits ``token`` entirely so a stray repr cannot leak it.
        return f"GitHubCredentials(source={self.source!r}, token={_MASK})"


@runtime_checkable
class GitHubCredentialProvider(Protocol):
    """Supplies GitHub credentials on demand, or ``None`` when unavailable."""

    def credentials(self) -> GitHubCredentials | None:
        """Return credentials for a remote write, or ``None`` when unset."""


class NullCredentialProvider:
    """Provider that never yields credentials (publishing stays disabled)."""

    def credentials(self) -> GitHubCredentials | None:
        return None


class EnvCredentialProvider:
    """Read the token from the environment variable named by settings.

    The value is looked up lazily at each call so rotating the environment
    variable does not require rebuilding the application.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def credentials(self) -> GitHubCredentials | None:
        name = self._settings.github_token_env
        value = (os.environ.get(name) or "").strip()
        if not value:
            return None
        return GitHubCredentials(token=Secret(value), source=f"env:{name}")


class StaticCredentialProvider:
    """Provider backed by an explicit credential, used for controlled tests."""

    def __init__(self, credentials: GitHubCredentials | None) -> None:
        self._credentials = credentials

    def credentials(self) -> GitHubCredentials | None:
        return self._credentials
