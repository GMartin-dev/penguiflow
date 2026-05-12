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

```python
FlowEvent(
    event_type: str,             # see catalog below
    trace_id: str | None,
    node_id: str | None,
    node_name: str | None,
    queue_depth_in: int | None,
    queue_depth_out: int | None,
    queue_depth_total: int | None,
    trace_pending: int | None,
    trace_inflight: int | None,
    queue_maxsize: int | None,
    latency_ms: float | None,
    attempt: int | None,
    extra: dict,
)
```

Methods:
- `event.to_payload() -> dict` — JSON-friendly serialization for logging.
- `event.metric_samples() -> dict[str, float]` — numeric samples for metrics.
- `event.tag_values() -> dict[str, str]` — bounded-cardinality tags.

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

```python
import logging
from penguiflow import log_flow_events

mw = log_flow_events(
    logger=logging.getLogger("penguiflow.flow"),
    level=logging.INFO,
    include_payload=True,    # full event.to_payload() in extra
)
```

Defaults log each event at the configured level with structured `extra={...}`. Tweak the logger name to route events to a dedicated handler / sink.

### Reducing volume

`node_start` and `node_success` dominate volume. Reduce by:
- Lowering log level for these events specifically (custom middleware filter).
- Sampling — log every Nth event.
- Filtering by `node_name` if some nodes are noisy.

```python
def filtered(logger):
    base = log_flow_events(logger)
    async def mw(event):
        if event.event_type in {"node_start"}: return   # drop
        if event.event_type == "node_success" and random.random() > 0.1: return
        await base(event)
    return mw
```

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
