# FAST vs DEEP Rules

Guardrail rules fall into two cost classes. Misclassification causes latency outages (FAST rules that are actually slow) or false-allows (DEEP rules running too slowly to matter).

## FAST rules

Run **synchronously** on the request path. Must complete inside `sync_timeout_ms` (default 15ms).

Use for:
- Regex pattern matches (`re.search` over small text).
- Tool name allowlist/denylist checks (set lookup).
- Cheap structural validation (JSON schema validation of `tool_args`).
- Conversation-history flag checks (deterministic boolean logic).

Don't use for:
- LLM-as-judge (always too slow).
- Network calls (always too slow).
- Heavy regex on large text (`re.findall` over megabytes).
- DB lookups (latency unpredictable).

## DEEP rules

Run **asynchronously** via a `SteeringGuardInbox`. No latency budget on the request path. Result comes back later; the planner can:
- Continue executing and apply the decision retrospectively (REDACT, log, etc.).
- Block on the next planner step pending the deep verdict (configuration choice).

Use for:
- LLM-as-judge (slow but accurate).
- External classifier services.
- Vector similarity ("is this similar to known attack patterns?").
- Multi-step rule chains.

The catch: by the time the DEEP rule returns, the action it would have blocked may have already happened. DEEP rules are good for:
- Post-hoc audit (the deep verdict is logged, even if it didn't prevent the action).
- Conditional follow-up (the deep verdict triggers a remediation action).
- Async approval (deep rule's result is consumed at the next planner step).

## Classification rule of thumb

> If you can guarantee p95 < 5ms with your input distribution, it's FAST. Otherwise, DEEP.

Measure. Don't assume.

## Sync timeout and parallelism

```python
GatewayConfig(
    sync_timeout_ms=15,         # hard ceiling for sync rules
    sync_parallel=True,         # run sync rules concurrently
)
```

With `sync_parallel=True`, the gateway runs all FAST rules concurrently (via `asyncio.gather` with timeout). Total cost ≈ slowest rule, not sum. Default and recommended.

With `sync_parallel=False`, rules run serially. Total cost = sum. Slower but more predictable resource usage.

## `sync_fail_open` semantics

When a FAST rule times out:
- `sync_fail_open=True` → treat as ALLOW (availability over safety).
- `sync_fail_open=False` → block (typically treated as STOP-equivalent).

For an external-facing high-risk agent, `False` is the principled choice but creates an availability dependency: a slow guardrail timeout blocks legitimate traffic. For internal or low-risk agents, `True` is acceptable.

Mitigate the trade-off:
- Keep FAST rules truly fast (well under timeout).
- Use the gateway's monitoring to surface timeouts before they become outages.
- Have a "kill switch" rule list — a smaller set of FAST rules that you trust to always run under timeout, even when other rules misbehave.

## `async_fail_open`

When a DEEP rule's `SteeringGuardInbox` is unavailable (queue full, service down):
- `async_fail_open=True` → treat as ALLOW.
- `async_fail_open=False` → block (rare; only when the deep verdict is critical).

Typical setting: `True`. DEEP rules are usually advisory; if the deep service is down, allow but log loudly.

## `SteeringGuardInbox`

The async dispatcher for DEEP rules. Implementations:
- **In-memory queue + background tasks** — simplest, fine for single process.
- **Redis stream + worker pool** — scales, persists.
- **External service** — call out to a moderation API.

Contract (signatures, duck-typed):
- `submit(event: GuardrailEvent) -> str` — enqueue, return id.
- `poll(id: str) -> GuardrailDecision | None` — get result.
- `subscribe(id: str) -> awaitable` — wait for result.

The planner submits the event, continues executing, and consumes results at the next planning step. If the rule produces a STOP for an already-executed action, the planner emits a "violation detected post-execution" event (audit only).

## Mixing FAST and DEEP per concern

A common pattern: FAST regex check first; if uncertain, escalate to DEEP LLM judgment.

```python
class TwoStageRule:
    rule_id = "content_v1"
    REGEX = re.compile(...)

    def __init__(self, deep_judge):
        self.deep_judge = deep_judge

    async def evaluate_fast(self, event):
        if not self.REGEX.search(event.text_content or ""):
            return GuardrailDecision(action="ALLOW", rule_id=self.rule_id)
        # uncertain — escalate
        return None        # FAST returns None; gateway routes to DEEP

    async def evaluate_deep(self, event):
        verdict = await self.deep_judge.classify(event.text_content)
        if verdict.is_unsafe:
            return GuardrailDecision(action="STOP", rule_id=self.rule_id, ...)
        return GuardrailDecision(action="ALLOW", rule_id=self.rule_id)
```

The gateway invokes FAST first; if `None`, schedules DEEP via the inbox; combines results.

## Operational defaults

- 80%+ of rules should be FAST. DEEP is the exception.
- Set `sync_timeout_ms` to 2× your p95 sync rule latency, but cap at 30ms.
- Monitor sync rule timeouts in production; investigate any rule that exceeds 1% timeout rate.
- For high-traffic flows, batch DEEP rule invocations on the inbox side (call the moderation API with a batch of events, not one at a time).

## Common mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Slow FAST rule | p95 latency tanks | Move to DEEP or speed up the rule |
| `sync_fail_open=False` with flaky deep service | Rolling outages | Set to `True` for that deployment; rely on post-hoc audit |
| All rules DEEP | No actual blocking at request time | Audit which rules need to block; promote to FAST |
| FAST rule does network I/O | Random latency spikes | Move to DEEP |
| Heavy regex compiled per call | Latency degrades over time | Compile module-level |
