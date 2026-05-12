# Persistence and Replay (AG-UI + PenguiFlow)

## Table of Contents

- What to persist (PlannerEvents vs AG-UI events)
- ID strategy (threadId/runId) and pause/resume continuity
- Snapshots vs pure event replay
- Dedupe and ordering pitfalls
- Backend replay endpoints (Playground reference)
- Suggested production patterns

## What to persist

You have two viable persistence strategies:

### Option A: Persist PlannerEvents (recommended for PenguiFlow-native observability)

Persist raw `PlannerEvent` objects keyed by `trace_id` (== AG-UI `runId`).

Pros:
- Matches what PenguiFlow produces internally.
- Lets you re-map to multiple wire protocols (legacy SSE, AG-UI, task streams).
- Supports trajectory reconstruction and debugging.

Cons:
- You must re-run the adapter mapping logic when replaying as AG-UI.

Playground reference implementation:
- Persist: `penguiflow/cli/playground_wrapper.py:_EventRecorder.persist`
- Store: `penguiflow/state/in_memory.py:InMemoryStateStore.save_planner_event`
- Replay: `penguiflow/cli/playground.py:GET /events`

### Option B: Persist AG-UI events (recommended for UI-only systems)

Persist the exact AG-UI event stream (including `CUSTOM` events) keyed by `(threadId, runId)`.

Pros:
- Replays directly into the same frontend reducer with zero remapping.
- Easy to snapshot/replay for UX continuity.

Cons:
- You lose some of the planner-native structure unless you store both.
- If backend mapping changes, old logs may not match new UI expectations.

## ID strategy and pause/resume continuity

Hard rules:
- Keep `threadId` stable for a conversation.
- Treat `runId` as your trace key for a single planner run.

Pause/resume decision:
- If you want a single trace for the whole run (recommended), reuse the same `runId` on resume.
- If you intentionally create a new `runId` on resume, store a link:
  - `resumed_from_run_id`
  - `resume_token`
  - `parent_task_id`

Why reuse `runId`:
- You append events under one key in your store.
- Debugging is simpler (one run timeline).
- `/events?trace_id=...&follow=true` becomes a single consistent stream.

Playground caveat:
- The Playground UI currently generates a new run id in `resumeAgui(...)`.
- This is acceptable for a demo, but for production persistence it is usually better to keep one `runId` per paused run.

## Snapshots vs pure event replay

Pure replay is conceptually clean but can be expensive for long sessions.

Recommended hybrid:
- Persist the event log.
- Every N events (or every M seconds), store a snapshot:
  - chat messages + tool calls
  - agent state (if you use AG-UI STATE_SNAPSHOT/DELTA)
  - pending interactions (pause)
  - artifact references
- On load: apply the latest snapshot, then replay events after the snapshot offset.

## Dedupe and ordering pitfalls

Common issues:
- Reconnects can cause replays and duplicated events.
- Multiple sources (replay + live follow) can interleave if not coordinated.

Defensive techniques:
- Add a monotonically increasing `event_seq` on the server and persist it.
- If you cannot add `event_seq`, dedupe by stable identities:
  - Text: `(messageId, deltaIndex)` if you index deltas, or by concatenated length guard
  - Tool calls: `toolCallId`
  - Steps: `(stepName, startedAt)` (or treat as set membership)
  - Artifacts: `artifact_id` and `(stream_id, seq)`

Frontend reducer rule:
- Make the reducer idempotent where possible. If you see the same toolCallId twice, overwrite/merge rather than append duplicates.

## Backend replay endpoints (Playground reference)

The Playground provides:
- `GET /events?trace_id=...&follow=true` (SSE replay + follow)

This replays stored PlannerEvents framed as legacy SSE payloads, not AG-UI.

If you need AG-UI-native replay:
- Add an endpoint like `GET /agui/events?run_id=...` that:
  1. loads stored PlannerEvents
  2. runs the same mapping logic as `PenguiFlowAdapter._convert_planner_event`
  3. encodes as AG-UI SSE via `EventEncoder`

## Suggested production patterns

- Store PlannerEvents and/or AG-UI events in durable storage (DB or log store).
- Keep artifacts in an artifact store and only transmit references/URLs in events.
- Authorize artifact and resource fetches by `(tenant_id, user_id, session_id)` carried in `tool_context`, not by user-supplied URLs alone.
- Add a lightweight "run index" table keyed by `threadId` that lists `runId`s and their terminal state (`finished|paused|error`) for resume UX.

