"""Deterministic offline model client.

The stub exists so the whole agent loop, quality gate and report can be tested
without a model server. It is deliberately strict: when it runs out of scripted
answers it fails instead of inventing a completion, so a misconfigured run can
never be mistaken for a successful repair.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from repopilot.errors import ModelUnavailableError
from repopilot.llm.base import ModelHealth, ModelRequest, ModelResponse, estimate_tokens

Responder = Callable[[ModelRequest, int], "str | ModelResponse | BaseException"]
Scripted = "str | ModelResponse | BaseException"


class StubModelClient:
    """Replays a fixed script of responses, keyed by call order."""

    provider = "stub"

    def __init__(
        self,
        responses: Sequence[Scripted] | None = None,
        *,
        model: str = "stub-model",
        responder: Responder | None = None,
        repeat_last: bool = False,
        latency_ms: int = 1,
    ) -> None:
        if responses is not None and responder is not None:
            msg = "pass either responses or responder, not both"
            raise ValueError(msg)
        self.model = model
        self._responses = list(responses or [])
        self._responder = responder
        self._repeat_last = repeat_last
        self._latency_ms = latency_ms
        self.requests: list[ModelRequest] = []
        self.calls: list[ModelResponse] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def _next(self, request: ModelRequest, index: int) -> Scripted:
        if self._responder is not None:
            return self._responder(request, index)
        if index < len(self._responses):
            return self._responses[index]
        if self._repeat_last and self._responses:
            return self._responses[-1]
        raise ModelUnavailableError(f"stub model has no scripted response for call #{index + 1}")

    def complete(self, request: ModelRequest) -> ModelResponse:
        index = len(self.requests)
        self.requests.append(request)
        produced = self._next(request, index)
        if isinstance(produced, BaseException):
            raise produced
        if isinstance(produced, ModelResponse):
            response = produced
        else:
            text = produced
            if not isinstance(text, str):
                msg = f"stub response #{index + 1} must be str, ModelResponse or exception"
                raise TypeError(msg)
            response = ModelResponse(
                text=text,
                model=self.model,
                prompt_tokens=estimate_tokens(
                    "".join(message.content for message in request.messages)
                ),
                completion_tokens=estimate_tokens(text),
                total_tokens=estimate_tokens(
                    "".join(message.content for message in request.messages)
                )
                + estimate_tokens(text),
                latency_ms=self._latency_ms,
                finish_reason="stop",
                truncated=False,
            )
        self.calls.append(response)
        return response

    def health_check(self) -> ModelHealth:
        return ModelHealth(
            provider=self.provider,
            model=self.model,
            reachable=True,
            detail=f"stub model scripted with {len(self._responses)} response(s)",
            latency_ms=self._latency_ms,
        )
