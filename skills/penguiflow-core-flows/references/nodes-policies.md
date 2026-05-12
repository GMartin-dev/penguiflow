# Nodes and NodePolicy

## `Node`

```python
Node(fn, name=..., policy=NodePolicy(...), allow_cycle=False)
```

- `fn` is `async def fn(msg, ctx)`. Two positional params, always.
- `name` is the routing key and the registry key. Must be unique in the graph.
- `policy` controls validation, timeouts, retries.
- `allow_cycle=True` permits self-cycles for loop-style nodes.

### Adjacency syntax

```python
node.to(other_node, another_node)   # connects node → {other_node, another_node}
node.to()                            # leaves node as egress (routes to Rookery)
```

The graph is the tuple of these expressions passed to `create(...)`.

## `NodePolicy` fields

| Field | Type | Default | Purpose |
|---|---|---|---|
| `validate` | `"both" \| "in" \| "out" \| "none"` | `"none"` | Pydantic validation gating |
| `timeout_s` | `float \| None` | `None` | Hard per-invocation timeout |
| `max_retries` | `int` | `0` | Retry count after failure (total attempts = max_retries + 1) |
| `backoff_base` | `float` | `0.5` | Initial backoff seconds |
| `backoff_mult` | `float` | `2.0` | Exponential multiplier |
| `max_backoff` | `float` | `30.0` | Backoff ceiling |

### `validate` semantics

- `"both"` — validate input AND output against `ModelRegistry` entries for this node name.
- `"in"` — validate input only (cheaper; useful when output is dynamic).
- `"out"` — validate output only.
- `"none"` — skip validation. Use for performance-sensitive paths or untyped flows.

When `validate != "none"`, `flow.run(registry=...)` fails fast if the registry is missing an entry for the node name. Register with `ModelRegistry().register(name, InModel, OutModel)`.

### Retry semantics

- On failure (raise or timeout), runtime emits `node_error` or `node_timeout`.
- If `attempt < max_retries`, emit `node_retry`, sleep `min(backoff_base * backoff_mult ** attempt, max_backoff)`, re-invoke.
- After retries exhaust, build a `FlowError` and emit `node_failed`. See `errors-retries.md`.

Retries are only safe if the node is idempotent. Use `trace_id` as the request id when calling external services. For non-idempotent side effects, split "plan" and "commit" nodes and gate commit behind HITL or explicit checks.

## `Context` (the `ctx` arg)

`ctx` provides emit helpers and runtime introspection inside a node:

- `await ctx.emit(msg, to=node_or_name)` — emit to a specific successor.
- `await ctx.emit_chunk(parent=msg, text=..., done=..., to=node_or_name)` — emit a `StreamChunk` inheriting `parent.trace_id`.
- Trace metadata is read via `msg.trace_id` and `msg.headers`.

## Recommended defaults

- `NodePolicy(validate="both")` on boundary nodes (ingress/egress, external tool wrappers).
- `NodePolicy(validate="none")` on internal high-throughput nodes if profiling shows validation cost.
- `timeout_s` on every network-bound node. Tune from p99 latency + headroom.
- `max_retries=1-3` for transient errors (HTTP 5xx, 429). Don't retry validation errors.

## Common patterns

### Boundary validation, internal speed

```python
ingress = Node(parse, name="parse", policy=NodePolicy(validate="both"))
worker  = Node(compute, name="compute", policy=NodePolicy(validate="none"))
egress  = Node(deliver, name="deliver", policy=NodePolicy(validate="both"))
```

### Loop-style controller

```python
controller = Node(step, name="controller", allow_cycle=True, policy=NodePolicy(validate="in"))
flow = create(controller.to(controller, finish_node), finish_node.to(), allow_cycles=True)
```

Use a budget (hop count, deadline) inside `step` to terminate; `allow_cycles=True` only opens the door.

### External I/O with timeout + retry

```python
fetch = Node(
    call_api,
    name="fetch",
    policy=NodePolicy(
        validate="in",
        timeout_s=8.0,
        max_retries=2,
        backoff_base=0.5,
        backoff_mult=2.0,
        max_backoff=4.0,
    ),
)
```

## Pitfalls

- **Forgot the `ctx` arg** — node signature must be two positional params. Even if you don't use it, name it `_ctx`.
- **Synchronous node function** — must be `async def`. PenguiFlow has no thread pool for sync nodes.
- **Mutating shared state** — nodes can run concurrently; treat closures as read-only and avoid module-level mutable state.
- **Swallowing `asyncio.CancelledError`** — breaks `flow.cancel(...)`. Let it propagate or do minimal cleanup and re-raise.
- **Naming collisions** — two nodes with the same `name` will silently route to one. Pick distinct names.
- **Forgetting registry entries** — `validate != "none"` requires `ModelRegistry().register(name, In, Out)` for that node.
