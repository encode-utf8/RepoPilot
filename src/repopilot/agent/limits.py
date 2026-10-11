"""Finite loop control.

Every budget the design document calls for (max repair rounds, wall clock, model
call budget, tool output size) lives here, and the loop asks :class:`LoopMeter`
before each step. Hitting any budget returns a specific
:class:`~repopilot.models.TerminationReason`, so a terminated run always records
*why* it stopped instead of silently reporting success.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from repopilot.config import Settings
from repopilot.models import TerminationReason


@dataclass(frozen=True)
class AgentLimits:
    """Immutable budget for one repair run."""

    max_rounds: int = 6
    max_wall_seconds: float = 900.0
    max_model_calls: int = 12
    max_model_tokens: int = 60_000
    max_tool_calls: int = 40
    max_tool_output_bytes: int = 200_000
    max_diff_lines: int = 400
    max_changed_files: int = 10
    max_parse_retries: int = 1

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentLimits:
        return cls(
            max_rounds=settings.agent_max_rounds,
            max_wall_seconds=settings.agent_max_wall_seconds,
            max_model_calls=settings.agent_max_model_calls,
            max_model_tokens=settings.agent_max_model_tokens,
            max_tool_calls=settings.agent_max_tool_calls,
            max_tool_output_bytes=settings.agent_max_tool_output_bytes,
            max_diff_lines=settings.agent_max_diff_lines,
            max_changed_files=settings.agent_max_changed_files,
            max_parse_retries=settings.agent_max_parse_retries,
        )


@dataclass
class LoopMeter:
    """Live counters checked before every loop step."""

    limits: AgentLimits
    started: float = field(default_factory=time.monotonic)
    model_calls: int = 0
    model_tokens: int = 0
    tool_calls: int = 0
    rounds: int = 0
    parse_failures: int = 0

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started

    def check(self) -> TerminationReason | None:
        """Return the exhausted budget, if any.

        Checked in a fixed order so termination reasons are deterministic.
        """

        if self.elapsed_seconds() >= self.limits.max_wall_seconds:
            return TerminationReason.limit_wall_clock
        if self.model_calls >= self.limits.max_model_calls:
            return TerminationReason.limit_model_calls
        if self.model_tokens >= self.limits.max_model_tokens:
            return TerminationReason.limit_model_tokens
        if self.tool_calls >= self.limits.max_tool_calls:
            return TerminationReason.limit_tool_calls
        return None

    def check_round(self) -> TerminationReason | None:
        """Return the exhaustion reason for starting another repair round."""

        reason = self.check()
        if reason is not None:
            return reason
        if self.rounds >= self.limits.max_rounds:
            return TerminationReason.limit_max_rounds
        return None

    def note_model_call(self, tokens: int) -> None:
        self.model_calls += 1
        self.model_tokens += max(int(tokens), 0)

    def note_tool_call(self) -> None:
        self.tool_calls += 1

    def note_round(self) -> None:
        self.rounds += 1

    def note_parse_failure(self) -> bool:
        """Record an unparseable model answer; ``True`` when retries remain."""

        self.parse_failures += 1
        return self.parse_failures <= self.limits.max_parse_retries

    def snapshot(self) -> dict[str, float | int]:
        return {
            "rounds": self.rounds,
            "model_calls": self.model_calls,
            "model_tokens": self.model_tokens,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
        }
