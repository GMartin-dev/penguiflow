---
name: penguiflow-core-flows
description: Build deterministic async pipelines and agent graphs with the PenguiFlow core runtime — wire Nodes with NodePolicy, construct the graph with `create(...)` and adjacency tuples, use Messages with Headers/trace_id envelopes, run/emit/fetch/stop, branch with `predicate_router`/`union_router`, fan-out and `join_k`, run parallel work with `map_concurrent`, cancel a trace, set deadlines, retry transient failures, and surface FlowError to callers. Use when a user says "build a flow", "wire a node", "fan-out/fan-in", "router", "cancel a run", "set a deadline", "retry policy", "envelope vs payload", "queue backpressure", or asks how the runtime works without a planner.
---

# PenguiFlow Core Runtime (Flows, Nodes, Routing)

## When to use
- The user is building a deterministic DAG or controller-style pipeline.
- They need fan-out, fan-in, routing, cancellation, deadlines, or retries.
- They are choosing between payload-only and envelope (`Message`) flows.

## When NOT to use
- LLM-driven step selection → use [[penguiflow-reactplanner-config]] (planner) and [[penguiflow-reactplanner-config]].
- Final-response token streaming UX → use [[penguiflow-streaming]].
- Cross-process / remote node execution → use [[penguiflow-deployment]] and [[penguiflow-a2a-integration]].
- Per-trace persistence and audit → use [[penguiflow-statestore]].

## Hard boundaries vs siblings
The runtime is the topology engine. It does **not** plan, persist, or stream UX events. Pair it with the planner skill for LLM control flow, the statestore skill for durability, the streaming skill for client-facing tokens.

## Workflow

### 1) Decide message style: payload-only vs envelope
- **Payload-only**: nodes take and return Pydantic models. Fastest to start. No `trace_id`, no cancellation, no deadlines.
- **Envelope (`Message`)**: payload + `Headers(tenant=...)` + `trace_id` + optional `deadline_s` + `meta`. Required for cancel, deadlines, streaming chunks, fan-in joins.

Production answer: envelope. Pick payload-only only for toy/learning flows. Full rules in `references/messages-and-envelopes.md`.

### 2) Define nodes with NodePolicy
Each `Node(fn, name=..., policy=NodePolicy(...))` wraps an `async def fn(msg, ctx)`. `NodePolicy` controls validation (`"both"|"in"|"out"|"none"`), `timeout_s`, `max_retries`, and backoff. Defaults are safe-ish but production needs explicit timeouts. See `references/nodes-policies.md`.

### 3) Build the graph with `create(...)` and adjacency tuples
```python
flow = create(a.to(b, c), b.to(d), c.to(d), d.to())
```
Knobs: `queue_maxsize` (default 64; `<=0` disables backpressure), `allow_cycles` (default False; raises `CycleError`), `middlewares=[...]`, `emit_errors_to_rookery=True`, `state_store=...`. The runtime synthesizes **OpenSea** (ingress) and **Rookery** (egress sink). See `references/topology-and-runtime.md`.

### 4) Run, emit, fetch, stop
```python
flow.run(registry=registry)
await flow.emit(msg)
result = await flow.fetch()
await flow.stop()
```
For request-scoped correlation across many concurrent traces, use `await flow.emit(msg, trace_id=trace_id)` + `await flow.fetch(trace_id=trace_id)` — this enables a trace-scoped Rookery dispatcher (and disables `fetch(from_=...)`). See `references/messages-and-envelopes.md`.

### 5) Branch with routers
- **`predicate_router(name, predicate)`** — predicate returns a Node, name, sequence, or `None` (drop). `KeyError` if target not connected.
- **`union_router(name, union_model)`** — validates a discriminated Pydantic union and routes by `kind`/class name to the matching successor name.
- Both accept an optional `policy` (sync or async) returning the same decision shape or `None`. `DictRoutingPolicy` (with `from_json`/`from_env`) is the built-in config-driven helper.

Full semantics + `RoutingRequest` fields in `references/routing.md`.

