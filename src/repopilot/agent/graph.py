"""LangGraph repair workflow with a finite, budgeted loop.

Nodes mirror design document section 3.3:

``understand`` -> ``explore`` -> ``locate`` -> ``patch`` -> ``execute`` ->
``analyze`` -> (retry) or ``summarize``.

The model drives task understanding, code location (it may request one more
``read_file``/``search_code`` call per turn) and patch generation. The decision to
retry or stop is deterministic: it is driven by test outcomes and the loop
budget, never by the model's own opinion about whether the repair worked.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from repopilot.agent import prompts
from repopilot.agent.limits import AgentLimits, LoopMeter
from repopilot.agent.parsing import (
    Diagnosis,
    PatchProposal,
    Plan,
    ToolAction,
    parse_locate,
    parse_payload,
)
from repopilot.agent.quality_gate import (
    evaluate_quality_gate,
    normalize_nodeid,
    normalize_outcomes,
    resolve_target,
)
from repopilot.agent.tools import ToolResult, WorkspaceTools
from repopilot.cancellation import CancellationToken
from repopilot.config import Settings
from repopilot.errors import ModelOutputError, RepoPilotError
from repopilot.llm.base import ChatMessage, ModelClient, ModelRequest, ModelResponse
from repopilot.logging_setup import RunLogger
from repopilot.models import (
    ModelCallUsage,
    ModelUsage,
    PatchArtifact,
    QualityGateReport,
    RepairRound,
    TerminationReason,
    TestSnapshot,
)

_TRANSCRIPT_LIMIT = 20
_PROMPT_TOOL_EXCERPT = 1_200
_FAILING_OUTCOMES = ("failed", "error")
_LOCATE_TOOLS = frozenset({"read_file", "search_code"})


class LoopStop(Exception):
    """Raised when a loop budget is exhausted mid step."""

    def __init__(self, reason: TerminationReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class AgentCancelled(Exception):
    """Raised when the run was cancelled or a command was cancelled."""


class AgentState(TypedDict, total=False):
    """LangGraph state: the runtime object plus the next node to run."""

    runtime: AgentRuntime
    route: str


@dataclass
class AgentRuntime:
    """Shared mutable state for one repair run.

    Everything the nodes need is on this object, so the graph nodes stay thin and
    the whole run can be inspected, persisted and resumed from one place.
    """

    settings: Settings
    client: ModelClient
    tools: WorkspaceTools
    logger: RunLogger
    token: CancellationToken
    problem: str
    limits: AgentLimits
    meter: LoopMeter
    target_tests: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    profile_summary: str = ""
    file_listing: str = ""
    baseline_tests: TestSnapshot | None = None
    baseline_sources: dict[str, str] = field(default_factory=dict)
    state_path: Path | None = None
    final_sources_provider: Callable[[], dict[str, str]] | None = None
    resumed_from: str | None = None

    usage: ModelUsage = field(default_factory=ModelUsage)
    plan: Plan | None = None
    diagnosis: Diagnosis | None = None
    context_blocks: list[tuple[str, str]] = field(default_factory=list)
    located_files: list[str] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)
    patch: PatchArtifact | None = None
    proposal_diff: str = ""
    feedback: str = ""
    rounds: list[RepairRound] = field(default_factory=list)
    current_round: RepairRound | None = None
    final_tests: TestSnapshot | None = None
    final_sources: dict[str, str] = field(default_factory=dict)
    gate: QualityGateReport = field(default_factory=QualityGateReport)
    termination: TerminationReason | None = None
    result: str = "not_fixed"
    last_diff_hash: str | None = None
    repeated_attempts: int = 0
    tool_index: int = 0
    last_call: ModelCallUsage | None = None

    # -- logging ---------------------------------------------------------------

    def log(self, event: str, message: str = "", *, level: str = "info", **data: Any) -> None:
        self.logger.event(event, message, level=level, **data)

    # -- model access ----------------------------------------------------------

    def _cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round(
            prompt_tokens / 1000 * self.settings.model_prompt_cost_per_1k_tokens
            + completion_tokens / 1000 * self.settings.model_completion_cost_per_1k_tokens,
            8,
        )

    def _record_call(
        self,
        *,
        phase: str,
        model: str,
        ok: bool,
        response: ModelResponse | None,
        latency_ms: int,
        error_code: str | None = None,
        error: str | None = None,
    ) -> None:
        call = ModelCallUsage(
            index=self.usage.calls + 1,
            phase=phase,
            model=model,
            ok=ok,
            prompt_tokens=response.prompt_tokens if response else 0,
            completion_tokens=response.completion_tokens if response else 0,
            total_tokens=response.total_tokens if response else 0,
            latency_ms=latency_ms,
            finish_reason=response.finish_reason if response else None,
            truncated=response.truncated if response else False,
            cost_usd=self._cost(
                response.prompt_tokens if response else 0,
                response.completion_tokens if response else 0,
            ),
            error_code=error_code,
            error=error,
        )
        self.usage.record(call)
        self.meter.note_model_call(call.total_tokens)
        self.last_call = call
        if self.current_round is not None:
            self.current_round.model_calls.append(call)

    def mark_last_call_unusable(self, error: ModelOutputError) -> None:
        """Flag the most recent call as failed because its answer was unusable.

        The transport call succeeded, but the output could not be parsed even
        after the retry budget, so it must not be counted as a healthy call.
        """

        call = self.last_call
        if call is None or not call.ok:
            return
        call.ok = False
        call.error_code = error.code
        call.error = error.message
        self.usage.failed_calls += 1
        self.log(
            "agent.model_output_invalid",
            error.message[:400],
            level="error",
            phase=call.phase,
            call_index=call.index,
        )

    def call_model(self, messages: list[ChatMessage], *, phase: str) -> ModelResponse:
        """Make one budgeted model call, recording usage whether or not it fails."""

        if self.token.is_cancelled():
            raise AgentCancelled
        reason = self.meter.check()
        if reason is not None:
            raise LoopStop(reason)

        request = ModelRequest(
            messages=list(messages),
            phase=phase,
            max_tokens=self.settings.model_max_tokens,
        )
        if request.character_count() > self.settings.model_max_prompt_chars:
            raise ModelOutputError(
                f"prompt of {request.character_count()} characters exceeds the configured "
                f"limit of {self.settings.model_max_prompt_chars}"
            )

        started = time.monotonic()
        try:
            response = self.client.complete(request)
        except RepoPilotError as exc:
            self._record_call(
                phase=phase,
                model=self.client.model,
                ok=False,
                response=None,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=exc.code,
                error=exc.message,
            )
            self.log("agent.model_failed", exc.failure_reason(), level="error", phase=phase)
            raise
        self._record_call(
            phase=phase,
            model=response.model,
            ok=response.ok,
            response=response,
            latency_ms=response.latency_ms,
        )
        self.log(
            "agent.model_call",
            f"{phase} completion received",
            phase=phase,
            tokens=response.total_tokens,
            latency_ms=response.latency_ms,
            truncated=response.truncated,
        )
        return response

    def _retry_messages(
        self, messages: list[ChatMessage], previous: str, error: str, *, phase: str
    ) -> list[ChatMessage]:
        self.log(
            "agent.parse_retry",
            "model answer was not usable; asking again",
            level="warning",
            phase=phase,
            error=error[:400],
        )
        return [
            *messages,
            ChatMessage("assistant", previous),
            *prompts.parse_retry_messages(previous=previous, error=error),
        ]

    def call_model_json(self, messages: list[ChatMessage], model_cls, *, phase: str):
        """Call the model and parse a strict JSON object, with bounded retries."""

        attempt = list(messages)
        while True:
            response = self.call_model(attempt, phase=phase)
            try:
                return parse_payload(response, model_cls)
            except ModelOutputError as exc:
                if not self.meter.note_parse_failure():
                    self.mark_last_call_unusable(exc)
                    raise
                attempt = self._retry_messages(messages, response.text, str(exc), phase=phase)

    def call_locate(self, messages: list[ChatMessage]) -> ToolAction | Diagnosis:
        """Call the model for the location step, with bounded parse retries."""

        attempt = list(messages)
        while True:
            response = self.call_model(attempt, phase="locate")
            try:
                return parse_locate(response)
            except ModelOutputError as exc:
                if not self.meter.note_parse_failure():
                    self.mark_last_call_unusable(exc)
                    raise
                attempt = self._retry_messages(messages, response.text, str(exc), phase="locate")

    # -- tool access -----------------------------------------------------------

    def invoke_tool(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        """Run one controlled tool, enforcing cancellation and tool budget."""

        if self.token.is_cancelled():
            raise AgentCancelled
        reason = self.meter.check()
        if reason is not None:
            raise LoopStop(reason)
        result = self.tools.dispatch(tool, arguments)
        self.tool_index += 1
        self.meter.note_tool_call()
        if self.current_round is not None:
            self.current_round.tool_calls.append(
                result.to_invocation(
                    index=self.tool_index,
                    round_index=self.current_round.index,
                    arguments=arguments,
                )
            )
        self.log(
            "agent.tool_call",
            f"{tool} -> {'ok' if result.ok else 'error'}",
            tool=tool,
            ok=result.ok,
            error_code=result.error_code,
            duration_ms=result.duration_ms,
            output_bytes=result.output_bytes,
            truncated=result.truncated,
        )
        return result

    def note_for_prompt(self, label: str, result: ToolResult) -> None:
        state = "ok" if result.ok else f"error:{result.error_code or 'tool_error'}"
        self.transcript.append(f"[{label} {state}] {result.output[:_PROMPT_TOOL_EXCERPT]}")
        if len(self.transcript) > _TRANSCRIPT_LIMIT:
            self.transcript = self.transcript[-_TRANSCRIPT_LIMIT:]

    def read_repo_file(self, path: str, *, end_line: int | None = None) -> tuple[str, str] | None:
        """Read a repository file through ``read_file``; ``None`` when unavailable."""

        arguments: dict[str, Any] = {"path": path}
        if end_line is not None:
            arguments["end_line"] = end_line
        result = self.invoke_tool("read_file", arguments)
        self.note_for_prompt(f"read_file {path}", result)
        if not result.ok:
            return None
        return str(result.data.get("path", path)), str(result.data.get("content", ""))

    # -- rounds and persistence ------------------------------------------------

    def start_round(self, *, phase: str) -> RepairRound:
        rnd = RepairRound(index=len(self.rounds) + 1, phase=phase)
        self.rounds.append(rnd)
        self.current_round = rnd
        self.meter.note_round()
        self.log("agent.round_started", f"round {rnd.index} started", round=rnd.index)
        return rnd

    def state_snapshot(self) -> dict[str, Any]:
        """Full, persisted snapshot of the run, enough to resume or debug."""

        return {
            "schema_version": "repair-state-1.0",
            "task_id": self.logger.task_id,
            "run_id": self.logger.run_id,
            "problem": self.problem,
            "target_tests": self.target_tests,
            "allowed_paths": self.allowed_paths,
            "resumed_from": self.resumed_from,
            "rounds": [rnd.model_dump() for rnd in self.rounds],
            "usage": self.usage.model_dump(),
            "meter": self.meter.snapshot(),
            "located_files": self.located_files,
            "patch": self.patch.model_dump() if self.patch else None,
            "gate": self.gate.model_dump(),
            "termination_reason": self.termination.value if self.termination else None,
            "result": self.result,
            "baseline_tests": self.baseline_tests.model_dump() if self.baseline_tests else None,
            "final_tests": self.final_tests.model_dump() if self.final_tests else None,
        }

    def persist(self) -> None:
        """Write the run state after every round so a run can be inspected/resumed."""

        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self.state_snapshot(), ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - defensive
            self.log("repair.state_write_failed", str(exc), level="warning")


def _guard(node: Callable[[AgentState], AgentState]) -> Callable[[AgentState], AgentState]:
    """Convert a budget stop into a graceful jump to ``summarize``."""

    def wrapped(state: AgentState) -> AgentState:
        runtime = state["runtime"]
        try:
            return node(state)
        except LoopStop as stop:
            runtime.termination = stop.reason
            runtime.log(
                "agent.limit_hit",
                f"loop limit reached: {stop.reason.value}",
                level="warning",
                reason=stop.reason.value,
                **runtime.meter.snapshot(),
            )
            return {"route": "summarize"}

    return wrapped


def _check_cancel(runtime: AgentRuntime) -> None:
    if runtime.token.is_cancelled():
        raise AgentCancelled


def _snapshot_from_tool(result: ToolResult) -> TestSnapshot | None:
    payload = result.data.get("snapshot")
    if not isinstance(payload, dict):
        return None
    try:
        return TestSnapshot.model_validate(payload)
    except Exception:  # pragma: no cover - defensive against schema drift
        return None


def _targets_fixed(runtime: AgentRuntime) -> bool:
    if runtime.final_tests is None or not runtime.final_tests.ran:
        return False
    final_map = normalize_outcomes(runtime.final_tests)
    baseline_map = normalize_outcomes(runtime.baseline_tests)
    targets = [normalize_nodeid(target) for target in runtime.target_tests] or [
        node for node, outcome in baseline_map.items() if outcome in _FAILING_OUTCOMES
    ]
    if not targets:
        return False
    for target in targets:
        node = resolve_target(target, final_map, baseline_map)
        if node is None or final_map.get(node) != "passed":
            return False
    return all(
        final_map.get(node) == "passed"
        for node, outcome in baseline_map.items()
        if outcome == "passed"
    )


def _summarise_tests(baseline: TestSnapshot | None, final: TestSnapshot | None) -> str:
    if final is None:
        return "the test suite could not be executed, so the repair is unverified"
    baseline_passed = baseline.passed if baseline else 0
    baseline_failed = baseline.failed if baseline else 0
    return (
        f"pytest: {final.passed} passed, {final.failed} failed, {final.errors} error, "
        f"{final.skipped} skipped (baseline: {baseline_passed} passed, {baseline_failed} failed)"
    )


# -- nodes ---------------------------------------------------------------------


@_guard
def node_understand(state: AgentState) -> AgentState:
    """Task understanding: restate the goal and plan searches."""

    runtime = state["runtime"]
    _check_cancel(runtime)
    if runtime.meter.check() is not None:
        raise LoopStop(runtime.meter.check())
    messages = prompts.understand_messages(
        problem=runtime.problem,
        profile_summary=runtime.profile_summary,
        file_listing=runtime.file_listing,
    )
    plan = runtime.call_model_json(messages, Plan, phase="understand")
    runtime.plan = plan
    runtime.log(
        "agent.understood",
        "task understanding complete",
        goal=plan.goal,
        queries=plan.queries,
        suspect_paths=plan.suspect_paths,
    )
    return {"route": "explore"}


@_guard
def node_explore(state: AgentState) -> AgentState:
    """Repository exploration driven by the plan's search queries."""

    runtime = state["runtime"]
    _check_cancel(runtime)
    plan = runtime.plan or Plan()
    queries = [query for query in plan.queries if query.strip()][:3]
    candidates: list[str] = [path for path in plan.suspect_paths if path.strip()]
    for query in queries:
        result = runtime.invoke_tool("search_code", {"pattern": query, "max_results": 20})
        runtime.note_for_prompt(f"search_code {query!r}", result)
        if result.ok:
            for hit in result.data.get("matches") or []:
                path = hit.get("path")
                if isinstance(path, str) and path not in candidates:
                    candidates.append(path)
    candidates = candidates[: runtime.settings.agent_max_context_files]
    for path in candidates:
        loaded = runtime.read_repo_file(path, end_line=400)
        if loaded is None:
            continue
        relative, content = loaded
        if all(relative != existing for existing, _ in runtime.context_blocks):
            runtime.context_blocks.append((relative, content))
        if relative not in runtime.located_files:
            runtime.located_files.append(relative)
    runtime.log(
        "agent.explored",
        f"collected context for {len(runtime.context_blocks)} file(s)",
        queries=queries,
        files=[path for path, _ in runtime.context_blocks],
    )
    return {"route": "locate"}


