# Metrics, Dashboards, and Alerts

PenguiFlow doesn't ship a metrics exporter — you derive metrics from `FlowEvent` via a middleware. This reference covers the recommended baseline.

## Cardinality rules (read first)

**Never** tag metrics by `trace_id`. It's unbounded.

**Be cautious** with:
- `tenant` — only if tenant count is bounded and small.
- `error_class` — only if exception types are a closed set.
- `user_id` — never.

**Safe** tags:
- `event_type`
- `node_name` (bounded by graph definition)
- `env`, `service`, `worker_id` (platform labels)
- Outcome (`success` / `error` / `timeout`)

Tag explosion is the #1 way to OOM your metrics backend. Move correlation to logs, not metrics.

## Counters

| Counter | From event | Tags |
|---|---|---|
| `pf_node_success_total` | `node_success` | `node_name` |
| `pf_node_error_total` | `node_error` | `node_name` |
| `pf_node_timeout_total` | `node_timeout` | `node_name` |
| `pf_node_retry_total` | `node_retry` | `node_name`, `attempt` (bounded) |
| `pf_node_failed_total` | `node_failed` | `node_name`, `code` (from FlowErrorCode — bounded) |
| `pf_trace_cancel_start_total` | `trace_cancel_start` | — |
| `pf_trace_cancel_drop_total` | `trace_cancel_drop` | `node_name` |
| `pf_deadline_skip_total` | `deadline_skip` | `node_name` |

## Histograms

| Histogram | Source | Tags | Buckets |
|---|---|---|---|
| `pf_node_latency_ms` | `node_success`/`error`/`timeout` `latency_ms` | `node_name`, `outcome` | `5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000` |
| `pf_retry_backoff_ms` | `node_retry` `extra.backoff_s * 1000` | `node_name` | `100, 500, 1000, 5000, 30000` |

## Gauges

| Gauge | Source |
|---|---|
| `pf_queue_depth_in` | `FlowEvent.queue_depth_in` |
| `pf_queue_depth_out` | `FlowEvent.queue_depth_out` |
| `pf_queue_depth_total` | `FlowEvent.queue_depth_total` |
| `pf_trace_inflight` | `FlowEvent.trace_inflight` (envelope flows only) |
| `pf_trace_pending` | `FlowEvent.trace_pending` |

Gauges are sampled on every event — high frequency. Apply per-scrape aggregation in your backend.

## Prometheus middleware recipe

```python
from prometheus_client import Counter, Histogram, Gauge

NODE_SUCCESS = Counter("pf_node_success_total", "Successes", ["node"])
NODE_ERROR = Counter("pf_node_error_total", "Errors", ["node"])
NODE_LATENCY = Histogram(
    "pf_node_latency_ms", "Node latency", ["node", "outcome"],
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
)
QUEUE_DEPTH_TOTAL = Gauge("pf_queue_depth_total", "Total queue depth")

async def prom_mw(event):
    name = event.node_name or "unknown"
    if event.event_type == "node_success":
        NODE_SUCCESS.labels(node=name).inc()
        if event.latency_ms is not None:
            NODE_LATENCY.labels(node=name, outcome="success").observe(event.latency_ms)
    elif event.event_type == "node_error":
        NODE_ERROR.labels(node=name).inc()
        if event.latency_ms is not None:
            NODE_LATENCY.labels(node=name, outcome="error").observe(event.latency_ms)
    # ... etc

    if event.queue_depth_total is not None:
        QUEUE_DEPTH_TOTAL.set(event.queue_depth_total)
```

Attach: `flow = create(..., middlewares=[log_flow_events(...), prom_mw])`.

## StatsD / DogStatsD recipe

```python
from statsd import StatsClient
client = StatsClient(host="localhost", port=8125, prefix="pf")

async def statsd_mw(event):
    name = event.node_name or "unknown"
    tags = [f"node:{name}", f"event:{event.event_type}"]

    if event.event_type == "node_success":
        client.incr("node_success", tags=tags)
        if event.latency_ms is not None:
            client.timing("node_latency_ms", event.latency_ms, tags=tags + ["outcome:success"])
    # ... etc
```

DogStatsD supports tags natively; vanilla StatsD does not — embed tags in metric names if you must.

## OpenTelemetry recipe

```python
from opentelemetry import metrics
meter = metrics.get_meter("penguiflow")

node_latency = meter.create_histogram("pf.node.latency_ms", "ms")
node_errors = meter.create_counter("pf.node.errors")

async def otel_mw(event):
    attrs = {"node": event.node_name or "unknown", "event": event.event_type}
    if event.event_type == "node_success" and event.latency_ms is not None:
        node_latency.record(event.latency_ms, {**attrs, "outcome": "success"})
    elif event.event_type == "node_error":
        node_errors.add(1, attrs)
```

## Dashboards (recommended)

### 1. System overview
- Throughput (requests/s) — derived from `pf_node_success_total` rate at ingress nodes.
- Error rate — `(error + timeout + failed) / total`.
- p50/p95/p99 latency per top-N nodes.

### 2. Saturation
- Queue depth over time (total).
- Queue depth by node (if you split the gauge per `node_name`).
- Trace inflight count.

### 3. Reliability
- Retries over time.
- `node_failed` counts (terminal failures).
- Backoff distribution.

### 4. Control plane
- Cancellations / sec.
- `deadline_skip` / sec.

## Alerts (starter set)

| Alert | Condition | Suggested threshold |
|---|---|---|
| Error spike | `rate(pf_node_error_total[5m]) / rate(pf_node_success_total[5m]) > X` | >5% sustained 5m |
| Timeout spike | `rate(pf_node_timeout_total[5m]) > X` | Above baseline + 3σ |
| Retry storm | `rate(pf_node_retry_total[5m]) > X` | >50% of success rate |
| Queue saturation | `pf_queue_depth_total > 0.8 * queue_maxsize` for 2m | Backpressure imminent |
| Terminal failure rate | `rate(pf_node_failed_total[15m]) > X` | Above ops tolerance |
| Cancellation surge | `rate(pf_trace_cancel_start_total[5m]) > baseline * 3` | Users abandoning |
| Deadline skip surge | `rate(pf_deadline_skip_total[5m]) > X` | Deadlines too tight or slow dependency |

Always combine with SLO-burn alerting for the user-visible SLIs (overall request success rate, end-to-end latency p95).

## Common mistakes

- Tagging by `trace_id`. Cardinality explosion.
- Tagging by `error_class` for unbounded exception types (e.g., `KeyError("...")` strings).
- Not separating outcome dimensions on latency histograms — success and error mix.
- Alerting on absolute counts instead of rates — false alerts at low traffic.
- No alert on `node_failed` — terminal failures slip through.

## Cost shape

- Counters: cheap.
- Histograms: moderately expensive (proportional to bucket count × cardinality).
- Gauges: cheap to write, can be costly if backed by high-frequency scrapes.

Tune histogram buckets to your latency distribution. Default buckets above are reasonable for typical agent workloads (5ms - 10s).
