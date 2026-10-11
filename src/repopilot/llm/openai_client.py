"""OpenAI compatible model client, used with a local vLLM deployment.

The endpoint is assumed to serve ``POST {base_url}/chat/completions`` and
``GET {base_url}/models``. Credentials are read from the environment at request
time and never stored on the client, logged, or written to a report.
"""

from __future__ import annotations

import os
import time

import httpx

from repopilot.config import Settings
from repopilot.errors import ModelOutputError, ModelRequestError, ModelUnavailableError
from repopilot.llm.base import ModelHealth, ModelRequest, ModelResponse, estimate_tokens

_ERROR_EXCERPT_CHARS = 300


def _excerpt(text: str, limit: int = _ERROR_EXCERPT_CHARS) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


class OpenAICompatibleModelClient:
    """Client for any OpenAI compatible chat completions endpoint."""

    provider = "openai"

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self.model = settings.model_name
        self._base_url = settings.model_base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None

    # -- connection management -------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._settings.model_timeout_seconds)
        return self._client

    def close(self) -> None:
        """Release the underlying HTTP client when this instance owns it."""

        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        name = self._settings.model_api_key_env
        if name:
            value = os.environ.get(name)
            if value and value.strip():
                headers["Authorization"] = f"Bearer {value.strip()}"
        return headers

    # -- model client protocol -------------------------------------------------

    def complete(self, request: ModelRequest) -> ModelResponse:
        settings = self._settings
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "max_tokens": request.max_tokens or settings.model_max_tokens,
            "temperature": (
                settings.model_temperature if request.temperature is None else request.temperature
            ),
            "stream": False,
        }
        if request.stop:
            payload["stop"] = list(request.stop)

        started = time.monotonic()
        try:
            response = self._http().post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=settings.model_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelUnavailableError(
                f"model request timed out after {settings.model_timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(
                f"model endpoint unreachable: {type(exc).__name__}: {_excerpt(str(exc))}"
            ) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 500:
            raise ModelUnavailableError(
                f"model endpoint returned HTTP {response.status_code}: {_excerpt(response.text)}"
            )
        if response.status_code >= 400:
            raise ModelRequestError(
                f"model request rejected with HTTP {response.status_code}: "
                f"{_excerpt(response.text)}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelOutputError("model response body was not valid JSON") from exc
        if not isinstance(body, dict):
            raise ModelOutputError("model response body was not a JSON object")

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelOutputError("model response did not contain any choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelOutputError("model response choice was not a JSON object")
        message = first.get("message")
        if not isinstance(message, dict):
            raise ModelOutputError("model response choice had no message object")
        text = message.get("content")
        if text is None:
            raise ModelOutputError("model response message had no content")
        if not isinstance(text, str):
            raise ModelOutputError("model response content was not a string")

        truncated = first.get("finish_reason") == "length"
        if len(text) > settings.model_max_response_chars:
            text = text[: settings.model_max_response_chars]
            truncated = True

        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}

        def _usage(name: str) -> int:
            value = usage.get(name)
            return int(value) if isinstance(value, (int, float)) else 0

        prompt_tokens = _usage("prompt_tokens")
        completion_tokens = _usage("completion_tokens")
        total_tokens = _usage("total_tokens")
        if not prompt_tokens:
            prompt_tokens = estimate_tokens(
                "".join(message.content for message in request.messages)
            )
        if not completion_tokens:
            completion_tokens = estimate_tokens(text)
        if not total_tokens:
            total_tokens = prompt_tokens + completion_tokens

        return ModelResponse(
            text=text,
            model=str(body.get("model") or self.model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            finish_reason=first.get("finish_reason"),
            truncated=truncated,
        )

    def health_check(self) -> ModelHealth:
        started = time.monotonic()
        try:
            response = self._http().get(
                f"{self._base_url}/models",
                headers=self._headers(),
                timeout=self._settings.model_health_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            return ModelHealth(
                provider=self.provider,
                model=self.model,
                reachable=False,
                detail=f"{type(exc).__name__}: {_excerpt(str(exc))}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        reachable = response.status_code < 400
        return ModelHealth(
            provider=self.provider,
            model=self.model,
            reachable=reachable,
            detail=f"HTTP {response.status_code} from GET {self._base_url}/models",
            latency_ms=latency_ms,
        )