@_guard
def node_locate(state: AgentState) -> AgentState:
    """Code location: allow one more tool call per turn, then require a diagnosis."""

    runtime = state["runtime"]
    _check_cancel(runtime)
    messages = prompts.locate_messages(
        problem=runtime.problem,
        plan_goal=runtime.plan.goal if runtime.plan else "",
        context=prompts.format_blocks(runtime.context_blocks),
        tool_history="\n".join(runtime.transcript[-8:]),
    )
    decision = runtime.call_locate(messages)
    if isinstance(decision, ToolAction):
        if decision.tool not in _LOCATE_TOOLS:
            runtime.feedback = (
                f"tool {decision.tool!r} is not available during code location; "
                "use read_file or search_code, or return a diagnosis"
            )
            runtime.log("agent.tool_rejected", runtime.feedback, level="warning")
            return {"route": "locate"}
        result = runtime.invoke_tool(decision.tool, decision.arguments)
        runtime.note_for_prompt(decision.tool, result)
        return {"route": "locate"}

    runtime.diagnosis = decision
    for path in decision.target_files:
        if path not in runtime.located_files:
            runtime.located_files.append(path)
    runtime.log(
        "agent.located",
        decision.root_cause or "code location complete",
        target_files=decision.target_files,
        root_cause=decision.root_cause,
        edit_intent=decision.edit_intent,
    )
    return {"route": "patch"}


