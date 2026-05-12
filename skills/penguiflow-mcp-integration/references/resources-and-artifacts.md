# MCP Resources and Artifact Extraction

## What resources are

Many MCP servers expose **resources** in addition to tools. Resources are:
- Addressable by URI (`file://...`, `mcp://...`, custom schemes).
- Often read-heavy and cacheable.
- Useful for "browse then act" patterns: list → read → summarize → decide.

ToolNode handles resources by discovering them on connect, exposing them as planner tools, and caching reads in an `ArtifactStore` for large/binary content.

## Auto-generated tools

If the connected server supports resources, ToolNode generates:

| Tool | Purpose |
|---|---|
| `{ns}.resources_list` | List available resources (paginated by server). |
| `{ns}.resources_read` | Read a resource by URI. |
| `{ns}.resources_templates_list` | List resource templates (URI patterns the server can read). |

They appear alongside the server's normal tools in `tool_node.get_tools()`.

## Programmatic API

```python
# discover
resources = await tool_node.list_resources(refresh=False)

# read (cache-on by default)
result = await tool_node.read_resource(uri, ctx, use_cache=True)

# templates
templates = await tool_node.list_resource_templates(refresh=False)

# subscribe (best-effort; server-dependent)
await tool_node.subscribe_resource(uri, callback=lambda update: ...)
await tool_node.unsubscribe_resource(uri)
```

`refresh=True` forces a fresh server call (bypasses the in-memory list cache).
`use_cache=False` forces a fresh read (bypasses the read cache).

## Read result shapes

`read_resource(...)` returns one of:

```python
{"text": "..."}                  # inline text (under threshold)
{"artifact": ArtifactRef(...)}   # binary or large text (over threshold)
{"error": "..."}                 # server error or auth failure
```

Threshold: `ExternalToolConfig.artifact_extraction.resources.inline_text_if_under_chars`.

For the artifact path, use `ctx.artifacts.download(ref)` to fetch the bytes.

## Artifact extraction pipeline

The same pipeline applies to tool **outputs**, not just resource reads. When `artifact_extraction.enabled=True`:

1. Tool returns content.
2. ToolNode inspects size and type.
3. Inline if text under threshold; otherwise upload to artifact store via `ctx._artifacts` and return an `ArtifactRef`.

### Plumbing vs application API

| API | Audience | Purpose |
|---|---|---|
| `ctx._artifacts` (raw `ArtifactStore`) | ToolNode's extraction pipeline | Internal; don't use from tool code |
| `ctx.artifacts` (`ScopedArtifacts` facade) | Tool authors | `upload(name, content)`, `download(ref)`, `list()`; tenant-scoped |

Tool developers writing custom tools that produce artifacts manually should use `ctx.artifacts.upload(...)`, not `ctx._artifacts`.

## Subscriptions (best-effort)

`subscribe_resource(uri, callback)` registers a callback for server-pushed resource updates. Semantics:
- Server-dependent. Not all MCP servers support subscriptions.
- Callbacks are async-friendly; pass an async function for non-blocking handling.
- Use to invalidate read caches: when an update arrives, drop the cached entry for that URI and re-read on next request.

## Cache behavior

`ResourceCache`:
- In-memory by default. Survives within a process but not across restarts.
- Keyed by URI.
- Invalidation: explicit (`use_cache=False`) or subscription-driven.

For multi-worker deployments where cache coherence matters, build a shared cache layer (e.g., backed by Redis) wrapped around the ToolNode.

## Operational defaults

- Use a **durable `ArtifactStore`** in production. Without one, large reads can't be returned safely (they'd OOM the worker).
- Keep `use_cache=True` (default) unless you have strict freshness requirements.
- Prefer explicit reads over inlining: list resources, then read the specific URI.
- Set `inline_text_if_under_chars` to a value that fits comfortably in an LLM context window (e.g., 4-8 KB).
- Treat resource URIs as sensitive if they encode identifiers, paths, or tenant info.

## Security and multi-tenancy

- Don't let an LLM read arbitrary URIs across tenants. Use:
  - Tenant-scoped MCP servers (one per tenant), or
  - Tool visibility policies that filter `resources_*` per request.
- Ensure your artifact retrieval endpoint enforces scope checks (tenant/user/session) — `ArtifactRef`s are guessable by themselves, so authorization must live at the retrieval API.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `resources_*` tools not generated | Server doesn't support resources | Use the server's tools directly, or upgrade to a resources-capable server |
| Read returns `{"error": ...}` | Disconnected, bad URI, server/auth failure | Verify connection/auth; re-list resources for fresh URIs |
| Reads always return artifacts | All content over inline threshold | Bump `inline_text_if_under_chars` if size profile allows |
| Stale content | Cache hit on changed resource | `read_resource(..., use_cache=False)` for a one-off, or subscribe for invalidation |
| Subscriptions silently drop | Server doesn't support subscriptions, or worker restarted | Treat subscriptions as best-effort; rely on TTLs |
| Artifact storage unbounded | No retention policy on the store | Configure `ArtifactRetentionConfig` ([[penguiflow-statestore]]) |

## Observability

Track:
- `tool_call_*` events for `{ns}.resources_*` tools (latency, error rate).
- `artifact_stored` events when extraction fires (size distribution).
- Cache hit rate (application-level; `ResourceCache` is in-memory and you must instrument).
- Subscription update rate per URI.

## Worked example

```python
from penguiflow import ModelRegistry
from penguiflow.tools import ExternalToolConfig, ToolNode, TransportType

registry = ModelRegistry()
node = ToolNode(
    config=ExternalToolConfig(
        name="filesystem",
        transport=TransportType.MCP,
        connection="npx -y @modelcontextprotocol/server-filesystem /data",
    ),
    registry=registry,
)
await node.connect()

resources = await node.list_resources()
for r in resources[:5]:
    print(r.uri, r.name)

# read the first one
result = await node.read_resource(resources[0].uri, ctx, use_cache=True)
if "text" in result:
    print(result["text"][:200])
elif "artifact" in result:
    ref = result["artifact"]
    blob = await ctx.artifacts.download(ref)
```
