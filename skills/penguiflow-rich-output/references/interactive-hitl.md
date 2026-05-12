# Interactive HITL Tools: `ui_form`, `ui_confirm`, `ui_select_option`

These rich-output tools double as **pause tools** — they emit a UI component **and** pause the planner waiting for user input.

This reference covers the rich-output side of the contract. The general pause/resume mechanics are covered in [[penguiflow-hitl-pause-resume]].

## How they're different from `render_*`

| | `render_*` | `ui_form` / `ui_confirm` / `ui_select_option` |
|---|---|---|
| Emits `artifact_chunk` | Yes | Yes |
| Pauses planner | No | Yes (`reason="await_input"`) |
| Returns `artifact_ref` | Yes | N/A (run pauses before return) |
| Reads user response | N/A | Yes (via `tool_context` on resume) |

## `ui_form`

Render a form, pause for the user to fill it.

```python
ui_form(
    title="Send email",
    schema={                      # JSON Schema for the form
        "type": "object",
        "properties": {
            "recipient": {"type": "string", "format": "email"},
            "subject": {"type": "string"},
            "body": {"type": "string", "ui:widget": "textarea"},
        },
        "required": ["recipient", "subject"],
    },
    submit_label="Send",
    cancel_label="Cancel",
    initial_values={"subject": "Re: ..."},
    input_key="email_form",        # where the answer lands in tool_context
)
```

Pause payload (what `PlannerPause.payload` contains):
```python
{
    "ui_kind": "form",
    "artifact_ref": "art_form_1",
    "title": "Send email",
    "schema": {...},
    "submit_label": "Send",
    "cancel_label": "Cancel",
    "initial_values": {...},
    "input_key": "email_form",
}
```

On resume:
```python
result = await planner.resume(
    pause.resume_token,
    user_input="submitted",       # or "cancelled"
    tool_context={
        **original_ctx,
        "email_form": {           # keyed by input_key
            "recipient": "alice@example.com",
            "subject": "Re: ...",
            "body": "...",
        },
    },
)
```

The next planner step sees `email_form` in `tool_context` and proceeds.

## `ui_confirm`

Yes/no confirmation gate.

```python
ui_confirm(
    title="Delete this record?",
    message="This action cannot be undone.",
    confirm_label="Delete",
    cancel_label="Keep",
    danger=True,                  # visual emphasis on confirm
    input_key="delete_confirmed",
)
```

Pause payload:
```python
{
    "ui_kind": "confirm",
    "artifact_ref": "art_confirm_1",
    "title": "Delete this record?",
    "message": "This action cannot be undone.",
    "confirm_label": "Delete",
    "cancel_label": "Keep",
    "danger": True,
    "input_key": "delete_confirmed",
}
```

On resume:
```python
tool_context={**original_ctx, "delete_confirmed": True}
# or False for cancel
```

## `ui_select_option`

Pick one (or many) from a list.

```python
ui_select_option(
    title="Select target environment",
    options=[
        {"value": "prod", "label": "Production"},
        {"value": "staging", "label": "Staging"},
        {"value": "dev", "label": "Development"},
    ],
    multi_select=False,
    default=None,
    input_key="target_env",
)
```

Pause payload:
```python
{
    "ui_kind": "select",
    "artifact_ref": "art_select_1",
    "title": "Select target environment",
    "options": [...],
    "multi_select": False,
    "default": None,
    "input_key": "target_env",
}
```

On resume:
```python
tool_context={**original_ctx, "target_env": "staging"}
# or ["staging", "dev"] for multi_select
```

## Wiring requirements

1. `pause_enabled=True` on the planner (default).
2. `RichOutputConfig.enabled=True` and the interactive tools in the allowlist (they're separate from the regular components).
3. The frontend renders the components AND knows how to call `planner.resume(...)` (typically via your host API).
4. For durable resume across workers: `StateStore` implementing `SupportsPlannerState` — see [[penguiflow-hitl-pause-resume]].

## Why not just `ctx.pause(...)` directly?

You could pause directly in any tool. The interactive HITL tools have advantages:
- **Structured payload schema.** The frontend can render any `ui_form` / `ui_confirm` / `ui_select_option` consistently.
- **Validation.** Form schemas are JSON-validated server-side.
- **Tool-call lifecycle events.** Tool-call telemetry tracks the pause as a tool invocation, not a freeform raise.
- **AG-UI compatibility.** AG-UI maps these to standard interactive events.

Use `ctx.pause(...)` directly for ad-hoc / non-standard pauses. Use the interactive tools for any UI gating.

## Anti-patterns

- **Embedding side effects before the pause.** The tool body returns when `ctx.pause` raises, but anything **before** the pause already happened. Don't write to a DB and then ask for confirmation — split into a plan tool and a commit tool.
- **Skipping `input_key`.** Without a stable key, your resume `tool_context` collides with other inputs.
- **Long-running prompts.** Pauses can sit waiting for hours. Set TTL on the StateStore record.
- **Reusing the same `input_key` for different forms.** Confuses downstream reads. Namespace per session/turn.

## Operational defaults

- Default `confirm_label`/`cancel_label` should describe the actual action ("Delete" not "OK").
- `danger=True` only for destructive confirmations.
- Form schemas: small, validated server-side (the planner enforces JSON Schema before pause).
- Multi-select forms: keep options short; for long lists, switch to a search/typeahead in your frontend (which still uses a single `ui_select_option` underneath).

## Cross-channel rules

For A2A specialists serving downstream agents (not humans), **hide these tools** via `ToolVisibilityPolicy` and set `tool_context["delivery_channel"]="a2a"`. Downstream agents can't render forms; emitting them is lossy. See [[penguiflow-reactplanner-config]] step 9.