@_guard
def node_patch(state: AgentState) -> AgentState:
    """Patch generation for the current round."""

    runtime = state["runtime"]
    _check_cancel(runtime)
    reason = runtime.meter.check_round()
    if reason is not None:
        raise LoopStop(reason)
    rnd = runtime.start_round(phase="patch")

    targets = list(runtime.diagnosis.target_files) if runtime.diagnosis else []
    if not targets:
        targets = runtime.located_files[:2]
    blocks: list[tuple[str, str]] = []
    for path in targets:
        loaded = runtime.read_repo_file(path)
        if loaded is None:
            continue
        relative, content = loaded
        blocks.append((relative, content))
        if relative not in runtime.located_files:
            runtime.located_files.append(relative)

    diagnosis_summary = (
        f"{runtime.diagnosis.root_cause}\nEdit intent: {runtime.diagnosis.edit_intent}"
        if runtime.diagnosis
        else "(no diagnosis recorded)"
    )
    messages = prompts.patch_messages(
        problem=runtime.problem,
        diagnosis_summary=diagnosis_summary,
        file_blocks=prompts.format_blocks(blocks),
        feedback=runtime.feedback,
    )
    proposal = runtime.call_model_json(messages, PatchProposal, phase="patch")
    runtime.proposal_diff = proposal.diff
    runtime.feedback = ""
    rnd.summary = proposal.notes or "patch proposed"
    runtime.log(
        "agent.patch_proposed", "patch proposed", round=rnd.index, notes=proposal.notes[:500]
    )
    return {"route": "execute"}


