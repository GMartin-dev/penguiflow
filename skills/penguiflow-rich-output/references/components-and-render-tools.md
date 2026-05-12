# Components and `render_*` Tools

The canonical component set ships with PenguiFlow. Your frontend may add custom components — register them in `RichOutputConfig.allowlist` and your renderer.

## Canonical component allowlist

| Component | Typed wrapper | Use for |
|---|---|---|
| `markdown` | `render_markdown(text, ...)` | Plain markdown blocks |
| `echarts` | `render_chart_echarts(option, ...)` | Charts (line, bar, pie, scatter, heatmap) — uses Apache ECharts schema |
| `datagrid` | `render_table(columns, rows, ...)` | Result tables, data grids |
| `report` | `render_report(title, sections, ...)` | Document-style multi-section output |
| `grid` | `render_grid(cells, ...)` | Dashboard-style layout |
| `tabs` | `render_tabs(tabs, ...)` | Multiple related views in one component |
| `accordion` | `render_accordion(sections, ...)` | Collapsible sections |

Plus interactive HITL tools (covered in `interactive-hitl.md`):
- `ui_form`, `ui_confirm`, `ui_select_option`

## `render_component` (generic)

```python
render_component(
    component: str,
    props: dict,
    id: str | None = None,
    title: str | None = None,
    metadata: dict | None = None,
)
```

Returns `{"artifact_ref": "art_..."}` and **emits the artifact_chunk for visible UI**.

Use when:
- A typed wrapper doesn't exist for the component you need (custom components your frontend implements).
- You're at the edge of what's allowlisted — the wrappers enforce stricter schemas than the generic.

Don't use when:
- A typed wrapper exists — use it for better validation and tighter LLM behavior.

## Typed `render_*` wrappers

Each typed wrapper:
- Takes a Pydantic-validated input model specific to the component.
- Emits the artifact_chunk for visible UI.
- Returns `{"artifact_ref": ...}`.

### `render_chart_echarts`
```python
render_chart_echarts(
    option: dict,                # ECharts option object
    id: str | None = None,
    title: str | None = None,
    metadata: dict | None = None,
)
```
`option` follows the ECharts schema (xAxis, yAxis, series, tooltip, etc.). For multi-series:
```python
option={
    "xAxis": {"type": "category", "data": ["Q1", "Q2", "Q3"]},
    "yAxis": {"type": "value"},
    "series": [{"type": "bar", "data": [10, 20, 15]}],
}
```

### `render_report`
```python
render_report(
    title: str,
    sections: list[dict],        # each section: {heading, content}
    metadata: dict | None = None,
)
```
Sections render in order, each with a heading and markdown content.

### `render_table`
```python
render_table(
    columns: list[dict],         # {name, type, label?}
    rows: list[dict],            # one dict per row, keyed by column name
    metadata: dict | None = None,
)
```
`columns[].type`: `"string"`, `"number"`, `"date"`, `"boolean"`. Add `label` for display-only column titles.

### `render_grid`
```python
render_grid(
    cells: list[dict],           # {component, props, span?}
    columns: int = 2,
    metadata: dict | None = None,
)
```
`cells[].component` can be any allowlisted component. `span` controls grid-column span.

### `render_tabs`
```python
render_tabs(
    tabs: list[dict],            # {label, component, props}
    default_index: int = 0,
    metadata: dict | None = None,
)
```
Each tab references a child component by name + props. Use with `build_*` to pre-build complex children.

### `render_accordion`
```python
render_accordion(
    sections: list[dict],        # {heading, component, props, default_open?}
    metadata: dict | None = None,
)
```

### `render_markdown`
```python
render_markdown(text: str, metadata: dict | None = None)
```
The simplest renderer. Frontend renders as standard markdown.

## When to use which

- **Single chart/table/report**: typed wrapper.
- **Composed dashboard**: `render_grid` or `render_tabs` referencing pre-built children (use `build_*`, see `builders-and-artifact-refs.md`).
- **Custom component**: `render_component` with your frontend's registered name.
- **Plain text response**: `render_markdown` or just return text (no rich output needed).

## Component validation

Each typed wrapper validates its props against the component schema before emitting. If validation fails, the tool returns an error and the planner can recover (often by calling `describe_component` to see the schema).

`render_component` (generic) only checks the component name is in the allowlist. The frontend may still reject malformed props.

## `describe_component(name)`

Returns the JSON schema for the named component:
```python
describe_component(name="echarts")
# -> {"name": "echarts", "schema": {...JSON Schema...}, "examples": [...]}
```

Use cases:
- LLM recovery after a schema validation error.
- Pre-flight check when the LLM is unfamiliar with a component.
- Always-visible discovery tool.

Keep `describe_component` always-visible in tool discovery so the planner can self-correct.

## `list_artifacts(scope=None)`

Returns artifacts produced earlier in the same run:
```python
list_artifacts()
# -> [{"id": "art_...", "type": "ui_component", "component": "echarts", "title": "..."}, ...]
```

Scope filter:
- `"ui_component"` — only UI artifacts.
- `"binary"` — only binary artifacts.
- `None` — all.

Use to:
- Find a `build_*` output to compose later.
- Avoid re-rendering an identical chart.
- Show "here's what we've shown so far" to the user.

## Stable component ids

Pass `id="trend_chart"` on `render_*` calls to give the artifact a stable, human-readable id. Useful when:
- The frontend tracks artifacts by id (e.g., for replacement vs append).
- You want to reference the same artifact across tools.

Without `id`, the runtime generates a UUID-style id.

## Operational defaults

- Allowlist exactly the components your frontend renders. Nothing more.
- Prefer typed wrappers; `render_component` is an escape hatch.
- Keep `describe_component` and `list_artifacts` always-visible.
- For multi-chart dashboards: build each chart with `build_chart_echarts`, then `render_grid([{component: "echarts", props_artifact_ref: ref1}, ...])`.
- Don't put secrets in `props` — they end up in the frontend.
