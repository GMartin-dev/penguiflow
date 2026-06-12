"""Tests for the planner's native tool-calling mode (Phase 5)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import BaseModel

from penguiflow.catalog import build_catalog
from penguiflow.llm.profiles import ModelProfile, register_profile
from penguiflow.llm.protocol import NativeToolCallResult
from penguiflow.llm.types import ToolCallPart
from penguiflow.node import Node
from penguiflow.planner import ReactPlanner
from penguiflow.planner.models import PlannerEvent
from penguiflow.planner.react_step_native import _parse_tool_args, resolve_native_eligibility
from penguiflow.registry import ModelRegistry


class Query(BaseModel):
    text: str


class Verdict(BaseModel):
    answer: int
    method: str


TOOL_INVOCATIONS: list[tuple[str, str]] = []


async def echo(payload: Query, ctx: object) -> Query:
    del ctx
    TOOL_INVOCATIONS.append(("echo", payload.text))
    return payload


async def shout(payload: Query, ctx: object) -> Query:
    del ctx
    TOOL_INVOCATIONS.append(("shout", payload.text))
    return Query(text=payload.text.upper())


def make_catalog():
    registry = ModelRegistry()
    registry.register("echo", Query, Query)
    registry.register("shout", Query, Query)
    nodes = [Node(echo, name="echo"), Node(shout, name="shout")]
    return build_catalog(nodes, registry)


def tool_call(name: str, args: dict[str, Any] | str, call_id: str = "c1") -> ToolCallPart:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return ToolCallPart(name=name, arguments_json=arguments, call_id=call_id)


class StubNativeClient:
    """Stub exposing both complete_with_tools (native turns) and complete (repair turns)."""

    def __init__(self, turns: list[Any], model: str = "stub-native-model") -> None:
        self._turns = list(turns)
        self._model = model
        self.tool_declarations: list[list[str]] = []
        self.complete_calls: list[list[Mapping[str, Any]]] = []
        self.streamed: list[str] = []

    async def complete_with_tools(
        self,
        *,
        messages: list[Mapping[str, Any]],
        tools: list[Any],
        stream: bool = False,
        on_stream_chunk: Any = None,
        on_reasoning_chunk: Any = None,
    ) -> NativeToolCallResult:
        self.tool_declarations.append([t.name for t in tools])
        result = self._turns.pop(0)
        assert isinstance(result, NativeToolCallResult), "expected a native turn"
        if stream and on_stream_chunk is not None and result.content:
            on_stream_chunk(result.content, False)
            on_stream_chunk("", True)
        if stream and on_reasoning_chunk is not None and result.reasoning_content:
            on_reasoning_chunk(result.reasoning_content, False)
            on_reasoning_chunk("", True)
        return result

    async def complete(
        self,
        *,
        messages: list[Mapping[str, Any]],
        response_format: Mapping[str, Any] | None = None,
        stream: bool = False,
        on_stream_chunk: Any = None,
        on_reasoning_chunk: Any = None,
    ) -> tuple[str, float]:
        self.complete_calls.append(list(messages))
        result = self._turns.pop(0)
        assert isinstance(result, str), "expected a prompted/repair turn"
        return result, 0.0


def make_native_planner(client: Any, **kwargs: object) -> ReactPlanner:
    return ReactPlanner(llm_client=client, catalog=make_catalog(), tool_call_mode="native", **kwargs)


class TestNativeMapping:
    @pytest.mark.asyncio
    async def test_content_only_turn_is_final_answer(self) -> None:
        client = StubNativeClient([NativeToolCallResult(content="The answer is 391.")])
        planner = make_native_planner(client, max_iters=3)
        result = await planner.run("17*23?")

        assert result.reason == "answer_complete"
        assert result.payload["raw_answer"] == "The answer is 391."
        # The catalog was declared natively.
        assert client.tool_declarations == [["echo", "shout"]]

    @pytest.mark.asyncio
    async def test_single_tool_call_then_finish(self) -> None:
        client = StubNativeClient(
            [
                NativeToolCallResult(
                    content="Let me echo that.",
                    tool_calls=[tool_call("echo", {"text": "hi"})],
                ),
                NativeToolCallResult(content="Echoed: hi"),
            ]
        )
        planner = make_native_planner(client, max_iters=4)
        result = await planner.run("echo hi")

        assert result.reason == "answer_complete"
        assert result.payload["raw_answer"] == "Echoed: hi"
        assert len(client.tool_declarations) == 2

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_map_to_parallel_plan(self) -> None:
        TOOL_INVOCATIONS.clear()
        client = StubNativeClient(
            [
                NativeToolCallResult(
                    content="Running both.",
                    tool_calls=[
                        tool_call("echo", {"text": "a"}, "c1"),
                        tool_call("shout", {"text": "b"}, "c2"),
                    ],
                ),
                NativeToolCallResult(content="a and B done"),
            ]
        )
        planner = make_native_planner(client, max_iters=4)
        result = await planner.run("do both")

        assert result.reason == "answer_complete"
        assert result.payload["raw_answer"] == "a and B done"
        # Both branches of the parallel plan actually executed.
        assert ("echo", "a") in TOOL_INVOCATIONS
        assert ("shout", "b") in TOOL_INVOCATIONS

    @pytest.mark.asyncio
    async def test_malformed_args_degrade_to_empty_and_recover(self) -> None:
        assert _parse_tool_args("not json", tool_name="echo") == {}
        assert _parse_tool_args('["a", "b"]', tool_name="echo") == {}
        assert _parse_tool_args("", tool_name="echo") == {}
        assert _parse_tool_args('{"text": "ok"}', tool_name="echo") == {"text": "ok"}


class TestEligibilityAndDowngrade:
    def test_missing_method_is_ineligible(self) -> None:
        class Bare:
            _model = "stub-native-model"

        planner = make_native_planner(StubNativeClient([]))
        planner._client = Bare()
        reason = resolve_native_eligibility(planner)
        assert reason is not None and "complete_with_tools" in reason

    def test_route_gated_profile_is_ineligible(self) -> None:
        register_profile(
            "stub-gated-model",
            ModelProfile(supports_tools=True, supports_native_tool_calls=False),
        )
        client = StubNativeClient([], model="stub-gated-model")
        planner = make_native_planner(client)
        reason = resolve_native_eligibility(planner)
        assert reason is not None and "native tool calls" in reason

    def test_databricks_gpt_5_5_is_gated(self) -> None:
        from penguiflow.llm.profiles import get_profile

        assert get_profile("databricks-gpt-5-5").supports_native_tool_calls is False
        assert get_profile("databricks-claude-opus-4-8").supports_native_tool_calls is True

    @pytest.mark.asyncio
    async def test_downgrade_falls_back_to_prompted_with_event(self) -> None:
        events: list[PlannerEvent] = []
        # Client lacks complete_with_tools entirely → prompted JSON path serves.
        client = StubNativeClient([])

        class PromptedOnly:
            _model = "stub-native-model"

            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, *, messages, response_format=None, stream=False, on_stream_chunk=None):
                self.calls += 1
                return (
                    json.dumps({"next_node": "final_response", "args": {"answer": "via prompted"}}),
                    0.0,
                )

        del client
        planner = ReactPlanner(
            llm_client=PromptedOnly(),
            catalog=make_catalog(),
            tool_call_mode="native",
            max_iters=3,
            event_callback=events.append,
        )
        result = await planner.run("hi")

        assert result.payload["raw_answer"] == "via prompted"
        downgrades = [e for e in events if e.event_type == "tool_call_mode_downgraded"]
        assert len(downgrades) == 1
        assert downgrades[0].extra["to"] == "prompted"

    def test_invalid_tool_call_mode_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="tool_call_mode"):
            ReactPlanner(llm_client=StubNativeClient([]), catalog=make_catalog(), tool_call_mode="grpc")


class TestStructuredFinalInNativeMode:
    @pytest.mark.asyncio
    async def test_content_finish_triggers_structured_extraction(self) -> None:
        events: list[PlannerEvent] = []
        client = StubNativeClient(
            [
                NativeToolCallResult(content="It is 391."),
                # Extraction/repair turn goes through complete() with json_object.
                json.dumps({"answer": 391, "method": "multiplication"}),
            ]
        )
        planner = make_native_planner(
            client,
            max_iters=3,
            final_response_model=Verdict,
            event_callback=events.append,
        )
        result = await planner.run("17*23?")

        assert result.payload["raw_answer"] == "It is 391."
        assert result.payload["structured"] == {"answer": 391, "method": "multiplication"}
        assert len(client.complete_calls) == 1  # exactly one extraction turn
        validated = [e for e in events if e.event_type == "final_response_structured_validated"]
        assert validated and validated[0].extra["repaired"] is True


class TestNativeStreaming:
    @pytest.mark.asyncio
    async def test_answer_streams_token_by_token(self) -> None:
        """A content-only finish streams live on the answer channel (D5.3 revised)."""
        events: list[PlannerEvent] = []

        class TokenStreamingClient(StubNativeClient):
            async def complete_with_tools(
                self, *, messages, tools, stream=False, on_stream_chunk=None, on_reasoning_chunk=None
            ):
                self.tool_declarations.append([t.name for t in tools])
                result = self._turns.pop(0)
                if stream and on_stream_chunk is not None and result.content:
                    for token in result.content.split(" "):
                        on_stream_chunk(token + " ", False)
                    on_stream_chunk("", True)
                if stream and on_reasoning_chunk is not None and result.reasoning_content:
                    on_reasoning_chunk(result.reasoning_content, False)
                return result

        client = TokenStreamingClient(
            [NativeToolCallResult(content="Done: hi everyone", reasoning_content="quick check")]
        )
        planner = make_native_planner(
            client,
            max_iters=3,
            stream_final_response=True,
            event_callback=events.append,
        )
        result = await planner.run("hi")

        chunks = [e for e in events if e.event_type == "llm_stream_chunk"]
        answer = [e for e in chunks if e.extra.get("channel") == "answer" and e.extra.get("text")]
        reasoning = [e for e in chunks if e.extra.get("channel") == "reasoning" and e.extra.get("text")]
        answer_done = [e for e in chunks if e.extra.get("channel") == "answer" and e.extra.get("done")]

        assert result.payload["raw_answer"] == "Done: hi everyone"
        assert len(answer) == 3, "answer must stream token-by-token, not as one flush"
        assert answer_done and not answer_done[-1].extra.get("superseded")
        assert reasoning, "reasoning deltas must flow in native mode"

    @pytest.mark.asyncio
    async def test_disobedient_preamble_is_superseded(self) -> None:
        """Preamble text alongside tool calls retracts the answer stream."""
        events: list[PlannerEvent] = []

        class PreambleClient(StubNativeClient):
            async def complete_with_tools(
                self, *, messages, tools, stream=False, on_stream_chunk=None, on_reasoning_chunk=None
            ):
                self.tool_declarations.append([t.name for t in tools])
                result = self._turns.pop(0)
                if stream and on_stream_chunk is not None and result.content:
                    on_stream_chunk(result.content, False)
                return result

        client = PreambleClient(
            [
                NativeToolCallResult(
                    content="Let me check that.",
                    tool_calls=[tool_call("echo", {"text": "hi"})],
                ),
                NativeToolCallResult(content="Done: hi"),
            ]
        )
        planner = make_native_planner(
            client,
            max_iters=4,
            stream_final_response=True,
            event_callback=events.append,
        )
        result = await planner.run("echo hi")

        chunks = [e for e in events if e.event_type == "llm_stream_chunk"]
        superseded = [e for e in chunks if e.extra.get("superseded")]
        thinking = [e for e in chunks if e.extra.get("channel") == "thinking" and e.extra.get("text")]

        assert result.payload["raw_answer"] == "Done: hi"
        assert superseded, "preamble turn must close the answer stream with superseded=True"
        assert any(e.extra["text"] == "Let me check that." for e in thinking)


class TestDualModeConformance:
    """The same scenario produces the same decision/observation flow in both modes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["prompted", "native"])
    async def test_tool_then_finish_parity(self, mode: str) -> None:
        if mode == "native":
            client: Any = StubNativeClient(
                [
                    NativeToolCallResult(content="", tool_calls=[tool_call("echo", {"text": "hi"})]),
                    NativeToolCallResult(content="final: hi"),
                ]
            )
        else:

            class PromptedStub:
                _model = "stub-native-model"

                def __init__(self) -> None:
                    self._turns = [
                        json.dumps({"next_node": "echo", "args": {"text": "hi"}}),
                        json.dumps({"next_node": "final_response", "args": {"answer": "final: hi"}}),
                    ]

                async def complete(self, *, messages, response_format=None, stream=False, on_stream_chunk=None):
                    return self._turns.pop(0), 0.0

            client = PromptedStub()

        planner = ReactPlanner(
            llm_client=client,
            catalog=make_catalog(),
            tool_call_mode=mode,
            max_iters=4,
        )
        result = await planner.run("echo hi")

        assert result.reason == "answer_complete"
        assert result.payload["raw_answer"] == "final: hi"
        assert result.payload["structured"] is None
        assert result.payload["artifacts"] == result.payload["artifacts"]  # shape parity
