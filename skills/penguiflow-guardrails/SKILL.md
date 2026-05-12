---
name: penguiflow-guardrails
description: Add a policy-enforcement layer to a PenguiFlow ReactPlanner — pass a `guardrail_gateway` that inspects `llm_before`/`tool_call_start`/`tool_call_result`/`llm_stream_chunk` events and returns `GuardrailDecision`s with actions `STOP`/`PAUSE`/`RETRY`/`REDACT`/`ALLOW`, run in `shadow` or `enforce` mode, classify rules as FAST (sync, `sync_timeout_ms`) or DEEP (async, `SteeringGuardInbox`), choose `sync_fail_open` vs safety, and configure the separate `ObservationGuardrailConfig` reliability clamp that prevents context overflow on huge tool outputs. Use when a user says "add guardrails", "PII redaction", "tool denylist", "policy enforcement", "stop unsafe tool", "fail-closed", or names `GuardrailEvent`, `GuardrailDecision`, `GatewayConfig`.
---

# PenguiFlow Guardrails

## When to use
- Enforce tool allowlists/denylists at runtime, outside the prompt.
- Redact secrets/PII from streamed output and tool results.
- Fail-closed gating ("STOP if the tool would touch prod").
- HITL approval triggered by policy (`PAUSE`).
- Audit-friendly policy with stable `rule_id`s.
- Reliability safety net for oversized tool observations.

## When NOT to use
- Primary access control → use `ToolPolicy` / `ToolVisibilityPolicy` (see [[penguiflow-reactplanner-config]]). Guardrails are policy *enforcement*, not the access boundary.
- Sandboxing untrusted code → use a real sandbox; guardrails are a control plane.
- Content moderation product — guardrails are intentionally small and composable.
- HITL UX patterns → use [[penguiflow-hitl-pause-resume]] (guardrails can *trigger* a pause; the resume contract lives there).

## Hard boundaries
Guardrails are the **policy enforcement layer**. They don't replace tool visibility, ToolNode hardening, or sandboxing. The reliability `ObservationGuardrailConfig` is bundled here but is conceptually separate — it's a context-overflow safety net, not a policy decision.

## Workflow

### 1) Wire a gateway
Guardrails are off by default. Enable by passing a `guardrail_gateway`:
```python
from penguiflow.planner import ReactPlanner
from penguiflow.planner.guardrails import GuardrailGateway, GatewayConfig

gateway = GuardrailGateway(
    config=GatewayConfig(
        mode="shadow",                  # shadow | enforce
        sync_timeout_ms=15,
        sync_parallel=True,
        sync_fail_open=False,           # safety > availability
        async_fail_open=True,           # availability for deep rules
    ),
    rules=[...],                        # see step 3
)

planner = ReactPlanner(
    ...,
    guardrail_gateway=gateway,
    guardrail_conversation_history_turns=3,   # optional context
)
```

### 2) Start in `shadow`, then move to `enforce`
- `mode="shadow"` — evaluate and log decisions; do not block.
- `mode="enforce"` — apply STOP/PAUSE/RETRY/REDACT.

Shadow mode is the standard rollout path. Measure rule hit rates and false positives, fix the rules, then enforce.

### 3) Define rules — FAST or DEEP
Each rule classifies as:
- **FAST** — synchronous, runs on the request path. Bounded by `sync_timeout_ms` (default 15ms). Use for regex/allowlist checks and deterministic logic.
- **DEEP** — asynchronous, runs via a `SteeringGuardInbox`. No latency budget on the request path. Use for LLM-as-judge, network checks, slow external services.

Always-cheap rules: tool allowlists, regex secret patterns, structural validation. Push everything else to DEEP.

### 4) Pick decision actions
Rules return `GuardrailDecision`:
- `ALLOW` — pass through.
- `REDACT` — apply `redactions: tuple[RedactionSpec, ...]` to text content.
- `RETRY` — re-run the LLM call with `corrective_message` (`RetrySpec`).
- `PAUSE` — pause planner with `reason="approval_required"` (`PauseSpec` carries scope, approver roles, prompt, timeout).
- `STOP` — terminate with `StopSpec(error_code, user_message, internal_reason)`.

