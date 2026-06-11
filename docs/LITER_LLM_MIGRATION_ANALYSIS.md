# liter-llm Client Migration — Analysis & Branch Plan

> Branch: `feat/liter-llm-client-analysis`
> Status: **analysis only** — no implementation yet. This document is the
> doc-first artifact for the migration; implementation phases start only after
> the Phase 0 spike gate passes.
> Last updated: 2026-06-11

This document analyzes moving PenguiFlow's LLM transport to
[liter-llm](https://github.com/kreuzberg-dev/liter-llm) and bundling three
capability upgrades into the same effort:

1. **Multimodal inputs** — image (and later audio) content parts reaching the
   model.
2. **Streamlined fallback** — keep the rate-limit fallback shipped in 3.10.0
   transparent, with cleaner error classification underneath.
3. **Structured final answers** — the planner's final response validated
   against a developer-supplied Pydantic model instead of prompt-trusted
   formatting.

**Hard constraint: zero breaking changes.** PenguiFlow is in production. An
agent that upgrades the library must behave identically or better with no code
changes. Every decision below is evaluated against that constraint first.

---

## 1. Summary of the recommendation

**Adopt liter-llm as a new, opt-in provider backend behind the existing
`Provider` seam — not as a replacement for the native layer.** Ship the
structured-final-answer and multimodal features as provider-agnostic layers in
PenguiFlow itself (they do not actually require liter-llm), so they work on the
current native providers on day one and on liter-llm identically. Flip
liter-llm to default only after a parity gate passes, and retire the
hand-maintained providers and the `litellm` optional dependency on a
deprecation schedule.

Rationale in one paragraph: liter-llm's value is **provider breadth (143
providers) and maintenance reduction** (we currently hand-maintain 7 provider
implementations and carry `litellm` as a legacy path). Its weaknesses are
**youth and binding maturity** (v1.0.0 on 2026-03-28; 9 releases in ~10 weeks;
Python binding async/streaming semantics were broken until v1.4.0; breaking
changes in v1.4.0 and v1.5.0; single primary maintainer; no Pydantic
integration; programmatic fallback chains not clearly exposed to Python). The
existing architecture already has the right seam to absorb exactly this
trade-off: the `Provider` ABC (`penguiflow/llm/providers/base.py:22`). Putting
liter-llm behind it gives us the breadth while every layer we've hardened in
production — profiles, temperature recovery, rate-limit fallback, cost
tracking, the planner's parse/repair loop — stays untouched and in control.

---

## 2. Current state (what must not break)

### 2.1 The client contract

`JSONLLMClient` (`penguiflow/planner/models.py:93`) is the only contract the
planner depends on:

```python
class JSONLLMClient(Protocol):
    async def complete(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        response_format: Mapping[str, Any] | None = None,
        stream: bool = False,
        on_stream_chunk: Callable[[str, bool], None] | None = None,
    ) -> str | tuple[str, float]: ...
```

Implementations today: `NativeLLMAdapter` (`penguiflow/llm/protocol.py:161`),
`FallbackLLMClient` (`penguiflow/llm/fallback.py:137`), the legacy
`_LiteLLMJSONClient` (`penguiflow/planner/llm.py:536`), and `DSPyLLMClient`.
`NativeLLMAdapter` and `_LiteLLMJSONClient` additionally accept an
`on_reasoning_chunk` kwarg outside the protocol
(`penguiflow/planner/react_step.py:262–278`).

### 2.2 The stack underneath

```
ReactPlanner step
  └─ JSONLLMClient.complete()
       └─ FallbackLLMClient            (429 → cooldown → model/key rotation)
            └─ NativeLLMAdapter        (dict msgs → LLMMessage; output strategies;
                 │                      temperature-400 recovery; cost)
                 └─ Provider ABC       (validate_request / complete / stream)
                      └─ 7 concrete providers: OpenAI, Anthropic, Google,
                         Bedrock, Databricks, OpenRouter, NIM
```

Supporting machinery that encodes hard-won production knowledge:

- **Model profiles** (`penguiflow/llm/profiles/__init__.py`) — per-model
  capability metadata: `supports_temperature`, reasoning support, context
  windows. Includes route-specific quirks (e.g. `databricks-claude-opus-4-7`
  rejects temperature while native Anthropic does not; Databricks Claude
  Opus 4.7 reasoning mapping fixed in 3.10.0a3).
- **Rate-limit fallback** (`penguiflow/llm/fallback.py`) — shipped in 3.10.0:
  `ModelFallbackConfig`, per-`(model, key)` `CooldownStore`, revert-to-primary,
  mid-stream 429 semantics, auxiliary-client wiring. See
  `docs/LLM_PROVIDER_ROBUSTNESS_PLAN.md`.
