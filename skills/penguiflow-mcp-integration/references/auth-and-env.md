# Auth Modes and `${VAR}` Substitution

## `AuthType` matrix

### `AuthType.NONE`
No auth headers. For public APIs only.

```python
ExternalToolConfig(..., auth_type=AuthType.NONE)
```

### `AuthType.BEARER`
Sends `Authorization: Bearer <token>`.

```python
ExternalToolConfig(
    ...,
    auth_type=AuthType.BEARER,
    auth_config={"token": "${GITHUB_TOKEN}"},
)
```

### `AuthType.API_KEY`
Injects an API key header. Default header is `X-API-Key` unless overridden.

```python
ExternalToolConfig(
    ...,
    auth_type=AuthType.API_KEY,
    auth_config={
        "api_key": "${SVC_API_KEY}",
        "header_name": "X-Custom-Key",   # optional
    },
)
```

### `AuthType.COOKIE`
Sets a cookie. Useful for legacy systems.

```python
ExternalToolConfig(
    ...,
    auth_type=AuthType.COOKIE,
    auth_config={
        "cookie_name": "session",
        "cookie_value": "${SESSION_COOKIE}",
    },
)
```

### `AuthType.OAUTH2_USER`
Per-user OAuth via planner HITL pause/resume. See `oauth-and-hitl.md` for the full flow.

```python
ExternalToolConfig(
    ...,
    auth_type=AuthType.OAUTH2_USER,
    # auth_config typically empty here; provider config lives in OAuthManager
)
```

Requires `tool_context["user_id"]` on every planner call.

## Auth resolution timing

ToolNode resolves auth at two moments:
1. **`connect(...)`** — assembles connection-time headers (for MCP/HTTP transports that send them on handshake).
2. **`ToolNode.call(...)`** — refreshes per-call so tokens can rotate without reconnecting.

This means:
- Rotating a token doesn't require a reconnect.
- A token that expires mid-session causes the next call to fail (and, for OAuth, trigger a pause).

## `${VAR}` substitution

Any string in `auth_config`, `connection`, or other config fields can reference environment variables:

```python
auth_config={
    "token": "${GITHUB_TOKEN}",
    "org": "${GITHUB_ORG}",
    "header_name": "X-${ORG}-Auth",     # multiple per string supported
}
connection="https://api.${REGION}.example.com/v1"
```

Behavior:
- Resolved at config processing time (when ToolNode reads the field).
- Missing var → `ToolAuthError` raised **fail-fast** (at connect or first call).
- Substitution is regex-based; `${...}` is the only syntax.

The fail-fast behavior is intentional. Missing credentials should surface as a startup error, not a runtime planner failure halfway through a session.

## Secret hygiene

### Don't put secrets in `llm_context`
`llm_context` is fed to the LLM in every prompt. Tokens leak there will leak into model providers and logs.

Instead:
- Put secrets in `auth_config` (env-substituted).
- Put per-user identity in `tool_context["user_id"]` (an opaque ID, not a token).
- Use `OAUTH2_USER` for user-scoped tokens.

### Where secrets are safe
- Process env vars (referenced via `${VAR}`).
- `tool_context` (planner side, never sent to LLM).
- Provider-specific token stores (e.g., your `OAuthManager.token_store`).

### Per-tenant tool secrets
For multi-tenant deployments, scope env vars per worker (one worker per tenant) or use a runtime auth manager that looks up tokens by tenant. Don't try to encode `${TENANT_*_TOKEN}` patterns — it's fragile and leaks tenant names into config files.

## `OAuthManager`

Default implementation in `penguiflow.tools.auth`. Constructed with:

```python
from penguiflow.tools.auth import OAuthManager

manager = OAuthManager(
    providers={
        "github": {
            "client_id": "${GH_CLIENT_ID}",
            "client_secret": "${GH_CLIENT_SECRET}",
            "auth_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "scopes": ["repo"],
        },
    },
    token_store=my_token_store,   # implements get/set per (user_id, provider)
)
```

Methods:
- `get_token(user_id, provider) -> token | None` — fetches a cached token.
- `get_auth_request(provider, user_id, trace_id) -> payload` — produces the pause payload with `auth_url`, `state`, `scopes`, `display_name`.

Pending `state` values expire after ~10 minutes by default — if your callback latency is higher, build a custom auth manager.

## Token store requirements (multi-worker)

For deployments with multiple workers / restarts:
- The token store must be durable and shared (Redis, DB, KMS-backed).
- Key tokens by `(user_id, provider)`. In multi-tenant systems, use `(tenant:user_id, provider)`.
- Treat tokens as encrypted-at-rest. Most providers' access tokens are short-lived; refresh tokens are long-lived and must be protected.

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `ToolAuthError: missing env var X` | Var set in your shell, not in worker env | Set the var in the process environment that runs the worker |
| Tokens leak into prompts | Token put in `llm_context` | Move to `auth_config` / `tool_context`; redact prompt logs |
| `header_name` ignored | Mis-typed key in `auth_config` | Use exactly `header_name` |
| Multiple vars don't substitute | Used `$VAR` instead of `${VAR}` | Always use the `${...}` braces |
| Cross-tenant token reuse | TokenStore keyed by `user_id` only | Use `(tenant:user_id, provider)` composite key |
