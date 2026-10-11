"""Strict parsing of model output into agent decisions.

The model must answer with a single JSON object. Anything else - prose, a
truncated answer, an unknown tool, a missing field - raises
:class:`~repopilot.errors.ModelOutputError`, which the loop turns into an
explicit failure. There is no "best effort" interpretation, because guessing
would let an unusable answer masquerade as a repair.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repopilot.errors import ModelOutputError
from repopilot.llm.base import ModelResponse

T = TypeVar("T", bound=BaseModel)


class ToolAction(BaseModel):
    """A request to run one controlled tool."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    """Output of the task understanding node."""

    model_config = ConfigDict(extra="forbid")

    goal: str = ""
    queries: list[str] = Field(default_factory=list)
    suspect_paths: list[str] = Field(default_factory=list)


class Diagnosis(BaseModel):
    """Output of the code location node."""

    model_config = ConfigDict(extra="forbid")

    target_files: list[str] = Field(default_factory=list)
    root_cause: str = ""
    edit_intent: str = ""


class PatchProposal(BaseModel):
    """Output of the patch generation node."""

    model_config = ConfigDict(extra="forbid")

    diff: str
    notes: str = ""


def strip_code_fence(text: str) -> str:
    """Remove a single markdown code fence when the model wraps its JSON."""

    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    fenced = len(lines) >= 2 and lines[-1].strip().startswith("```")
    body = lines[1:-1] if fenced else lines[1:]  # unterminated fence: drop opening line only
    return "\n".join(body).strip()


def _balanced_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` block, honouring string literals."""

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from a model answer or raise ``ModelOutputError``."""

    candidates = [text, strip_code_fence(text)]
    balanced = _balanced_object(strip_code_fence(text))
    if balanced is not None:
        candidates.append(balanced)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    preview = " ".join((text or "").split())[:200]
    raise ModelOutputError(f"model answer was not a JSON object: {preview!r}")


def _validate(response: ModelResponse, model: type[T], payload: dict[str, Any]) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ModelOutputError(
            f"model answer did not match the {model.__name__} schema: {exc.errors()[:4]}"
        ) from exc


def parse_payload(response: ModelResponse, model: type[T]) -> T:
    """Parse a complete JSON answer into ``model``."""

    if response.truncated:
        raise ModelOutputError(
            "model output was truncated (finish_reason=length); the answer cannot be used"
        )
    return _validate(response, model, extract_json_object(response.text))


def parse_locate(response: ModelResponse) -> ToolAction | Diagnosis:
    """Parse the location node answer: another tool call, or a diagnosis."""

    if response.truncated:
        raise ModelOutputError(
            "model output was truncated (finish_reason=length); the answer cannot be used"
        )
    payload = extract_json_object(response.text)
    if "action" in payload:
        action = payload["action"]
        if not isinstance(action, dict):
            raise ModelOutputError("'action' must be a JSON object")
        return _validate(response, ToolAction, action)
    if "diagnosis" in payload:
        diagnosis = payload["diagnosis"]
        if not isinstance(diagnosis, dict):
            raise ModelOutputError("'diagnosis' must be a JSON object")
        return _validate(response, Diagnosis, diagnosis)
    if "target_files" in payload or "root_cause" in payload:
        return _validate(response, Diagnosis, payload)
    raise ModelOutputError(
        "model answer contained neither 'action' nor 'diagnosis'; "
        "expected one of the documented shapes"
    )
