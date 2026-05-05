# Auto-Seq: deterministic post-tool execution

Auto-Seq lets `ReactPlanner` call the next tool automatically after a tool returns, without asking the model again, when the next step is deterministic.

The common case is a typed pipeline:

```text
LLM chooses triage -> triage returns RouteDecision
Auto-Seq sees exactly one visible tool accepts RouteDecision -> calls that tool
Auto-Seq repeats after each structured tool result
```

This is useful for predictable glue steps such as parse -> normalize -> enrich, where the model should decide the first action but should not spend another LLM call on an obvious typed transition.

## What Auto-Seq does

After each completed tool call, Auto-Seq:

1. Reads the previous tool's structured observation.
2. Scans the planner's currently visible and policy-allowed tool specs.
3. Keeps only tools marked with `extra={"auto_seq": True}`.
4. Validates the previous observation against each candidate tool's `args_model`.
5. If exactly one candidate validates, it can select that tool as the next action.
6. If planner execution and tool execution are both enabled, it executes the selected tool without another LLM step.

Auto-Seq does not pick the first tool from raw user text. The first step still comes from the model, an explicit flow, or your own preprocessing.

## Enable it

There are two gates:

```python
planner = ReactPlanner(
    llm="gpt-4o-mini",
    catalog=catalog,
    auto_seq_enabled=True,       # detect deterministic transitions
    auto_seq_execute=True,       # execute eligible unique transitions
)
```

Then opt tools into detection and, separately, execution:

```python
from typing import Any

from pydantic import BaseModel

from penguiflow.catalog import tool


class RouteDecision(BaseModel):
    route: str
    text: str


class DocumentState(BaseModel):
    route: str
    text: str
    doc_ids: list[str]


@tool(desc="Choose the route for a request.")
async def triage(args: RouteDecision, ctx: Any) -> RouteDecision:
    return args


@tool(
    desc="Initialize document processing.",
    extra={
        "auto_seq": True,          # can be selected by Auto-Seq
        "auto_seq_execute": True,  # can be executed without the model
    },
)
async def init_docs(args: RouteDecision, ctx: Any) -> DocumentState:
    return DocumentState(route=args.route, text=args.text, doc_ids=[])
```

With that setup, if `triage` returns `{"route": "...", "text": "..."}` and `init_docs` is the only visible auto-seq tool whose args model accepts that payload, `init_docs` is selected. If `auto_seq_execute=True` on the planner and `auto_seq_execute=True` on the tool, it is executed immediately.

## Safety gates

Auto-Seq only considers tools that pass all active gates:

- The planner has `auto_seq_enabled=True`.
- The previous tool output serializes as a mapping/dict-like payload.
- The previous action was not `next_node="parallel"`.
- The tool is visible in the current `ToolVisibilityPolicy` scope.
- The tool is not denied by `ToolPolicy`.
- The tool has `extra={"auto_seq": True}`.
- By default, the tool has `side_effects` of `"pure"` or `"read"`.
- Auto-execution additionally requires planner `auto_seq_execute=True` and tool `extra={"auto_seq_execute": True}`.

The read-only default is controlled by:

```python
planner = ReactPlanner(
    llm="gpt-4o-mini",
    catalog=catalog,
    auto_seq_enabled=True,
    auto_seq_execute=True,
    auto_seq_read_only_only=True,  # default
)
```

Keep `auto_seq_read_only_only=True` unless you have a clear reason to automate writeful operations. For writeful tools, prefer explicit model selection, human approval, or a domain-specific policy gate.

## Ambiguity and fallback

Auto-Seq only bypasses the model when there is exactly one match.

If two or more tools accept the same previous payload, Auto-Seq emits an ambiguous event and falls back to normal LLM planning. To fix ambiguity, make downstream argument schemas more specific, hide irrelevant tools with tool visibility, deny tools with policy, or avoid marking broad utility tools with `auto_seq=True`.

If no tool matches, the planner also falls back to normal LLM planning.

## Observability

When `auto_seq_enabled=True`, the planner emits one detection event per iteration after a tool step:

- `auto_seq_detected_unique`
- `auto_seq_detected_ambiguous`
- `auto_seq_detected_none`
- `auto_seq_skipped`

When Auto-Seq executes a tool without the model, it also emits:

- `auto_seq_executed`

Detection event metadata includes privacy-safe payload shape information, such as `payload_fingerprint`, `payload_keys_count`, and `payload_type`. It does not include the raw payload.

Example callback:

```python
from penguiflow.planner import PlannerEvent


def record_event(event: PlannerEvent) -> None:
    if event.event_type.startswith("auto_seq_"):
        print(event.event_type, event.extra)


planner = ReactPlanner(
    llm="gpt-4o-mini",
    catalog=catalog,
    event_callback=record_event,
    auto_seq_enabled=True,
    auto_seq_execute=True,
)
```

## Recommended rollout

1. Start with detection only:

   ```python
   ReactPlanner(..., auto_seq_enabled=True, auto_seq_execute=False)
   ```

2. Watch `auto_seq_detected_unique`, `auto_seq_detected_ambiguous`, and `auto_seq_skipped` events.
3. Tighten schemas and visibility until unique detections are expected.
4. Add `extra={"auto_seq_execute": True}` only to the downstream tools you trust.
5. Enable planner execution in a controlled environment:

   ```python
   ReactPlanner(..., auto_seq_enabled=True, auto_seq_execute=True)
   ```

## Troubleshooting

**No auto-seq events appear**

Set `auto_seq_enabled=True` and confirm the planner is using an `event_callback`.

**Auto-Seq detects but does not execute**

Check both execution gates: planner `auto_seq_execute=True` and tool `extra={"auto_seq_execute": True}`.

**The event is `auto_seq_skipped`**

Common reasons are non-structured observations, a previous parallel action, no previous step, or pending actions already queued.

**The event is `auto_seq_detected_ambiguous`**

More than one auto-seq tool accepted the same payload. Make candidate schemas more specific or use visibility/policy to narrow the current tool set.

**A tool is never considered**

Confirm the tool has `extra={"auto_seq": True}`, is visible, is not denied by policy, and satisfies the read-only side-effect gate.
