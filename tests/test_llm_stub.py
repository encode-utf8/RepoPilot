"""Deterministic stub model client behaviour."""

from __future__ import annotations

import pytest

from repopilot.config import Settings
from repopilot.errors import ModelUnavailableError
from repopilot.llm.base import ChatMessage, ModelRequest, ModelResponse
from repopilot.llm.registry import build_model_client
from repopilot.llm.stub import StubModelClient


def request(phase: str = "generic") -> ModelRequest:
    return ModelRequest(messages=[ChatMessage("user", "hi")], phase=phase)


def test_scripted_responses_are_replayed_in_order() -> None:
    client = StubModelClient(["first", "second"])
    assert client.complete(request()).text == "first"
    assert client.complete(request()).text == "second"
    assert client.call_count == 2


def test_running_out_of_scripts_fails_instead_of_inventing_an_answer() -> None:
    client = StubModelClient(["only"])
    client.complete(request())
    with pytest.raises(ModelUnavailableError):
        client.complete(request())


def test_repeat_last_can_be_enabled_explicitly() -> None:
    client = StubModelClient(["same"], repeat_last=True)
    client.complete(request())
    assert client.complete(request()).text == "same"


def test_scripted_exceptions_are_raised() -> None:
    client = StubModelClient([ModelUnavailableError("down")])
    with pytest.raises(ModelUnavailableError):
        client.complete(request())


def test_model_response_objects_are_passed_through_untouched() -> None:
    scripted = ModelResponse(text="body", truncated=True, total_tokens=3)
    assert StubModelClient([scripted]).complete(request()) is scripted


def test_responder_receives_phase_and_call_index() -> None:
    seen: list[tuple[str, int]] = []

    def responder(req: ModelRequest, index: int) -> str:
        seen.append((req.phase, index))
        return "{}"

    client = StubModelClient(responder=responder)
    client.complete(request("understand"))
    client.complete(request("patch"))
    assert seen == [("understand", 0), ("patch", 1)]


def test_registry_selects_provider_from_settings() -> None:
    assert isinstance(build_model_client(Settings(model_provider="stub")), StubModelClient)
    assert build_model_client(Settings(model_provider="vllm")).provider == "openai"


def test_registry_rejects_unknown_provider() -> None:
    with pytest.raises(Exception, match="unknown model provider"):
        build_model_client(Settings(model_provider="does-not-exist"))
