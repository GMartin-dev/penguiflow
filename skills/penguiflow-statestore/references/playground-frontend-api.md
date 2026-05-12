# Playground frontend API (HTTP + SSE)

This document describes the **Playground backend contract** implemented by `penguiflow/cli/playground.py`. It is intended for frontend/UI teams building against PenguiFlow’s dev server (or reusing the contract in production).

> Source of truth: `penguiflow/cli/playground.py`, `penguiflow/cli/playground_sse.py`, `penguiflow/sessions/session.py`, `penguiflow/sessions/projections.py`.

## Table of contents

1. [Capabilities you must provide](#capabilities-you-must-provide)
2. [HTTP endpoints](#http-endpoints)
3. [SSE event types](#sse-event-types)
4. [Data models the UI should expect](#data-models-the-ui-should-expect)
5. [Reference UI flows](#reference-ui-flows)
6. [Common pitfalls](#common-pitfalls)

---

## Capabilities you must provide

The Playground server always runs with a `SessionManager` (task orchestration), but some endpoints require a configured StateStore or ArtifactStore.

### For live chat + task updates (UI “works”, but may not be durable)

- `SessionManager` + `StreamingSession` (in-memory by default)
- `/chat` and `/chat/stream` do **not** require a durable `StateStore`
- `/session/stream`, `/tasks*`, `/steer` do **not** require a durable `StateStore`, but persistence across restarts does

### For replay / history views (requires StateStore optional capabilities)

- `/events` requires `store.list_planner_events(trace_id)` (StateStore capability: **SupportsPlannerEvents**)
- `/trajectory/{trace_id}` requires `store.get_trajectory(trace_id, session_id)` (StateStore capability: **SupportsTrajectories**)

If the playground `store` is not configured, these endpoints return HTTP 500.

### For artifact downloads (requires ArtifactStore)

The artifact endpoints use the running planner’s artifact store (not the StateStore directly):

- Planner must expose an `ArtifactStore` (and it must **not** be `NoOpArtifactStore`).
- `/artifacts/{artifact_id}` optionally enforces session isolation if the ArtifactStore supports `get_with_session_check()` (Playground’s default store does).

See also: `references/artifacts-and-resources.md`.

---

## HTTP endpoints

### `GET /health`

Returns `{"status":"ok"}`.

### `POST /chat` (non-streaming)

Request model (`ChatRequest`):

- `query: str` (required)
- `session_id: str | null` (optional; server generates if missing)
- `llm_context: object` (optional)
- `tool_context: object` (optional)
- `context: object | null` (deprecated alias of `llm_context`)

Response model (`ChatResponse`):

- `trace_id: str`
- `session_id: str`
- `answer: str | null`
- `metadata: object | null`
- `pause: object | null` (present on HITL/OAuth pause)

### `GET /chat/stream` (streaming)

Query params:

- `query: str` (required)
- `session_id: str` (optional)
- `llm_context: str` (optional JSON string)
- `tool_context: str` (optional JSON string)
- `context: str` (optional JSON string; deprecated alias merged into `llm_context`)

Returns `text/event-stream` containing a mix of:

- planner event frames (e.g., `chunk`, `tool_call_*`, `llm_stream_chunk`, `artifact_*`, `event`, …)
- `state_update` frames (task-addressable updates)
- a final `done` frame
- `error` on failures

### `GET /session/stream` (session task updates)

Query params:

- `session_id: str` (required)
- `since_id: str` (optional; exclusive cursor)
- `task_ids: list[str]` (optional; repeatable query param)
- `update_types: list[UpdateType]` (optional; repeatable)

Returns `text/event-stream` with only:

- `state_update` frames

The first frame is a `state_update` with `{"event":"connected","session_id":...}`.

### `GET /sessions/{session_id}`

Returns `SessionInfo`:

- `session_id`
- `task_count`
- `active_tasks`
- `pending_patches`
- `context_version`
- `context_hash`

### `PATCH /sessions/{session_id}/context`

Body (`SessionContextUpdate`):

- `llm_context: object | null`
- `tool_context: object | null`
- `merge: bool` (default `False`)

Returns `{"ok": true, "context_version": <int>}`.

### `POST /sessions/{session_id}/apply-context-patch`

Body:

- `patch_id: str`
- `strategy: MergeStrategy | null`
- `action: "apply" | "reject"`

Returns `{"ok": true, "action": "applied"|"rejected"}` or 404 if patch not found.

### `POST /steer`

Body (`SteerRequest`):

- `session_id: str`
- `task_id: str`
- `event_type: SteeringEventType`
- `payload: object` (default `{}`)
- `trace_id: str | null`
- `source: str` (default `"user"`)
- `event_id: str | null` (server generates if missing)

Returns `{"accepted": true|false}`.

### `GET /tasks`

Query params:

- `session_id: str` (required)
- `status: TaskStatus | null` (optional)

Returns a list of `TaskStateModel` (task snapshots).

### `GET /tasks/{task_id}`

Query params:

- `session_id: str` (required)

Returns `TaskStateModel` or 404.

### `DELETE /tasks/{task_id}`

Query params:

- `session_id: str` (required)

Cancels the task via steering. Returns `{"ok": true, "task_id": ...}` or 404.

### `POST /tasks` (spawn a task)

Body (`TaskSpawnRequest`):

- `session_id: str`
- `query: str | null`
- `task_type: "foreground"|"background"` (default `"background"`)
- `priority: int` (default `0`)
- `llm_context: object`
- `tool_context: object`
- `spawn_reason: str | null`
- `description: str | null`
- `wait: bool` (default `False`) — if `True`, blocks until completion
- `merge_strategy: MergeStrategy | null`
- `parent_task_id: str | null`
- `spawned_from_event_id: str | null`

Returns `TaskSpawnResponse` with `status` and optional `result`.

> Note: background tasks require a configured `planner_factory` (else 501).

### `GET /events` (replay planner events; optionally follow)

Query params:

- `trace_id: str` (required)
- `session_id: str | null` (optional; if provided, server checks that `trace_id` belongs to session via `get_trajectory`)
- `follow: bool` (default `False`; when true, server subscribes to in-memory broker for live tail)

Returns `text/event-stream` with:

- initial `"event"` frame announcing `{"event":"connected", ...}`
- replay of stored planner events (from `store.list_planner_events(trace_id)`)
- if `follow=true`, live frames published during execution

### `GET /trajectory/{trace_id}`

Query params:

- `session_id: str` (required)

Returns `Trajectory.serialise()` payload with `trace_id` and `session_id` injected.

### `GET /artifacts/{artifact_id}` (download)

Query params / headers:

- `session_id: str | null` query param, or
- `X-Session-ID: str | null` header

If a session is provided, the server enforces session isolation if supported.

Returns bytes with `Content-Disposition` and `Content-Type`.

### `GET /artifacts/{artifact_id}/meta`

Same session scoping rules as download; returns `ArtifactRef.model_dump()`.

---

## SSE event types

SSE frames are encoded as:

```
event: <name>
data: <json>
```

### Task updates: `state_update`

Produced by the session broker and by projecting planner events.

Payload is `StateUpdate.model_dump(mode="json")`.

### Chat completion: `done`

Payload:

- `trace_id`, `session_id`
- `answer: str | null`
- `metadata: object | null`
- `pause: object | null`
- `answer_action_seq: int | null`

### Errors: `error`

Payload:

- `error: str`
- optional `trace_id`, `session_id`

### Planner / execution events

These come from `_event_frame(PlannerEvent, ...)` and are meant for richer UIs:

- `chunk` (planner `"stream_chunk"`)
- `llm_stream_chunk`
- `step` (for `step_start`, `step_complete`)
- `tool_call_start`, `tool_call_args`, `tool_call_end`
- `artifact_chunk`, `artifact_stored`
- `resource_updated`
- generic `event` (fallback for all other PlannerEvent types)

Important details:

- Payloads always include `trace_id`, `session_id`, `ts`, and `step`.
- Some frames include `message_id` to correlate chunks to a UI message.

---

## Data models the UI should expect

### `StateUpdate`

Key fields:

- `session_id`, `task_id`, `trace_id`
- `update_id` (cursor for `since_id`)
- `update_type` ∈ `THINKING|PROGRESS|TOOL_CALL|RESULT|ERROR|CHECKPOINT|STATUS_CHANGE|NOTIFICATION`
- `content` (polymorphic JSON)
- `step_index`, `total_steps` (optional)
- `created_at`

### `SteeringEvent`

Key fields:

- `event_id` (cursor for steering streams if you implement one)
- `event_type` (CANCEL/PAUSE/RESUME/PRIORITIZE/USER_MESSAGE/etc.)
- `payload` (sanitized + size bounded)

### `TaskStateModel`

Key fields:

- `task_id`, `session_id`, `status`, `task_type`, `priority`
- `context_snapshot` (may include context hash/version)
- `result` / `error` (optional)

### `ArtifactRef`

Key fields:

- `id`, `mime_type`, `size_bytes`, `filename`, `sha256`
- `scope` (tenant/user/session/trace metadata for host enforcement)
- `source` (tool metadata, warnings, previews, etc.)

---

## Reference UI flows

### Foreground chat (streaming-first)

1. Call `GET /chat/stream?query=...&session_id=...`.
2. Render incremental:
   - `llm_stream_chunk` / `chunk` into the transcript
   - `tool_call_*` and `state_update` into side panels
3. On `done`, finalise the assistant message and store `trace_id`.
4. Optionally open `/events?trace_id=...&follow=true` in a separate panel for richer replay.

### Background tasks

1. `POST /tasks` with `task_type="background"` and a merge strategy.
2. Subscribe to `GET /session/stream?session_id=...` and filter by `task_id`.
3. Use `state_update` + `TaskStateModel` to render progress, terminal state, and context patches.

### Artifacts

1. Watch for `artifact_stored` (planner event) or `StateUpdate(update_type=RESULT, content={artifact_id,...})`.
2. Fetch metadata via `GET /artifacts/{id}/meta` (include `session_id` for access control).
3. Download bytes via `GET /artifacts/{id}`.

---

## Common pitfalls

- **No replay available**: store is not configured with `SupportsPlannerEvents`/`SupportsTrajectories` → `/events` or `/trajectory` returns 500.
- **Artifacts 404**: TTL/eviction expired, or planner is using `NoOpArtifactStore`.
- **Cross-session artifact leak**: you didn’t provide session_id or your host endpoint doesn’t enforce `ArtifactScope`.
- **UI shows out-of-order updates**: backend cursor semantics broken (ensure stable ordering + exclusive `since_id`).
