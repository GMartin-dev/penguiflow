"""Native tool-calling step for the React planner.

Provider-native tool calls replace the prompted ``{next_node, args}`` JSON
envelope as the WIRE FORMAT only — the result is the same ``PlannerAction``
the runtime already executes (decision-shape invariance, transport analysis
Decision 9 / Phase 5).

Mapping (Phase 5 design decisions D5.1/D5.5):
- zero tool calls   → ``final_response`` with the content as the answer
- one tool call     → direct tool action (args parsed from provider-validated JSON)
- N tool calls      → ``parallel`` plan with one step per call (no join in v1)
- content alongside tool calls → ``action.thought``

Streaming (D5.3 revised): the native prompt forbids text alongside tool
calls, so content deltas stream token-by-token on the ``answer`` channel
(parity with prompted mode). A disobedient preamble turn closes the answer
stream with ``superseded: True`` and re-emits the text on ``thinking``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..llm.types import ToolSpec
from .llm import _coerce_llm_response
from .models import PlannerAction, PlannerEvent
from .trajectory import Trajectory

logger = logging.getLogger("penguiflow.planner")

_DOWNGRADE_FLAG = "native_tool_call_downgraded"


def resolve_native_eligibility(planner: Any) -> str | None:
    """Return None when native mode can run, else a human-readable reason."""
    client = planner._client
    if not hasattr(client, "complete_with_tools"):
        return f"client {type(client).__name__} does not expose complete_with_tools"

    model = getattr(client, "_model", None)
    if isinstance(model, str) and model:
        from ..llm.profiles import get_profile

        profile = get_profile(model)
        if not getattr(profile, "supports_tools", True):
            return f"model '{model}' does not support tools"
        if not getattr(profile, "supports_native_tool_calls", True):
            return f"model '{model}' route does not support native tool calls"
    return None


def emit_downgrade_once(planner: Any, trajectory: Trajectory, reason: str) -> None:
    """Emit the downgrade event once per run (the condition is run-stable)."""
    if trajectory.metadata.get(_DOWNGRADE_FLAG):
        return
    trajectory.metadata[_DOWNGRADE_FLAG] = reason
    logger.warning("tool_call_mode_downgraded", extra={"reason": reason})
    planner._emit_event(
        PlannerEvent(
            event_type="tool_call_mode_downgraded",
            ts=planner._time_source(),
            trajectory_step=len(trajectory.steps),
            extra={"from": "native", "to": "prompted", "reason": reason},
        )
    )


_TOOL_NAME_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def build_native_tools(planner: Any) -> tuple[list[ToolSpec], dict[str, str]]:
    """Declare the currently visible catalog as native tool specs.

    Catalog names may contain characters that provider function-name patterns
    reject (e.g. MCP tools like ``youtube.download_video`` vs the
    ``^[a-zA-Z0-9_-]{1,128}$`` rule — found live on Databricks). Each tool is
    declared under a wire-safe alias; the returned mapping translates the
    provider's calls back to real catalog names.
    """
    tools: list[ToolSpec] = []
    alias_to_name: dict[str, str] = {}
    used: set[str] = set()
    for spec in planner._specs:
        alias = _TOOL_NAME_UNSAFE_RE.sub("_", spec.name)[:128] or "tool"
        if alias in used:
            base = alias[:120]
            suffix = 2
            while f"{base}_{suffix}" in used:
                suffix += 1
            alias = f"{base}_{suffix}"
        used.add(alias)
        alias_to_name[alias] = spec.name
        tools.append(
            ToolSpec(
                name=alias,
                description=spec.desc,
                json_schema=spec.args_model.model_json_schema(),
            )
        )
    return tools, alias_to_name


def _parse_tool_args(arguments_json: str, *, tool_name: str) -> dict[str, Any]:
    """Parse provider-validated args defensively (D5.5).

    A malformed payload degrades to ``{}`` so the runtime's existing
    arg-validation repair handles it instead of crashing the step.
    """
    if not arguments_json:
        return {}
    try:
        parsed = json.loads(arguments_json)
    except (TypeError, ValueError):
        logger.warning(
            "native_tool_args_unparseable",
            extra={"tool": tool_name, "preview": arguments_json[:200]},
        )
        return {}
    if isinstance(parsed, dict):
        return parsed
    logger.warning(
        "native_tool_args_not_object",
        extra={"tool": tool_name, "type": type(parsed).__name__},
    )
    return {}


async def step_native(planner: Any, trajectory: Trajectory) -> PlannerAction:
    """One native-mode planner step: declare tools, map the reply to a PlannerAction."""
    had_pending_steering = bool(trajectory.steering_inputs)
    base_messages = list(await planner._build_messages(trajectory))
    # Arg-validation repair guidance is delivered via trajectory metadata by the
    # runtime (same contract as the prompted step) - consume it or it is lost.
    if isinstance(trajectory.metadata, dict):
        arg_repair_message = trajectory.metadata.pop("arg_repair_message", None)
        if arg_repair_message:
            base_messages.insert(0, {"role": "system", "content": arg_repair_message})
    tools, alias_to_name = build_native_tools(planner)
    # The runtime increments _action_seq and emits step_start BEFORE step runs;
    # chunks must carry the SAME seq or the UI answer gate drops them.
    current_action_seq = planner._action_seq
    stream_allowed = bool(planner._stream_final_response)

    # D5.3 (revised): the native-mode prompt forbids text alongside tool calls,
    # so content deltas stream OPTIMISTICALLY to the answer channel,
    # token-by-token, exactly like prompted mode. If the model disobeys and the
    # turn turns out to be a tool call, the answer stream is closed with
    # ``superseded: True`` and the text is re-emitted on the thinking channel
    # so traces stay coherent (consumers that ignore the marker degrade to
    # briefly showing preamble text — never broken state).
    answer_chunks_emitted = 0

    def _enqueue_chunk(extra: dict[str, Any]) -> None:
        # Same path as prompted mode: guardrail redaction applies when enabled.
        planner._enqueue_llm_stream_chunk(
            trajectory,
            {
                "ts": planner._time_source(),
                "trajectory_step": len(trajectory.steps),
                "extra": {"action_seq": current_action_seq, **extra},
            },
        )

    def _emit_chunk(text: str, done: bool) -> None:
        nonlocal answer_chunks_emitted
        if not text:
            return
        answer_chunks_emitted += 1
        _enqueue_chunk({"text": text, "done": False, "phase": "answer", "channel": "answer"})

    def _emit_reasoning_chunk(text: str, done: bool) -> None:
        if not text and not done:
            return
        # Parity with prompted mode: reasoning renders on the thinking channel.
        _enqueue_chunk({"text": text, "done": done, "phase": "thinking", "channel": "thinking"})

    use_reasoning = getattr(planner, "_use_native_reasoning", True)
    result = await planner._client.complete_with_tools(
        messages=base_messages,
        tools=tools,
        stream=stream_allowed,
        on_stream_chunk=_emit_chunk if stream_allowed else None,
        on_reasoning_chunk=_emit_reasoning_chunk if (stream_allowed and use_reasoning) else None,
    )
    raw_result = getattr(result, "content", None), getattr(result, "cost", None)
    if raw_result[0] is None:
        # Defensive: a JSONLLMClient-shaped return slipped through.
        content, cost = _coerce_llm_response(result)
        tool_calls: list[Any] = []
    else:
        content = result.content
        cost = result.cost
        tool_calls = result.tool_calls
    planner._cost_tracker.record_main_call(cost or 0.0)

    # Steering inputs were rendered into this step's messages; clear them so
    # they are not repeated on every subsequent step (prompted-step parity).
    if had_pending_steering:
        trajectory.steering_inputs.clear()

    content = (content or "").strip()

    def _close_answer_stream(*, superseded: bool) -> None:
        _enqueue_chunk(
            {
                "text": "",
                "done": True,
                "phase": "answer",
                "channel": "answer",
                **({"superseded": True} if superseded else {}),
            }
        )

    if not tool_calls:
        # D5.1: content-only turn is the final answer. It already streamed
        # token-by-token on the answer channel; emit the full text only when
        # nothing streamed (non-streaming runs), then close the stream.
        if content and stream_allowed:
            if answer_chunks_emitted == 0:
                _enqueue_chunk({"text": content, "done": False, "phase": "answer", "channel": "answer"})
            _close_answer_stream(superseded=False)
        return PlannerAction(
            next_node="final_response",
            args={"answer": content, "raw_answer": content},
            thought="",
            raw_llm_response=content,
        )

    # Tool turn. If the model disobeyed the no-preamble instruction and text
    # already streamed to the answer channel, retract it: close the answer
    # stream with the superseded marker and re-emit the text as thinking.
    if stream_allowed and answer_chunks_emitted > 0:
        _close_answer_stream(superseded=True)
        if content:
            _enqueue_chunk({"text": content, "done": False, "phase": "thinking", "channel": "thinking"})
        logger.info(
            "native_answer_stream_superseded",
            extra={"chars_streamed": len(content), "action_seq": current_action_seq},
        )

    def real_name(call: Any) -> str:
        return alias_to_name.get(call.name, call.name)

    if len(tool_calls) == 1:
        call = tool_calls[0]
        return PlannerAction(
            next_node=real_name(call),
            args=_parse_tool_args(call.arguments_json, tool_name=call.name),
            thought=content,
            raw_llm_response=content or None,
        )

    # D5.5: N>1 native tool calls map to the existing parallel plan (no join v1).
    steps = [
        {"node": real_name(call), "args": _parse_tool_args(call.arguments_json, tool_name=call.name)}
        for call in tool_calls
    ]
    return PlannerAction(
        next_node="parallel",
        args={"steps": steps},
        thought=content,
        raw_llm_response=content or None,
    )


__all__ = [
    "build_native_tools",
    "emit_downgrade_once",
    "resolve_native_eligibility",
    "step_native",
]
