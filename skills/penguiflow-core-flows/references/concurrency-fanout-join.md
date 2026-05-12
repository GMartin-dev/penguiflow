# Concurrency, Fan-Out, and `join_k`

## Where concurrency comes from

PenguiFlow concurrency is structural:
- Each `Node` runs its own worker task.
- Multiple nodes execute concurrently when they have work and queue capacity.
- Each edge is a bounded `asyncio.Queue` providing backpressure.

The runtime does not parallelize a single node's body — that's your job (use `asyncio.gather`, `map_concurrent`, or downstream fan-out).

## `map_concurrent(items, worker, max_concurrency=N)`

Semaphore-gated `async` map helper. **Not a graph node** — call it inside a node body or in your app.

```python
from penguiflow import map_concurrent

async def worker(x: int) -> int:
    await asyncio.sleep(0.01)
    return x * 2

results = await map_concurrent([1, 2, 3, 4, 5], worker, max_concurrency=8)
```

Use when:
- You have N independent async calls inside one node.
- You want to bound concurrency without building a sub-graph.

Don't use when:
- You need per-item backpressure across nodes (use graph fan-out instead).
- You need to cancel a subset (use trace cancellation + graph fan-out).

## `join_k(name, k)`

A runtime `Node` that buckets `k` messages **per `trace_id`** and emits the batch as a list payload to the next successor.

```python
join = join_k("join", k=3)
```

Hard requirements:
- Envelope flow (`Message` with `trace_id`).
- Every trace produces exactly `k` branch messages — no more, no fewer.

Canonical fan-out / fan-in pattern:

```python
async def fanout(msg: Message, ctx) -> None:
    for item in msg.payload:                  # payload is a list of N items
        await ctx.emit(msg.model_copy(update={"payload": item}), to=worker_node)

async def work(msg: Message, _ctx) -> Message:
    return msg.model_copy(update={"payload": msg.payload * 2})

async def deliver(msg: Message, _ctx) -> list[int]:
    return msg.payload

fanout_node = Node(fanout, name="fanout", policy=NodePolicy(validate="none"))
worker_node = Node(work, name="work", policy=NodePolicy(validate="none"))
join_node = join_k("join", k=3)
final_node = Node(deliver, name="final", policy=NodePolicy(validate="none"))

flow = create(
    fanout_node.to(worker_node),
    worker_node.to(join_node),
    join_node.to(final_node),
    final_node.to(),
)
```

## Backpressure tuning

Each edge is `asyncio.Queue(maxsize=queue_maxsize)`:
- Default 64.
- 128–256 for bursty fan-out.
- `<=0` disables backpressure (unbounded). Only use if you have your own flow control.

Signs of trouble:
- `emit()` appears to hang → downstream queue full (backpressure working as designed).
- Queue depth in `FlowEvent` trends up → consumer slower than producer; add workers or reduce fan-out.

## Operational defaults

- Bound queues. Don't disable backpressure unless you must.
- Bound per-trace fan-out. Don't emit thousands of messages for one trace without an aggregating `join_k`.
- Use envelope style for fan-out/fan-in — joins need `trace_id`.
- Set `Headers.tenant` so traces are tenant-scoped.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Queue deadlock / `emit()` hangs forever | Downstream stalled or fan-out > consumer capacity | Add workers, reduce fan-out, add timeouts |
| Memory growth | Unbounded queues or joins that never complete | Bound queues; ensure fan-out emits exactly `k` per trace |
| `join_k` never emits | Fan-out emitted < k items, or `trace_id` lost mid-graph | Verify fan-out count and that every hop preserves `trace_id` |
| Cross-trace mix in join output | `trace_id` reused across requests | Generate fresh `trace_id` per request |
| Lopsided latency under load | One node is the bottleneck | Look at `FlowEvent.queue_depth_out` per node; scale or split |

## Joining variable-count fan-outs

`join_k` requires a known `k`. If your fan-out count varies per trace, options:

1. Compute `k` in the fan-out node and emit it via `meta` (custom join node that reads `meta["expected_count"]`).
2. Use a sentinel "done" message and an accumulator node that watches for it.
3. Use a planner with parallel-and-joins (see [[penguiflow-reactplanner-config]]) instead of a static `join_k`.

## Observability

`FlowEvent` exposes:
- `queue_maxsize`, `queue_depth_in`, `queue_depth_out`
- `trace_pending`, `trace_inflight`
- Per-node lifecycle events

Alert on: queue depth trending up, retry/timeout bursts, joins with high `trace_pending` that never complete.

See [[penguiflow-observability]] for the full event catalog and exporter recipes.
