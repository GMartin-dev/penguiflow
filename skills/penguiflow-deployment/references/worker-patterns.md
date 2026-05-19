# Worker Patterns

## Pattern: stateless-per-job (recommended default)

One flow per job. Worker loop:

```python
async def worker_loop(queue, registry):
    while not shutting_down:
        job = await queue.fetch()
        if job is None:
            continue

        flow = create(my_topology, queue_maxsize=64, middlewares=[log_flow_events(...)])
        flow.run(registry=registry)
        try:
            msg = Message(
                payload=job.payload,
                headers=Headers(tenant=job.tenant),
                deadline_s=job.deadline,
            )
            await flow.emit(msg, trace_id=f"job-{job.id}")
            try:
                result = await asyncio.wait_for(
                    flow.fetch(trace_id=f"job-{job.id}"),
                    timeout=job.deadline,
                )
                await queue.mark_complete(job.id, result=result)
            except asyncio.TimeoutError:
                await flow.cancel(f"job-{job.id}")
                await queue.retry_job(job.id, delay_s=60, reason="deadline_exceeded")
        finally:
            await flow.stop()
```

Pros:
- Best isolation between jobs.
- Easy to reason about.
- No shared state between jobs.
- Per-job tenant scope is clean.

Cons:
- Flow construction cost per job (typically small).
- More state-store reconnects (mitigate with connection pooling).

## Pattern: shared-flow (lower overhead)

One flow, many traces. Worker loop:

```python
flow = create(my_topology, ...)
flow.run(registry=registry)

try:
    while not shutting_down:
        job = await queue.fetch()
        msg = Message(payload=job.payload, headers=Headers(tenant=job.tenant))
        await flow.emit(msg, trace_id=f"job-{job.id}")
        result = await flow.fetch(trace_id=f"job-{job.id}")
        await queue.mark_complete(job.id, result=result)
finally:
    await flow.stop()
```

Pros:
- Lower per-job cost.
- Better connection pooling.

Cons:
- Shared state risks (closures, module-level vars).
- Failure isolation weaker — one bad node can stall workers serving other traces.
- Tenant data lives concurrently inside one process; rely strictly on `Headers.tenant` + `trace_id` scoping.

Use when:
- High job rate (>100/s) where per-job flow construction dominates.
- Stateless nodes that don't share resources.

## Queue contracts

Whatever queue you use (Redis, SQS, Kafka, DB-backed), the worker integration is easiest when the queue exposes:

```python
class Queue(Protocol):
    async def fetch(self, *, timeout_s: float | None) -> Job | None: ...
    async def mark_complete(self, job_id: str, *, result: Any) -> None: ...
    async def retry_job(self, job_id: str, *, delay_s: float, reason: str) -> None: ...
    async def mark_failed(self, job_id: str, *, reason: str, details: dict | None) -> None: ...
```

Required invariants:
- **At-least-once delivery**. Build for idempotent flows.
- **Visibility timeout** (or equivalent) larger than your worst-case flow latency, so the queue doesn't redeliver mid-execution.
- **Dead-letter** for terminally failed jobs.

## Per-job correlation

Use `trace_id = f"job-{job.id}"` (or a UUID). Benefits:
- Logs are searchable by `trace_id`.
- `flow.fetch(trace_id=...)` is deterministic.
- `flow.cancel(trace_id)` works for timeouts.
- StateStore audit per job.

Don't reuse `trace_id`s. Don't reuse `trace_id` for retries — generate a fresh one (with a parent reference in `meta` if you need linkage).

## Idempotency

At-least-once delivery + retries = side effects can repeat. Strategies:

### Idempotency keys
Use `trace_id` as the key when calling external services. Most APIs (Stripe, SQS, AWS Lambda) support this natively.

### Plan/commit split
- **Plan node** — pure; produces an action description.
- **Commit node** — gated by an idempotency check (or HITL).

The commit node looks up the plan id; if already committed, returns the cached result.

### Atomic write-and-mark
For DB writes:
```python
async def commit(msg, _ctx):
    async with db.transaction():
        if await already_committed(msg.trace_id):
            return await fetch_existing(msg.trace_id)
        result = do_write(msg.payload)
        await mark_committed(msg.trace_id, result)
        return result
```

Single transaction prevents double-commit.

## Time budgets

Two layers:
1. **Per-node**: `NodePolicy(timeout_s=...)`. Tight (seconds).
2. **End-to-end**: `Message(deadline_s=...)` + `asyncio.wait_for(flow.fetch(...), timeout=job.deadline)`. Loose (tens of seconds to minutes).

When the end-to-end deadline expires:
1. `await flow.cancel(trace_id)`.
2. Mark the job for retry (transient) or dead-letter (terminal).

## Graceful shutdown

Subscribe to `SIGTERM` (or your platform's shutdown signal):

```python
shutting_down = asyncio.Event()

def _on_sigterm(sig, frame):
    shutting_down.set()

signal.signal(signal.SIGTERM, _on_sigterm)
```

Then in the worker loop:
1. `if shutting_down.is_set(): break` between job fetches.
2. After loop: drain in-flight tasks with a deadline.
3. Cancel any over-deadline traces: `await flow.cancel(trace_id)`.
4. `await flow.stop()`.

Most platforms send SIGTERM, wait 30-60s, then SIGKILL. Tune your drain deadline to fit within that window with safety margin.

```python
async def graceful_shutdown(flow, inflight_traces, deadline_s=30):
    end = time.monotonic() + deadline_s
    while inflight_traces and time.monotonic() < end:
        await asyncio.sleep(0.1)
    for trace_id in list(inflight_traces):
        await flow.cancel(trace_id)
    await flow.stop()
```

## Tenant isolation

For multi-tenant workers:
- `Headers.tenant` set on every emit.
- Per-tenant rate-limiting at the queue level (one tenant doesn't starve others).
- Tenant-scoped credentials (don't share API tokens across tenants).
- Tenant-scoped state-store keys (`tenant:<id>:...`).
- Tenant-scoped artifact retrieval (authz on the retrieval endpoint).

A single worker can serve many tenants safely if isolation is enforced at every layer. Resist "one worker per tenant" unless tenants have very different resource profiles or compliance requirements.

## Health checks

Expose `/health` (or your platform's equivalent) returning:
- 200 while the worker is processing.
- 503 once `shutting_down` is set (lets the load balancer drain).
- 503 if a critical dependency (queue, state store) is unreachable.

Don't include "expensive" probes (`SELECT 1` against the DB on every health check) unless your platform's health-check rate is bounded.

## Anti-patterns

- **Module-level shared mutable state in nodes** — race conditions under concurrency.
- **No `flow.stop()` on shutdown** — orphaned tasks, leaked queues, dropped jobs.
- **Synchronous I/O inside a node body** — blocks the event loop.
- **Per-job DB connection** — connection churn; use a pool.
- **Returning huge results from worker** — push to artifact store; return a ref.
- **`asyncio.create_task` without tracking** — fire-and-forget that you can't wait for or cancel.
