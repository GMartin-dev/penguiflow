# Frontend Reducer Guide (AG-UI Streams -> Chat UI)

## Table of Contents

- Choose an integration shape (store vs reducer)
- Build RunAgentInput correctly (threadId/runId/messages/forwardedProps)
- Minimal client state model
- Event handling (standard events)
- Event handling (PenguiFlow custom events)
- Message multiplexing and ID mapping
- Pause/resume UX
- Streaming artifacts and UI components (`artifact_type="ui_component"`)
- Testing pointers (Playground UI reference tests)

## Choose an integration shape

You need a single "stream reducer" that is protocol-agnostic and deterministic.

Two proven shapes in this repo:
- Minimal store: `penguiflow/cli/playground_ui/src/lib/stores/features/agui.svelte.ts`
- Full chat stream manager: `penguiflow/cli/playground_ui/src/lib/services/chat-stream.ts`

Guideline:
- If you already have a chat store and message model, copy the `chat-stream.ts` approach: keep a reducer in one place and route AG-UI + legacy SSE into the same state transitions.
- If you are starting from scratch, the minimal AGUI store is a good blueprint.

## Build RunAgentInput correctly

Required:
- `threadId`: stable conversation/session id
- `runId`: stable run/trace id (recommend stable across pause/resume for a single trace)
- `messages`: full message history (at least user messages)
- `forwardedProps.penguiflow.llm_context/tool_context`: preserve context split

Example (TypeScript):

```ts
import { HttpAgent } from '@ag-ui/client';
import type { RunAgentInput, Message } from '@ag-ui/core';

const agent = new HttpAgent({ url: '/agui/agent' });

const input: RunAgentInput = {
  threadId: sessionId,
  runId: runId,
  messages: history as Message[],
  tools: [],
  context: [],
  state: currentAgentState,
  forwardedProps: {
    penguiflow: {
      llm_context: llmContext,
      tool_context: toolContext
    }
  }
};

agent.run(input).subscribe({ next: onEvent, error: onError, complete: onComplete });
```

Reference implementation:
- `chat-stream.ts:startAgui(...)`

## Minimal client state model

Treat the event stream as event-sourced state. You should be able to replay events to reconstruct UI.

Recommended fields:
- `runStatus`: `idle|running|paused|finished|error`
- `threadId`, `runId`
- `messagesById: Map<string, MessageState>`
- `orderedMessageIds: string[]`
- `toolCallsById: Map<string, ToolCallState>`
- `activeSteps: Map<string, StepState>`
- `agentState: Record<string, unknown>` (optional; for AG-UI STATE_SNAPSHOT/DELTA)
- `pendingInteraction: { resumeToken, component, props, ... } | null` (for pause / interactive tools)

## Event handling (standard events)

Implement these invariants:
- Ignore events until you see `RUN_STARTED` (or at least store `runId`/`threadId` from it).
- Do not drop events after `RUN_FINISHED` if you also support replay/follow; use your own stream boundary.

### Text messages

- `TEXT_MESSAGE_START`: create message with `messageId`, set `isStreaming=true`.
- `TEXT_MESSAGE_CONTENT`: append `delta` to the message's text.
- `TEXT_MESSAGE_END`: set `isStreaming=false`.

Important:
- Do not infer message end from provider-level `done` signals; PenguiFlow closes messages via `TEXT_MESSAGE_END` (or auto-close at stream end).

### Tool calls

Maintain a `toolCallsById` map:
- `TOOL_CALL_START`: create tool call, attach to `parentMessageId`
- `TOOL_CALL_ARGS`: append `delta` to the args buffer (often one full JSON blob)
- `TOOL_CALL_END`: mark args streaming complete (tool execution may still be running)
- `TOOL_CALL_RESULT`: store result payload (string)

Reference implementation:
- `agui.svelte.ts` stores tool calls inside each message
- `chat-stream.ts` keeps a map while streaming and writes tool events to an Events panel

### Steps

If you display step progress:
- `STEP_STARTED`: push to `activeSteps`
- `STEP_FINISHED`: remove from `activeSteps`

## Event handling (PenguiFlow custom events)

Custom events come through `EventType.CUSTOM` with fields `{ name, value }`.

### `state_update`

Apply to a task store and (optionally) render:
- task status (`PENDING|RUNNING|PAUSED|COMPLETE|FAILED|CANCELLED`)
- notifications/toasts (`update_type="NOTIFICATION"`)

Reference: `chat-stream.ts` handles notification actions and task updates.

### `artifact_stored`

Store artifact metadata and show a download action:
- `value.artifact.id`
- `value.download_url`

Reference: `chat-stream.ts` converts this to the same model as legacy SSE artifacts.

### `artifact_chunk`

Two common uses:
- Passive UI components: `artifact_type === "ui_component"` (render inline)
- Other structured streams: append chunks to a per-stream accumulator

Reference:
- `chat-stream.ts` routes `ui_component` chunks into `interactionsStore.addArtifactChunk(...)`
- `docs/RFC/Done/RFC_AGUI_COMPONENTS.md` describes `ui_component` payload conventions

### `resource_updated`

Invalidate/refetch MCP resource views:
- Use `read_url` as the fetch target
- Update any resource cache in your UI

### `thinking` and `revision`

Use these to avoid polluting the final answer bubble:
- `thinking.phase === "action"`: show "typing" UI
- Otherwise: append to an observations panel
- `revision`: replace or re-stream the visible answer text

Reference: `chat-stream.ts` shows observations and supports revision streaming.

### `pause`

Treat pause as a terminal UI state for the current stream:
- Capture `resume_token`
- Show an approval/OAuth UI
- Resume by calling `/agui/resume` and streaming events again

Reference: `chat-stream.ts` sets a pending interaction and marks the run completed for callback purposes.

## Message multiplexing and ID mapping

If your chat store uses its own internal message IDs, map AG-UI `messageId` -> UI message ID.

Playground strategy (`chat-stream.ts`):
- Create an assistant placeholder message before streaming.
- Map the first seen AG-UI assistant `messageId` to the placeholder.
- Create new assistant messages if additional message IDs appear.

If you do not need placeholder messages:
- Use `messageId` directly as the UI message ID.

## Pause/resume UX

Recommended UX flow:
1. Receive `CUSTOM pause`: show pause banner + pending interaction UI.
2. Optionally show the accompanying assistant text message (PenguiFlow emits one).
3. When the user completes the action, call `/agui/resume` with:
   - `resume_token`
   - `thread_id` (same as `threadId`)
   - `run_id` (recommend same `runId` for a single trace; choose otherwise intentionally)
   - `result` and `component` if your backend validates rich-output/HITL payloads
   - `tool_context` updates if needed
4. Stream the resume events into the same reducer, appending to the conversation.

Backend reference:
- `penguiflow/cli/playground.py:/agui/resume`
- `penguiflow/agui_adapter/penguiflow.py:PenguiFlowAdapter.resume`

## Testing pointers

Use the Playground UI tests as your gold standard:
- `penguiflow/cli/playground_ui/tests/unit/services/chat-stream-agui.test.ts`
- `penguiflow/cli/playground_ui/tests/unit/agui/stores.test.ts`
- `penguiflow/cli/playground_ui/tests/unit/component_artifacts/interactive-flow.test.ts`

Add tests for:
- message streaming across multiple chunks
- tool call args accumulation + result
- custom pause -> pending interaction -> resume continuation
- `artifact_chunk` with `artifact_type="ui_component"`

