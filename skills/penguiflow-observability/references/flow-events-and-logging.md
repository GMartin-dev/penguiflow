# `FlowEvent` Catalog and Structured Logging

## `configure_logging(...)`

```python
from penguiflow import configure_logging

configure_logging(
    level="INFO",          # standard logging level
    structured=True,       # JSON lines; False = human-readable
    include_extras=False,  # when structured=False, append extra fields
)
```

Configures the `penguiflow` logger and its children. Subloggers:
- `penguiflow.core` — runtime worker loop, queue ops.
- `penguiflow.flow` — middleware output (when you use that logger name).
- `penguiflow.planner` — planner-internal.
- `penguiflow.tools` — ToolNode and external tool calls.

Call once at process startup. Re-calling resets handlers.

## `FlowEvent`

Actual frozen dataclass (`penguiflow.metrics.FlowEvent`):

```python
FlowEvent(
    event_type: str,                # see catalog below
    ts: float,
    node_name: str | None,
    node_id: str | None,
    trace_id: str | None,
    attempt: int,
    latency_ms: float | None,
    queue_depth_in: int,
    queue_depth_out: int,
    outgoing_edges: int,            # number of successor edges for this node
    queue_maxsize: int,             # `queue_maxsize` applied to this edge
    trace_pending: int | None,
    trace_inflight: int,
    trace_cancelled: bool,
    extra: Mapping[str, Any] = MappingProxyType({}),
)
```

`queue_depth_total` is a **computed property** (`queue_depth_in + queue_depth_out`), not a field. Constructor expects all fields except `extra` (which defaults).

Methods:
- `event.to_payload() -> dict` — JSON-friendly serialization for logging. Renames `event_type` to `event` and renames depth fields to `q_depth_in`/`q_depth_out`/`q_depth_total`. Merges `extra` flat into the payload.
- `event.metric_samples() -> dict[str, float]` — numeric samples for metrics (`queue_depth_in/out/total`, `attempt`, `trace_inflight`, `trace_pending`, `trace_cancelled` as 0/1, plus `latency_ms` when present; `extra["latency_ms"]` overrides if supplied).
- `event.tag_values() -> dict[str, str]` — bounded-cardinality tags starting with `event_type`.
- `event.error_payload` (property) — returns the structured `FlowError` payload when `extra["flow_error"]` is present.

## Event type catalog

### Node lifecycle

| Event | Phase | Latency? | Notes |
|---|---|---|---|
| `node_start` | Worker dequeued a message | No | Used for throughput counters. |
| `node_success` | Node returned normally | Yes | Most useful latency signal. |
| `node_error` | Node raised | Yes (for the attempt) | Retry may follow. |
| `node_timeout` | `timeout_s` fired | Yes (= timeout_s) | Retry may follow. |
| `node_retry` | Retry scheduled | No | `extra.backoff_s` carries the delay. |
| `node_failed` | Retries exhausted | Total elapsed | `extra.flow_error` carries the `FlowError` payload. |
| `node_trace_cancelled` | In-flight task cancelled | Yes (elapsed) | Trace cancellation propagated. |

### Trace lifecycle

| Event | Meaning |
|---|---|
| `trace_cancel_start` | `flow.cancel(trace_id)` invoked. |
| `trace_cancel_drop` | Runtime dropped a queued message for the trace. |
| `deadline_skip` | A message expired before its node could process it. |

### Other (extension points)
Custom middleware can attach additional events by emitting `FlowEvent` instances through the same hook. The runtime treats them as opaque — define your own `event_type` namespace.

## `log_flow_events(...)` middleware

Actual signature:
```python
log_flow_events(
    logger: logging.Logger | None = None,
    *,
    start_level: int = logging.INFO,
    success_level: int = logging.INFO,
    error_level: int = logging.ERROR,
    latency_callback: Callable[[str, float, FlowEvent], None] | None = None,
) -> Middleware
```

