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

Streaming (D5.3): content deltas stream live on the ``thinking`` channel
(preambles alongside tool calls are real); when a turn completes with no tool
calls, the full answer is flushed to the ``answer`` channel.
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
    base_messages = await planner._build_messages(trajectory)
    tools, alias_to_name = build_native_tools(planner)
    current_action_seq = planner._action_seq + 1
    stream_allowed = bool(planner._stream_final_response)

    thinking_emitted = False

    def _emit_chunk(text: str, done: bool) -> None:
        nonlocal thinking_emitted
        if not text and not done:
            return
        if text:
            thinking_emitted = True
            planner._emit_event(
                PlannerEvent(
                    event_type="llm_stream_chunk",
                    ts=planner._time_source(),
                    trajectory_step=len(trajectory.steps),
                    extra={
                        "text": text,
                        "done": False,
                        "phase": "observation",
                        "channel": "thinking",
                        "action_seq": current_action_seq,
                    },
                )
            )

    def _emit_reasoning_chunk(text: str, done: bool) -> None:
        if not text:
            return
        planner._emit_event(
            PlannerEvent(
                event_type="llm_stream_chunk",
                ts=planner._time_source(),
                trajectory_step=len(trajectory.steps),
                extra={
                    "text": text,
                    "done": done,
                    "phase": "observation",
                    "channel": "reasoning",
                    "action_seq": current_action_seq,
                },
            )
        )

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

    content = (content or "").strip()

    if not tool_calls:
        # D5.1: content-only turn is the final answer. Flush it to the answer
        # channel (single chunk + done — same emission shape as finish_repair).
        if content and stream_allowed:
            planner._emit_event(
                PlannerEvent(
                    event_type="llm_stream_chunk",
                    ts=planner._time_source(),
                    trajectory_step=len(trajectory.steps),
                    extra={
                        "text": content,
                        "done": False,
                        "phase": "answer",
                        "channel": "answer",
                        "action_seq": current_action_seq,
                        "thinking_superseded": thinking_emitted,
                    },
                )
            )
            planner._emit_event(
                PlannerEvent(
                    event_type="llm_stream_chunk",
                    ts=planner._time_source(),
                    trajectory_step=len(trajectory.steps),
                    extra={
                        "text": "",
                        "done": True,
                        "phase": "answer",
                        "channel": "answer",
                        "action_seq": current_action_seq,
                    },
                )
            )
        return PlannerAction(
            next_node="final_response",
            args={"answer": content, "raw_answer": content},
            thought="",
            raw_llm_response=content,
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
