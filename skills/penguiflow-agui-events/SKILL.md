---
name: penguiflow-agui-events
description: "Implement, extend, and debug PenguiFlow's AG-UI (AGUI) event streaming contract across backend and frontend: FastAPI /agui/agent + /agui/resume endpoints, PenguiFlowAdapter mapping from ReactPlanner PlannerEvent to AG-UI events, custom events (artifact_stored, artifact_chunk, resource_updated, pause, state_update, thinking, revision), and frontend reducer/state machine patterns for smooth streaming UI with persistence/replay."
---

# PenguiFlow AG-UI Events (Frontend/Backend Contract)

## Overview

Use this skill to wire PenguiFlow/ReactPlanner into a frontend via the official AG-UI protocol, with a clear event catalog and reducer patterns for messages, tool calls, artifacts, pause/resume, and persistence.

Hard boundaries vs siblings:
- Planner configuration knobs (LLM adapters, tool discovery, allowlists, runtime skills packs) → [[penguiflow-reactplanner-config]] and [[penguiflow-runtime-skills-pack]].
- Protocol-agnostic streaming layer beneath AG-UI (`StreamChunk`, SSE/WebSocket sinks, `format_sse_event`, `stream_flow`) → [[penguiflow-streaming]].
- Pause/resume mechanics (the contract underneath AG-UI `pause`/`resume` custom events) → [[penguiflow-hitl-pause-resume]].
- Rich-output components (`ui_component` artifacts AG-UI renders) → [[penguiflow-rich-output]].

## Canonical Code Pointers (PenguiFlow repo)

Backend:
- `penguiflow/agui_adapter/base.py` (AGUIAdapter lifecycle helpers)
- `penguiflow/agui_adapter/penguiflow.py` (PlannerEvent -> AG-UI mapping + custom events)
- `penguiflow/agui_adapter/fastapi.py` (EventEncoder + StreamingResponse)
- `penguiflow/cli/playground.py` (FastAPI routes `/agui/agent`, `/agui/resume`)

Planner emission sources:
- `penguiflow/planner/tool_calls.py` (`tool_call_*`)
- `penguiflow/planner/planner_context.py` (`artifact_chunk`, `stream_chunk`)
- `penguiflow/planner/artifact_handling.py` (`artifact_stored`)
- `penguiflow/planner/react.py` (`resource_updated` callback wiring)

Frontend (Playground UI is the reference implementation):
- `penguiflow/cli/playground_ui/src/lib/services/chat-stream.ts` (AG-UI stream reducer + custom event routing)
- `penguiflow/cli/playground_ui/src/lib/stores/features/agui.svelte.ts` (minimal AG-UI store)

Docs:
- `docs/PLAYGROUND_BACKEND_CONTRACTS.md` (endpoint + event catalog)
- `docs/agui/*` (flow diagrams)
- `docs/RFC/Done/RFC_AGUI_INTEGRATION.md` (design notes)
- `docs/RFC/Done/RFC_AGUI_COMPONENTS.md` (`ui_component` artifacts + interactive tools)

## Workflow (do in order)

### 1) Confirm the wire contract
- Use the official SDKs: Python `ag-ui-protocol`, TypeScript `@ag-ui/client` + `@ag-ui/core`.
- Use `threadId` as your conversation/session key and `runId` as your trace/run key.
- Preserve PenguiFlow’s context split by putting `{ llm_context, tool_context }` under `forwardedProps.penguiflow`.

Read: `references/event-contract.md`.

### 2) Backend: emit PlannerEvents and map them to AG-UI
- Ensure the planner run is wired with an event callback (PenguiFlow does this via `AgentWrapper` + `event_consumer`).
- Use/extend `PenguiFlowAdapter` mapping rules; add new mappings as `CUSTOM` events unless they fit a standard AG-UI event.
- Keep custom event payloads small and stable; store large payloads as artifacts and emit URLs.

Read: `references/backend-emission.md`.

### 3) Frontend: implement a single reducer/state machine
- Treat the AG-UI stream as event-sourced state: process events in order and derive UI state.
- Maintain maps keyed by `messageId` and `toolCallId`.
- Do not assume a message ends when a provider emits a `done` marker; use `TEXT_MESSAGE_END` (and/or `RUN_FINISHED`) as the close signal.

Read: `references/frontend-reducer.md`.

### 4) Pause/resume and interactive tools
- Treat `CUSTOM name="pause"` as terminal for the current stream; you will still see `RUN_FINISHED`.
- Capture `resume_token` and resume via `/agui/resume` (or your service equivalent).
- Prefer the tool-call pattern (`ui_form`, `ui_confirm`, `ui_select_option`) over bespoke pause payloads when you need structured user input.

Read: `references/frontend-reducer.md` and `references/backend-emission.md`.

### 5) Persistence and replay
- Persist either (A) raw `PlannerEvent` and remap to AG-UI on replay, or (B) raw AG-UI events as your UI event log.
- Prefer stable `runId` across pause/resume if you want a single trace for the whole run.
- Snapshot occasionally (messages + agent state) to avoid O(n) replays on every load.

Read: `references/persistence-replay.md`.

## Extending Safely (checklist)
- Add planner/tool emission with stable `extra` keys.
- Map it in `PenguiFlowAdapter._convert_planner_event`.
- Route it in the frontend reducer (standard event vs `CUSTOM name=...`).
- Add at least one backend test for event order + payload shape and one frontend unit test for reducer behavior.

Read: `references/testing.md`.

## References (read only as needed)
- `references/event-contract.md`
- `references/backend-emission.md`
- `references/frontend-reducer.md`
- `references/persistence-replay.md`
- `references/testing.md`
