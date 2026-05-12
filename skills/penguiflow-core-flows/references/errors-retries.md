# Errors and Retries

PenguiFlow wraps node failures with trace and node metadata so callers, logs, and middlewares get a consistent error surface.

## Failure pipeline

When a node raises (or its `timeout_s` fires):
1. Runtime emits `node_error` (raise) or `node_timeout`.
2. If `attempt < max_retries`, emits `node_retry`, sleeps `min(backoff_base * backoff_mult ** attempt, max_backoff)`, re-invokes.
3. When retries exhaust, builds a `FlowError(trace_id, node_name, node_id, code, message, original_exc, ...)` and emits `node_failed`.

## `FlowError` and `FlowErrorCode`

```python
from penguiflow import FlowError, FlowErrorCode
```

`FlowError` fields (typical):
- `trace_id: str`
- `node_name: str`
- `node_id: str`
- `code: FlowErrorCode`
- `message: str`
- `original_exc: BaseException | None`
- attempt / latency metadata

`FlowErrorCode` includes (non-exhaustive):
- `NODE_EXCEPTION` — node body raised.
- `NODE_TIMEOUT` — `timeout_s` exceeded.

Other codes exist for cancellation, validation, etc. — inspect a returned `FlowError` to see its code at runtime.

## Surfacing errors to callers

By default, `FlowError` is observable via `FlowEvent.node_failed` (and `state_store.save_event` if configured) but `fetch()` does not see it.

To make `FlowError` an "output":

```python
flow = create(..., emit_errors_to_rookery=True)
```

Now `await flow.fetch()` (or `fetch(trace_id=...)`) can return a `FlowError`. Useful when:
- You want callers to handle errors uniformly with results.
- You want a single egress path (`FlowError` or `FinalAnswer`).

Don't enable this if `FlowError.message` could leak sensitive details to callers — redact at boundary or keep errors event-only.

## Idempotency and retry safety

Retries are only safe if your node is idempotent. If it isn't, you'll double-charge cards, double-send emails, double-write data.

Strategies:
1. **Idempotency keys.** Use `trace_id` (or `trace_id + node_name`) as the request id when calling external services. Most APIs (Stripe, AWS, SQS) support an idempotency header.
2. **Split plan and commit.** "Plan" node prepares the action; "commit" node executes it. Gate commit behind HITL or explicit policy. See [[penguiflow-hitl-pause-resume]].
3. **Don't retry classification errors.** Retrying a `ValidationError` won't help. Catch in the node body and emit a structured error message instead of raising.
4. **Bound timeouts.** Short `timeout_s` lets you cancel quickly, reducing the window in which retries pile up.

## NodePolicy retry knobs

| Field | Default | Effect |
|---|---|---|
| `max_retries` | `0` | Total attempts = `max_retries + 1`. `0` = no retry. |
| `backoff_base` | `0.5` | First backoff seconds. |
| `backoff_mult` | `2.0` | Multiplier per attempt. |
| `max_backoff` | `30.0` | Ceiling. |

A `max_retries=3, backoff_base=0.5, backoff_mult=2.0` schedule sleeps: 0.5s, 1s, 2s, 4s before giving up.

## Operational defaults

- Network-bound nodes: `timeout_s` always; `max_retries=1-3`; `backoff_base=0.5`.
- CPU-bound nodes: no retries (failures are deterministic); short `timeout_s`.
- Validation nodes: no retries; bubble errors.
- External commit nodes: `max_retries=0` unless you have an idempotency key.

## What happens when retries exhaust

Runtime:
- Emits `node_failed` with the `FlowError`.
- If `emit_errors_to_rookery=True`, routes the `FlowError` to Rookery.
- If `state_store` is configured, calls `save_event` for `node_failed`.
- The trace continues for any unrelated branches; the failed branch terminates.

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| Retries don't happen | `max_retries=0` (default), or node returns instead of raising | Set `max_retries > 0`; raise to signal failure |
| Everything times out | `timeout_s` too low or blocking I/O | Tune timeout; switch to async I/O |
| Duplicate side effects after retries | Non-idempotent node | Idempotency key, plan/commit split, or `max_retries=0` |
| Errors don't reach callers | `emit_errors_to_rookery=False` (default) | Enable it, then redact `FlowError` if needed |
| Secrets leak into logs | `repr(exc)` includes URLs, headers, tokens | Catch in node, raise a sanitized exception, redact in middleware |

## Runnable example: retry then succeed

```python
from penguiflow import Node, NodePolicy, create
import asyncio

class Flaky:
    def __init__(self): self.calls = 0
    async def __call__(self, msg, _ctx):
        self.calls += 1
        if self.calls < 3:
            raise RuntimeError("transient failure")
        return 42

flaky = Flaky()
node = Node(flaky, name="flaky", policy=NodePolicy(
    validate="none", max_retries=3, backoff_base=0.01,
))
flow = create(node.to())
flow.run()

await flow.emit({"x": 1})
print(await flow.fetch())   # 42, after 2 retries
await flow.stop()
```

## See also

- [[penguiflow-observability]] — `FlowEvent` catalog, structured logging, MLflow exporter.
- [[penguiflow-statestore]] — persisting `node_failed` and `FlowError` for audit.
- [[penguiflow-hitl-pause-resume]] — gating commit steps behind human review.