Returns a middleware that logs **only** `node_start`, `node_success`, and `node_error` events. All other event types (`node_timeout`, `node_retry`, `node_failed`, `deadline_skip`, `trace_cancel_*`, `node_trace_cancelled`, …) pass through silently — write a custom middleware if you need to log them. There is no `level` or `include_payload` knob; the payload always comes from `event.to_payload()` (plus `error_payload` injected for `node_error`).

```python
import logging
from penguiflow import log_flow_events

mw = log_flow_events(
    logging.getLogger("penguiflow.flow"),
    start_level=logging.DEBUG,
    success_level=logging.INFO,
    error_level=logging.ERROR,
    latency_callback=lambda name, ms, evt: histogram.observe(ms, tags=[f"event:{name}"]),
)
```

When `logger` is omitted, the middleware uses `logging.getLogger("penguiflow.flow")`. The optional `latency_callback` fires on `node_success`/`node_error` events with `latency_ms` present.

### Reducing volume

`node_start` and `node_success` dominate volume. Reduce by:
- Raising `start_level` so the start logs sit below your handler threshold.
- Wrapping `log_flow_events` in a custom middleware that drops or samples events before delegating.

```python
def filtered(logger):
    base = log_flow_events(logger)
    async def mw(event):
        if event.event_type == "node_start":
            return                                       # drop
        if event.event_type == "node_success" and random.random() > 0.1:
            return                                       # 10% sample
        await base(event)
    return mw
```

To log the events `log_flow_events` ignores (timeouts, retries, deadline skips, cancellations), add a sibling middleware that handles those types explicitly.

## Custom middleware

A middleware is any async callable accepting a `FlowEvent`:

```python
async def my_mw(event):
    if event.event_type == "node_failed":
        await alert_pager(event.to_payload())
```

Attach in `create(...)`:
```python
flow = create(..., middlewares=[log_flow_events(...), my_mw])
```

Or dynamically: `flow.add_middleware(mw)`.

Middleware runs sequentially in declaration order. **Don't do blocking I/O** — it stalls the worker loop. Use queue-based dispatch for external sinks.

## Trace correlation

Every `FlowEvent` with a `trace_id` is correlatable across:
- Logs (`extra.trace_id`).
- Metrics (don't tag by `trace_id`, but include in exemplars if your backend supports them).
- StateStore audit (`save_event` persists with `trace_id` indexed).

For end-to-end correlation:
- Set `trace_id` from your inbound HTTP/WS request id (or generate one and return it).
- Propagate via `Message.trace_id`.
- Include in your `meta` for outbound calls to other services.

## Redaction

PenguiFlow does **not** redact secrets. Common leak vectors:
- `repr(exc)` in `node_error` events contains URLs, headers, tokens.
- `Message.payload` if you log full events.
- `Message.meta` if it carries secrets.

Mitigation patterns:

### Middleware-level redaction
```python
SECRET_KEYS = {"authorization", "api_key", "token", "cookie"}

def redact(payload):
    if isinstance(payload, dict):
        return {k: ("[redacted]" if k.lower() in SECRET_KEYS else redact(v)) for k, v in payload.items()}
    if isinstance(payload, list):
        return [redact(x) for x in payload]
    return payload

async def redacting_mw(event):
    safe = redact(event.to_payload())
    logger.info("flow_event", extra=safe)
```

### At the boundary
- Tools should never put secrets in tool outputs.
- Don't `raise Exception(f"bearer {token} failed")` — sanitize messages.

## Operational defaults

- `configure_logging(structured=True)` in production.
- `log_flow_events` on every flow (one logger per service).
- Volume tuning: drop `node_start`, sample `node_success`, keep everything else.
- Tag logs with `service`, `env`, `worker_id`.
- Centralized log aggregation indexed by `trace_id`.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| No events | Middleware not attached | `create(..., middlewares=[log_flow_events(...)])` |
| Duplicate logs | Root logger + `configure_logging` both add handlers | One entrypoint per process |
| Missing `trace_id` | Payload-only flow | Envelope (`Message`) |
| Worker stall | Slow middleware | Don't do blocking I/O in middlewares; queue and dispatch |
| Secrets in logs | No redaction | Add redaction middleware |
| Logs too verbose | All events at INFO | Lower `node_start` / sample `node_success` |