- **Planner intent via structured output** — the planner asks for JSON
  (`response_format`), parses a `PlannerAction` (`next_node` + `args`),
  validates against tool schemas, and runs a bounded repair loop on bad JSON.
  Parallel plans are plain data (`next_node="parallel"`, `args.steps`) fanned
  out by the runtime. No provider-native tool calling anywhere.
- **Streaming** — `on_stream_chunk(text, done)` plus in-flight JSON extractors
  (`_StreamingArgsExtractor`, `_StreamingThoughtExtractor`) that surface the
  final answer token-by-token out of the structured JSON envelope.

### 2.3 What already exists but is dormant

- `ImagePart` exists in `penguiflow/llm/types.py` (`ContentPart = TextPart |
  ToolCallPart | ToolResultPart | ImagePart`) but the planner path is
  text-only: `NativeLLMAdapter._convert_messages()`
  (`penguiflow/llm/protocol.py:552`) maps `{"role", "content"}` dicts to a
  single `TextPart`.
- `StructuredOutputSpec` exists in the native layer, but the planner's final
  answer is free text inside `FinalPayload.raw_answer` — formatting is
  prompt-trusted, not schema-enforced.

---

## 3. Findings incorporated (prior internal architecture research)

Internal research on LLM-client architecture for agent runtimes (validated
against 20+ providers in a separate production system) settled several
principles we adopt here rather than re-derive:

1. **The client is transport; the runtime owns intent.** Keep the LLM client
   to one completion method. Parsing, validation, repair, and tool dispatch
   live in the runtime, never in the client. PenguiFlow already conforms
   (`JSONLLMClient` + planner repair loop). The migration must not leak
   liter-llm's tool-calling or typed-response shapes into the planner.
2. **Structured-output modes need a downgrade chain.** Providers fail
   `json_schema` in diverse ways. The proven chain is
   `json_schema → json_object → prompted-text`, triggered by invalid-schema
   error classification, with an event emitted on each downgrade. PenguiFlow's
   output strategies (`penguiflow/llm/output/`) already implement the modes;
   the downgrade trigger should be formalized during this work.
3. **Validator-driven retry with a corrective turn.** For schema-enforced
   responses, on validation failure append a corrective message containing the
   validation error and retry, bounded per model profile (default 1 retry).
   This is the mechanism for the new structured final answer — it lives in
   PenguiFlow because liter-llm has no Pydantic/validation story.
4. **Per-provider quirks are a correction layer, not client flags.** Message
   reordering, schema sanitization (`additionalProperties: false` + `strict`
   for OpenAI-strict vs stripped for permissive providers), reasoning-effort
   routing, temperature support — all keyed off model profiles. PenguiFlow's
   profile system is the same idea; it must keep working when the transport is
   a compiled core we cannot patch.
5. **Multimodal inputs as a content-part sum type with size discipline.**
   Image/audio/file parts with three supply forms (URL, inline data, artifact
   reference), and a hard threshold above which inline data must not reach the
   LLM edge raw. Inputs first; generation/transcription outputs are tools,
   not client methods.
6. **Prompt-engineered and native tool calling can coexist** behind a planner
   opt-in, because both reduce to the same decision shape (`PlannerAction`).
   Native tool calling is explicitly **out of scope** for this branch but the
   provider seam should not preclude it (liter-llm supports parallel native
   tool calls when we want them).

---

## 4. liter-llm assessment (verified 2026-06-11)

What it is: a Rust-core universal LLM client (reqwest + rustls inside the
binary) with PyO3 `abi3-py310` Python bindings. MIT. v1.5.0 (2026-06-08).
Zero Python runtime dependencies. Async-native Python API
(`await client.chat(request)`, `async for chunk in client.chat_stream(...)`).

| Capability | State | Notes for us |
|---|---|---|
| Providers | **143**, prefix-routed (`openai/`, `anthropic/`, `databricks/`, `gemini/`, `bedrock/`, `azure/`, `vertex_ai/`, `custom/`…) | Covers all 7 current providers incl. NIM-style OpenAI-compatible via `custom/` + `register_custom_provider` |
| Auth | SigV4, Vertex ADC, Azure AD in-core | Removes our Bedrock/Google auth code |
| Structured output | OpenAI-style `response_format` incl. `json_schema` + `strict` | **No Pydantic integration, no validation-retry** — stays ours |
| Streaming | OpenAI-shaped chunks; usage only in final chunk | Maps cleanly onto `on_stream_chunk`; reasoning-delta exposure must be verified in the spike |
| Multimodal in | Image parts; audio via `input_audio` (wav/mp3/ogg/flac/m4a) | The enabler for audio support |
| Tool calling | Native, incl. parallel | Unused now; future option |
| Retry/fallback | `max_retries` exposed to Python; FallbackLayer/Router are **Rust/proxy-side**, not clearly exposed to Python | Confirms keeping `FallbackLLMClient` |
| Errors | Typed (`RateLimited`, `ContextWindowExceeded`, `BudgetExceeded`…) | Clean mapping to `LLMRateLimitError` etc. |
| Cost/tokens | `completion_cost`, `count_tokens` in core | Can cross-check our pricing tables |
| Observability hooks | Advertised (`onRequest/onResponse/onError`) but **unconfirmed in Python bindings** | Keep telemetry at our adapter layer regardless |

### Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| Project age (~2.5 months stable), single primary maintainer | High | Opt-in flag + exact version pin (`liter-llm==X.Y.Z`); parity test matrix; native layer remains the escape hatch for ≥2 minor releases |
| Python binding churn (async semantics broken until v1.4.0; renames in v1.4/v1.5) | High | Pin + a thin `LiterLLMProvider` wrapper is the only file touching its API |
| Compiled core — cannot patch provider behavior (e.g. Databricks reasoning quirks we fixed in-house) | Medium | Spike must reproduce the exact Databricks GPT-5 / Claude Opus 4.7 temperature & reasoning scenarios; quirks we can't express upstream stay corrected in our profile layer before the request reaches the provider |
| Wheel coverage: macOS arm64, manylinux2014 x86_64/aarch64, Windows — **no musl/Alpine** | Medium | Audit production deploy images in the spike; sdist requires a Rust toolchain to build |
| PyO3 request/response objects (not Pydantic; `from_json` construction) | Low | Isolated inside the provider wrapper |
| Usage/cost deltas vs our pricing tables | Low | Parity harness asserts cost within tolerance; our tables remain authoritative |

---

## 5. Decisions (proposed)

1. **Integration point: the `Provider` ABC, not the adapter.**
   `LiterLLMProvider` in `penguiflow/llm/providers/liter.py` implements the
   existing `Provider` interface. Everything above it — `NativeLLMAdapter`,
   output strategies, temperature recovery, `FallbackLLMClient`, profiles,
   pricing, telemetry — is reused verbatim. *Rejected alternative:* replacing
   `NativeLLMAdapter` internals with liter-llm wholesale — discards the
   production-hardened correction/recovery layers and couples us to the least
   mature surface of a 2-month-old binding.
2. **Opt-in first, default later.** Selection via
   `create_native_adapter(transport="native" | "liter")` (or equivalent
   config), defaulting to `"native"`. Default flips only when the §8 parity
   gate passes; the hand-written providers then enter deprecation (≥2 minor
   releases) before removal. The `litellm` extra is deprecated on the same
   schedule.
3. **Fallback stays ours.** liter-llm's `max_retries` handles transient
   in-provider retry; 429s surface as typed errors mapped to
   `LLMRateLimitError`, and `FallbackLLMClient`/`CooldownStore` semantics
   (revert-to-primary, key rotation, mid-stream propagation) are unchanged.
   "Streamlining" = one classification path via liter-llm's typed errors
   instead of seven per-provider HTTP-error parsers.
4. **Structured final answer is a PenguiFlow feature, provider-agnostic.**
   New opt-in `ReactPlanner(final_response_model: type[BaseModel] | None =
   None)`. When set: the model's JSON schema is injected into the
   final-response action schema, the returned args are validated with
   `model_validate`, and failures trigger a bounded corrective-turn retry
   (finding 3). `FinalPayload` gains a `structured: BaseModel | None` field;
   `raw_answer` keeps today's text so existing consumers see no change. When
   unset (default): behavior is byte-for-byte today's.
5. **Multimodal inputs are a PenguiFlow feature, provider-agnostic.** The
   planner accepts content parts on the input message; `JSONLLMClient.messages`
   widens from `Sequence[Mapping[str, str]]` to `Sequence[Mapping[str, Any]]`
   (parameter-type widening — non-breaking for every existing implementation
   and caller). `AudioPart` joins `ImagePart` in `penguiflow/llm/types.py`.
   Providers that can't carry a part fail loudly via profile capability flags
   (`supports_image_input`, `supports_audio_input`) — no silent dropping.
   Inline-data size threshold (default 32 KB) above which parts must be passed
   by URL/reference, enforced at the adapter edge.
6. **`JSONLLMClient` protocol is frozen except for the widening in (5).**
   Same method, same kwargs, same return union. `on_reasoning_chunk` should be
   formally added to the protocol (it's already implemented by both shipping
   adapters) — additive, optional, defaulted.
7. **Native tool calling: out of scope, not precluded.** The decision shape
   (`PlannerAction`) stays the single planner contract, so a future
   native-tool-calling mode is an additive planner driver, not a rework.

---

## 6. Non-goals

- Replacing the planner's structured-output-as-intent loop with native tool
  calling (future branch).
