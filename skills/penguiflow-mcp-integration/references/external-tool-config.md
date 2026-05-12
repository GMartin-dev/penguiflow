# `ExternalToolConfig` reference

The full configuration surface for `ToolNode`.

## Required fields

| Field | Type | Purpose |
|---|---|---|
| `name` | `str` | Namespace prefix. Tools surface as `{name}.{tool}`. |
| `transport` | `TransportType` | `MCP | HTTP | UTCP | CLI` |
| `connection` | `str` | MCP command or URL; HTTP/UTCP base URL; CLI invocation. |

## Auth fields

| Field | Default | Purpose |
|---|---|---|
| `auth_type` | `AuthType.NONE` | `NONE | API_KEY | BEARER | COOKIE | OAUTH2_USER` |
| `auth_config` | `{}` | Auth-specific keys (see `auth-and-env.md`). |

## Resilience fields

| Field | Default | Purpose |
|---|---|---|
| `timeout_s` | `30.0` | Per-call timeout in seconds. |
| `max_concurrency` | `10` | Semaphore size for parallel calls in this ToolNode. |
| `retry_policy` | tenacity defaults | `max_attempts`, min/max wait, retryable HTTP status codes. |

Operational guidance:
- External SaaS: `max_concurrency=3..5`.
- Internal services: start `10`, tune from observed throughput.
- Always set `timeout_s` explicitly. Never trust the default.

## Filtering

`tool_filter: list[str] | None`. Regex allowlist applied via `re.match(pattern, tool_name)` against the **bare** tool name (without the `{name}.` prefix).

- `None` (default): expose all discovered tools.
- `[]` (empty list): expose **no** tools (often a misconfig — be explicit if you want to disable).
- Read-only: `["get_.*", "list_.*", "search_.*"]`.
- Specific tools: `["create_issue", "list_repositories"]`.

For multi-tenant deployments, prefer `ToolVisibilityPolicy` at the planner layer (per-request filtering) over hard-coding `tool_filter` per tenant.

## Artifact extraction

`artifact_extraction` controls how large/binary tool outputs are extracted into an `ArtifactStore`:
- `enabled: bool` — master switch.
- `inline_text_if_under_chars: int` — text smaller than this stays inline; larger is stored as artifact.
- Binary content is always stored as artifact when extraction is enabled.

Returned shapes from extracted reads:
- Inline: `{"text": "..."}`.
- Artifact: `{"artifact": <ArtifactRef>}`.

The extraction pipeline uses `ctx._artifacts` (raw `ArtifactStore`). Tool developers storing artifacts manually should use `ctx.artifacts` (the `ScopedArtifacts` facade) with `upload()`/`download()`/`list()`.

## Transport semantics

### `TransportType.MCP`
`connection` can be:
- A launch command (`npx -y @modelcontextprotocol/server-github`) — FastMCP spawns the server (stdio).
- A URL (HTTP/SSE) — connects to a running MCP service; auto-detects transport unless `mcp_transport_mode` overrides.

`mcp_transport_mode` (`UtcpMode`-style `McpTransportMode` enum): `AUTO` (default), `SSE`, or `STREAMABLE_HTTP`. Set explicitly only when auto-detection fails — modern servers should use `STREAMABLE_HTTP`.

Production recommendation: run MCP servers as managed services (Docker/k8s) and connect via URL. Avoid `npx -y ...` in long-lived workers — process lifecycle, version drift, and node_modules churn cause flakiness.

### `TransportType.HTTP`
`connection` is a base URL. Tools are discovered via UTCP's HTTP client. Use for REST APIs without an MCP wrapper.

### `TransportType.UTCP`
`connection` points to a UTCP manual (a YAML/JSON manifest describing the tools). Use when integrating with services that publish a UTCP manual.

### `TransportType.CLI`
`connection` is a CLI pattern. Use for command-line tools that follow UTCP's CLI conventions.

### `utcp_mode` (HTTP / UTCP only)

`utcp_mode: UtcpMode` controls how the connection string is interpreted for HTTP/UTCP transports:
- `AUTO` (default) — try manual_url first, fallback to base_url.
- `MANUAL_URL` — connection is a UTCP manual endpoint (recommended for clean discovery).
- `BASE_URL` — connection is a plain REST base URL (limited discovery).

**Hard rule**: setting `utcp_mode` to anything other than `AUTO` while `transport=TransportType.MCP` raises `ValueError("utcp_mode is only valid for HTTP/UTCP transports")` at config validation.

### `auth_config` validation

The `validate_config` model-validator enforces:
- `auth_type=BEARER` → `auth_config["token"]` must be present.
- `auth_type=API_KEY` → `auth_config["api_key"]` must be present.
- `auth_type=COOKIE` → both `auth_config["cookie_name"]` and `auth_config["cookie_value"]` must be present.

Missing required keys raise `ValueError` at construction time, not at first call.

## Namespacing

Tool names: `{name}.{tool}`. Example: `name="github"`, server tool `create_issue` → planner tool name `github.create_issue`.

Rules:
- Pick stable namespaces (`github`, `slides`, `slack`) — they leak into prompts and logs.
- Don't reuse a namespace across multiple ToolNodes — discovery will collide.
- For the same vendor with multiple personas (`github_readonly`, `github_admin`), use distinct namespaces.

## Lifecycle

```python
tool_node = ToolNode(config=cfg, registry=registry)
await tool_node.connect()              # discovers tools, resolves auth headers
specs = tool_node.get_tools()          # list[NodeSpec] for the planner catalog
result = await tool_node.call(name, args, ctx)   # invoke a single tool
await tool_node.disconnect()           # tear down transport
```

Pass `specs` to your planner's catalog. The planner skill ([[penguiflow-reactplanner-config]]) handles catalog composition.

## Observability hooks

Every tool call emits `PlannerEvent(event_type="tool_call_start"/"tool_call_end"/"tool_call_error")` with the namespaced tool name, latency, error class, and (when artifact extraction fires) `artifact_stored` events.

Track per-namespace:
- Connect duration + discovered tool count.
- Call latency p50/p95/p99.
- Error rate by tool.
- Retry counts.

For full event schema see [[penguiflow-observability]].

## Anti-patterns
- Skipping `timeout_s` → tools can hang forever and stall the planner.
- Unbounded `max_concurrency` → external SaaS rate-limits storm.
- No `tool_filter` in production → planner gets every dangerous tool the server exposes.
- Sharing one ToolNode across tenants without a visibility policy → cross-tenant tool leak.
- `npx -y ...` in serverless / autoscaling workers → cold-start flakiness.
