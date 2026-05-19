# Durability and `SupportsPlannerState`

## Why durability matters

Without a `StateStore`, pause records live in the planner instance. A worker restart between pause and resume drops them — `planner.resume(...)` raises `KeyError`.

Single-process scripts and demos can skip durability. Anything else (multi-worker, autoscaling, restart-prone) must wire it.

## The capability

`StateStore` implementations that want to support planner pauses expose two methods:

```python
class SupportsPlannerState(Protocol):
    async def save_planner_state(self, token: str, payload: dict) -> None: ...
    async def load_planner_state(self, token: str) -> dict | None: ...
```

When the planner has `state_store=...` and pauses:
1. Records the pause in-memory.
2. Calls `save_planner_state(resume_token, payload_dict)` — best-effort. Errors are logged and ignored (in-memory still works).
3. On `resume(resume_token)`, looks in-memory first, then `load_planner_state(resume_token)`.

The payload dict contains:
- `reason`
- `payload`
- captured `tool_context` (JSON-serializable parts only)
- planner trajectory checkpoint
- any other internal state needed to replay

You don't need to know the exact shape — just store and return it as-is.

## Backends

### In-memory (default)
Used when no `state_store` is configured. Adequate for single-process scripts.

### Redis
```python
class RedisPlannerStore:
    def __init__(self, redis, ttl_s=3600):
        self.redis = redis
        self.ttl = ttl_s

    async def save_planner_state(self, token: str, payload: dict) -> None:
        await self.redis.set(
            f"planner:pause:{token}",
            json.dumps(payload),
            ex=self.ttl,
        )

    async def load_planner_state(self, token: str) -> dict | None:
        raw = await self.redis.get(f"planner:pause:{token}")
        if raw is None:
            return None
        # consume-on-load: delete after retrieval (one-time use)
        await self.redis.delete(f"planner:pause:{token}")
        return json.loads(raw)
```

Recommended TTL: ≥ user attention span for the pause (1-24 hours for approvals, 5-15 minutes for OAuth).

### Database
Same shape, persisted to a `(token, payload_json, created_at)` table. Add an index on `created_at` for periodic cleanup.

### Custom
Implement the protocol on the same `StateStore` you use for events, memory, and bindings. One store, many capabilities — see [[penguiflow-statestore]].

## Consume-on-load semantics

The planner does **not** enforce one-time use. If your `load_planner_state` returns the same payload twice, the planner will happily resume twice — duplicating side effects.

Implement consume-on-load in your store:

```python
async def load_planner_state(self, token: str) -> dict | None:
    payload = await self.kv.get(f"planner:pause:{token}")
    if payload is None:
        return None
    await self.kv.delete(f"planner:pause:{token}")    # delete on read
    return payload
```

Trade-off: if the resume call fails after `load_planner_state` succeeds, the pause is lost. For idempotency-critical flows, use a transactional pattern (read → mark as in-flight → process → mark as consumed).

## TTL

Pause records should expire. Reasons:
- Storage hygiene (orphaned pauses accumulate).
- Security (stale `resume_token`s become attack surface).
- UX (a stale pause shouldn't suddenly resume after the user forgot about it).

Suggested TTLs by reason:
- `approval_required`: 30 min - 24 h.
- `await_input`: 10 - 60 min.
- `external_event` (OAuth): 5 - 15 min (provider state usually expires within 10 min).
- `constraints_conflict`: 30 min - 24 h.

Enforce TTL at the storage layer (`SETEX` in Redis, `TTL` columns in DB) — the planner has no notion of TTL.

## Token security

`resume_token`s are UUIDs by default (unguessable). But:

- **Don't log them.** They appear in error traces, structured logs, debug output. Filter.
- **Don't expose them to the wrong tenant.** If a UI shows a "pending pauses" list, scope by tenant.
- **Don't reuse storage namespaces across tenants.** Prefix StateStore keys with `tenant:<id>:` if your store is shared.
- **Don't put them in URLs without auth.** A token in a URL leaks via referrer/log.

For high-security flows, layer additional checks at the host app:
- Verify the resumer is the same user as the pauser.
- Require a fresh auth cookie at resume time.
- Rate-limit resume attempts per token.

## Cross-worker recipes

### Stateless workers + shared Redis
```python
planner = ReactPlanner(
    llm="gpt-4o-mini",
    catalog=catalog,
    state_store=SharedStateStore(redis_client),
)
```
Any worker can resume any pause. Memory tolerates last-write-wins. Standard SaaS pattern.

### Sticky sessions (suboptimal)
Route resume calls back to the same worker that issued the pause. Avoid — it complicates deployments and doesn't survive worker death.

### Distributed lock for resume
For idempotency-critical resumes, take a per-token lock at resume start:

```python
async def resume_with_lock(planner, token, **kwargs):
    async with redis_lock(f"resume:{token}", ttl=60):
        return await planner.resume(token, **kwargs)
```

Two simultaneous resume calls for the same token now serialize.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `KeyError` on resume after restart | No `state_store` or it doesn't implement the protocol | Configure store; implement `save_planner_state`/`load_planner_state` |
| Pause persists but resume `KeyError` | Token expired before resume attempt | Lengthen TTL or surface "expired" UX |
| Same pause resumes twice | No consume-on-load | Delete on read in `load_planner_state` |
| Token in error log | Log filter missing | Redact `resume_token` in log formatters |
| Cross-tenant resume succeeds | StateStore keys not tenant-scoped | Prefix keys with `tenant:<id>:` |
| Save fails silently | `state_store` exception | Check store logs; pause still works in-memory but won't survive restart |

## Observability

Track:
- Pause count by `reason` and tool.
- Time-to-resume p50/p95/p99 by reason.
- Resume failure rate (`KeyError` vs other).
- Store save/load failure rate.
- Token TTL expiry rate (pauses that timed out without resume).

A spike in token-expiry rate suggests the UI flow is too slow or the TTL is too short.
