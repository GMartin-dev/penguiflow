# OAuth via HITL Pause/Resume

This reference covers the ToolNode side of the OAuth flow. The planner-side pause/resume mechanics are in [[penguiflow-hitl-pause-resume]].

## End-to-end flow

```
ReactPlanner.run(...)
  -> tool call
  -> ToolNode.call(...)
  -> OAuthManager.get_token(user_id, provider) returns None
  -> ToolNode pauses planner:
       reason="external_event"
       payload={
         "pause_type": "oauth",
         "provider": <toolnode name>,
         "auth_url": "...",
         "state": "...",
         "scopes": [...],
         "display_name": "...",
       }
  -> planner returns PlannerPause(resume_token, payload)
  -> host app opens auth_url for user
  -> user completes OAuth
  -> callback handler stores token in OAuthManager.token_store under (user_id, provider)
  -> host app calls planner.resume(resume_token, ...)
  -> planner re-invokes the tool
  -> OAuthManager.get_token now returns the token
  -> ToolNode adds Authorization header and the call succeeds
```

## Required setup

### 1. ToolNode config
```python
ExternalToolConfig(
    name="github",
    transport=TransportType.MCP,
    connection="...",
    auth_type=AuthType.OAUTH2_USER,
)
```

### 2. `tool_context` keys
On every `planner.run(...)` and `planner.resume(...)`:
- `tool_context["user_id"]` — **required**. Identifies the user owning the token.
- `tool_context["trace_id"]` — optional but strongly recommended for correlation in logs.

In multi-tenant systems, encode tenant in `user_id` (`acme:user-123`) or include `tenant_id` in `tool_context` and key the token store by composite.

### 3. `OAuthManager`
Configured with:
- Provider entries: `client_id`, `client_secret`, `auth_url`, `token_url`, `scopes`.
- A `token_store` that implements `get_token(user_id, provider)` and `set_token(user_id, provider, token)`.

### 4. Callback handler (in your host app)
Receives the OAuth redirect at your registered callback URL, validates `state`, exchanges the auth code for a token, stores it under `(user_id, provider)`, and (optionally) signals the planner to resume.

PenguiFlow does **not** ship the callback handler — your host app owns it.

### 5. Durable planner pause state
For multi-worker deployments, the planner pause state must persist. Configure a `StateStore` implementing:
- `save_planner_state(resume_token, state)`
- `load_planner_state(resume_token)` with consume-on-load semantics (TTL recommended).

Without this, a worker restart between pause and resume drops the pending state.

## Pause payload schema

```python
{
    "pause_type": "oauth",
    "provider": "<toolnode name>",     # e.g. "github"
    "auth_url": "https://...",          # where the user must go
    "state": "<random>",                # OAuth state param (treat as secret)
    "scopes": ["repo", "user"],
    "display_name": "GitHub (read-only)",
}
```

Additional fields may be present (provider-dependent). The host UI uses this payload to render the "Connect your GitHub account" CTA.

## Resume flow

```python
while isinstance(result, PlannerPause):
    # 1. UI shows result.payload["auth_url"] to the user.
    # 2. User completes OAuth in browser.
    # 3. Callback handler stores the token.
    # 4. Host app calls resume:
    result = await planner.resume(
        result.resume_token,
        user_input="oauth_completed",
        tool_context={**original_ctx, "oauth_ready": True},
    )
```

`oauth_ready` in `tool_context` is a convention the example flow uses to bypass the pause path on re-invocation. In production, the token check in `OAuthManager.get_token` handles this naturally — once the token is in the store, the next call succeeds.

## Operational defaults

- **Durable token store** — DB or Redis. Tokens outlive process restarts and span workers.
- **HTTPS-only callbacks** — OAuth callback URLs must use HTTPS in production.
- **Validate `state`** — every callback. Reject mismatched `state` to prevent CSRF.
- **Short pause TTL** — pending states should expire (default ~10 min). Re-prompting is safer than letting stale states accumulate.
- **Tenant-scoped `user_id`** — composite key in TokenStore (`tenant:user_id`) to avoid cross-tenant token reuse.
- **No tokens in `llm_context`** — ever.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Planner raises `ToolAuthError: user_id required` | `tool_context` missing `user_id` | Set it on every `run(...)` / `resume(...)` |
| Pause fires but resume `KeyError` | Pause state was in-memory and worker restarted | Implement `save_planner_state`/`load_planner_state` durably |
| OAuth completed but tool still pauses | Callback didn't store token, or stored under wrong key | Verify callback uses the same `(user_id, provider)` keying |
| User re-auths every request | TokenStore isn't durable or shared across workers | Move to Redis/DB-backed store |
| `state` rejected | Pending state expired (default ~10 min) or mismatched | Build a custom auth manager with longer TTL, or retry auth |
| Cross-tenant token leak | TokenStore keyed by `user_id` only | Use `tenant:user_id` composite |
| Tokens visible in prompts | Stored in `llm_context` | Move to `auth_config` / `OAuthManager.token_store`; audit prompt logs |

## Observability

Track:
- Pause count by provider (`pause_type="oauth"`).
- Time-to-resume p50/p95/p99.
- OAuth callback success/failure rates (state invalid, expired, provider errors).
- TokenStore hit rate (how often `get_token` returns non-null without prompting).

If `pause_type="oauth"` rate is high during sessions where the user already authenticated, the TokenStore is likely not durable or the key is wrong.

## Test double

For tests that need to exercise the pause shape without a real OAuth flow, the planner accepts a manual `oauth_ready` toggle (see the runnable example in the canonical docs). Use it in integration tests to assert the pause payload shape and resume contract.
