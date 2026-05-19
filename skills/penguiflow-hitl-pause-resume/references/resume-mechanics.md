# Resume Mechanics

## `planner.resume(resume_token, ...)`

```python
result = await planner.resume(
    pause.resume_token,
    user_input="approved",
    tool_context={"session_id": "...", "approvals": {"write_line": True}},
    memory_key=optional_memory_key,
)
```

Behavior:
1. Looks up the pause record (in-memory; falls back to `state_store.load_planner_state(token)`).
2. Replays the planner trajectory up to the pause point.
3. Continues the run with the new `tool_context` (overrides what was captured at pause time).
4. Returns either a final result or another `PlannerPause` if the resume hits a new pause.

`result` is the same shape as `planner.run(...)` returns. Loop until it's not a `PlannerPause`.

## Parameter semantics

### `resume_token: str`
Opaque token from the prior `PlannerPause`. Treat as a **secret** — it's an authorization capability. Anyone with the token can resume the paused run.

### `user_input: Any` (optional)
A free-form value made available in the next planner step. Use cases:
- Approval decision: `"approved"` / `"rejected"`.
- Pasted code: `"123456"` for 2FA.
- Selected option: `"prod"` from an `await_input` list.

The LLM sees `user_input` in its next prompt and incorporates it. For structured decisions, prefer setting `tool_context` flags instead — it's more deterministic than relying on the LLM to parse `user_input`.

### `tool_context: dict` (optional but recommended)
Replaces (not merges) the `tool_context` from the pause record. This is the safest way to communicate decisions back:

```python
# at pause time, tool_context might have been:
# {"session_id": "s1", "approvals": {}}

# at resume, supply the approval:
result = await planner.resume(
    token,
    tool_context={"session_id": "s1", "approvals": {"write_line": True}},
)
```

Why replace, not merge: the pause record may have lost non-serializable values during persistence. Re-passing the full `tool_context` ensures clients and config are present.

**Always include**:
- `session_id` (for dispatch).
- `tenant_id`, `user_id` if applicable.
- The decision flag (`approvals`, `oauth_ready`, `provided_input`, etc.).
- Anything the next tool will need.

### `memory_key: MemoryKey` (optional)
For `MemoryIsolation.require_explicit_key=True`, pass the same `MemoryKey` you used at `run()`. Memory hydrates the same session correctly.

## What survives pause/resume

Survives:
- Planner trajectory (steps taken so far).
- Pause `payload` (your structured app-defined data).
- Pause `reason`.
- `tool_context` from the pause (best-effort, JSON-serializable parts only).

Does **not** survive automatically:
- Local variables inside the paused tool function (gone — the tool raised).
- In-memory clients (DB connections, HTTP clients) — re-supply via `tool_context` or registry.
- Non-JSON `tool_context` values (dropped during serialization to StateStore).

## The replay model

When the planner resumes, it doesn't continue inside the tool function. Instead:

1. The planner re-reads its trajectory.
2. The LLM is prompted with the trajectory + the pause's `user_input`.
3. The LLM picks the next action — usually re-invoking the same tool with updated args.
4. The tool re-runs from the top, re-checks `tool_context`, and this time the precondition is satisfied.

This is why **tools must re-check at the top** rather than expecting "to wake up after the pause."

## OAuth resume

OAuth resumes work the same way mechanically:

```python
# at pause:
# pause.payload = {
#   "pause_type": "oauth",
#   "provider": "github",
#   "auth_url": "https://...",
#   ...
# }

# UI opens auth_url
# user completes OAuth
# callback handler stores token in OAuthManager.token_store under (user_id, "github")

# host app resumes:
result = await planner.resume(
    pause.resume_token,
    user_input="oauth_completed",
    tool_context={
        **original_ctx,
        "user_id": "user-123",           # same key OAuthManager will look up
    },
)
```

On the next tool invocation, `OAuthManager.get_token("user-123", "github")` returns the freshly stored token. The tool gets `Authorization: Bearer <token>` and proceeds.

You don't have to set `oauth_ready=True` — the example flow uses it for clarity, but the real signal is the token being present in the store.

## Multiple sequential pauses

A single run can pause multiple times:

```python
result = await planner.run(...)
while isinstance(result, PlannerPause):
    # render result.payload, wait for action
    result = await planner.resume(result.resume_token, ...)
```

Each `PlannerPause.resume_token` is distinct. Treat them as independent records.

## Concurrent sessions

One planner instance serves many sessions concurrently — that's the point of `session_id` dispatch. As long as each call carries the right `session_id` in `tool_context`, sessions don't interfere.

The pause record is keyed by `resume_token`, not by session. Two pauses in different sessions get different tokens.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `KeyError` on resume | Token not found in memory or state store | Configure durable `StateStore`; check token wasn't already consumed |
| Resume re-invokes a different tool | LLM didn't pick the gated tool | Tighten the prompt; verify the gated tool is in the catalog |
| Tool re-checks `tool_context` but the flag is missing | Resume `tool_context` didn't include the flag | Always re-pass the full `tool_context` on resume |
| Approval set but tool still pauses | The check reads a different key than the resume sets | Use a constant for the key shared between tool and host |
| `user_input` ignored | LLM doesn't see it as decisive | Encode the decision in `tool_context` instead |
| OAuth token in store but tool re-pauses | `user_id` differs between pause and resume | Normalize `user_id` (tenant-scoped, no whitespace) |

## Hard rules

- `resume_token` is a secret. Don't log it, don't expose it to other users.
- One-time use is **not** enforced by the planner. If you need it, delete the token from your `StateStore` on `load_planner_state`.
- A `state_store` failure does not block pause emission — the pause is already in memory. Save/load errors are logged.
- `tool_context` on resume is a full replacement; don't omit keys you still need.
