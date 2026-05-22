# LLM Provider Robustness — Branch Plan

> Branch: `feat/llm-rate-limit-handling`
> Status: planning — **do not push until items below are implemented + tested**
> Last updated: 2026-05-22

This document accumulates context and the implementation plan for the LLM
provider robustness work on this branch. Each section is an independent,
shippable work item.

---

## Work Item 1 — Temperature parameter compatibility

### Problem

Some models reject the `temperature` parameter and return HTTP 400. Observed
in production:

```
Unsupported value: 'temperature' does not support 0.0 with this model.
Only the default (1) value is supported. | provider=databricks | status=400
```

```
Error code: 400 - {'error_code': 'BAD_REQUEST', 'message': 'BAD_REQUEST:
Model global.anthropic.claude-opus-4-7 does not support the temperature
parameter.'} | provider=databricks | status=400
```

Two distinct failure modes:

1. **Fixed-value models** — GPT-5 reasoning models on Databricks accept only
   the default temperature (`1`); any explicit value (incl. `0.0`) is a 400.
2. **No-temperature models** — `global.anthropic.claude-opus-4-7` on Databricks
   rejects the `temperature` parameter entirely.

### Root cause

- `LLMRequest.temperature` is typed `float = 0.0` (`penguiflow/llm/types.py:134`)
  — **non-optional**, so providers cannot distinguish "developer explicitly
  chose 0.0" from "nobody set it".
- Provider `_build_params` handling is **inconsistent**:

  | Provider | File | Current behavior |
  |---|---|---|
  | Databricks | `providers/databricks.py:526` | **always sends** ← primary bug |
  | Google | `providers/google.py:289` | always sends |
  | Bedrock | `providers/bedrock.py:380` | always sends |
  | OpenRouter | `providers/openrouter.py:380` | always sends |
  | OpenAI | `providers/openai.py:258` | sends if `not supports_reasoning or temp > 0` |
  | NIM | `providers/nim.py:286` | sends if `not supports_reasoning or temp > 0` |
  | Anthropic | `providers/anthropic.py:339` | sends if `temp > 0` |

- The output strategies (`output/native.py`, `output/prompted.py`,
  `output/tool.py`) **hardcode `temperature=0.0`** in every `LLMRequest` they
  build.
- `LLMClient.generate(temperature=...)` accepts a `temperature` override but it
  is **not threaded** into `call_with_retry` / the output strategies — it is
  currently dead code.

### Decisions (agreed)

1. **Temperature is opt-in.** `temperature` defaults to `None` everywhere and is
   only sent to the provider when a developer explicitly sets it.
2. **Runtime 400 recovery.** When a provider returns a temperature-related 400,
   strip `temperature` and retry once; remember the model as
   temperature-unsupported for the rest of the session.

⚠️ **Behavior change:** structured-output calls currently get an implicit
`temperature=0.0` (deterministic). After this change they send no temperature
unless the developer opts in, so output on temperature-capable models is no
longer guaranteed deterministic. This must be called out in `CHANGELOG.md` and
migration notes. Developers who need determinism must pass `temperature=0.0`
explicitly.

### Plan

**1.1 `LLMRequest` — make temperature optional**
- `penguiflow/llm/types.py`: `temperature: float = 0.0` → `temperature: float | None = None`.

**1.2 `ModelProfile` — add capability flag**
- `penguiflow/llm/profiles/__init__.py`: add `supports_temperature: bool = True`.
  Semantics: `False` = the model rejects an explicit temperature value (drop it,
  log at debug, even if the developer set it).

**1.3 Audit profiles and set `supports_temperature=False`** where applicable:
  - Databricks GPT-5 series (`databricks-gpt-5*`) — fixed-value.
  - `databricks-claude-opus-4-7` — no-temperature. **Databricks route only** —
    native Anthropic `claude-opus-4-7` keeps `supports_temperature=True`
    (confirmed: the restriction is specific to the Databricks-hosted route).
  - OpenAI/Databricks o-series reasoning models — fixed-value.
  - Leave native Anthropic/OpenAI non-reasoning models `True`.

**1.4 Unify provider temperature handling**
- Add a shared helper (e.g. `providers/_params.py`):
  ```python
  def resolve_temperature(profile, temperature):
      if temperature is None:
          return None
      if not profile.supports_temperature:
          logger.debug("model does not support temperature; dropping")
          return None
      return temperature
  ```
