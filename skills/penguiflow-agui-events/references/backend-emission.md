# Backend Emission Guide (ReactPlanner -> PlannerEvent -> AG-UI)

## Table of Contents

- Minimal endpoint wiring (FastAPI)
- Context split (llm_context vs tool_context) via forwardedProps
- PlannerEvent emission sources (what emits what)
- PenguiFlowAdapter mapping rules (what becomes which AG-UI event)
- Adding new events safely (backend checklist)
- Persistence/replay: store PlannerEvents, trajectories, artifacts
- Common gotchas (reserved keys, message lifecycles)

## Minimal endpoint wiring (FastAPI)

Use the official encoder from `ag-ui-protocol` and stream events as SSE.

Reference implementation:
- `penguiflow/agui_adapter/fastapi.py:create_agui_endpoint`
- `penguiflow/cli/playground.py` routes: `POST /agui/agent`, `POST /agui/resume`

Pattern:

```python
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from ag_ui.core import RunAgentInput

from penguiflow.agui_adapter import PenguiFlowAdapter, create_agui_endpoint

app = FastAPI()

adapter = PenguiFlowAdapter(agent_wrapper, session_manager=session_manager)

@app.post("/agui/agent")
async def agui_agent(input: RunAgentInput, request: Request) -> StreamingResponse:
    return await create_agui_endpoint(adapter.run)(input, request)
```

Notes:
- If you accept raw JSON dicts (for backwards compatibility), explicitly parse `RunAgentInput(**payload)` so snake_case aliases work.
- Preserve the `Accept` header and pass it to `EventEncoder` so the client can negotiate framing/content-type.

## Context split via forwardedProps

AG-UI does not define `llm_context` or `tool_context`. Preserve the split by putting them under:

`forwardedProps.penguiflow = { llm_context: {...}, tool_context: {...} }`

Reference implementation:
- Adapter reads it in `penguiflow/agui_adapter/penguiflow.py:_extract_forwarded_contexts`
- Contract described in `docs/PLAYGROUND_BACKEND_CONTRACTS.md` and `docs/agui/flow-context-mapping.md`

Hard rule:
- Never mix runtime-only identifiers and handles into `llm_context`. Keep them in `tool_context`.

## PlannerEvent emission sources

PenguiFlow’s UI contract starts at `PlannerEvent`. The ReactPlanner emits events via its internal `_emit_event(...)` callback, which the wrapper forwards to the adapter (`event_consumer`) and persists to a state store.

Source of truth for the PlannerEvent type:
- `penguiflow/planner/models.py:PlannerEvent`

Key emission sites:

### Step boundaries

- `event_type="step_start"` and `event_type="step_complete"`
- Emitted by the planner runtime around node/tool execution
- Typically carries:
  - `node_name` and/or `extra.step_name`
  - `latency_ms`, `token_estimate`

### LLM token streaming

- `event_type="llm_stream_chunk"`
- Emitted by `ReactPlanner._enqueue_llm_stream_chunk(...)` in `penguiflow/planner/react.py`
- `extra` commonly includes:
  - `channel`: `answer|thinking|revision`
  - `phase`: `action|answer|revision|...` (UI uses this for typing/placement)
  - `text`: delta
  - `done`: bool (provider "stream done" marker)

### Tool calls

- `event_type="tool_call_start"`, `"tool_call_end"`, `"tool_call_result"`
- Emitted by `penguiflow/planner/tool_calls.py:execute_tool_call`
- `extra` keys you can rely on:
  - `tool_call_id`
  - `tool_name`
  - `args_json` (JSON string) on `tool_call_start`
  - `result_json` (JSON string) on `tool_call_result`
  - `action_seq` (ordering within a step)

Semantics:
- `tool_call_end` means "argument streaming ended" (not "tool finished"). Result arrives in `tool_call_result`.

### Streaming artifacts (structured)

- `event_type="artifact_chunk"`
- Emitted by `ToolContext.emit_artifact(...)` via `penguiflow/planner/planner_context.py`
- `extra` keys:
  - `stream_id` (grouping)
  - `seq` (monotonic per stream)
  - `chunk` (JSON-serializable)
  - `artifact_type` (e.g. `"ui_component"`)
  - `done` (bool)
  - `meta` (dict)

