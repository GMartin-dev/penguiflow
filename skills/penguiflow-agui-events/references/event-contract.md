# Event Contract (AG-UI + PenguiFlow Custom Events)

## Table of Contents

- Terminology and IDs
- RunAgentInput contract (request)
- Standard AG-UI events used by PenguiFlow
- PenguiFlow custom events (EventType.CUSTOM)
- PlannerEvent -> AG-UI mapping (source of truth)
- Ordering invariants and UI state machine notes
- Canonical end-to-end sequences

## Terminology and IDs

- `threadId`: Conversation/session identifier. In PenguiFlow this maps to `session_id`.
- `runId`: Single planner run identifier. In PenguiFlow this maps to `trace_id` (and is the key used for event persistence/replay in the Playground state store).
- `messageId`: Identity of a single streamed assistant message.
- `toolCallId`: Identity of a single tool call lifecycle.

Recommended identity strategy:
- Keep `threadId` stable for the entire conversation.
- Keep `runId` stable for the entire run, including pause/resume, if you want a single trace. If you generate a new `runId` on resume, ensure you link it to the previous run via your own metadata.

## RunAgentInput contract (request)

AG-UI uses `RunAgentInput`. PenguiFlow consumes it like this (Playground backend):
- `threadId` -> `session_id`
- `runId` -> `trace_id`
- `forwardedProps.penguiflow.llm_context` -> passed to `planner.run(..., llm_context=...)`
- `forwardedProps.penguiflow.tool_context` -> passed to `planner.run(..., tool_context=...)`

Reference implementation:
- Backend: `penguiflow/agui_adapter/penguiflow.py:_extract_forwarded_contexts`
- Docs: `docs/PLAYGROUND_BACKEND_CONTRACTS.md` (`POST /agui/agent`)

Minimal request shape:

```json
{
  "threadId": "session-id",
  "runId": "trace-id",
  "messages": [{ "id": "m1", "role": "user", "content": "Hello" }],
  "tools": [],
  "state": {},
  "forwardedProps": {
    "penguiflow": {
      "llm_context": {},
      "tool_context": {}
    }
  }
}
```

Notes:
- The Python SDK accepts snake_case aliases (`thread_id`, `run_id`, `forwarded_props`) if you are not using the official TS client.
- The Playground currently sends `tools: []` and relies on PenguiFlow’s server-side tool catalog, not the frontend-supplied tool list.

## Standard AG-UI events used by PenguiFlow

PenguiFlow’s adapter emits the standard AG-UI lifecycle and rendering events:
- `RUN_STARTED`, `RUN_FINISHED`, `RUN_ERROR`
- `STEP_STARTED`, `STEP_FINISHED`
- `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT`, `TEXT_MESSAGE_END`
- `TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END`, `TOOL_CALL_RESULT`

Optional AG-UI events:
- `STATE_SNAPSHOT`, `STATE_DELTA` (emitted by `AGUIAdapter.with_run_lifecycle(...)` only when you supply `initial_state`; deltas are available via helpers but not emitted by default in PenguiFlowAdapter today)
- `MESSAGES_SNAPSHOT` (frontend support exists; PenguiFlowAdapter does not emit it by default)

## PenguiFlow custom events (EventType.CUSTOM)

Custom events are the primary extension mechanism for:
- artifacts and resources
- pause/resume payloads
- task state updates
- non-final streaming channels (thinking, revision)

Reference implementation:
- Backend emission: `penguiflow/agui_adapter/penguiflow.py`
- Frontend consumption: `penguiflow/cli/playground_ui/src/lib/services/chat-stream.ts` and `.../stores/features/agui.svelte.ts`

### `CUSTOM name="state_update"`

Purpose:
- Transport task-addressable state updates in the same stream as chat rendering (status/progress/tool/results/notifications).

Source of truth:
- Backend: `penguiflow/agui_adapter/penguiflow.py:_make_status_update` and `_convert_planner_event` (via `PlannerEventProjector`)
- Model: `penguiflow/state/models.py:StateUpdate`

Shape:

```json
{
  "type": "CUSTOM",
  "name": "state_update",
  "value": {
    "session_id": "sess-1",
    "task_id": "turn-or-task-id",
    "trace_id": "trace-1",
    "update_id": "uuid-hex",
    "update_type": "STATUS_CHANGE|PROGRESS|THINKING|TOOL_CALL|RESULT|ERROR|CHECKPOINT|NOTIFICATION",
    "content": {},
    "step_index": 3,
    "total_steps": null,
    "created_at": "2026-01-01T00:00:00Z"
  }
}
```

