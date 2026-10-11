"""Prompt templates for the repair loop.

Prompts only ever contain repository content, the user supplied problem
statement and tool output. Credentials are never rendered into a prompt: the
model client reads its key from the environment at request time and the agent
layer has no access to it.
"""

from __future__ import annotations

from repopilot.llm.base import ChatMessage

_JSON_ONLY = "Answer with a single JSON object and nothing else."

SYSTEM_PROMPT = (
    "You are RepoPilot's repair agent. You work on a pinned revision of a Python "
    "repository inside an isolated sandbox. You cannot run arbitrary commands; the "
    "only way to inspect or change the workspace is the controlled tool set "
    "(read_file, search_code, apply_patch, run_tests, view_diff). Make the smallest "
    "change that fixes the stated problem and never weaken, delete or skip tests to "
    "make a suite pass. " + _JSON_ONLY
)


def format_blocks(blocks: list[tuple[str, str]], *, limit: int = 12_000) -> str:
    """Render ``(path, content)`` pairs, bounded to ``limit`` characters."""

    parts: list[str] = []
    used = 0
    for path, content in blocks:
        piece = f"--- {path} ---\n{content}"
        if used + len(piece) > limit:
            piece = piece[: max(limit - used, 0)]
        parts.append(piece)
        used += len(piece)
        if used >= limit:
            break
    return "\n\n".join(parts)


def understand_messages(
    *,
    problem: str,
    profile_summary: str,
    file_listing: str,
) -> list[ChatMessage]:
    """Task understanding: restate the goal and propose search queries."""

    user = (
        "Problem statement:\n"
        f"{problem}\n\n"
        f"Repository profile:\n{profile_summary}\n\n"
        f"Repository files (truncated):\n{file_listing}\n\n"
        "Produce a plan. " + _JSON_ONLY + "\n"
        'Schema: {"goal": str, "queries": [str], "suspect_paths": [str]}'
    )
    return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", user)]


def locate_messages(
    *,
    problem: str,
    plan_goal: str,
    context: str,
    tool_history: str,
) -> list[ChatMessage]:
    """Code location: either request one more tool call or give a diagnosis."""

    user = (
        f"Goal: {plan_goal}\n\n"
        f"Problem statement:\n{problem}\n\n"
        f"Repository context:\n{context or '(none yet)'}\n\n"
        f"Tool results so far:\n{tool_history or '(none)'}\n\n"
        "Decide the target file(s) and the root cause. If you still need to read or "
        'search code, ask for exactly one tool call with {"action": {"tool": '
        '"read_file"|"search_code", "arguments": {...}}}. Otherwise answer with '
        '{"diagnosis": {"target_files": [str], "root_cause": str, "edit_intent": str}}. '
        + _JSON_ONLY
    )
    return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", user)]


def patch_messages(
    *,
    problem: str,
    diagnosis_summary: str,
    file_blocks: str,
    feedback: str,
) -> list[ChatMessage]:
    """Patch generation: a unified diff compatible with ``git apply``."""

    user = (
        f"Problem statement:\n{problem}\n\n"
        f"Diagnosis:\n{diagnosis_summary}\n\n"
        f"Current file contents:\n{file_blocks}\n\n"
        f"Previous attempt feedback:\n{feedback or '(first attempt)'}\n\n"
        "Return a unified diff (git apply compatible) that fixes the problem with the "
        "smallest possible change. Do not modify tests to make them pass. "
        + _JSON_ONLY
        + '\nSchema: {"diff": str, "notes": str}'
    )
    return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", user)]


def parse_retry_messages(*, previous: str, error: str) -> list[ChatMessage]:
    """Corrective turn used when the previous answer was not valid JSON."""

    user = (
        "Your previous answer could not be parsed.\n\n"
        f"Parser error: {error}\n\n"
        f"Previous answer:\n{previous[:2_000]}\n\n"
        "Reply again with a single valid JSON object, no prose, no markdown fence."
    )
    return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", user)]
