# `BackgroundTasksConfig` Reference

Configuration for planner-driven background tasks. Pass to `ReactPlanner(..., background_tasks=BackgroundTasksConfig(...))`.

## Enablement

| Field | Default | Purpose |
|---|---|---|
| `enabled` | `False` | Master switch. Tasks are off unless this is True. |
| `include_prompt_guidance` | `True` | Add planner-side hints describing `task.subagent`/`task.tool` opcodes and `tasks.*` tools. |

When `enabled=False`, `tasks.*` tools and `task.*` opcodes are not available; spawn attempts fail explicitly.

## Tool-initiated spawns

| Field | Default | Purpose |
|---|---|---|
| `allow_tool_background` | `False` | When True, tools with `spec.extra["background"]["enabled"]=True` can spawn in background instead of running inline. |
| `default_mode` | `"subagent"` | `"subagent"` or `"job"`. Subagent runs a full sub-planner; job runs a single tool. |
| `default_merge_strategy` | `"HUMAN_GATED"` | `"HUMAN_GATED"`, `"APPEND"`, `"REPLACE"`. Default merge for tool-initiated and explicit spawns. |

### Tool-initiated path

A tool call is rerouted to a background task when **all** are true:
1. `background_tasks.allow_tool_background=True`.
2. The tool's `NodeSpec.extra["background"]["enabled"] is True`.
3. `tool_context["task_service"]` is present.
4. `tool_context["session_id"]` is a string.

When that happens, the foreground tool call returns:
```python
{
    "task_id": "...",
    "status": "PENDING",
    "message": "spawned:job"  # or "spawned:subagent"
}
```

The planner records this observation. Foreground continues; the task runs out-of-band and merges per the configured strategy when done.

Tool-spec metadata example:
```python
spec = NodeSpec(
    name="scrape_site",
    desc="...",
    side_effects="external",
    extra={
        "background": {
            "enabled": True,
            "mode": "job",                    # override default_mode
            "merge_strategy": "APPEND",        # override default_merge_strategy
            "notify_on_complete": True,        # platform-specific
        },
    },
    ...
)
```

## Limits

| Field | Default | Purpose |
|---|---|---|
| `max_concurrent_tasks` | platform | Hard cap on simultaneous tasks. Spawn fails when exceeded. |
| `max_tasks_per_session` | platform | Hard cap per session. Reset per session id. |
| `task_timeout_s` | `None` | Per-task wall-clock timeout. None = no timeout (use sparingly). |

Always set these. Unlimited concurrency on a small executor blocks the foreground; unlimited per-session lets a misbehaving session DoS the worker.

## Proactive reporting (auto-merge modes)

When `default_merge_strategy` is `APPEND` or `REPLACE`, completed tasks merge automatically. Controls:

| Field | Default | Purpose |
|---|---|---|
| `proactive_report_enabled` | `False` | When True, the planner notifies the user as tasks complete. |
| `proactive_report_max_hops` | platform | Max planner hops to delay a report (gives the LLM a chance to integrate). |

For chat UIs, `proactive_report_enabled=True` produces "Heads up — research on segment A is in. Want me to incorporate it?" style messages.

## Task groups

| Field | Default | Purpose |
|---|---|---|
| `default_group_merge_strategy` | `"HUMAN_GATED"` | Strategy applied at `tasks.apply_group`. |
| `default_group_report` | `True` | Produce a single user-facing summary on apply. |
| `max_tasks_per_group` | platform | Hard cap per group. |

Groups bundle related background work into one merge. Useful when:
- Multi-segment research: each segment is a task, the group is the consolidated report.
- Parallel transformations: each variant is a task, the group decides which to apply.
- Multi-step ETL: each step is a task, the group seals when all succeed.

## Operational defaults

Conservative starting point:
```python
BackgroundTasksConfig(
    enabled=True,
    allow_tool_background=False,            # flip True only after observability is in place
    default_mode="subagent",
    default_merge_strategy="HUMAN_GATED",
    max_concurrent_tasks=4,
    max_tasks_per_session=20,
    task_timeout_s=300.0,
    proactive_report_enabled=False,
    default_group_merge_strategy="HUMAN_GATED",
    default_group_report=True,
    max_tasks_per_group=10,
)
```

Tighter (latency-sensitive product):
- `max_concurrent_tasks=2`
- `task_timeout_s=60.0`
- `default_merge_strategy="APPEND"` (lower friction)

Looser (long-running research agent):
- `max_concurrent_tasks=8`
- `task_timeout_s=1800.0`
- `default_merge_strategy="HUMAN_GATED"` (operator reviews each)

## Prompt guidance

With `include_prompt_guidance=True`, the planner's system prompt gains a section describing:
- How to use `task.subagent` and `task.tool` opcodes.
- When to prefer background over inline (slow work, parallel-able work).
- How task groups consolidate reports.

Disable this if your prompt is already opinionated about task patterns and you don't want the default guidance to conflict.

## Foreground vs subagent visibility

**Critical**: subagents must not be able to spawn more tasks unless you explicitly want recursion.

Two layers of defense:
1. Don't include `tasks.*` in the subagent's catalog.
2. Apply a `ToolVisibilityPolicy` for subagent contexts that filters out `tasks.*`.

The planner doesn't enforce this automatically — it's your responsibility at planner-init time.

## Observability

`PlannerEvent` types emitted by the background subsystem:
- `task_spawned`, `task_started`, `task_completed`, `task_failed`, `task_cancelled`.
- `task_merged` (when context patch applies).
- `task_group_sealed`, `task_group_applied`.

Track:
- Tasks spawned per session.
- Time-to-completion p50/p95/p99 by mode.
- Failure rate by mode.
- Merge backlog (tasks completed but not yet merged, for HUMAN_GATED).
- Time-to-merge (operator workflow signal).

See [[penguiflow-observability]] for the broader event taxonomy.

## Common configuration mistakes

| Mistake | Consequence | Fix |
|---|---|---|
| `enabled=True` but no `task_service` in `tool_context` | All `tasks.*` calls fail | Wire the service |
| No limits set | One bad session DoSes the worker | Set `max_concurrent_tasks`, `max_tasks_per_session` |
| `default_merge_strategy="APPEND"` from day 1 | Hard to debug context drift | Start `HUMAN_GATED`, promote to APPEND only with observability |
| `tasks.*` visible to subagents | Recursive task explosion | Visibility policy |
| `task_timeout_s=None` for tool-initiated background | Wedged tasks accumulate | Set a real timeout |
