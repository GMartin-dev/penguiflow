# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Fixed
- Databricks Claude Opus 4.8 reasoning: requests with `reasoning_effort` sent a
  thinking budget that Databricks rejects with 400 ("thinking.type.enabled is not
  supported"). Reasoning request shaping is now profile metadata
  (`ModelProfile.reasoning_request_style`) with model-name heuristics as fallback;
  the new `databricks-claude-opus-4-8` profile routes to adaptive thinking +
  `output_config.effort` (live-verified) and marks temperature unsupported
  (Databricks rejects the parameter for this model — live-verified).

### Added
- LLM call auto-tracing: every `NativeLLMAdapter.complete()` (and therefore every
  `FallbackLLMClient` adapter, with spans attributed to the model actually called)
  can emit one span per LLM call to a pluggable `LLMTraceSink`. Ships with
  `MlflowLLMTraceSink` (MLflow Tracing spans, `span_type="LLM"`, lazy import,
  degrades to no-op when mlflow is absent) and `LoggingLLMTraceSink`. Enable
  explicitly via `create_native_adapter(trace_sink=...)` or transparently with
  `PENGUIFLOW_LLM_TRACING=mlflow` (or `log`) — no agent code changes, no message
  contents captured, default behavior unchanged when unset. The seam sits above
  the `Provider` abstraction, so future transports are traced identically.
- `databricks-claude-opus-4-8` model profile and pricing entries.
- Model profiles for GPT-5.5 / GPT-5.5 Pro, Gemini 3.5 Flash, and Claude Opus 4.7,
  plus their Databricks variants (`databricks-gpt-5-5`, `databricks-gpt-5-5-pro`,
  `databricks-gpt-5-4-mini`, `databricks-gpt-5-4-nano`, `databricks-gemini-3-5-flash`,
  `databricks-claude-opus-4-7`).
- `ModelProfile.supports_temperature` capability flag and runtime recovery: a
  temperature-related provider 400 now disables temperature for that model and
  retries automatically instead of failing the call.
- Rate-limit model fallback: `ModelFallbackConfig` + `FallbackLLMClient` and a new
  `ReactPlanner(llm_fallback=...)` option. On a 429 — even mid-run — the call fails
  over to the next configured model (rotating API keys within a model first) and
  places the rate-limited `(model, key)` pair in a cooldown (default 45s). After
  cooldown the higher-priority model is preferred again. All of a run's planner
  clients share one cooldown store.
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
- **Temperature is now opt-in.** `LLMRequest.temperature`, `LLMClientConfig.temperature`,
  and `create_native_adapter(temperature=...)` default to `None`, meaning no `temperature`
  is sent and the model uses its provider default. Previously structured-output calls
  sent an implicit `temperature=0.0`. Callers that require deterministic output should
  now pass `temperature=0.0` explicitly. This also fixes 400 errors from models that
  reject the `temperature` parameter (e.g. Databricks GPT-5 reasoning models,
  `databricks-claude-opus-4-7`).
- Root README rewritten to be a concise “front door” with stable links.
- Generated tool registries and planner prompt constants now include managed markers so future `apply` runs can update
  only PenguiFlow-owned blocks.
- `penguiflow generate --init` now emits updated assistant instructions that direct ongoing changes through
  `penguiflow apply`.

## 2.12.1

Initial entry for the current packaging version. Prior release notes are being backfilled.
