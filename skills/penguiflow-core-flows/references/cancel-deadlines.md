# Cancellation and Deadlines

Both apply to **envelope flows only** — payload-only messages have no `trace_id` or `deadline_s`.

## `flow.cancel(trace_id)`

```python
cancelled: bool = await flow.cancel(trace_id)
```

Returns `True` if the trace was active and cancellation was triggered; `False` if not found or already complete.

Best-effort semantics. The runtime:
1. Sets a per-trace cancellation event.
2. Drops queued messages for that trace from all edge queues and fetch queues.
3. Cancels in-flight node invocation tasks that are processing messages for that trace.

What `cancel(...)` does **not** do:
- Doesn't kill blocking I/O. If a node is in `socket.recv()` without `asyncio.wait_for(...)` or async I/O, it can't be interrupted.
- Doesn't emit a "cancelled final answer" to Rookery. If you want users to see a "cancelled" message, your egress node should detect the cancellation event and emit one.
- Doesn't auto-cancel external work (background HTTP calls, subprocesses, threadpools) you launched from a node. You wire that yourself.

## Internal types

- `TraceCancelled` — sentinel exception raised inside cancelled work paths.
- `asyncio.CancelledError` — what the runtime raises into node tasks.

You rarely catch these. Write cancellation-friendly node code instead.

## Cancellation-friendly nodes

- Use async SDKs (httpx, aiohttp, async DB drivers).
- Wrap external calls with `asyncio.wait_for(..., timeout=...)` or pass `timeout_s` via `NodePolicy`.
- Don't swallow `asyncio.CancelledError`. If you must clean up, re-raise after.
- Check cancellation explicitly for long-running compute:

```python
async def compute(msg, ctx):
    for chunk in chunks:
        # cooperative checkpoint
        if msg.headers and getattr(msg, "deadline_s", None) and time.time() > msg.deadline_s:
            break
        process(chunk)
        await asyncio.sleep(0)   # yield to the event loop
```

## Deadlines

Set `Message(deadline_s=...)` on the envelope. Wall-clock seconds since epoch is **not** the convention here — `deadline_s` is provided as an absolute or relative value the runtime evaluates when the message reaches a node.

Behavior when a node receives an expired message:
1. Node body is **skipped** (not invoked).
2. Runtime emits a `deadline_skip` event.
3. For `Message` envelopes, runtime emits `FinalAnswer(text="Deadline exceeded")` to Rookery so callers see a result.

Propagating deadlines downstream:

```python
async def step(msg: Message, ctx) -> Message:
    result = await do_work(msg.payload)
    # New message inherits deadline_s by passing model_copy(update={...})
    return msg.model_copy(update={"payload": result})
```

If you create a *fresh* `Message(...)` instead of `model_copy(update=...)`, the deadline is dropped.

## Operational defaults

- Always attach `trace_id` (use `Message` envelope) for request-scoped work you might cancel.
- Treat `trace_id` as an authorization surface — a user must not cancel another user's trace. Authz at the API layer that calls `flow.cancel(...)`.
- Set deadlines deliberately. Default no-deadline is fine; budget-bound user-facing requests.
- Make external tasks trace-aware (pass `trace_id` to them and have them check `flow.cancellation_event(trace_id)` or similar).

## Observability

`FlowEvent` types you care about:
- `trace_cancel_start` — `cancel(trace_id)` invoked.
- `trace_cancel_drop` — runtime dropped queued messages for the trace.
- `node_trace_cancelled` — an in-flight node was cancelled.
- `deadline_skip` — a node skipped an expired message.

Alerting ideas:
- Rising cancellation rate → users abandoning requests, UX problem.
- High `trace_pending` traces that get cancelled → backpressure problem; tune queues or scale.
- High `deadline_skip` rate → deadlines too tight or a slow dependency.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `cancel(trace_id)` returns `False` | Trace doesn't exist or already completed | Cancel the same id you emitted; check if trace finished naturally |
| Work continues after cancel | Blocking I/O, swallowed `CancelledError`, or external task not wired to cancellation | Use async I/O, don't swallow, wire external tasks |
| Deadline ignored | Downstream message constructed fresh (not `model_copy`d) | Propagate `deadline_s` via `model_copy(update=...)` |
| User never sees "cancelled" UX | Runtime drops messages but doesn't emit a final | Have an egress node detect cancellation and emit a custom `FinalAnswer` |
| Cancel slow | Many in-flight nodes with no timeouts | Set `NodePolicy.timeout_s`; keep work units small |

## Runnable example: best-effort cancel

```python
from penguiflow import Headers, Message, Node, NodePolicy, create
import asyncio

async def slow(msg: Message, _ctx):
    await asyncio.sleep(10.0)

slow_node = Node(slow, name="slow", policy=NodePolicy(validate="none"))
flow = create(slow_node.to())
flow.run()

msg = Message(payload={"work": "x"}, headers=Headers(tenant="demo"))
await flow.emit(msg, trace_id=msg.trace_id)
print("cancelled:", await flow.cancel(msg.trace_id))

try:
    await asyncio.wait_for(flow.fetch(trace_id=msg.trace_id), timeout=0.2)
except asyncio.TimeoutError:
    print("no result (expected)")

await flow.stop()
```