- Multimodal **outputs** (image generation, TTS, transcription) — those are
  tools, not client capabilities.
- Removing the native providers or `litellm` in this branch — deprecation
  begins only after the default flip.
- liter-llm's proxy server, caching, budget, or MCP features.
- Process-wide cooldown store (tracked in the robustness plan).

---

## 7. Phased delivery

### Phase 0 — Spike & go/no-go gate (this branch)

Build a throwaway harness (not shipped) that drives liter-llm v1.5.0 against
the routes production actually uses. Go/no-go checklist:

- [ ] Wheel installs in our deploy images (glibc check; no Rust toolchain).
- [ ] Databricks route: GPT-5 fixed-temperature, Claude Opus 4.7
      no-temperature + reasoning mapping — reproduce the exact scenarios from
      `docs/LLM_PROVIDER_ROBUSTNESS_PLAN.md` Work Item 1.
- [ ] `json_schema` response_format on Databricks + OpenRouter + OpenAI;
      observe failure shape for the downgrade classifier.
- [ ] Streaming: chunk cadence, final-chunk usage, reasoning deltas (or their
      absence — determines whether `on_reasoning_chunk` works on this
      transport), behavior on mid-stream 429.
- [ ] Typed error mapping table: `LiterLlmError.*` → `penguiflow.llm.errors`.
- [ ] Image part + audio part round-trip on at least one provider each.
- [ ] Latency/cost parity vs `NativeLLMAdapter` on a fixed prompt set.

**No-go** on any unfixable red item → liter-llm is shelved; Phases 2–3 proceed
anyway on the native layer (they don't depend on it).

### Phase 1 — `LiterLLMProvider` (opt-in)

- `penguiflow/llm/providers/liter.py` implementing the `Provider` ABC;
  optional extra `penguiflow[liter]` with an exact pin.
- Transport selection in `create_native_adapter` (default `"native"`).
- Error mapping + profile-driven request building (temperature, reasoning,
  schema sanitization) applied **before** the request enters the compiled core.
- Tests: provider conformance suite parameterized over `native` and `liter`
  stub transports; mid-stream behavior; negative paths per coverage policy.

### Phase 2 — Structured final answers (provider-agnostic)

- `final_response_model` on `ReactPlanner` per Decision 4; corrective-turn
  retry bounded by profile (default 1).
- Streaming: structured mode keeps `_StreamingArgsExtractor` token streaming;
  validation happens on the assembled result.
- Tests: valid/invalid/retry-exhausted; default-off byte-parity with today's
  output; template stubs updated (`penguiflow new` clients unchanged —
  protocol is identical).

### Phase 3 — Multimodal inputs (provider-agnostic)

- `AudioPart`; planner-level input parts; message widening per Decision 5;
  capability flags on profiles for all 7 native providers + liter; size
  threshold enforcement.
- Tests: parts survive the planner trajectory (incl. trajectory summarization
  — parts must be summarized/stubbed, never inlined into summaries); loud
  failure on unsupported provider; threshold violation.

### Phase 4 — Parity gate & default flip

- Full test suite + live validation matrix (Databricks + OpenRouter minimum,
  precedent: robustness plan) green under `transport="liter"`.
- Flip default; CHANGELOG migration note; native providers + `litellm` extra
  marked deprecated with timeline.

### Phase 5 (future, separate branch)

- Native tool-calling planner mode; process-wide cooldown store; multimodal
  outputs as tools.

---

## 8. Acceptance criteria (binding)

1. **Zero breaking changes:** the entire existing test suite passes unmodified
   on every phase; `JSONLLMClient` signature unchanged except documented
   widening; all `penguiflow.llm` / `penguiflow.planner` exports unchanged.
2. A production agent built on 3.10.0 (ReactPlanner + `llm="databricks-..."` +
   `llm_fallback=...`) runs identically with no code changes, with
   `transport="liter"` on and off.
3. New features are **opt-in**; defaults reproduce today's behavior exactly
   (including the structured-output prompt content when
   `final_response_model` is unset).
4. Every phase ships unit + negative-path tests and keeps coverage ≥ 84.5%.
5. Phase 4 flip requires the live validation matrix, not just unit tests.

---

## 9. Open questions

1. Does the liter-llm Python binding expose reasoning deltas during streaming?
   (Determines `on_reasoning_chunk` parity on the new transport — spike item.)
2. Can per-request provider params we rely on (e.g. Databricks
   reasoning-effort routing) be expressed through liter-llm's request surface,
   or do some models stay native-only after the flip?
3. Flat `api_keys` in `ModelFallbackConfig` across a 143-provider namespace —
   the per-model key form (robustness plan open question) likely becomes
   necessary; resolve before Phase 4.
4. Should `DSPyLLMClient` get the structured-final-answer path too, or is it
   exempt? (It bypasses the native layer entirely.)