### Stored artifacts (downloadable)

- `event_type="artifact_stored"`
- Emitted by `_EventEmittingArtifactStoreProxy` in `penguiflow/planner/artifact_handling.py`
- `extra` keys:
  - `artifact_id`
  - `mime_type`
  - `size_bytes`
  - `artifact_filename` (deliberately not `filename` to avoid Python LogRecord collisions)
  - `source` (dict, typically `{"namespace": ...}`)

### MCP resource invalidation

- `event_type="resource_updated"`
- Emitted via callback wiring in `penguiflow/planner/react.py:ReactPlanner._register_resource_callbacks`
- `extra` keys:
  - `namespace`
  - `uri`

## PenguiFlowAdapter mapping rules

Source of truth:
- `penguiflow/agui_adapter/penguiflow.py:PenguiFlowAdapter._convert_planner_event`

Key mapping behaviors that downstream code must preserve:

- `llm_stream_chunk channel=answer` -> `TEXT_MESSAGE_*`
  - Do not emit `TEXT_MESSAGE_END` on provider `done` markers.
  - Let `AGUIAdapter.with_run_lifecycle(...)` close the message at end of stream so multi-LLM-call runs stay in one assistant bubble.

- `llm_stream_chunk channel=thinking` -> `CUSTOM name="thinking"`
- `llm_stream_chunk channel=revision` -> `CUSTOM name="revision"`

- `tool_call_start` -> `TOOL_CALL_START` and (usually) a single `TOOL_CALL_ARGS` containing the full JSON args string.
- `tool_call_end` -> `TOOL_CALL_END`
- `tool_call_result` -> `TOOL_CALL_RESULT`

- `artifact_chunk` -> `CUSTOM name="artifact_chunk"` (payload is 1:1 with planner payload)
- `artifact_stored` -> `CUSTOM name="artifact_stored"` (adds `download_url`)
- `resource_updated` -> `CUSTOM name="resource_updated"` (adds `read_url`)

- Every PlannerEvent also projects into `CUSTOM name="state_update"` via `PlannerEventProjector`
  - This is how the frontend gets task/progress/tool-call state without building another channel.

## Adding new events safely (backend checklist)

Do this in order:

1. Emit a new `PlannerEvent` with stable `event_type` and stable `extra` keys.
2. Persist it (Playground does this automatically via wrapper `EventRecorder.persist`).
3. Decide whether it maps to:
   - a standard AG-UI event type, or
   - a `CUSTOM` event (preferred for app-specific extensions).
4. Extend `PenguiFlowAdapter._convert_planner_event` to map it.
5. Update the frontend reducer to handle it.
6. Add at least one test.

Prefer `CUSTOM` for:
- progress channels that should not concatenate into the answer
- UI-specific render payloads
- task/projection updates

## Persistence/replay (PlannerEvents, trajectories, artifacts)

Playground reference implementation:
- PlannerEvents stored by `penguiflow/cli/playground_wrapper.py:_EventRecorder.persist`
- State store: `penguiflow/state/in_memory.py:InMemoryStateStore.save_planner_event`
- Replay endpoint: `GET /events` in `penguiflow/cli/playground.py` (SSE replay + follow)

Recommended persistence strategy for production:
- Persist PlannerEvents keyed by `trace_id` (== `runId`) to enable replay.
- Persist trajectories if you need reconstructable step-by-step audit and/or resume UX.
- Persist artifacts in an artifact store keyed by (session_id, artifact_id) and authorize fetches by session/tenant.

If you want AG-UI-native replay:
- Either store AG-UI events directly, or
- Re-map stored PlannerEvents into AG-UI events on the fly (same logic as `PenguiFlowAdapter`).

## Common gotchas

### Reserved keys in planner event `extra`

`PlannerEvent.to_payload()` filters out keys that collide with Python logging `LogRecord` fields.
Avoid using keys like `filename`, `msg`, `args`, etc. in `extra` (use `artifact_filename`).

### Message boundaries

If you close text messages too early, you will get UX regressions:
- tool calls attach to the wrong message
- multi-tool runs render as fragmented assistant bubbles

Preserve the invariant:
- start message once, stream all answer deltas, close once at end of run (or at explicit message end points if your runtime multiplexes).

