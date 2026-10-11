"""Builds the configured :class:`~repopilot.llm.base.ModelClient`."""

from __future__ import annotations

from repopilot.config import Settings
from repopilot.errors import InvalidRequestError
from repopilot.llm.base import ModelClient
from repopilot.llm.openai_client import OpenAICompatibleModelClient
from repopilot.llm.stub import StubModelClient

_STUB_PROVIDERS = {"stub", "fake", "offline", "none"}
_OPENAI_PROVIDERS = {"openai", "oai", "vllm", "openai-compatible", "openai_compatible"}
_KNOWN_PROVIDERS = _STUB_PROVIDERS | _OPENAI_PROVIDERS


def build_model_client(settings: Settings, *, client: ModelClient | None = None) -> ModelClient:
    """Return the configured model client.

    Tests and callers can inject ``client`` to bypass configuration entirely,
    which is how the deterministic stub is wired in without touching the agent.
    """

    if client is not None:
        return client
    provider = (settings.model_provider or "stub").strip().lower()
    if provider in _STUB_PROVIDERS:
        return StubModelClient(model=settings.model_name)
    if provider in _OPENAI_PROVIDERS:
        return OpenAICompatibleModelClient(settings)
    raise InvalidRequestError(
        f"unknown model provider {settings.model_provider!r}; expected one of "
        f"{sorted(_KNOWN_PROVIDERS)}"
    )
