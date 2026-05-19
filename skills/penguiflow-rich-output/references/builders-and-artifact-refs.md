# Builders and `artifact_ref` Composition

`build_*` tools are the non-emitting counterparts to `render_*`. They register a reusable component payload in the in-run artifact registry and return an `artifact_ref` you can compose into parent renderers.

## When builders are useful

### Case 1: One child, many parents
You want to show the same chart in both a tab and a grid:

```python
chart = build_chart_echarts(option=...)
# chart = {"artifact_ref": "art_chart_1"}

render_tabs(tabs=[
    {"label": "Trend", "component_artifact_ref": chart["artifact_ref"]},
    ...
])

render_grid(cells=[
    {"component_artifact_ref": chart["artifact_ref"], "span": 2},
    ...
])
```

Without builders, you'd have to inline the chart props in both places — wasted tokens, mismatched data.

### Case 2: Conditional inclusion
Build several candidate children, decide later which to render:

```python
maybe_chart = build_chart_echarts(option=...)
maybe_table = build_table(columns=..., rows=...)

if data.has_trend:
    render_grid(cells=[{"component_artifact_ref": maybe_chart["artifact_ref"]}])
else:
    render_grid(cells=[{"component_artifact_ref": maybe_table["artifact_ref"]}])
```

### Case 3: Composed reports
A report with multiple custom sections:

```python
chart = build_chart_echarts(option=...)
table = build_table(columns=..., rows=...)

render_report(
    title="Q3 review",
    sections=[
        {"heading": "Summary", "content": "..."},
        {"heading": "Trend", "component_artifact_ref": chart["artifact_ref"]},
        {"heading": "Details", "component_artifact_ref": table["artifact_ref"]},
    ],
)
```

## Builder tools (mirroring renderers)

| Builder | Pairs with |
|---|---|
| `build_chart_echarts` | `render_chart_echarts` |
| `build_table` | `render_table` |
| `build_report` | `render_report` |
| `build_grid` | `render_grid` |
| `build_tabs` | `render_tabs` |
| `build_accordion` | `render_accordion` |
| `build_markdown` | `render_markdown` |

Each builder:
- Takes the same input model as its renderer.
- Does **not** emit `artifact_chunk` — the component isn't visible until composed into a parent.
- Returns `{"artifact_ref": "art_..."}`.

## In-run artifact registry

Builders register payloads in a per-run registry. The registry:
- Lives only for the duration of one `planner.run(...)`.
- Doesn't persist across pause/resume **automatically** — but if you have a `StateStore` with planner state persistence, refs are reachable during the same run via the saved state.
- Is **not** the durable `ArtifactStore` (covered in [[penguiflow-statestore]]) — those are different surfaces.

For cross-run persistence (e.g., "the chart from yesterday's session"), use:
1. Render with `render_*` so an `artifact_chunk` is emitted.
2. Frontend snapshots the chunk to durable storage of your choice.

Or:
1. Use the `ArtifactStore` directly (`ctx.artifacts.upload(...)`).
2. Reference the persisted artifact in future runs via your app layer.

## Composition rules

### Parent slots that accept `component_artifact_ref`

Renderers expose `component_artifact_ref` (or `component` + `props` inline) on child slots. Examples:
- `render_grid(cells=[{component_artifact_ref: ...}, {component: "echarts", props: {...}}, ...])` — mix and match.
- `render_tabs(tabs=[{label: ..., component_artifact_ref: ...}, ...])`.
- `render_accordion(sections=[{heading: ..., component_artifact_ref: ...}, ...])`.

When you pass `component_artifact_ref`, the renderer resolves it at emit time. If the ref isn't found, the renderer fails — typically because:
- You called the builder in a different run.
- You consumed the ref via a `render_*` that finalized the registry entry.

### Refs are single-use? No.
A ref can be composed into multiple parents within the same run. The registry retains the payload until the run ends.

### Refs in nested compositions
You can build a grid of charts:

```python
charts = [build_chart_echarts(option=opt) for opt in options]
grid = build_grid(cells=[{"component_artifact_ref": c["artifact_ref"]} for c in charts])
render_tabs(tabs=[
    {"label": "Dashboard", "component_artifact_ref": grid["artifact_ref"]},
    {"label": "Detail",    "component_artifact_ref": charts[0]["artifact_ref"]},
])
```

The resolver walks the ref tree at emit time.

## Patterns

### Pattern: "compose then render"
```python
# build all children
parts = {
    "chart": build_chart_echarts(option=chart_opt),
    "table": build_table(columns=cols, rows=rows),
    "summary": build_markdown(text=summary_text),
}

# decide layout based on user intent
if user_wants_dashboard:
    render_grid(cells=[
        {"component_artifact_ref": parts["chart"]["artifact_ref"]},
        {"component_artifact_ref": parts["table"]["artifact_ref"]},
        {"component_artifact_ref": parts["summary"]["artifact_ref"]},
    ])
else:
    render_report(title="Analysis", sections=[
        {"heading": "Trend",   "component_artifact_ref": parts["chart"]["artifact_ref"]},
        {"heading": "Data",    "component_artifact_ref": parts["table"]["artifact_ref"]},
        {"heading": "Summary", "component_artifact_ref": parts["summary"]["artifact_ref"]},
    ])
```

### Pattern: "inline then promote"
Start with inline props for prototyping; when a child becomes reusable, promote it to a builder.

```python
# v1: inline (single use)
render_tabs(tabs=[
    {"label": "Chart", "component": "echarts", "props": chart_opt},
])

# v2: promoted (reused in tabs and a follow-up report)
chart = build_chart_echarts(option=chart_opt)
render_tabs(tabs=[{"label": "Chart", "component_artifact_ref": chart["artifact_ref"]}])
render_report(title="Detail", sections=[
    {"heading": "Trend", "component_artifact_ref": chart["artifact_ref"]},
])
```

## Operational defaults

- Use builders when ref reuse > 1 within a run.
- Don't pre-build everything speculatively — each `build_*` call costs LLM tokens.
- Keep ref ids opaque; don't parse them on the frontend.
- Treat refs as run-scoped. For cross-run persistence, use `ArtifactStore`.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| "artifact_ref not found" | Ref from a different run | Build in the same run as the renderer |
| Renderer emits but frontend shows empty cells | `component_artifact_ref` not resolved (bug or version mismatch) | Inspect emitted `artifact_chunk`; check renderer wiring |
| Duplicate UI showing same chart twice | Both built and inlined the same data | Pick one; either build once and ref, or inline once |
| Slow UI for large tabs | Many builders create many refs that all resolve at parent emit time | Reduce ref tree depth; flatten if possible |
