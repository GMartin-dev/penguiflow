# `GuardrailGateway` and Decision Surface

## `GuardrailGateway`

Constructed with a `GatewayConfig` and a list of rules. Attached to the planner:

```python
gateway = GuardrailGateway(config=GatewayConfig(...), rules=[...])
planner = ReactPlanner(..., guardrail_gateway=gateway)
```

The gateway is the only thing the planner knows about. The planner emits `GuardrailEvent`s on the appropriate hooks; the gateway dispatches to rules; rules return `GuardrailDecision`s; the gateway aggregates and the planner enforces.

## `GatewayConfig`

| Field | Default | Purpose |
|---|---|---|
| `mode` | `"shadow"` | `"shadow"` (log only) or `"enforce"` (apply decisions) |
| `sync_timeout_ms` | `15` | Hard deadline for sync rules. Beyond this, the rule's evaluation is abandoned. |
| `sync_parallel` | `True` | Run sync rules concurrently within the timeout. |
| `sync_fail_open` | varies | Sync rule timeout → ALLOW (open) or treat as failure (closed). |
| `async_fail_open` | varies | Async rule unavailable → ALLOW or fail. |

Recommended starting values:
- Internal agents: `mode="enforce"`, `sync_timeout_ms=15`, `sync_fail_open=True`, `async_fail_open=True`.
- External agents (high-risk): `mode="shadow"` initially, then `enforce`; `sync_fail_open=False`, `async_fail_open=True`.

## `GuardrailEvent`

What rules receive.

```python
GuardrailEvent(
    event_type: str,                # see table below
    text_content: str | None,        # user input or streamed text
    tool_name: str | None,           # tool involved (when applicable)
    tool_args: dict | None,          # tool args (when applicable)
    payload: dict,                   # tool_call_id, action_seq, conversation_history, etc.
)
```

### Event types

| `event_type` | When | Fields populated |
|---|---|---|
| `llm_before` | Before LLM planning step | `text_content` (user message), conversation history in `payload` |
| `tool_call_start` | Before tool execution | `tool_name`, `tool_args`, `payload.tool_call_id` |
| `tool_call_result` | After tool returns | `tool_name`, result in `payload`, `payload.tool_call_id` |
| `llm_stream_chunk` | Each streamed text chunk | `text_content` (chunk text) |

Rules subscribe to specific event types — don't run a tool-call rule on `llm_stream_chunk`.

## `GuardrailDecision`

What rules return.

```python
GuardrailDecision(
    action: Literal["ALLOW", "REDACT", "RETRY", "PAUSE", "STOP"],
    rule_id: str,                    # stable identifier for audit
    severity: Literal["info", "warn", "error", "critical"] | None = None,
    confidence: float | None = None,  # 0.0 - 1.0
    redactions: tuple[RedactionSpec, ...] = (),
    retry: RetrySpec | None = None,
    pause: PauseSpec | None = None,
    stop: StopSpec | None = None,
    metadata: dict | None = None,
)
```

Always set `rule_id`. Auditors will need it.

### `RedactionSpec`
```python
RedactionSpec(
    span: tuple[int, int] | None = None,    # char range in text_content
    pattern: str | None = None,              # regex if span unavailable
    replacement: str = "[REDACTED]",
)
```
The gateway applies redactions to outgoing text. Multiple specs combine.

### `RetrySpec`
```python
RetrySpec(
    max_attempts: int = 1,
    corrective_message: str,                 # appended to prompt for retry
)
```
Use for fixable LLM mistakes (schema violation, unsafe phrasing). Don't use for transient errors — those are runtime retries (see [[penguiflow-core-flows]] `references/errors-retries.md`).

### `PauseSpec`
```python
PauseSpec(
    scope: str,                              # what's being approved
    approver_roles: tuple[str, ...] = (),
    prompt: str,                             # operator-facing prompt
    timeout_s: float | None = None,
)
```
Triggers `PlannerPause(reason="approval_required")`. The host resumes via `planner.resume(...)` after approval. See [[penguiflow-hitl-pause-resume]].

### `StopSpec`
```python
StopSpec(
    error_code: str,                         # stable code for clients
    user_message: str,                       # safe message to surface
    internal_reason: str,                    # detailed reason for logs
)
```
Terminal. The planner emits a "stopped" result with `error_code` and `user_message`. Don't leak `internal_reason` to users.

## Decision aggregation

When multiple rules return decisions for the same event:

| Mix | Result |
|---|---|
| Any STOP | STOP wins (with first STOP's `StopSpec`) |
| Any PAUSE (no STOP) | PAUSE wins |
| Any RETRY (no STOP/PAUSE) | RETRY wins (highest `max_attempts`) |
| Multiple REDACT | Union of `redactions` applied |
| All ALLOW | ALLOW |

Severity is for logging only — doesn't affect aggregation order.

## Conversation history in events

`guardrail_conversation_history_turns=N` includes the last N memory turns in `event.payload["conversation_history"]`. Useful for rules that need context ("did the user just deny this?"). Off by default (set to 0) for minimal payload.

## Authoring a rule

A rule is any object exposing:
```python
async def evaluate(self, event: GuardrailEvent) -> GuardrailDecision | None: ...
```

Returns `None` for "not applicable" (gateway treats as ALLOW for this rule). Otherwise returns a decision.

### Example: tool denylist
```python
class ToolDenylistRule:
    rule_id = "tool_denylist_v1"
    DENIED = {"shell", "exec", "delete_all"}

    async def evaluate(self, event):
        if event.event_type != "tool_call_start":
            return None
        if event.tool_name in self.DENIED:
            return GuardrailDecision(
                action="STOP",
                rule_id=self.rule_id,
                severity="critical",
                stop=StopSpec(
                    error_code="tool_denylisted",
                    user_message="I can't run that operation.",
                    internal_reason=f"Tool {event.tool_name} is on the denylist.",
                ),
            )
        return None
```

### Example: streaming PII redaction
```python
class PIIRedactRule:
    rule_id = "pii_redact_v1"
    SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

    async def evaluate(self, event):
        if event.event_type != "llm_stream_chunk":
            return None
        text = event.text_content or ""
        if not self.SSN_RE.search(text):
            return None
        return GuardrailDecision(
            action="REDACT",
            rule_id=self.rule_id,
            redactions=(
                RedactionSpec(pattern=self.SSN_RE.pattern, replacement="[SSN]"),
            ),
        )
```

### Example: approval gate
```python
class ProdWriteApproval:
    rule_id = "prod_write_approval_v1"

    async def evaluate(self, event):
        if event.event_type != "tool_call_start" or event.tool_name != "db.write":
            return None
        target = (event.tool_args or {}).get("target_db")
        if target == "prod":
            return GuardrailDecision(
                action="PAUSE",
                rule_id=self.rule_id,
                pause=PauseSpec(
                    scope="db.write:prod",
                    approver_roles=("admin",),
                    prompt=f"Approve write to prod for: {event.tool_args.get('preview', '...')}",
                    timeout_s=3600.0,
                ),
            )
        return None
```

## Audit format

When the gateway acts, log:
```json
{
  "event_type": "tool_call_start",
  "tool_name": "db.write",
  "action": "PAUSE",
  "rule_id": "prod_write_approval_v1",
  "severity": "warn",
  "trace_id": "...",
  "session_id": "..."
}
```

Drop everything sensitive (tool_args may contain user data). Keep enough to reconstruct what fired and why.