- Update **all** providers to: `temp = resolve_temperature(...); if temp is not
  None: params["temperature"] = temp`. Removes the ad-hoc per-provider checks in
  `openai.py` / `nim.py` / `anthropic.py` and fixes the unconditional senders
  (`databricks.py`, `google.py`, `bedrock.py`, `openrouter.py`).

**1.5 Plumb explicit temperature through**
- Output strategies stop hardcoding `temperature=0.0`; accept the value
  (default `None`) from the caller.
- Thread `LLMClient.generate(temperature=...)` → `call_with_retry` →
  `build_request`.
- `LLMClientConfig.temperature`: `0.0` → `None`.
- `protocol.py` `create_native_adapter(temperature=...)` and the
  `JSONLLMClient` default: `0.0` → `None` (public-ish API — note in migration).

**1.6 Runtime 400 recovery**
- Add `_is_temperature_error(message)` matching:
  `"does not support the temperature"`, `"'temperature' does not support"`,
  `"Only the default (1) value"`.
- On a temperature 400: drop `temperature` from params, retry once. Cache the
  model as temperature-unsupported (per-provider-instance set, or
  `register_profile` with `supports_temperature=False`) so later calls skip it.
- Belt-and-suspenders for new/unprofiled models.

**1.7 Tests** (coverage policy: include a negative path)
- `ModelProfile.supports_temperature` defaults `True`; audited models `False`.
- Per-provider `_build_params`: temperature omitted when `None`; omitted when
  profile says unsupported; included when explicitly set and supported.
  Databricks especially.
- 400-recovery: simulated temperature 400 → retried without temperature →
  succeeds → model cached.

**1.8 Docs**
- `CHANGELOG.md` entry + migration note for the determinism behavior change.

### Risks
- Determinism change for structured output (see warning above).
- Default change to `LLMClientConfig.temperature` and `create_native_adapter`.

---

## Work Item 2 — Rate-limit fallback with model cooldown

### Goal

Let developers configure an ordered list of fallback models (plus, optionally,
a pool of API keys). When the active model returns HTTP 429 — **even mid-run** —
the LLM call transparently routes to the next available model and the run
continues with no user-visible interruption. The rate-limited model enters a
**cooldown** (default 45s, configurable) and is skipped until it expires.

Example: a run needs 8 steps; step 6 hits a 429; the call switches model and
step 6 completes; steps 7–8 continue. The user perceives no failure.

### Why this can be transparent

Each ReactPlanner step is a discrete `JSONLLMClient.complete()` call. All
trajectory state (messages, scratchpad, step index) lives in the **planner**,
not the client. Swapping the underlying model between — or within — `complete()`
calls is invisible to the planner.

### Architecture

**New module: `penguiflow/llm/fallback.py`**

`ModelFallbackConfig` — dataclass (the agreed config surface):
- `models: list[str]` — ordered fallback chain, highest priority first.
- `api_keys: list[str] | None = None` — optional key pool. `None` → each
  provider uses its main/env key. Flat list assumed valid for the chain's
  provider(s); per-model keys supported via the richer entry form (see Keys).
- `cooldown_s: float = 45.0` — cooldown applied to a model after a 429.
- `max_wait_s: float = 30.0` — when all models are cooling down, max time a
  call will block waiting for the soonest expiry before failing.
- `from_env(...)` classmethod — build `models` from named env vars.

`FallbackLLMClient` — implements `JSONLLMClient`:
- Constructed with `(primary_model, ModelFallbackConfig, cooldown_store)`.
- **Effective chain** = `[primary_model] + [m for m in config.models if m !=
  primary_model]`. This lets each client (main or auxiliary) lead with its own
  configured model while sharing the same fallback pool.
- Builds one `NativeLLMAdapter` per `(model, key)` entry, lazily/ cached.
- `complete()`:
  1. Select the first `(model, key)` whose cooldown has expired, in priority
     order — selection always restarts from priority 0, so **after a cooldown
     expires the primary is preferred again** (agreed: always revert to primary).
  2. Call its `complete()`.
  3. On `LLMRateLimitError` (429): set cooldown for that `(model, key)`
     (`now + max(cooldown_s, retry_after)`), log `llm_model_cooldown`, advance
     to the next available entry, retry within the same call.
  4. If every entry is cooling down: block up to `max_wait_s` for the soonest
     expiry, then retry; if still exhausted, raise `LLMRateLimitError`.
  5. Non-429 errors propagate unchanged (per-adapter retry already handles
     transient 5xx/timeout).

