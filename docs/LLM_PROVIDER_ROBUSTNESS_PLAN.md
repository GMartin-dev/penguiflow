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

**1.3 Audit profiles and set `supports_temperature=False`** where applicable.
  Recommended initial set (to confirm):
  - Databricks GPT-5 series (`databricks-gpt-5*`) — fixed-value.
  - `databricks-claude-opus-4-7` — no-temperature.
  - OpenAI/Databricks o-series reasoning models — fixed-value.
  - Leave native Anthropic/OpenAI non-reasoning models `True`.
  Open question: does the no-temperature restriction apply only to the
  Databricks-hosted Claude route, or also native `claude-opus-4-7`? Default
  assumption: Databricks route only.

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

## Work Item 2 — Rate-limit handling

_To be detailed — additional context pending._
