# Distributed Primitives

PenguiFlow exposes three protocols for distributed deployments: `StateStore`, `MessageBus`, `RemoteTransport`. You provide the implementations; the runtime calls them at the right hooks.

## `StateStore` (durability + audit)

The cornerstone of distributed PenguiFlow. Required for any feature that must survive a process restart.

Required methods:
- `save_event(event: StoredEvent) -> None` — idempotent persistence of every `FlowEvent`.
- `load_history(trace_id: str) -> Sequence[StoredEvent]` — read events for a trace (audit, replay, debugging).
- `save_remote_binding(binding: RemoteBinding) -> None` — persist agent-to-agent / RemoteNode bindings.

Optional capabilities (duck-typed, detected at runtime):
- Planner pause/resume state — see [[penguiflow-hitl-pause-resume]].
- Memory state — see [[penguiflow-memory]].
- Conversation bindings for A2A — see [[penguiflow-a2a-integration]].
- Artifact store — see [[penguiflow-rich-output]].

Pass to flow construction:
```python
flow = create(..., state_store=my_state_store)
planner = ReactPlanner(..., state_store=my_state_store)
```

Backends: in-memory (dev), Redis, Postgres, custom. Full contract in [[penguiflow-statestore]].

### Idempotency rule
`save_event` may be called multiple times for the same `StoredEvent` (retries, recovery). Implementations must dedupe (unique constraint on event id, fingerprint, etc.). The runtime treats `save_event` as best-effort; errors are logged and execution continues.

## `MessageBus` (distributed edges)

Optional. Lets the runtime publish edge traffic to a queue/bus so out-of-process workers can consume.

Interface (signature):
```python
class MessageBus(Protocol):
    async def publish(self, envelope: BusEnvelope) -> None: ...
```

`BusEnvelope` carries the message + routing metadata (source node, target node, trace_id).

When configured (`create(..., message_bus=my_bus)`):
- Every edge `emit` also publishes a `BusEnvelope`.
- Downstream workers consume from the bus and re-emit into their local flow.

Use when:
- Splitting one graph across processes/machines (e.g., heavy GPU node on a separate worker).
- Tracing edge traffic for replay.
- Hooking PenguiFlow into your event/streaming platform (Kafka, Pub/Sub).

Don't use when:
- A single process can handle the load (just use the in-process runtime).
- You don't have a durable bus (you'll lose edges on bus failure).

### Implementation hints
- The bus must guarantee ordering per `trace_id` if you want join semantics to hold across processes.
- Treat bus publish as best-effort but log failures loudly — a missed publish silently loses an edge.

## `RemoteTransport` + `RemoteNode`

For calling a remote service as if it were a local node.

Interface:
```python
class RemoteTransport(Protocol):
    async def send(self, request: RemoteCallRequest) -> RemoteCallResult: ...
    def stream(self, request: RemoteCallRequest) -> AsyncIterator[RemoteStreamEvent]: ...
    async def cancel(self, agent_url: str, task_id: str) -> None: ...
```

Construct a remote node:
```python
from penguiflow import RemoteNode
remote = RemoteNode(
    transport=my_transport,
    agent_url="https://specialist.example.com",
    skill="answer",
    record_binding=True,             # persist RemoteBinding for cancellation across restarts
)
```

Include `remote` in your graph like any `Node`. The runtime calls the transport on edge dequeue.

### `record_binding=True`
Persists `RemoteBinding(trace_id, agent_url, task_id, ...)` to the `StateStore`. Required for:
- Cancellation across worker restarts.
- A2A conversation continuity.
- Distributed observability of remote tasks.

If you don't set this (or `StateStore.save_remote_binding` isn't implemented), remote tasks can leak — you can spawn them but not cancel them after a restart.

### Cancellation
`flow.cancel(trace_id)` calls `transport.cancel(agent_url, task_id)` for every recorded binding. Best-effort — the remote may or may not honor it.

### Implementations
- `A2AHttpTransport` (in `penguiflow_a2a`) implements this protocol for HTTP A2A endpoints.
- For gRPC, custom HTTP, internal services — implement the protocol yourself.

## A2A handoff

`penguiflow_a2a` provides full A2A protocol bindings (server, JSON-RPC, REST, SSE, gRPC, push notifications). For a manager↔specialist topology:

- **Manager**: uses `A2AAgentToolset` (a higher-level wrapper than `RemoteNode`) to wrap remote agents as planner tools.
- **Specialist**: uses `A2AService` + an HTTP binding to expose itself.

See [[penguiflow-a2a-integration]]. This deployment skill points there; don't duplicate.

## Recovery semantics

On worker restart, the runtime can recover state when:
- `StateStore.load_history(trace_id)` returns past events.
- `StateStore.load_planner_state(token)` returns pause records.
- `StateStore` exposes `list_bindings` and `find_binding` for active remote tasks.

What's NOT recovered automatically:
- In-flight node tasks. They die with the worker.
- Edge queue contents. They die with the process unless backed by a `MessageBus`.
- Tool job state inside a `TaskService`. Depends on the service's persistence.

Implication: design for at-least-once. Anything sensitive (writes, sends) needs idempotency keys.

## At-least-once + idempotency rules

In a distributed deployment with retries, redeliveries, and recovery:

1. **Every external write** has an idempotency key (use `trace_id` or `trace_id:node_name`).
2. **Plan/commit split** for high-stakes side effects.
3. **Dead-letter** terminal failures to a human-reviewed queue.
4. **Re-execute only after verifying** the previous attempt didn't already succeed.

The library can't enforce idempotency for you — it's a property of the surrounding system.

## A practical distributed setup

For a multi-worker production agent:

1. **Postgres `StateStore`** — events + bindings + pause state + memory.
2. **Redis** queue for inbound jobs.
3. **Stateless worker pool** consuming from Redis.
4. **`A2AHttpTransport`** for outbound agent calls.
5. **MLflow / Prometheus** exporter via `FlowEvent` middleware.
6. **OpenTelemetry** for cross-service traces (`trace_id` ↔ OTel `trace_id`).

A2A bindings recover correctly across restarts. Memory persists. Pauses survive worker swaps. Cancellation propagates to remote tasks via `RemoteBinding`.

## Anti-patterns

- **`StateStore` only in-memory** — defeats every multi-worker capability.
- **`record_binding=False`** in production — orphaned remote tasks accumulate.
- **`MessageBus` without `StateStore`** — bus traffic vanishes on restart.
- **Mixing tenants in the same `trace_id`** — recovery merges them.
- **No idempotency on external writes** — duplicates on retry and recovery.