@_guard
def node_execute(state: AgentState) -> AgentState:
    """Apply the patch and run the target tests through the sandbox."""

    runtime = state["runtime"]
    _check_cancel(runtime)
    rnd = runtime.current_round or runtime.start_round(phase="execute")
    diff = runtime.proposal_diff
    if not diff.strip():
        runtime.feedback = "the previous answer contained an empty diff; return a real unified diff"
        runtime.patch = PatchArtifact(applied=False)
        rnd.notes.append("model returned an empty diff")
        runtime.persist()
        return {"route": "analyze"}

    apply_result = runtime.invoke_tool("apply_patch", {"diff": diff})
    runtime.note_for_prompt("apply_patch", apply_result)
    changed = [str(path) for path in (apply_result.data.get("changed_files") or [])]
    diff_hash = apply_result.data.get("diff_hash")
    added = int(apply_result.data.get("added_lines") or 0)
    removed = int(apply_result.data.get("removed_lines") or 0)
    runtime.patch = PatchArtifact(
        applied=apply_result.ok,
        diff=diff,
        changed_files=changed,
        added_lines=added,
        removed_lines=removed,
        diff_lines=added + removed,
        diff_hash=str(diff_hash) if diff_hash else None,
    )
    rnd.patch_hash = runtime.patch.diff_hash

    if not apply_result.ok:
        runtime.feedback = f"the patch was rejected: {apply_result.error}"
        runtime.log("agent.patch_rejected", apply_result.error or "", level="warning")
        runtime.persist()
        return {"route": "analyze"}

    # The full suite runs so the gate can compare against the baseline and detect
    # regressions; the target tests are matched from that same snapshot.
    tests = runtime.invoke_tool("run_tests", {})
    runtime.note_for_prompt("run_tests", tests)
    snapshot = _snapshot_from_tool(tests)
    if snapshot is not None:
        runtime.final_tests = snapshot
        rnd.tests = snapshot

    diff_view = runtime.invoke_tool("view_diff", {})
    if diff_view.ok and diff_view.data.get("diff"):
        runtime.patch.diff = str(diff_view.data["diff"])

    runtime.feedback = _summarise_tests(runtime.baseline_tests, runtime.final_tests)
    runtime.persist()
    return {"route": "analyze"}


