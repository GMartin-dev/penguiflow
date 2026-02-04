# RFC: Trace-Derived Datasets and Evals

Status: TODO (draft)

## Problem

PenguiFlow has strong support for executing agent flows and inspecting runs (Playground, trace replay, `FlowEvent` hooks), but it does not provide a first-class workflow for AI evaluation:

- turning production traces into repeatable datasets
- running custom metrics (per node / per orchestration)
- tracking quality regressions over time
- generating artifacts that can be used by optimizers (e.g., DSPy) to improve prompts/nodes

In practice, optimizers like DSPy GEPA (reflective prompt evolution) need two things:

- datasets (trace-derived or curated) that can be mapped into signature-shaped examples
- an eval harness + metric contract that can be reused for both manual iteration and optimizer-driven loops

Today, most "quality" practice is:

- run the app
- eyeball the Playground trace
- optionally log some metrics (e.g., MLflow example)

This creates an execution-path bias: good debugging surfaces exist, but evaluation surfaces are ad-hoc.

## Goals

Phase 1 goals (dataset + metrics):

- Export an evaluation dataset from stored traces with a stable, versioned schema.
- Support both orchestration-level and node-level metrics.
- Keep the default export "safe" (avoid dumping sensitive tool outputs by default).
- Keep dependencies minimal; integrate with existing `StateStore` optional capabilities.

Phase 2 goals (optimization templates):

- Provide templates that take exported datasets + metrics and run optimization loops for:
  - node-level prompt/signature improvements
  - limited orchestration prompt component tuning (planner hints/system prompt/tool descriptions)
- Ensure templates are runnable and repo-agnostic.

## Non-Goals

- A full observability platform (OTel exporters, Prometheus collectors, vendor SDK integrations).
- A universal definition of "quality"; teams must bring task-specific metrics.
- Automatic labeling / ground-truth generation for all tasks.

## Existing Building Blocks

- `FlowEvent` emitted by runtime and persisted as `StoredEvent` via `StateStore.save_event(...)`.
- `StateStore` optional capabilities:
  - trajectories: `save_trajectory/get_trajectory/list_traces` (`SupportsTrajectories`)
  - planner events: `save_planner_event/list_planner_events` (`SupportsPlannerEvents`)
- Planner-native structure: `Trajectory` (query, contexts, steps, observations, streams, background results).
- CLI pattern for extracting trace history: `penguiflow-admin history|replay`.

## Proposal

### 0) Concepts: Trace Records, Dataset Views, Patch Points

This RFC separates three related but different artifacts:

- Trace record: a rich, safe-by-default representation of what happened in one run (used for debugging, slicing, drill-down).
- Dataset view: a flat, signature-shaped projection of traces into rows that can become `dspy.Example(...)` (used for evaluation/optimization).
- Patch point: a named, version-controlled text/config surface that can be changed between eval runs (used for manual sweeps and GEPA-style optimization).

This separation keeps PenguiFlow evaluation grounded in real execution while staying compatible with DSPy conventions.

### 1) Trace Example Schema

Define a versioned dataset row model (JSON-serializable) representing one trace.

Working name: `TraceExampleV1`.

Minimum fields:

- `schema_version`: "TraceExampleV1"
- `trace_id`: string
- `session_id`: string | null (when applicable)
- `query`: string
- `inputs`:
  - `llm_context`: object (optional, default excluded)
  - `tool_context`: object (optional, default excluded)
- `outputs`:
  - `final`: object | string | null (best-effort)
  - `status`: "ok" | "error" | "cancelled" | "unknown"
- `trajectory`:
  - `steps`: list (optional; may be compacted)
  - `summary`: object | null
  - `metadata`: object
- `events`:
  - `flow_events`: list (optional)
  - `planner_events`: list (optional)
- `derived`:
  - cheap deterministic aggregates (counts, latencies, tool failure rate, reflection score, cost)

Notes:

- The schema must tolerate missing capabilities (e.g., stores that only support `load_history`).
- Large payloads should be truncated or moved into artifacts; rows should remain small enough for JSONL workflows.

Notes on compatibility:

- `TraceExampleV1` is not required to be a DSPy dataset row. DSPy rows are signature-driven and typically flat (see "Dataset Views").

### 2) Export Pipeline

Add a dataset exporter that reads from a `StateStore` and emits JSONL.

Primary sources (best to worst):

1. `SupportsTrajectories` -> `get_trajectory(...)`
2. `SupportsPlannerEvents` -> `list_planner_events(trace_id)`
3. Base store -> `load_history(trace_id)` returning `StoredEvent` items