### Key rotation (agreed: rotate within a model first)

When `api_keys` is provided, on a 429 the client tries the **same model with the
next key** (cheap — same model, different quota bucket) before falling through
to the next model. Cooldown is tracked per `(model, key)` pair. With no
`api_keys`, there is one key per model → pure model fallback.

Open sub-decision (non-blocking): a flat `api_keys` list only works when the
chain shares a provider. For mixed-provider chains, support a richer entry form
where each model carries its own keys. v1 may ship the flat list and add the
richer form if needed.

### Cooldown scope (agreed: per-run now, process-wide later)

The cooldown state lives behind a small `CooldownStore` interface so the scope
can be widened without a rewrite:
- v1 default: `InMemoryCooldownStore` instantiated **per planner run** and
  **shared by all of that run's clients** (main + auxiliary), so a 429 seen by
  the summarizer also protects the main client within the same run.
- Future: a process-wide singleton `CooldownStore` implementation can be swapped
  in so one run's 429 protects all concurrent runs/agents.

### Auxiliary clients (agreed: fallback for all, each keeps its own primary)

All planner LLM clients get fallback — `_client`, `_reflection_client`,
`_summarizer_client`, `_clarification_client`, `_memory_summarizer_client`.
Each is wrapped in its own `FallbackLLMClient` whose **primary** is the model
that client was configured with (e.g. `summarizer_model`), falling back to the
main `llm` model only when the dev did not set a distinct one. They all share
the run's `ModelFallbackConfig` chain and `CooldownStore`. This preserves a
developer's deliberate choice of a different model per auxiliary client while
still giving each one fallback coverage.

### Streaming mid-run (agreed: propagate, recover by continuing)

A 429 raised **before the first chunk** → transparently switch + retry inside
the same `complete()` call.

A 429 raised **after** chunks were already streamed to the user cannot be
un-emitted. In that case the client still records the model's cooldown, then
re-raises the `LLMRateLimitError`. The planner's existing step-level error
recovery (`classify_llm_error` → `RATE_LIMIT`) retries the step; on the retry
the `FallbackLLMClient` selects a fresh (non-cooling) model and the run
continues. Some partial chunks may have reached the user before the switch —
accepted for v1.

### Wiring

- `create_native_adapter()` gains a `fallback: ModelFallbackConfig | None`
  argument (Option A — agreed). When `None` it returns a bare
  `NativeLLMAdapter` (current behavior, untouched); when set it returns a
  `FallbackLLMClient`. Return type is `JSONLLMClient`, the only contract the
  planner depends on, so all ~5 construction sites just thread one extra kwarg
  with no `if/else` branching.
- `ReactPlanner` / `react_init.py`: new `llm_fallback: ModelFallbackConfig |
  None` parameter. `llm` stays a single string (the primary). Auxiliary client
  construction threads the same `llm_fallback` + shared `CooldownStore`.

### Observability

- Structured logs / telemetry events: `llm_model_cooldown` (model, key index,
  cooldown_until, retry_after), `llm_model_switch` (from_model → to_model).
- Routed through the existing `telemetry.py` hooks.

### Tests

- 429 on model #1 → switches to #2, call succeeds, #1 in cooldown.
- Cooldown expiry → primary reused (revert-to-primary).
- Key rotation: 429 rotates to the next key for the same model before switching
  model.
- All entries cooling down → blocks up to `max_wait_s`, then raises.
- Auxiliary client with a distinct primary → leads with its own model, shares
  the cooldown store.
- Negative: a non-429 error is **not** swallowed by the fallback client.
- Mid-run simulation: 8-step planner run, inject 429 at step 6, assert the run
  completes on the fallback model.
- Mid-stream 429: cooldown recorded, error propagates, planner retry succeeds.

### Open questions / non-blocking sub-decisions

1. Flat `api_keys` vs per-model keys for mixed-provider chains (see Keys).