Frontend handling:
- Apply to a task store (`tasksStore.applyUpdate(...)` in Playground UI).
- Treat `update_type="NOTIFICATION"` specially if you show toast/alerts.

### `CUSTOM name="artifact_stored"`

Purpose:
- Notify the UI that a downloadable (binary/large-text) artifact was persisted and can be fetched.

Source of truth:
- Planner emission: `penguiflow/planner/artifact_handling.py:_EventEmittingArtifactStoreProxy`
- Adapter mapping: `penguiflow/agui_adapter/penguiflow.py:_artifact_custom_event`

Shape:

```json
{
  "type": "CUSTOM",
  "name": "artifact_stored",
  "value": {
    "artifact": {
      "id": "artifact-id",
      "mime_type": "application/pdf",
      "size_bytes": 1234,
      "filename": "report.pdf",
      "source": { "namespace": "mcp-or-tool" }
    },
    "download_url": "/artifacts/artifact-id"
  }
}
```

Frontend handling:
- Store metadata immediately for UI listing.
- Fetch actual bytes lazily via `download_url` (or your own gateway).

### `CUSTOM name="artifact_chunk"`

Purpose:
- Stream structured artifacts (including non-interactive UI components) incrementally.

Source of truth:
- Planner emission: `penguiflow/planner/planner_context.py:_PlannerContext.emit_artifact`
- Adapter mapping: `penguiflow/agui_adapter/penguiflow.py:_artifact_chunk_custom_event`

Shape:

```json
{
  "type": "CUSTOM",
  "name": "artifact_chunk",
  "value": {
    "stream_id": "ui|artifact|...",
    "seq": 0,
    "done": false,
    "artifact_type": "ui_component|<custom>",
    "chunk": { "id": "stable-id", "component": "report", "props": {} },
    "meta": {}
  }
}
```

Notes:
- Use `artifact_type="ui_component"` for passive UI artifacts. Keep payloads small; store big data as `artifact_stored` and reference by URL.
- `seq` is monotonic per `stream_id` and is assigned by the planner context.

### `CUSTOM name="resource_updated"`

Purpose:
- Notify the UI that an MCP resource cache was invalidated/updated and should be re-fetched.

Source of truth:
- Planner emission wiring: `penguiflow/planner/react.py:ReactPlanner._register_resource_callbacks`
- Adapter mapping: `penguiflow/agui_adapter/penguiflow.py:_resource_custom_event`

Shape:

```json
{
  "type": "CUSTOM",
  "name": "resource_updated",
  "value": {
    "namespace": "mcp-server-name",
    "uri": "resource://...",
    "read_url": "/resources/mcp-server-name/resource%3A%2F%2F..."
  }
}
```

### `CUSTOM name="pause"`

Purpose:
- Carry pause payloads for HITL / OAuth / approvals and indicate that the current stream is terminal until resumed.

Source of truth:
- Planner pause model: `penguiflow/planner/models.py:PlannerPause`
- Adapter emission: `penguiflow/agui_adapter/penguiflow.py` (pause branch)

Shape:

```json
{
  "type": "CUSTOM",
  "name": "pause",
  "value": {
    "reason": "await_input|approval_required|oauth|...",
    "payload": { "auth_url": "https://...", "provider": "github" },
    "resume_token": "opaque-token"
  }
}
```

Notes:
- In PenguiFlow, a pause also emits a human-friendly assistant text message describing the pause.
- You will still receive `RUN_FINISHED` after a pause because the stream ends; treat that as transport completion, not task completion.

### `CUSTOM name="thinking"` and `CUSTOM name="revision"`

Purpose:
- Provide extra channels from `llm_stream_chunk` that the adapter does not merge into the final answer bubble.

Source of truth:
- Adapter mapping: `penguiflow/agui_adapter/penguiflow.py` (`llm_stream_chunk` handling)

Shapes:

```json
{ "type": "CUSTOM", "name": "thinking", "value": { "text": "...", "phase": "action|...", "done": false } }
{ "type": "CUSTOM", "name": "revision", "value": { "text": "...", "done": false } }
```

