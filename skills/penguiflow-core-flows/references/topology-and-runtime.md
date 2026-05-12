# Topology and Runtime

## `create(...)` builds a `PenguiFlow` runtime

```python
flow = create(
    a.to(b, c),
    b.to(d),
    c.to(d),
    d.to(),                # d has no successors → routes to Rookery
    queue_maxsize=64,
    allow_cycles=False,
    middlewares=[log_flow_events],
    emit_errors_to_rookery=False,
    state_store=None,
    message_bus=None,
)
```

### Knobs

| Knob | Default | Purpose |
|---|---|---|
| `queue_maxsize` | `64` | Bounded per-edge `asyncio.Queue`. `<= 0` disables backpressure. |
| `allow_cycles` | `False` | When False, `create()` raises `CycleError` on cycles. |
| `middlewares` | `[]` | Async hooks receiving `FlowEvent`. |
| `emit_errors_to_rookery` | `False` | When True, terminal `FlowError` lands on the egress sink. |
| `state_store` | `None` | Persistence hook (see [[penguiflow-statestore]]). |
| `message_bus` | `None` | Optional bus integration for edge traffic. |

## Endpoints: OpenSea and Rookery

The runtime synthesizes two endpoints automatically:

- **OpenSea** — virtual ingress. `await flow.emit(msg)` feeds nodes with no incoming edges.
- **Rookery** — virtual egress. Nodes with no outgoing edges route to Rookery, where `await flow.fetch()` reads.

This is why a minimal one-node graph works without explicit wiring.

## Lifecycle

```python
flow.run(registry=registry)   # spawns worker tasks for each node
await flow.emit(msg)          # send to OpenSea
result = await flow.fetch()    # receive from Rookery
await flow.stop()             # cancel all node tasks, drain queues
```

Rules:
- `run()` is synchronous; node workers are started but not awaited.
- `emit()` and `fetch()` are async.
- `stop()` is async and must be awaited; not stopping leaks worker tasks.

Calling `run()` twice raises `RuntimeError: PenguiFlow already running`. Emit/fetch before `run()` raises `RuntimeError: PenguiFlow is not running`.

## Cycles

By default `allow_cycles=False`. If the graph contains a cycle, `create()` raises `CycleError` with the offending nodes.

To opt in:
1. Pass `allow_cycles=True` to `create(...)`.
2. Mark cycle-friendly nodes with `Node(allow_cycle=True)`.
3. Implement a termination condition (hop budget, deadline) inside the node.

The runtime doesn't enforce termination — you do.

## Queue sizing and backpressure

Each graph edge is an `asyncio.Queue(maxsize=queue_maxsize)`.

- `queue_maxsize=64` (default) is a safe starting point for typical agent graphs.
- Bump to 128–256 for fan-out workloads with bursty producers.
- `queue_maxsize <= 0` disables backpressure — only use this if you have your own flow control.

Signs you need to tune:
- `FlowEvent` shows `queue_depth_out` trending up → consumer is slower than producer.
- `emit(...)` appears to hang → downstream queue is full (which is backpressure working).

## Middleware

A middleware is an async callable accepting a `FlowEvent`:

```python
async def my_mw(event: FlowEvent) -> None:
    if event.event == "node_failed":
        await alert(event)
```

Attach at construction (`create(..., middlewares=[my_mw])`) or dynamically (`flow.add_middleware(my_mw)`).

PenguiFlow ships `penguiflow.middlewares.log_flow_events` as a structured-logging baseline. See [[penguiflow-observability]] for the full event catalog.

## State store integration

Pass `state_store=...` to `create(...)` to persist runtime events. The runtime calls `await state_store.save_event(StoredEvent(...))` for every `FlowEvent` (best-effort: errors are logged but execution continues). For remote-binding persistence (A2A, distributed), see [[penguiflow-statestore]].

## Subflows / playbooks

`call_playbook(playbook, msg, parent_ctx)` lets a node invoke another `PenguiFlow` as a synchronous-looking subflow. Useful for composing reusable graph fragments. The subflow inherits the parent's registry by default.

## Errors at construction time

| Error | Cause | Fix |
|---|---|---|
| `CycleError` | Cycle without `allow_cycles=True` | Remove cycle or opt in |
| Duplicate node name | Two `Node(name="x")` in the graph | Pick unique names |
| Unknown adjacency target | `a.to(b)` where `b` isn't in the graph | Include `b` somewhere in adjacency tuples |
| Validation registry missing entries | `validate != "none"` but no entry for node name | `registry.register(name, In, Out)` |

## Runnable pattern

```python
from penguiflow import (
    Headers, Message, ModelRegistry, Node, NodePolicy, create,
    log_flow_events,
)

async def parse(msg: Message, _ctx) -> Message:
    return msg.model_copy(update={"payload": msg.payload.upper()})

async def deliver(msg: Message, _ctx):
    return msg.payload

parse_node = Node(parse, name="parse", policy=NodePolicy(validate="none"))
deliver_node = Node(deliver, name="deliver", policy=NodePolicy(validate="none"))

flow = create(
    parse_node.to(deliver_node),
    deliver_node.to(),
    queue_maxsize=128,
    middlewares=[log_flow_events],
)
flow.run()

msg = Message(payload="hello", headers=Headers(tenant="demo"))
await flow.emit(msg, trace_id=msg.trace_id)
print(await flow.fetch(trace_id=msg.trace_id))
await flow.stop()
```
