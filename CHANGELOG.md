# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added
- Enterprise-grade documentation site (MkDocs) and doc CI checks.
- Experimental A2A router continuity support for Phase 0/1, including specialist-side `a2a_context_id`
  session precedence, outbound A2A `contextId` support, and StateStore-backed remote conversation bindings.
- Experimental A2A router Phase 2/3 support, including normalized remote task lifecycle APIs, input/auth-required
  planner pause mapping, push notification config client helpers, agent registry scoring, and router delegation tools.
- Experimental A2A router Phase 4/5 API freeze candidate, including router policy guardrails, declarative registry
  loading, per-agent A2A auth headers, route decision metadata, and tag-triggered PyPI prerelease publishing.
- `penguiflow apply` for safe, Ansible-style reconciliation of spec changes into existing projects without
  overwriting implemented tool files.

### Changed
- Root README rewritten to be a concise “front door” with stable links.
- Generated tool registries and planner prompt constants now include managed markers so future `apply` runs can update
  only PenguiFlow-owned blocks.
- `penguiflow generate --init` now emits updated assistant instructions that direct ongoing changes through
  `penguiflow apply`.
- `Trajectory` gained `final_answer` and `finish_reason` fields, so `serialise()` emits two additional keys and
  exported rows gain `trajectory.finish_reason`. Consumers that assert on the exact serialised key set need
  updating; readers using `.get`/`payload[...]` lookups are unaffected.

### Fixed
- `run_harness_eval` now awaits async metrics. Previously an async metric (including any metric built on the
  async `llm_judge` helper) was invoked without `await`, so the scorer received a coroutine and silently
  recorded `0.0` with no error — only a `coroutine was never awaited` warning. Affected `run_manual_sweep` and
  `run_eval_workflow`, which call the harness; the `penguiflow eval evaluate` CLI and the Playground eval
  runner already awaited correctly and were never affected. Sync metrics are unchanged.
- Exported `TraceExampleV1` rows now populate `outputs.final` with the trajectory's final answer, derived from
  the terminal planner step (covering both the modern `args.answer` and the legacy `args.raw_answer` shapes)
  instead of always emitting `null`. `outputs.final` is now also declared in the row's
  `redaction.fields_included`.
- The planner now records the answer it terminated with on `Trajectory.final_answer`, so it survives
  serialisation and persistence. Previously the terminal action was never appended to `trajectory.steps` and
  the answer travelled only on `PlannerFinish`, so every persisted trajectory — and every exported eval row —
  recorded the route the agent took but not the answer it gave, forcing offline evaluation to re-run the agent
  to score anything. The field is additive: `serialise()` emits it and `from_serialised()` reads it via `.get`,
  so trajectories stored before this release deserialise unchanged with `final_answer=None`. Note the answer
  text now reaches the state store by default; no redaction is applied on that path.
  `final_answer` is recorded only for `answer_complete` terminations: the deadline, hop-budget and
  iteration-limit paths pass the last raw tool observation as their finish payload, so extracting from it
  would promote internal tool data (any tool returning an `answer` key) to the run's answer and export it as
  though the agent had answered the user. Terminations that deliberately produce a user-facing message on a
  failure reason — currently the guardrail STOP path — pass it explicitly instead.
- Exported `TraceExampleV1` rows now report `outputs.status: "error"` for runs that ended without
  answering. Status was previously derived only from step errors, so a run that exhausted its deadline, hop
  budget or iteration limit while every individual step succeeded was exported as `"ok"` and counted as a
  success by `success_rate`. The planner now records `Trajectory.finish_reason`, and the exporter surfaces it
  as `trajectory.finish_reason` on the row (also available in `trajectory_full`) so metrics can distinguish
  *why* a run ended — the normative status enum has no `incomplete`, so all non-answer reasons map to
  `error`. Traces stored before this release carry no reason and keep their previous status.
- `outputs.status` no longer masks flow-level failures. Status was computed from the trajectory alone whenever
  one existed, so a trace whose flow history recorded `node_error`, `node_failed` or `node_timeout` still
  exported as `"ok"` if the trajectory's own steps were clean. Flow events and trajectory steps are
  independent failure signals; a failure recorded in either now yields `"error"`.
- `collect_traces` now verifies that each run's trajectory actually reached the injected `state_store`,
  raising instead of reporting success over an empty store. Previously, if the discovered agent's builder
  or orchestrator did not accept a `state_store` argument, `_call_builder` dropped it silently and the
  planner persisted elsewhere; the compensating save inside the eval wrappers only ran for planners
  *without* `wait_for_trace_persistence`, which `ReactPlanner` always has, so it never fired. Collection
  returned `{"trace_count": N}` after N real runs, tagging silently no-opped, and the subsequent export
  produced zero rows. A persistence `TimeoutError` is now reported through the same path rather than
  swallowed. Stores without `get_trajectory` are unverifiable and skip the check.
- `_call_builder` and `_instantiate_orchestrator` now warn when a `state_store` was supplied but the
  agent's signature cannot accept it, surfacing the misconfiguration before any query is run. It is a
  warning rather than an error because a builder may legitimately construct its own store over the same
  backing database.
- `wrap_metric` no longer raises `TypeError: got multiple values for argument 'gold'` for metrics shaped
  `metric(gold, pred, **kwargs)`. Parameters bound positionally were also being filled into the keyword
  arguments for any metric declaring `**kwargs`. The all-`**kwargs` and explicit five-argument forms were
  unaffected.
- The Playground `/eval/run` fallback now records the answer on `Trajectory.final_answer` as well as
  `metadata["answer"]`. When the state store held no record for a prediction the endpoint synthesizes a
  trajectory itself, and that one path disagreed with every planner-produced trajectory about where the
  answer lives, so a metric reading `pred_trace["final_answer"]` saw `None` — and a later export of that
  trace emitted `outputs.final: null` despite the answer being known.
- `POST /eval/datasets/export` now returns HTTP 400 when the trace selector matches nothing, instead of
  surfacing the underlying `ValueError` as an unhandled 500.
- `GET /traces` now reports each trajectory's `finish_reason`, so the Playground trace list can
  distinguish a run that answered from one that exhausted its budget — previously identical rows. The
  field is omitted when unset. Run `status` is deliberately not exposed here: it needs flow history,
  which this endpoint does not load.
- The Playground UI trajectory store now retains `finish_reason` and `final_answer`. `setFromPayload`
  enumerates fields explicitly, so both were dropped at the parse boundary even though the backend sent
  them.
- `MetricFn` now types metrics as returning a score **or an awaitable** of one, matching the runtime, which
  awaits metrics on every harness path. The signature had been restated in six places across `runner.py`,
  `api.py`, `sweep.py` and `workflow.py`, all declaring a synchronous return; it is now defined once in
  `runner.py` and imported, so `run_harness_eval`, `run_manual_sweep`, `run_eval_workflow`,
  `evaluate_dataset` and `wrap_metric` all advertise async support correctly.

## 2.12.1

Initial entry for the current packaging version. Prior release notes are being backfilled.
