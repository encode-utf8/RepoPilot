"""Model client boundary.

Phase 2 only needs a ``complete``-style text interface: the agent asks the model
for a JSON action, not for provider specific tool calling. Keeping the boundary
this narrow means a vLLM deployment, another OpenAI compatible server, or the
deterministic stub are all interchangeable through configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    """One message in a chat style conversation."""

    role: Role
    content: str


@dataclass
class ModelRequest:
    """A single completion request."""

    messages: list[ChatMessage]
    phase: str = "generic"
    max_tokens: int | None = None
    temperature: float | None = None
    stop: tuple[str, ...] = field(default_factory=tuple)

    def character_count(self) -> int:
        """Total prompt size in characters, used for prompt size guards."""

        return sum(len(message.content) for message in self.messages)


@dataclass
class ModelResponse:
    """A single completion response with deterministic accounting fields."""

    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        """``False`` when the answer was cut off and cannot be trusted."""

        return not self.truncated


@dataclass
class ModelHealth:
    """Result of a connectivity probe against the configured endpoint."""

    provider: str
    model: str
    reachable: bool
    detail: str
    latency_ms: int = 0


class ModelClient(Protocol):
    """The only model surface the agent is allowed to depend on."""

    provider: str
    model: str

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return one completion or raise a :class:`ModelError` subclass."""

    def health_check(self) -> ModelHealth:
        """Probe the endpoint so operators can verify connectivity."""


def estimate_tokens(text: str) -> int:
    """Deterministic, provider agnostic token estimate for cost accounting.

    Real usage numbers from the server always win; this is only the fallback for
    providers (and the stub) that do not report usage.
    """

    if not text:
        return 0
    return max(1, len(text) // 4)
