"""Tests for the pydantic-ai transport provider."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import messages as pai_messages
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.usage import RequestUsage

from penguiflow.llm.errors import (
    LLMAuthError,
    LLMCancelledError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMServerError,
)
from penguiflow.llm.profiles import ModelProfile
from penguiflow.llm.providers.pydantic_ai import PydanticAIProvider
from penguiflow.llm.types import (
    AudioPart,
    CancelToken,
    ImagePart,
    LLMMessage,
    LLMRequest,
    StreamEvent,
    StructuredOutputSpec,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
)

MODULE = "penguiflow.llm.providers.pydantic_ai"


def _provider(model: str = "openai/gpt-4o", **kwargs: Any) -> PydanticAIProvider:
    return PydanticAIProvider(model, **kwargs)


def _pai_response(
    parts: list[Any] | None = None,
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> pai_messages.ModelResponse:
    return pai_messages.ModelResponse(
        parts=parts or [pai_messages.TextPart(content="hello")],
        usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _request(**kwargs: Any) -> LLMRequest:
    defaults: dict[str, Any] = dict(
        model="openai/gpt-4o",
        messages=(LLMMessage(role="user", parts=[TextPart(text="Hello")]),),
    )
    defaults.update(kwargs)
    return LLMRequest(**defaults)


class TestModelResolution:
    def test_openrouter_string(self) -> None:
        assert _provider("openrouter/openai/gpt-oss-20b")._pai_model == "openrouter:openai/gpt-oss-20b"

    def test_openai_prefix_and_bare(self) -> None:
        assert _provider("openai/gpt-4o")._pai_model == "openai:gpt-4o"
        assert _provider("gpt-4o")._pai_model == "openai:gpt-4o"

    def test_anthropic_and_google_strings(self) -> None:
        assert _provider("anthropic/claude-sonnet-4-5")._pai_model == "anthropic:claude-sonnet-4-5"
        assert _provider("claude-sonnet-4-5")._pai_model == "anthropic:claude-sonnet-4-5"
        assert _provider("gemini-2.5-flash")._pai_model == "google-gla:gemini-2.5-flash"

    def test_bedrock_dotted_ids(self) -> None:
        assert _provider("anthropic.claude-3-5-sonnet")._pai_model == "bedrock:anthropic.claude-3-5-sonnet"

    def test_databricks_builds_openai_chat_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABRICKS_API_BASE", "https://workspace.cloud.databricks.com/serving-endpoints")
        monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
        provider = _provider("databricks-claude-opus-4-5")
        from pydantic_ai.models.openai import OpenAIChatModel

        assert isinstance(provider._pai_model, OpenAIChatModel)
        # Pricing/profile lookups must keep the original string.
        assert provider.model == "databricks-claude-opus-4-5"

    def test_databricks_base_url_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.cloud.databricks.com")
        monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
        monkeypatch.delenv("DATABRICKS_API_BASE", raising=False)
        provider = _provider("databricks/databricks-gpt-5-5")
        base_url = str(provider._pai_model.client.base_url)
        assert base_url.rstrip("/").endswith("/serving-endpoints")

    def test_databricks_without_credentials_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("DATABRICKS_API_BASE", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValueError, match="Databricks"):
            _provider("databricks-claude-opus-4-5")

    def test_openai_with_api_key_builds_model_object(self) -> None:
        provider = _provider("gpt-4o", api_key="sk-test")
        from pydantic_ai.models.openai import OpenAIChatModel

        assert isinstance(provider._pai_model, OpenAIChatModel)


class TestMessageMapping:
    def test_roles_and_grouping(self) -> None:
        provider = _provider()
        messages = (
            LLMMessage(role="system", parts=[TextPart(text="be brief")]),
            LLMMessage(role="user", parts=[TextPart(text="hi")]),
            LLMMessage(role="assistant", parts=[TextPart(text="hello")]),
            LLMMessage(role="user", parts=[TextPart(text="and?")]),
        )
        converted = provider._to_pai_messages(messages)
        assert [type(m).__name__ for m in converted] == ["ModelRequest", "ModelResponse", "ModelRequest"]
        first = converted[0]
        assert isinstance(first.parts[0], pai_messages.SystemPromptPart)
        assert isinstance(first.parts[1], pai_messages.UserPromptPart)

    def test_tool_round_trip(self) -> None:
        provider = _provider()
        messages = (
            LLMMessage(role="user", parts=[TextPart(text="multiply 2x3")]),
            LLMMessage(
                role="assistant",
                parts=[ToolCallPart(name="multiply", arguments_json='{"a":2,"b":3}', call_id="c1")],
            ),
            LLMMessage(
                role="tool",
                parts=[ToolResultPart(name="multiply", result_json="6", call_id="c1")],
            ),
        )
        converted = provider._to_pai_messages(messages)
        assert [type(m).__name__ for m in converted] == ["ModelRequest", "ModelResponse", "ModelRequest"]
        tool_call = converted[1].parts[0]
        assert isinstance(tool_call, pai_messages.ToolCallPart)
        assert tool_call.tool_name == "multiply"
        assert tool_call.tool_call_id == "c1"
        tool_return = converted[2].parts[0]
        assert isinstance(tool_return, pai_messages.ToolReturnPart)
        assert tool_return.tool_call_id == "c1"

    def test_image_parts_become_binary_content(self) -> None:
        provider = _provider()
        message = LLMMessage(
            role="user",
            parts=[TextPart(text="what color?"), ImagePart(data=b"\x89PNG", media_type="image/png")],
        )
        content = provider._to_user_content(message)
        assert isinstance(content, list)
        assert content[0] == "what color?"
        assert isinstance(content[1], pai_messages.BinaryContent)
        assert content[1].media_type == "image/png"

    def test_audio_parts_become_binary_content(self) -> None:
        provider = _provider()
        message = LLMMessage(
            role="user",
            parts=[TextPart(text="transcribe"), AudioPart(data=b"RIFF", media_type="audio/wav")],
        )
        content = provider._to_user_content(message)
        assert isinstance(content, list)
        assert content[0] == "transcribe"
        assert isinstance(content[1], pai_messages.BinaryContent)
        assert content[1].media_type == "audio/wav"

    def test_text_only_user_content_is_plain_string(self) -> None:
        provider = _provider()
        message = LLMMessage(role="user", parts=[TextPart(text="hi")])
        assert provider._to_user_content(message) == "hi"


class TestRequestBuilding:
    def test_temperature_respects_profile(self) -> None:
        provider = _provider(profile=ModelProfile(supports_temperature=False))
        settings = provider._build_settings(_request(temperature=0.7))
        assert settings is None or "temperature" not in settings

    def test_temperature_sent_when_supported(self) -> None:
        provider = _provider(profile=ModelProfile(supports_temperature=True))
        settings = provider._build_settings(_request(temperature=0.7))
        assert settings is not None and settings["temperature"] == 0.7

    def test_reasoning_effort_routed_to_extra_body(self) -> None:
        provider = _provider()
        settings = provider._build_settings(_request(extra={"reasoning_effort": "high"}))
        assert settings is not None
        assert settings["extra_body"] == {"reasoning_effort": "high"}

    def test_structured_output_builds_native_parameters(self) -> None:
        provider = _provider()
        spec = StructuredOutputSpec(name="result", json_schema={"type": "object"}, strict=True)
        params = provider._build_parameters(_request(structured_output=spec))
        assert params is not None
        assert params.output_mode == "native"
        assert params.output_object is not None
        assert params.output_object.name == "result"
        assert params.allow_text_output is False

    def test_tools_map_to_function_tools(self) -> None:
        provider = _provider()
        tool = ToolSpec(name="multiply", description="x", json_schema={"type": "object"})
        params = provider._build_parameters(_request(tools=(tool,)))
        assert params is not None
        assert params.function_tools[0].name == "multiply"
        assert params.allow_text_output is True

    def test_plain_request_has_no_parameters(self) -> None:
        provider = _provider()
        assert provider._build_parameters(_request()) is None


class TestComplete:
    @pytest.mark.asyncio
    async def test_complete_maps_response(self) -> None:
        provider = _provider()
        response = _pai_response(
            [
                pai_messages.ThinkingPart(content="let me think"),
                pai_messages.TextPart(content="42"),
                pai_messages.ToolCallPart(tool_name="add", args='{"a":1}', tool_call_id="c9"),
            ]
        )
        with patch(f"{MODULE}.model_request", AsyncMock(return_value=response)) as mock_request:
            result = await provider.complete(_request())

        mock_request.assert_awaited_once()
        assert result.message.text == "42"
        assert result.reasoning_content == "let me think"
        assert result.message.tool_calls[0].name == "add"
        assert result.message.tool_calls[0].call_id == "c9"
        assert result.usage.input_tokens == 10
        assert result.usage.total_tokens == 15

    @pytest.mark.asyncio
    async def test_cancel_before_call(self) -> None:
        provider = _provider()
        token = CancelToken()
        token.cancel()
        with pytest.raises(LLMCancelledError):
            await provider.complete(_request(), cancel=token)

    @pytest.mark.asyncio
    async def test_timeout_maps_to_llm_timeout(self) -> None:
        provider = _provider()

        async def slow(*args: Any, **kwargs: Any) -> Any:
            import asyncio

            await asyncio.sleep(10)

        with patch(f"{MODULE}.model_request", side_effect=slow):
            from penguiflow.llm.errors import LLMTimeoutError

            with pytest.raises(LLMTimeoutError):
                await provider.complete(_request(), timeout_s=0.05)


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, LLMAuthError),
            (429, LLMRateLimitError),
            (400, LLMInvalidRequestError),
            (500, LLMServerError),
        ],
    )
    def test_http_errors(self, status: int, expected: type[LLMError]) -> None:
        provider = _provider()
        exc = ModelHTTPError(status_code=status, model_name="gpt-4o", body={"message": "x"})
        mapped = provider._map_error(exc)
        assert isinstance(mapped, expected)
        assert mapped.provider == "pydantic-ai"
        assert mapped.status_code == status

    def test_unexpected_model_behavior_not_retryable(self) -> None:
        provider = _provider()
        mapped = provider._map_error(UnexpectedModelBehavior("content blocks"))
        assert isinstance(mapped, LLMError)
        assert mapped.retryable is False

    @pytest.mark.asyncio
    async def test_complete_raises_mapped_error(self) -> None:
        provider = _provider()
        exc = ModelHTTPError(status_code=429, model_name="gpt-4o", body=None)
        with patch(f"{MODULE}.model_request", AsyncMock(side_effect=exc)):
            with pytest.raises(LLMRateLimitError):
                await provider.complete(_request())


class _FakeStream:
    """Stands in for pydantic-ai's StreamedResponse."""

    def __init__(self, events: list[Any], final: pai_messages.ModelResponse) -> None:
        self._events = events
        self._final = final

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for event in self._events:
                yield event

        return gen()

    def get(self) -> pai_messages.ModelResponse:
        return self._final


