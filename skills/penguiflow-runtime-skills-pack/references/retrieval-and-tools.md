# Retrieval, Tools, and `skill_propose`

How the planner actually finds and uses skills at runtime.

## The skill tool surface

When `SkillsConfig.enabled=True`, the planner gets three tools as always-visible:

| Tool | Purpose |
|---|---|
| `skill_search(query, limit=..., tags=[...], namespace=...)` | Semantic-style search over skills by `trigger` + `tags`. Optional `tags` is an AND filter against the skill's declared tags. Optional `namespace` is a dot-prefix match against the skill name (`name == namespace` or `name.startswith(namespace + ".")`). Empty defaults preserve legacy behavior. |
| `skill_get(names)` | Fetch full text for one or more skills by name. |
| `skill_list(..., tags=[...], namespace=...)` | Paginated listing for discovery / directory. Accepts the same `tags` / `namespace` filters as `skill_search`. |

The planner can call these at any step. They go through tool policy / visibility like any other tool, so a `ToolVisibilityPolicy` can hide them per request (e.g., for A2A specialist responses where the LLM shouldn't browse skills).

## Pre-flight injection ("relevant skills")

At the **start** of every `planner.run(...)`, the planner runs an automatic retrieval:

1. Embed (or otherwise index) the user message.
2. Find `top_k` skills with the best match (by `trigger` + tags).
3. Apply applicability filtering (the request's allowed capability set).
4. Inject the top matches into `llm_context.skills_context` (bounded by `max_tokens`).
5. The LLM sees them in its first prompt — no tool call needed.

Pros:
- Skills surface even when the LLM doesn't know to call `skill_search`.
- One round-trip cheaper than tool-call retrieval.

Cons:
- `top_k` mistakes (irrelevant retrievals) waste tokens.
- Multi-turn conversations may not re-trigger retrieval (depends on planner config).

Tune:
- `top_k=6` — typical.
- `max_tokens=2000` — keeps the injection bounded.
- `summarize=False` — only summarize if individual skills are large.

## Directory rendering

`SkillsDirectoryConfig(enabled=True, include_fields=["name", "title", "trigger"])` adds a `<skill_directory>` block to the prompt with a compact list of known skills.

When to use:
- Interactive UIs ("what can you help with?").
- Multi-turn conversations where the user may pivot topics.

When to skip:
- Token budget is tight.
- Skills are domain-specific and discoverability isn't a goal.

## Tool-aware retrieval

The planner combines skill applicability with tool visibility:

- If a skill's `required_tool_names` aren't allowed for this request → skill is filtered out (pre-flight, `skill_search`, `skill_get`).
- If a skill's text mentions a forbidden tool but applicability is satisfied otherwise → the runtime can rewrite the text to "use `tool_search` to find the right tool for X" (when `tool_search` is configured).

This prevents capability leakage: a tenant without access to `db.write` won't see (or be guided toward) skills that name `db.write` directly.

## `skill_propose` drafting

Enable:
```python
SkillsConfig(..., proposal={"enabled": True})
```

Exposes a `skill_propose` tool. The LLM calls it with raw source material; it returns a typed skill draft:

```python
{
    "name": "pack.proposed.q4_review",
    "title": "Conduct a Q4 review",
    "trigger": "When the user asks for a Q4 retrospective.",
    "task_type": "domain",
    "steps": ["Gather metrics from ...", "..."],
    "failure_modes": ["..."],
}
```

**Critical:** `skill_propose` **does not persist anything**. It's a pure drafting tool. The host app is responsible for:
1. Receiving the draft.
2. Showing it to a reviewer (admin UI, PR, ticket).
3. Persisting it (writing to a pack, adding to the runtime provider).
4. Re-loading the pack into the planner.

This keeps skill curation safe: agent-assisted drafting without auto-publishing.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Pre-flight injection misses obvious skills | `trigger` text doesn't match user intent | Rewrite triggers in user-intent terms |
| Wrong skills retrieved | Vector / lexical index is stale | Rebuild the SQLite index (delete `cache_dir`) |
| Skills "visible" via `skill_list` but not in pre-flight | Applicability gating filters them; `top_k` reached | Verify `required_*` fields; check pre-flight log |
| Token budget exceeded | `max_tokens` low or skills are huge | Lower `top_k`, enable `summarize=True`, or trim skill text |
| `skill_propose` returns garbage | LLM doesn't have good source material | Pre-fetch source content; give the model clear context |

## Observability hooks

Relevant `PlannerEvent` types:
- `skill_pack_loaded(name, count)` — at startup.
- `skills_retrieved(count, token_estimate, was_summarized)` — pre-flight.
- `skill_search_query(query, results_count)` — LLM-initiated search.
- `skill_get(names, found_count)` — LLM-initiated fetch.
- `skill_list(scope, count)` — directory render.
- `skill_propose(draft_name)` — drafting.
- `skill_directory_rendered(entry_count)` — when directory injection fires.

Track:
- Top-k retrieval hit rate (skills shown vs subsequently used).
- `skill_search` calls per run (high count = pre-flight retrieval is failing).
- `skill_propose` invocation rate (gauge of agent-driven authoring activity).

## Cross-skill patterns

### Pattern: tooling skill for rich-output

```markdown
---
name: pack.tooling.report_layout
trigger: When you need to produce a multi-section report with charts and tables.
required_tool_names: [render_report, build_chart_echarts, build_table]
steps:
  - Build charts and tables with build_* tools first.
  - Compose into a render_report with sections referencing build artifact_refs.
  - Set the title to match the user's intent verbatim.
---
```

### Pattern: safety skill for a destructive tool

```markdown
---
name: pack.safety.confirm_before_delete
trigger: When the user asks to delete data.
required_tool_names: [delete_record, ui_confirm]
task_type: safety
steps:
  - Call ui_confirm with a clear preview before delete_record.
  - If the user declines, propose alternatives (archive, soft-delete).
  - Never call delete_record without confirmation.
---
```

### Pattern: meta-skill on tool discovery

```markdown
---
name: pack.meta.use_tool_search
trigger: When you can't find a tool by name.
required_tool_names: [tool_search, tool_get]
task_type: meta
steps:
  - Call tool_search with the capability description (not a guessed tool name).
  - Inspect results; call tool_get on the most promising one.
  - Then call the tool.
---
```

These steer planner behavior beyond what the prompt alone teaches.

## Anti-patterns

- **Skills that duplicate prompt content** — wastes tokens; the prompt already says it.
- **Skills with vague triggers** — retrieval misses them.
- **Skills mentioning forbidden tools** — capability leak; gate with `required_tool_names`.
- **Skills as documentation** — they're operational playbooks, not docs. Docs live in `docs/`.
- **One mega-skill** — split into focused skills; retrieval picks the right one.
- **Skipping `name` field** — auto-generated names are unstable; explicit names version-control cleanly.