@_guard
def node_analyze(state: AgentState) -> AgentState:
    """Deterministic feedback analysis: retry or stop."""

    runtime = state["runtime"]
    _check_cancel(runtime)
    rnd = runtime.current_round

    if runtime.patch is not None and runtime.patch.diff_hash:
        if runtime.patch.diff_hash == runtime.last_diff_hash:
            runtime.repeated_attempts += 1
        else:
            runtime.repeated_attempts = 0
        runtime.last_diff_hash = runtime.patch.diff_hash

    if runtime.patch is not None and runtime.patch.applied and _targets_fixed(runtime):
        runtime.termination = TerminationReason.completed
        runtime.log("agent.analysis", "target tests pass without regressions")
        return {"route": "summarize"}

    reason = runtime.meter.check()
    if reason is not None:
        runtime.termination = reason
        runtime.log(
            "agent.limit_hit",
            f"loop limit reached: {reason.value}",
            level="warning",
            reason=reason.value,
        )
        return {"route": "summarize"}

    if runtime.repeated_attempts >= 1:
        runtime.termination = TerminationReason.no_progress
        runtime.log(
            "agent.no_progress", "identical patch produced again; stopping", level="warning"
        )
        return {"route": "summarize"}

    limit = runtime.meter.check_round()
    if limit is not None:
        runtime.termination = limit
        runtime.log(
            "agent.limit_hit",
            f"loop limit reached: {limit.value}",
            level="warning",
            reason=limit.value,
        )
        return {"route": "summarize"}

    if rnd is not None:
        rnd.termination_reason = TerminationReason.gate_failed
    runtime.log(
        "agent.retry",
        "repair attempt did not satisfy the targets; retrying",
        level="warning",
        round=rnd.index if rnd else 0,
        feedback=runtime.feedback[:500],
    )
    return {"route": "patch"}