Export selection:

- by `session_id` (via `list_traces(session_id)`)
- by explicit list of `trace_id`s
- by tag (recommended) for curated datasets
- (optional) by time range if store supports it later

Tagging (curation-first):

- A long, unfiltered history is rarely a useful dataset.
- The primary workflow should be to tag traces inside Playground (or via CLI) as belonging to a dataset.
- For MVP, tags can live in `Trajectory.metadata["tags"]` as a list of strings.
- Future state stores can add first-class index support (e.g., `list_traces_by_tag(...)`).

Recommended tag conventions:

- `dataset:<name>` (e.g., `dataset:customer_support_v1`)
- `split:train|val|test`
- optional: `route:<name>`, `tenant:<id>`, `path:<graph_hash>`

Default export mode: safe.

- Exclude raw `tool_context` / `llm_context` unless explicitly requested.
- Truncate observations over a configured size.
- Provide a hook for redaction policies (future).

CLI shape (consistent with existing patterns):

- `penguiflow-admin export-dataset --state-store module:factory --session-id X --out dataset.jsonl`
- `penguiflow-admin export-dataset --state-store module:factory --trace-id T1 --trace-id T2 --out dataset.jsonl`
- `penguiflow-admin export-dataset --state-store module:factory --tag dataset:foo --tag split:val --out dataset.jsonl`

### 2b) Dataset Views (Signature-Driven Projection)

DSPy datasets are signature-dependent: row keys should match the signature field names.
We therefore define a view/projection layer that maps a trace record into a flat dict.

Working name: `DatasetViewV1`.

Conceptual fields:

- `view_name`: string
- `schema_version`: "DatasetViewV1"
- `signature`: string (e.g., `"question -> answer"`) or dotted import path (templates)
- `input_keys`: list[str] (keys to pass to `dspy.Example(...).with_inputs(*input_keys)`)
- `field_map`: mapping from trace paths (e.g. `query`, `outputs.final`) to output field names (e.g. `question`, `answer`)
- `row_filter`: optional filters (status ok only, min/max size, etc.)

Exporters should be able to write:

- `trace.jsonl` (rich `TraceExampleV1`)
- `view.jsonl` (flat rows for evaluation/optimization)
- `manifest.json` (declares the view config, inputs, signature, and provenance)

This keeps "DSPy compatibility" as a property of a chosen view, not a property of the trace schema itself.

### 2c) Patch Points (What We Optimize)

Patch points are named configuration/text surfaces that can be varied between runs.
They are the unit of manual prompt iteration and GEPA-style optimization.

Examples (initial set):

- `planner.system_prompt_extra` (global system prompt overlay)
- `planner.planning_hints` (structured hints injected into the planner prompt)
- `tool.<tool_name>.desc` (tool description text shown to the planner)

Patch points must be:

- explicit and version-controlled
- safe to apply without changing runtime code paths
- serializable (so we can log the exact candidate config used in an eval)

### 2d) Patch Bundles (Single Entry Point)

We should not have separate patch point systems for "offline DSPy optimization" vs "live production".
Instead, we define a single, PenguiFlow-native artifact that represents a concrete set of patch point values.

Working name: `PatchBundleV1`.

Patch bundle responsibilities:

- Provide a single entry point for applying tuned prompt/config values to a PenguiFlow playbook/planner/tool catalog.
- Be safe to store, review, and deploy (JSON, no code execution).
- Capture provenance so results can be reproduced and audited.

Conceptual fields:

- `schema_version`: "PatchBundleV1"
- `patches`: dict[str, object] mapping patch point keys (e.g. `planner.system_prompt_extra`) to values
- `compat`: object (optional) describing what this bundle expects (flow id/version, planner type, tool catalog hash)
- `provenance`: object (optional) describing where it came from (dataset tags, metric, optimizer name/version, score)

Offline optimization (manual sweeps or GEPA) produces many candidate bundles, then emits a single "best" bundle.
Production consumes the same bundle format.

### 3) Metrics Runner

Provide an eval runner that consumes JSONL `TraceExampleV1` rows and produces:

- per-trace metric results (JSONL)
- aggregate report (JSON)
- optional tabular summaries (CSV)

Metrics must support two evaluation scopes:

- orchestration-level metrics: overall success, steps-to-complete, cost/latency budgets
- node-level metrics: error rate and latency per node_name/node_id, retry patterns

Proposed metric API (DSPy/GEPA compatible):

GEPA requires a metric callable that accepts five arguments `(gold, pred, trace, pred_name, pred_trace)`.
To maximize reuse, PenguiFlow should adopt this contract as the default metric signature.

