---
name: penguiflow-mcp-integration
description: Wire external tools (MCP servers, UTCP, HTTP, CLI) into a PenguiFlow ReactPlanner via `ToolNode` and `ExternalToolConfig` — pick a transport, choose an auth mode (none/API key/bearer/cookie/OAuth2), substitute env vars, set timeouts/retries/concurrency, allowlist tools with `tool_filter`, enable resource discovery (`{ns}.resources_list`/`resources_read`), extract large content into artifacts, and gate OAuth flows through HITL pause/resume. Use when a user says "wire MCP tools", "connect to an MCP server", "external tools", "ToolNode", "OAuth for tools", "tool filter", "UTCP", or names `ExternalToolConfig`, `TransportType`, `AuthType`.
---

# PenguiFlow MCP / External Tool Integration

## When to use
- Connecting an MCP server (`@modelcontextprotocol/server-*`) as planner tools.
- Wiring a UTCP/HTTP/CLI external tool catalog.
- Enabling OAuth-scoped per-user tool access.
- Tightening tool exposure (allowlists, read-only filters).
- Enabling MCP resources (URI discovery + read).

## When NOT to use
- Wiring planner-internal tools (Python functions decorated with `@tool`) → that's just [[penguiflow-reactplanner-config]] catalog wiring.
- Calling a remote A2A agent → use [[penguiflow-a2a-integration]] (`A2AAgentToolset`).
- The OAuth pause-resume lifecycle on the planner side → use [[penguiflow-hitl-pause-resume]].
- The artifact store backend used for large content → use [[penguiflow-statestore]] (`ArtifactStore`).

## Hard boundaries
This skill stops at "ToolNode exposes the right tools with safe defaults". Pause/resume state, the durable artifact store, and planner catalog wiring belong to sibling skills.

## Workflow

### 1) Pick a transport
`TransportType` values:
- `MCP` — `connection` is a launch command (`npx -y @modelcontextprotocol/server-github`) or an MCP service URL.
- `HTTP` — `connection` is a base URL; uses the `utcp` HTTP client under the hood.
- `UTCP` — `connection` points to a UTCP manual/base URL.
- `CLI` — `connection` is a CLI invocation pattern (via `utcp`).

Pick based on what the tool source actually speaks. MCP is the dominant choice for ecosystem tools.

### 2) Build an `ExternalToolConfig`
```python
from penguiflow.tools import AuthType, ExternalToolConfig, TransportType

cfg = ExternalToolConfig(
    name="github",                               # namespace; tools surface as github.<tool>
    transport=TransportType.MCP,
    connection="npx -y @modelcontextprotocol/server-github",
    auth_type=AuthType.BEARER,
    auth_config={"token": "${GITHUB_TOKEN}"},    # ${VAR} substitution; fail-fast if unset
    tool_filter=["get_.*", "list_.*", "search_.*"],   # regex allowlist (re.match)
    max_concurrency=3,                            # per-instance semaphore
    timeout_s=30.0,                               # per-call timeout
)
```

Required fields: `name`, `transport`, `connection`. Everything else has safe defaults; `references/external-tool-config.md` enumerates every field.

### 3) Choose an auth mode
| `AuthType` | Use for | `auth_config` keys |
|---|---|---|
| `NONE` | Public APIs | — |
| `API_KEY` | API key header | `api_key`, `header_name` (default `X-API-Key`) |
| `BEARER` | Service token | `token` |
| `COOKIE` | Cookie auth | `cookie_name`, `cookie_value` |
| `OAUTH2_USER` | Per-user OAuth | provider config in the `OAuthManager` (see Step 6) |

Auth resolves both at `connect(...)` (for connection-time headers) and on every `ToolNode.call(...)` (so tokens can rotate). See `references/auth-and-env.md`.

### 4) Substitute env vars with `${VAR}`
Any string in `auth_config`, `connection`, or other config fields can reference env vars. Missing vars raise `ToolAuthError` at connect/call time (fail-fast by design). Multiple vars per string are supported (regex-based substitution).

Don't put secrets in `llm_context` — env-substituted values stay in `tool_context` / auth headers.

### 5) Allowlist with `tool_filter`
`tool_filter` is a regex allowlist matched via `re.match(...)`. Examples: `["get_.*", "list_.*", "search_.*"]` (read-only), `["create_issue", "list_repositories"]` (specific), `None` (all). Default is permissive — use an explicit allowlist in production.

### 6) (Optional) Enable OAuth via HITL
Set `auth_type=AuthType.OAUTH2_USER`. When a token is missing, ToolNode pauses the planner with `reason="external_event"` and payload `{"pause_type": "oauth", "provider": ..., "auth_url": ..., "state": ..., "scopes": [...], "display_name": ...}`. Requires `tool_context["user_id"]`, a configured `OAuthManager` with token store, a durable planner state store ([[penguiflow-hitl-pause-resume]]), and a callback endpoint storing tokens under `(user_id, provider)`. See `references/oauth-and-hitl.md`.

### 7) (Optional) Enable MCP resources
If the server supports resources, ToolNode auto-generates `{ns}.resources_list`, `{ns}.resources_read`, `{ns}.resources_templates_list`. Programmatic API: `list_resources`, `read_resource`, `list_resource_templates`, `subscribe_resource`, `unsubscribe_resource`. Large reads go to the artifact store. See `references/resources-and-artifacts.md`.

### 8) Connect and inspect
```python
tool_node = ToolNode(config=cfg, registry=registry)
await tool_node.connect()
for spec in tool_node.get_tools():
    print(spec.name)
```

Pass `tool_node.get_tools()` (a list of `NodeSpec`) into your planner catalog. The planner skill handles catalog composition.

## Troubleshooting (fast checks)
- **`ToolAuthError` at connect** — a `${VAR}` is unset in the worker's env (not just your shell).
- **Too many tools exposed** — add a stricter `tool_filter`; never expose the full catalog in production.
- **`429`/rate-limit storms** — lower `max_concurrency`, raise retry backoff, reduce planner parallelism.
- **OAuth pauses but never completes** — missing callback handler, no `user_id` in `tool_context`, or no durable `StateStore` for planner pause state.
- **Resources tools not generated** — server doesn't support resources.
- **Reads return artifacts only** — over `inline_text_if_under_chars`; download from the artifact store.
- **Calls hang forever** — set `timeout_s` explicitly; the 30s default is too coarse for production.
- **`npx -y` crashes under load** — run MCP servers as managed services and connect via URL.
- **Cross-tenant token reuse** — use composite key `tenant:user_id` in your TokenStore.

## Worked examples
- `examples/toolnode_presets/flow.py` and `examples/toolnode_utcp_echo/flow.py` — runnable wiring patterns. Inspect the directory for preset templates.

## References (load only as needed)
- `references/external-tool-config.md` — every `ExternalToolConfig` field, transport semantics, resilience knobs, namespacing rules.
- `references/auth-and-env.md` — auth modes, `${VAR}` substitution, `OAuthManager`, secret hygiene.
- `references/oauth-and-hitl.md` — OAuth pause payload, resume flow, durable state requirements.
- `references/resources-and-artifacts.md` — MCP resources surface, `ResourceCache`, artifact extraction pipeline.