@_guard
def node_summarize(state: AgentState) -> AgentState:
    """Evaluate the quality gate and freeze the outcome."""

    runtime = state["runtime"]
    if runtime.termination is None:
        runtime.termination = TerminationReason.gate_failed
    if runtime.final_sources_provider is not None:
        runtime.final_sources = runtime.final_sources_provider()
    runtime.gate = evaluate_quality_gate(
        limits=runtime.limits,
        patch=runtime.patch,
        baseline_tests=runtime.baseline_tests,
        final_tests=runtime.final_tests,
        target_tests=runtime.target_tests,
        located_files=runtime.located_files,
        allowed_paths=runtime.allowed_paths,
        baseline_sources=runtime.baseline_sources,
        final_sources=runtime.final_sources,
    )
    runtime.result = "fixed" if runtime.gate.passed else "not_fixed"
    runtime.log(
        "agent.gate_evaluated",
        f"quality gate passed={runtime.gate.passed}",
        passed=runtime.gate.passed,
        primary_blocker=runtime.gate.primary_blocker,
        blockers=runtime.gate.blockers,
        warnings=runtime.gate.warnings,
        cheating_findings=runtime.gate.cheating_findings,
        termination_reason=runtime.termination.value,
    )
    runtime.persist()
    return {"route": "end"}