`STOP` is the fail-closed default for high-risk findings. `REDACT` for secrets. `PAUSE` for "human must approve". `RETRY` for fixable LLM mistakes.

### 5) Decide fail-open vs fail-closed semantics
| Knob | Effect | Trade-off |
|---|---|---|
| `sync_fail_open=True` | Sync rule timeout → ALLOW | Availability favored; policy bypass on timeout |
| `sync_fail_open=False` (recommended for high-risk) | Sync rule timeout → blocks (treated as STOP-like) | Safety favored; possible latency outages |
| `async_fail_open=True` (typical) | Async rule unavailable → ALLOW | Deep checks are advisory |
| `async_fail_open=False` | Async rule unavailable → block | Hard dependency on the deep service |

For internal-only or low-risk agents, `True` everywhere is fine. For external-facing high-risk agents, set `sync_fail_open=False`.

### 6) Tap the right event surfaces
| `event_type` | When it fires | What you can do |
|---|---|---|
| `llm_before` | Before the LLM plans | Inspect user input; STOP / RETRY / PAUSE / REDACT prompt content |
| `tool_call_start` | Before a tool runs | Inspect `tool_name`, `tool_args`; STOP / PAUSE / RETRY |
| `tool_call_result` | After a tool returns | Inspect result; REDACT secrets, STOP if disallowed content surfaced |
| `llm_stream_chunk` | Each streamed text chunk | REDACT secrets mid-stream |

Each `GuardrailEvent` exposes `text_content`, `tool_name`, `tool_args`, and `payload` (tool_call_id, action_seq, conversation history). Use what you need.

### 7) Wire the reliability observation clamp
Separately from policy guardrails: `ReactPlanner(..., observation_guardrail=ObservationGuardrailConfig(...))` (default enabled). On huge JSON tool outputs, stores as artifact (if `ArtifactStore` available) else truncates (JSON-preserving). Prevents context overflow. Leave enabled. See `references/observation-clamp.md`.

### 8) Audit and tune
Every decision carries a stable `rule_id`. Log in shadow mode for 1-2 weeks: measure hit rates, surface timeouts (promote to DEEP), drop never-fire rules. Promote to `enforce` per rule.

## Troubleshooting (fast checks)
- **Guardrails stop everything** — likely `mode="enforce"` with overly broad rules; revert to `shadow` and measure.
- **Sync rules timeout** — exceeded `sync_timeout_ms` (default 15ms); move expensive checks to DEEP.
- **Secrets leaking through stream** — no `llm_stream_chunk` rule with REDACT; add one.
- **Tool allowlist not enforced** — rule fires on `tool_call_start` but mode is `shadow`; flip to `enforce`.
- **PAUSE fires but resume KeyError** — guardrail pause uses planner pause machinery; needs durable state store ([[penguiflow-hitl-pause-resume]]).
- **`RETRY` exhausts retries** — `max_attempts` in `RetrySpec` too high or corrective message ineffective; reduce attempts or rewrite the message.
- **Context overflow despite observation clamp** — disabled or misconfigured; verify `observation_guardrail` is not `None`.
- **Async rules never fire** — `SteeringGuardInbox` not wired; check `async_fail_open` semantics.

## Worked example
- `examples/guardrails/flow.py` — runnable guardrail policy pack with shadow→enforce migration.
- Integration tests under `tests/test_guardrails*.py` exercise sync+async rules end-to-end.

## References (load only as needed)
- `references/gateway-and-decisions.md` — `GuardrailGateway`, `GatewayConfig`, `GuardrailEvent`, `GuardrailDecision`, action payloads (`RedactionSpec`, `RetrySpec`, `PauseSpec`, `StopSpec`).
- `references/rules-fast-vs-deep.md` — FAST/DEEP classification, sync timeouts, parallel evaluation, async via `SteeringGuardInbox`.
- `references/observation-clamp.md` — `ObservationGuardrailConfig`, artifact escape hatch, JSON-preserving truncation, why it's separate from the policy layer.