class TestStreaming:
    @pytest.mark.asyncio
    async def test_stream_emits_text_and_reasoning_deltas(self) -> None:
        provider = _provider()
        events = [
            pai_messages.PartStartEvent(index=0, part=pai_messages.ThinkingPart(content="hmm")),
            pai_messages.PartDeltaEvent(index=0, delta=pai_messages.ThinkingPartDelta(content_delta=" more")),
            pai_messages.PartStartEvent(index=1, part=pai_messages.TextPart(content="The ")),
            pai_messages.PartDeltaEvent(index=1, delta=pai_messages.TextPartDelta(content_delta="answer")),
        ]
        final = _pai_response(
            [pai_messages.ThinkingPart(content="hmm more"), pai_messages.TextPart(content="The answer")]
        )

        @asynccontextmanager
        async def fake_stream(*args: Any, **kwargs: Any) -> Any:
            yield _FakeStream(events, final)

        received: list[StreamEvent] = []
        with patch(f"{MODULE}.model_request_stream", fake_stream):
            result = await provider.complete(
                _request(),
                stream=True,
                on_stream_event=received.append,
            )

        text = "".join(e.delta_text for e in received if e.delta_text)
        reasoning = "".join(e.delta_reasoning for e in received if e.delta_reasoning)
        assert text == "The answer"
        assert reasoning == "hmm more"
        assert received[-1].done is True
        assert received[-1].usage is not None
        assert result.message.text == "The answer"
        assert result.reasoning_content == "hmm more"
        assert result.raw_response is None

    @pytest.mark.asyncio
    async def test_stream_cancel_mid_run(self) -> None:
        provider = _provider()
        token = CancelToken()
        events = [
            pai_messages.PartStartEvent(index=0, part=pai_messages.TextPart(content="a")),
            pai_messages.PartDeltaEvent(index=0, delta=pai_messages.TextPartDelta(content_delta="b")),
        ]
        final = _pai_response()

        @asynccontextmanager
        async def fake_stream(*args: Any, **kwargs: Any) -> Any:
            token.cancel()
            yield _FakeStream(events, final)

        with patch(f"{MODULE}.model_request_stream", fake_stream):
            with pytest.raises(LLMCancelledError):
                await provider.complete(_request(), stream=True, on_stream_event=lambda e: None, cancel=token)