def _route(state: AgentState) -> str:
    return state.get("route", "summarize")


def build_repair_graph():
    """Compile the finite repair loop as a LangGraph state machine."""

    graph = StateGraph(AgentState)
    graph.add_node("understand", node_understand)
    graph.add_node("explore", node_explore)
    graph.add_node("locate", node_locate)
    graph.add_node("patch", node_patch)
    graph.add_node("execute", node_execute)
    graph.add_node("analyze", node_analyze)
    graph.add_node("summarize", node_summarize)

    graph.set_entry_point("understand")
    graph.add_conditional_edges(
        "understand", _route, {"explore": "explore", "summarize": "summarize"}
    )
    graph.add_conditional_edges("explore", _route, {"locate": "locate", "summarize": "summarize"})
    graph.add_conditional_edges(
        "locate", _route, {"locate": "locate", "patch": "patch", "summarize": "summarize"}
    )
    graph.add_conditional_edges("patch", _route, {"execute": "execute", "summarize": "summarize"})
    graph.add_conditional_edges("execute", _route, {"analyze": "analyze", "summarize": "summarize"})
    graph.add_conditional_edges("analyze", _route, {"patch": "patch", "summarize": "summarize"})
    graph.add_edge("summarize", END)
    return graph.compile()


def recursion_limit(limits: AgentLimits) -> int:
    """LangGraph superstep budget large enough for the configured tool budget."""

    return min(500, 20 + 4 * (limits.max_rounds + limits.max_tool_calls + limits.max_model_calls))


def run_repair_agent(runtime: AgentRuntime) -> AgentRuntime:
    """Run the compiled graph until it reaches ``summarize``."""

    runtime.log(
        "agent.started",
        "repair agent started",
        provider=runtime.client.provider,
        model=runtime.client.model,
        limits={
            "max_rounds": runtime.limits.max_rounds,
            "max_wall_seconds": runtime.limits.max_wall_seconds,
            "max_model_calls": runtime.limits.max_model_calls,
            "max_model_tokens": runtime.limits.max_model_tokens,
            "max_tool_calls": runtime.limits.max_tool_calls,
            "max_diff_lines": runtime.limits.max_diff_lines,
        },
        target_tests=runtime.target_tests,
        resumed_from=runtime.resumed_from,
    )
    app = build_repair_graph()
    state: AgentState = {"runtime": runtime, "route": "understand"}
    app.invoke(state, {"recursion_limit": recursion_limit(runtime.limits)})
    return runtime
