# Testing the AG-UI Contract (Backend + Frontend)

## Table of Contents

- What to test (contract-level invariants)
- Backend unit tests (adapter mapping)
- Backend integration tests (endpoint streaming)
- Frontend unit tests (reducer/state machine)
- Golden-path fixtures (record and replay)

## What to test

Focus on invariants that prevent UX breakage:

- Lifecycle:
  - `RUN_STARTED` comes first
  - `RUN_FINISHED` or `RUN_ERROR` comes last
- Message integrity:
  - `TEXT_MESSAGE_START` precedes `CONTENT` and `END`
  - message closes exactly once
  - tool calls attach to the intended parent message
- Tool call integrity:
  - `TOOL_CALL_START` precedes `ARGS`/`END`/`RESULT`
  - tool args accumulation is correct
  - tool result does not create duplicate tool entries
- Pause/resume:
  - `CUSTOM pause` triggers the expected UI state and exposes `resume_token`
  - resume produces a new stream that appends to the conversation
- Artifacts:
  - `artifact_stored` creates a list item with a usable URL/id
  - `artifact_chunk` ordering is correct per `(stream_id, seq)`

## Backend unit tests (adapter mapping)

Goal:
- Validate that a given `PlannerEvent` maps to a stable AG-UI event(s) with the expected payload.

Recommended scope:
- Unit test `PenguiFlowAdapter._convert_planner_event(...)` for:
  - tool_call_* mapping
  - artifact_stored/resource_updated mapping
  - llm_stream_chunk channel mapping (answer vs thinking vs revision)
- Unit test `AGUIAdapter.with_run_lifecycle(...)` for:
  - auto-closing of open text messages
  - auto-closing of active steps

Pointers:
- Adapter: `penguiflow/agui_adapter/penguiflow.py`
- Base lifecycle: `penguiflow/agui_adapter/base.py`
- PlannerEvent model: `penguiflow/planner/models.py`

Test technique:
- Construct `PlannerEvent(...)` dataclasses directly with representative `extra`.
- Feed into `_convert_planner_event` and assert on:
  - event `type`
  - ids (`message_id`, `tool_call_id`, `parent_message_id`)
  - custom event `name` and `value` fields

## Backend integration tests (endpoint streaming)

Goal:
- Validate the full FastAPI stream:
  - request parsing (`RunAgentInput`)
  - SSE framing via `EventEncoder`
  - correct event order and termination

Recommended tests:
- `POST /agui/agent` returns `text/event-stream` (or negotiated content type)
- stream contains `RUN_STARTED` and `RUN_FINISHED`
- pause path returns `CUSTOM pause` and terminates cleanly

Pointers:
- Endpoint wiring: `penguiflow/cli/playground.py` and `penguiflow/agui_adapter/fastapi.py`

## Frontend unit tests (reducer/state machine)

Goal:
- Validate that your reducer/store turns event sequences into correct UI state.

Use the Playground UI tests as examples:
- `penguiflow/cli/playground_ui/tests/unit/services/chat-stream-agui.test.ts`
- `penguiflow/cli/playground_ui/tests/unit/agui/stores.test.ts`
- `penguiflow/cli/playground_ui/tests/unit/component_artifacts/interactive-flow.test.ts`

Recommended test cases:
- Text streaming builds a single assistant message with concatenated deltas.
- Tool call args append correctly and result attaches to tool call.
- Pause event sets pending interaction and does not mark the run as "complete" for UX purposes.
- `artifact_chunk` with `artifact_type="ui_component"` routes into the component artifact store.

## Golden-path fixtures (record and replay)

For high confidence, record a real run and use it as a fixture:

1. Run the agent once and capture the event stream (PlannerEvents or AG-UI events).
2. Store the captured events as JSON lines (one event per line).
3. In tests:
   - replay the fixture through the reducer
   - assert on final UI state (messages, artifacts, pause state, etc.)

If you persist PlannerEvents:
- Replay through the adapter mapping first, then through the frontend reducer.

If you persist AG-UI events:
- Replay directly through the frontend reducer.