```
def metric(
    gold: object,
    pred: object,
    trace: object | None = None,
    pred_name: str | None = None,
    pred_trace: object | None = None,
) -> float | dict:
    # Return either a float score, or {"score": float, "feedback": str}.
    ...
```

Notes:

- `gold` should usually be the exported dataset row (view row or trace record), dict-like.
- `pred` should be the model/program output for the example (final payload).
- `trace` is optional but allows metrics to use tool failures, latency, and other deterministic signals.
- `pred_name`/`pred_trace` enable future per-component feedback (required by GEPA). Metrics can ignore them initially.

Runner responsibilities:

- execute the eval harness (PenguiFlow run) to produce `(pred, trace)` per `gold`
- call `metric(gold, pred, trace, pred_name=None, pred_trace=None)` for standard eval
- aggregate scores + persist per-example JSONL

Optional future:

- LLM-as-judge metrics (pluggable, not required)
- slice filters (tenant, route, tool used, etc.)

### 3b) Manual Patch Sweeps (Before Auto Optimization)

Auto optimization should be a special path.
The baseline workflow is a manual sweep: evaluate a list of patch candidates (different patch point values)
against the same dataset and metric to see which change helps.

This produces the same artifacts as normal evals (per-example JSONL + aggregate report), but with provenance:

- `candidate_id`
- patch point values for that candidate

### 4) DSPy and GEPA Compatibility

Goal: make PenguiFlow eval artifacts usable for DSPy optimization (including GEPA) without forcing DSPy into core.

Key points:

- DSPy datasets are lists of `dspy.Example` built from dict-like rows. Rows must match the intended signature.
- GEPA optimizes textual components and requires the metric signature `(gold, pred, trace, pred_name, pred_trace)`.
- PenguiFlow should therefore export signature-shaped dataset views and adopt the GEPA metric signature in its own eval runner.

Templates (optional dependency path):

- `penguiflow.templates.dspy`: helpers that load `view.jsonl` into `dspy.Example` objects and run DSPy optimizers.
- `GEPA` integration focuses on patch points (text components) while executing real PenguiFlow runs for evaluation.

This provides a low-resistance path:

1. Export curated traces by tag into a dataset view.
2. Run eval harness with manual patch sweeps.
3. Enable GEPA (optional) to propose patch updates using the same metric.

### 5) Optional Integrations (MLflow)

PenguiFlow should keep the exported artifacts (datasets, views, patch bundles, eval results) as the canonical,
portable interface. Third-party systems can be used as optional sinks to track and compare runs.

MLflow integration is an example of such a sink:

- log `manifest.json`, `trace.jsonl`, `view.jsonl`, `PatchBundleV1` outputs, and reports as run artifacts
- log aggregate metrics as MLflow metrics
- log dataset tags / candidate ids / patch bundle hashes as MLflow params for slicing

This keeps dependencies minimal in core PenguiFlow while still enabling teams to use existing experiment
tracking workflows.

## Phased Plan

### Phase 1: Export + Evals MVP

Deliverables:

- `TraceExampleV1` schema and JSONL encoder
- dataset exporter from `StateStore` (capability-detected)
- `penguiflow-admin export-dataset` command
- minimal metrics runner (library function + small CLI)
- documentation + example workflow

Success criteria:

- A team can export last N traces from Playground state store and compute at least:
  - overall success rate
  - p50/p95 node latency by node_name
  - tool failure rate

### Phase 2: Optimization Templates

Deliverables:

- Template A: node-level prompt/signature optimization
  - inputs: dataset.jsonl, a metric function, a node/prompt surface
  - outputs: optimized prompt/signature + eval report

- Template B: orchestration tuning (bounded)
  - inputs: dataset.jsonl, metric(s)
  - knobs: planner system prompt, tool descriptions, hints policies
  - outputs: tuned configuration + eval report

Guardrails:

- do not optimize arbitrary code paths
- keep knobs explicit and version-controlled

Add Template C (GEPA-style prompt evolution):

- inputs: dataset view (JSONL), patch points, GEPA-compatible metric
- behavior: run PenguiFlow end-to-end for evaluation; optimizer proposes new patch candidates
- outputs: best patch candidate + eval report + logs

## Risks

- Sensitive data leakage via naive exports.
- Dataset drift and non-representative sampling.
- Over-fitting to trace-derived metrics.

Mitigations:

- safe-by-default export mode + explicit opt-in for sensitive fields
- deterministic truncation + hashing options
- encourage holdout splits and regression gating