### 6) Fan-out and fan-in
- **`map_concurrent(items, worker, max_concurrency=N)`** — semaphore-gated worker map (no graph; just async helper).
- **`join_k(name, k)`** — graph Node that buckets `k` messages **per trace_id** and emits a batch. Envelope flows only.

See `references/concurrency-fanout-join.md` for backpressure tuning + the canonical fan-out/`join_k` pattern.

### 7) Cancel a trace
```python
cancelled: bool = await flow.cancel(trace_id)
```
Best-effort: sets cancellation event, drops queued messages for the trace, cancels in-flight tasks. Only works on envelope flows (trace-scoped). Doesn't kill blocking I/O — keep nodes cooperative. Doesn't auto-cancel external work you launched. See `references/cancel-deadlines.md`.

### 8) Set deadlines
Set `Message(deadline_s=...)`. Expired inputs are skipped; for `Message` envelopes the runtime emits `FinalAnswer(text="Deadline exceeded")` to Rookery. Watch `deadline_skip` events. See `references/cancel-deadlines.md`.

### 9) Errors and retries
Configure `NodePolicy(max_retries=N, backoff_base=..., backoff_mult=..., max_backoff=...)`. After retries exhaust, the runtime builds a `FlowError(trace_id, node_name, node_id, code, ...)` with code from `FlowErrorCode` (e.g. `NODE_EXCEPTION`, `NODE_TIMEOUT`). Set `create(..., emit_errors_to_rookery=True)` to surface `FlowError` to callers via `fetch()`. Retries only safe if your node is idempotent — gate side effects. See `references/errors-retries.md`.

## Troubleshooting (fast checks)
- **`RuntimeError: PenguiFlow is not running`** — call `flow.run(...)` before emitting.
- **`RuntimeError: PenguiFlow already running`** — `run()` called twice; stop or use a new instance.
- **`CycleError`** at `create()` — graph has a cycle; remove it or pass `allow_cycles=True`.
- **`fetch()` hangs** — no egress reaches Rookery; ensure a terminal node emits/returns a value.
- **Validation failures at `flow.run(registry=...)`** — registry missing entries for nodes with `validate != "none"`.
- **`KeyError: No successor named ...`** in a router — predicate returned a name not connected as outgoing successor.
- **Join never emits** — fan-out didn't produce exactly `k` items for that trace, or messages lost their `trace_id` mid-graph.
- **`cancel` returns `False`** — trace never existed or already completed; cancel the same id you emitted.
- **Cancelled work appears to continue** — node has blocking I/O without await points, or you launched external tasks not wired to the cancellation event.

## Worked examples
- `examples/quickstart/flow.py` — minimal payload-only typed flow.
- `examples/routing_predicate/flow.py`, `examples/routing_union/flow.py`, `examples/routing_policy/flow.py` — three routing styles.
- `examples/fanout_join/flow.py` — canonical fan-out + `join_k`.
- `examples/trace_cancel/flow.py` — best-effort cancel.
- `examples/traceable_errors/flow.py` — `FlowError` emitted to Rookery.

## References (load only as needed)
- `references/messages-and-envelopes.md` — payload-only vs envelope, trace-scoped roundtrips, `StreamChunk`/`FinalAnswer` shapes, mixed-style pitfalls.
- `references/nodes-policies.md` — `Node`, `NodePolicy` fields and defaults, validate modes, cycle nodes.
- `references/topology-and-runtime.md` — `create(...)` knobs, OpenSea/Rookery, `queue_maxsize`, `emit_errors_to_rookery`, middleware/state-store hooks.
- `references/routing.md` — `predicate_router`/`union_router`/`DictRoutingPolicy`, `RoutingRequest`, drop semantics.
- `references/concurrency-fanout-join.md` — backpressure, `map_concurrent`, `join_k`, deadlock patterns and fixes.
- `references/cancel-deadlines.md` — `cancel(trace_id)`, deadline behavior, cooperative-cancellation rules, `TraceCancelled`.
- `references/errors-retries.md` — `FlowError`/`FlowErrorCode`, retry semantics, idempotency, `emit_errors_to_rookery`.