Notes:
- These events may be omitted when the upstream chunk has empty `text`. Do not rely on receiving a final `done=true` custom event; use `RUN_FINISHED` as the terminal signal.

## PlannerEvent -> AG-UI mapping (source of truth)

Treat these files as the definitive mapping:
- Backend adapter: `penguiflow/agui_adapter/penguiflow.py:PenguiFlowAdapter._convert_planner_event`
- Planner event model: `penguiflow/planner/models.py:PlannerEvent`

High-level mapping:
- `PlannerEvent(event_type="step_start")` -> `STEP_STARTED` (plus `CUSTOM state_update`)
- `PlannerEvent(event_type="step_complete")` -> `STEP_FINISHED` (plus `CUSTOM state_update`)
- `PlannerEvent(event_type="llm_stream_chunk", extra.channel="answer")` -> `TEXT_MESSAGE_*`
- `PlannerEvent(event_type="llm_stream_chunk", extra.channel="thinking")` -> `CUSTOM thinking`
- `PlannerEvent(event_type="llm_stream_chunk", extra.channel="revision")` -> `CUSTOM revision`
- `PlannerEvent(event_type="tool_call_start")` -> `TOOL_CALL_START` + `TOOL_CALL_ARGS`
- `PlannerEvent(event_type="tool_call_end")` -> `TOOL_CALL_END`
- `PlannerEvent(event_type="tool_call_result")` -> `TOOL_CALL_RESULT`
- `PlannerEvent(event_type="artifact_chunk")` -> `CUSTOM artifact_chunk`
- `PlannerEvent(event_type="artifact_stored")` -> `CUSTOM artifact_stored`
- `PlannerEvent(event_type="resource_updated")` -> `CUSTOM resource_updated`

Important: `PlannerEvent(event_type="stream_chunk")` (tool/progress chunks from `ToolContext.emit_chunk`) is not mapped to AG-UI by default in `PenguiFlowAdapter` today. If you need those in AG-UI, add an explicit mapping and a frontend handler.

## Ordering invariants and UI state machine notes

Lifecycle:
- `RUN_STARTED` is always first.
- `RUN_FINISHED` or `RUN_ERROR` is always last.

Messages:
- `TEXT_MESSAGE_START` must precede `TEXT_MESSAGE_CONTENT` and `TEXT_MESSAGE_END` for a given `messageId`.
- PenguiFlow keeps the assistant message open across multiple internal LLM calls; do not close the message on provider-level `done` markers. Close on `TEXT_MESSAGE_END` (or on `RUN_FINISHED` if you want a defensive fallback).

Tool calls:
- `TOOL_CALL_END` indicates "argument streaming is done", not "tool execution is complete".
- Expect `TOOL_CALL_RESULT` after `TOOL_CALL_END` (possibly much later, especially if you turn tools into background tasks).

Pause:
- A pause is terminal for the stream. Expect `CUSTOM pause` then `RUN_FINISHED`.
- Keep the UI in a "paused" state until the user resumes with `resume_token`.

Artifacts/resources:
- Treat `artifact_stored.download_url` and `resource_updated.read_url` as fetch targets.
- Treat `artifact_chunk` as a render payload stream; use `stream_id` + `seq` for ordering.

## Canonical end-to-end sequences

### Normal run (answer streaming)

1. `RUN_STARTED`
2. `CUSTOM state_update` (PENDING)
3. `CUSTOM state_update` (RUNNING)
4. `STEP_STARTED` / `STEP_FINISHED` (optional)
5. `TEXT_MESSAGE_START`
6. `TEXT_MESSAGE_CONTENT` (repeat)
7. `TEXT_MESSAGE_END` (or auto-closed at end)
8. `CUSTOM state_update` (COMPLETE)
9. `RUN_FINISHED`

### Tool call + result

1. `TOOL_CALL_START` (has `parentMessageId`)
2. `TOOL_CALL_ARGS` (often one full JSON blob from PenguiFlow)
3. `TOOL_CALL_END`
4. `TOOL_CALL_RESULT`

### Pause and resume

Pause stream:
1. `CUSTOM pause` (contains `resume_token`)
2. (Often) `TEXT_MESSAGE_*` describing the pause
3. `RUN_FINISHED`

Resume stream (via `/agui/resume`):
1. `RUN_STARTED`
2. `CUSTOM state_update` (RUNNING, `resumed=true`)
3. Continue as normal
4. `RUN_FINISHED`