## Open Questions (TODO)

- Where should this live? (`penguiflow.evals` module vs templates-only)
- Should the CLI live under `penguiflow-admin` or `penguiflow eval`?
- What is the canonical way to extract the "final" output for a trace across:
  - flow-only traces
  - planner traces
  - paused/resumed traces
- Redaction policy interface: config file vs Python hook?
- Where do tags live initially (Trajectory.metadata vs first-class StateStore index)?
- What are the initial patch points we commit to supporting in docs/templates?

## TODO Checklist (First Iteration)

- [ ] Specify `TraceExampleV1` as an explicit JSON schema (fields, optionality, size limits).
- [ ] Define canonical "final output" extraction rules (flow-only vs planner vs paused/resumed).
- [ ] Define safe export defaults (field allowlist, truncation rules, hashing options).
- [ ] Define exporter selection APIs (by `session_id`, by trace ids, by tail-N).
- [ ] Define minimal metric runner API + report formats (JSONL per-trace + aggregate JSON).
- [ ] Add one concrete example workflow using `InMemoryStateStore` / Playground store.
- [ ] Phase 2: define DSPy template contracts (dataset adapter + metric adapter + output artifact format).

## Appendix: Implementation Notes

### Phase-By-Phase Feature Matrix

This section is intentionally concrete: it enumerates what is already supported in the
current codebase vs what must be implemented to achieve each phase of this RFC.

Phase 1 (Export + Evals MVP)

Supported today:

- Trace persistence primitives: `StateStore.save_event(...)` and `StateStore.load_history(trace_id)`.
- Trajectory persistence capability: `SupportsTrajectories.save_trajectory/get_trajectory/list_traces(session_id)`.
- Planner event persistence capability: `SupportsPlannerEvents.save_planner_event/list_planner_events(trace_id)`.
- `Trajectory.metadata` exists and is persisted/round-tripped via `Trajectory.serialise()`.
- Planner patch points already exist as inputs/attributes:
  - `init_react_planner(..., system_prompt_extra=...)` -> stored as `planner._system_prompt_extra` and used in prompt building.
  - `init_react_planner(..., planning_hints=...)` -> stored as `planner._planning_hints` and injected into system prompt.

Missing (must add):

- Exporter implementation that materializes `TraceExampleV1` from:
  - `get_trajectory(...)` when available
  - otherwise `list_planner_events(...)`
  - otherwise `load_history(...)`
- Canonical final-output extraction rules (flow-only vs planner vs pause/resume), including a stable `outputs.status`.
- Safe export policy implementation:
  - field allowlist
  - truncation rules
  - optional hashing/redaction hooks
- Tagging workflow for dataset curation:
  - define a standard location (MVP: `Trajectory.metadata["tags"]`)
  - add Playground/CLI UX for applying tags
- Export-by-tag support:
  - MVP: exporter scans a bounded set of traces (e.g., `--session-id` + `--limit`) and filters by tags in trajectory metadata
  - future: add an indexed store capability (e.g., `SupportsTraceTags` / `list_traces_by_tag(...)`)
- Dataset view projection implementation (`DatasetViewV1`) and `manifest.json` provenance.
- Metrics runner implementation that:
  - runs a PenguiFlow eval harness per example
  - calls a GEPA-compatible metric signature `(gold, pred, trace, pred_name, pred_trace)`
  - writes per-example JSONL + aggregate report

Phase 2 (Optimization Templates)

Supported today:

- Patch points that are safe to vary without changing runtime code paths:
  - planner system prompt overlay (`system_prompt_extra`)
  - planning hints (`planning_hints`)
- Tool catalog has per-tool descriptions (`NodeSpec.desc`) and tags (`NodeSpec.tags`) that influence planner behaviour.

Missing (must add):

- A patch application mechanism that can update patch points consistently per candidate and record provenance.
  - At minimum this can live in templates, but should produce a serialized candidate config.
- Tool description patching support (`tool.<tool_name>.desc`):
  - define where patches are applied (catalog build time vs prompt render time)
  - ensure patched descriptions flow into `build_system_prompt(...)`
- Optional optimizer integration (templates-only):
  - DSPy dataset loader for `view.jsonl` into `dspy.Example(...).with_inputs(...)`
  - GEPA loop to propose patch candidates using the same metric

- Prefer capability detection via `SupportsTrajectories` / `SupportsPlannerEvents`.
- Reuse `StoredEvent` JSONL formatting patterns from `penguiflow-admin history`.
- Keep exported rows stable and explicitly versioned; allow additive fields.