class TestTransportSelection:
    def test_explicit_transport_uses_pydantic_ai(self) -> None:
        from penguiflow.llm.protocol import NativeLLMAdapter

        with patch(f"{MODULE}.PydanticAIProvider") as mock_provider_cls:
            mock_provider = MagicMock()
            mock_provider.model = "gpt-4o"
            mock_provider_cls.return_value = mock_provider
            adapter = NativeLLMAdapter("gpt-4o", transport="pydantic-ai")
            assert adapter._provider is mock_provider
            mock_provider_cls.assert_called_once()

    def test_default_transport_is_native(self) -> None:
        from penguiflow.llm.protocol import NativeLLMAdapter

        with patch("penguiflow.llm.protocol.create_provider") as mock_create:
            mock_create.return_value = MagicMock(model="gpt-4o")
            NativeLLMAdapter("gpt-4o")
            mock_create.assert_called_once()

    def test_profile_preferred_transport_is_honored(self) -> None:
        from penguiflow.llm.profiles import register_profile
        from penguiflow.llm.protocol import NativeLLMAdapter

        register_profile("test-pinned-pai-model", ModelProfile(preferred_transport="pydantic-ai"))
        with patch(f"{MODULE}.PydanticAIProvider") as mock_provider_cls:
            mock_provider_cls.return_value = MagicMock(model="test-pinned-pai-model")
            NativeLLMAdapter("test-pinned-pai-model")
            mock_provider_cls.assert_called_once()

    def test_explicit_kwarg_beats_profile(self) -> None:
        from penguiflow.llm.profiles import register_profile
        from penguiflow.llm.protocol import NativeLLMAdapter

        register_profile("test-pinned-native-model", ModelProfile(preferred_transport="native"))
        with patch(f"{MODULE}.PydanticAIProvider") as mock_provider_cls:
            mock_provider_cls.return_value = MagicMock(model="test-pinned-native-model")
            NativeLLMAdapter("test-pinned-native-model", transport="pydantic-ai")
            mock_provider_cls.assert_called_once()

    def test_databricks_reasoning_models_pin_native(self) -> None:
        from penguiflow.llm.profiles import get_profile

        assert get_profile("databricks-claude-opus-4-7").preferred_transport == "native"
        assert get_profile("databricks-claude-opus-4-8").preferred_transport == "native"

    def test_invalid_transport_fails_loudly(self) -> None:
        from penguiflow.llm.protocol import NativeLLMAdapter

        with pytest.raises(ValueError, match="Unknown transport"):
            NativeLLMAdapter("gpt-4o", transport="grpc")

    def test_create_native_adapter_threads_transport(self) -> None:
        from penguiflow.llm.protocol import create_native_adapter

        with patch(f"{MODULE}.PydanticAIProvider") as mock_provider_cls:
            mock_provider_cls.return_value = MagicMock(model="gpt-4o")
            adapter = create_native_adapter("gpt-4o", transport="pydantic-ai")
            assert adapter._provider is mock_provider_cls.return_value


class TestAdapterConformance:
    """The same adapter-level behavior holds on both transports."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", ["native", "pydantic-ai"])
    async def test_complete_round_trip(self, transport: str) -> None:
        from penguiflow.llm.protocol import NativeLLMAdapter
        from penguiflow.llm.types import CompletionResponse, Usage

        completion = CompletionResponse(
            message=LLMMessage(role="assistant", parts=(TextPart(text='{"ok": true}'),)),
            usage=Usage(input_tokens=7, output_tokens=3, total_tokens=10),
        )
        mock_provider = MagicMock()
        mock_provider.model = "gpt-4o"
        mock_provider.provider_name = transport
        mock_provider.complete = AsyncMock(return_value=completion)

        if transport == "native":
            patcher = patch("penguiflow.llm.protocol.create_provider", return_value=mock_provider)
        else:
            patcher = patch(f"{MODULE}.PydanticAIProvider", return_value=mock_provider)

        with patcher:
            adapter = NativeLLMAdapter("gpt-4o", transport=transport)
            content, cost = await adapter.complete(messages=[{"role": "user", "content": "Hello"}])

        assert content == '{"ok": true}'
        assert isinstance(cost, float)
        mock_provider.complete.assert_awaited_once()
