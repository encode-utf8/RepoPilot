"""Model client behaviour, exercised offline with a mock HTTP transport."""

from __future__ import annotations

import json

import httpx
import pytest

from repopilot.config import Settings
from repopilot.errors import ModelOutputError, ModelRequestError, ModelUnavailableError
from repopilot.llm.base import ChatMessage, ModelRequest
from repopilot.llm.openai_client import OpenAICompatibleModelClient


def make_settings(**overrides) -> Settings:
    base = {
        "model_base_url": "http://model.local/v1",
        "model_name": "test-model",
        "model_timeout_seconds": 5.0,
        "model_health_timeout_seconds": 2.0,
        "model_max_response_chars": 200,
        "model_prompt_cost_per_1k_tokens": 1.0,
        "model_completion_cost_per_1k_tokens": 2.0,
    }
    base.update(overrides)
    return Settings(**base)


def make_client(handler, **overrides) -> OpenAICompatibleModelClient:
    transport = httpx.MockTransport(handler)
    return OpenAICompatibleModelClient(
        make_settings(**overrides), client=httpx.Client(transport=transport)
    )


def make_request() -> ModelRequest:
    return ModelRequest(messages=[ChatMessage("user", "hello")], phase="test")


def body(text: str = '{"ok": true}', *, finish: str = "stop", usage: dict | None = None) -> dict:
    return {
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def test_completion_records_usage_latency_and_cost() -> None:
    client = make_client(lambda request: httpx.Response(200, json=body()))
    response = client.complete(make_request())

    assert response.text == '{"ok": true}'
    assert (response.prompt_tokens, response.completion_tokens, response.total_tokens) == (
        10,
        5,
        15,
    )
    assert response.finish_reason == "stop"
    assert response.truncated is False
    assert response.latency_ms >= 0
    assert client.health_check().reachable is True


def test_timeout_is_reported_as_model_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(ModelUnavailableError) as excinfo:
        make_client(handler).complete(make_request())
    assert excinfo.value.code == "model_unavailable"


def test_connection_error_is_reported_as_model_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ModelUnavailableError):
        make_client(handler).complete(make_request())


def test_server_error_is_reported_as_model_unavailable() -> None:
    client = make_client(lambda request: httpx.Response(503, text="overloaded"))
    with pytest.raises(ModelUnavailableError):
        client.complete(make_request())


def test_client_error_is_reported_as_model_request_error() -> None:
    client = make_client(lambda request: httpx.Response(400, text="bad request"))
    with pytest.raises(ModelRequestError):
        client.complete(make_request())


def test_non_json_body_is_reported_as_invalid_output() -> None:
    client = make_client(lambda request: httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(ModelOutputError):
        client.complete(make_request())


def test_missing_choices_is_reported_as_invalid_output() -> None:
    client = make_client(lambda request: httpx.Response(200, json={"model": "m"}))
    with pytest.raises(ModelOutputError):
        client.complete(make_request())


def test_length_finish_reason_marks_the_answer_truncated() -> None:
    client = make_client(lambda request: httpx.Response(200, json=body(finish="length")))
    response = client.complete(make_request())
    assert response.truncated is True
    assert response.ok is False


def test_oversized_answer_is_capped_and_marked_truncated() -> None:
    client = make_client(
        lambda request: httpx.Response(200, json=body("x" * 500)), model_max_response_chars=100
    )
    response = client.complete(make_request())
    assert len(response.text) == 100
    assert response.truncated is True


def test_usage_is_estimated_when_the_server_omits_it() -> None:
    payload = body()
    payload.pop("usage")
    client = make_client(lambda request: httpx.Response(200, json=payload))
    response = client.complete(make_request())
    assert response.prompt_tokens > 0
    assert response.total_tokens == response.prompt_tokens + response.completion_tokens


def test_api_key_comes_from_the_configured_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOPILOT_TEST_MODEL_KEY", "sekret-value")
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=body())

    client = make_client(handler, model_api_key_env="REPOPILOT_TEST_MODEL_KEY")
    response = client.complete(make_request())

    assert seen == ["Bearer sekret-value"]
    # The key must never be echoed back into a response object the agent can see.
    assert "sekret-value" not in json.dumps(response.__dict__, default=str)


def test_health_check_reports_an_unreachable_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    health = make_client(handler).health_check()
    assert health.reachable is False
    assert "ConnectError" in health.detail
