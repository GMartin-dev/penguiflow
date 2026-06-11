# LLM Transport Consolidation — Analysis & Branch Plan

> Branch: `feat/liter-llm-client-analysis`
> Status: **analysis only** — no implementation yet. This document is the
> doc-first artifact for the migration; implementation phases start only after
> the Phase 0 spike gate passes.
> Supersedes: the initial liter-llm-only analysis (this file's first revision).
> Last updated: 2026-06-11

This document analyzes consolidating PenguiFlow's LLM transport — today a
hand-maintained native provider layer (7 providers) plus a legacy `litellm`
path — onto a battle-tested third-party client, and bundling three capability
upgrades into the same effort:

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

**Primary transport candidate: `pydantic-ai-slim`'s direct model layer
(`pydantic_ai.direct.model_request` / `model_request_stream`), pinned `<2`,
behind the existing `Provider` seam, opt-in first.** liter-llm moves to the
watch-list (re-evaluate in ~6 months). `litellm` is exited on a deprecation
schedule — the exit is now grounded in a verified package compromise, not just
CVE count (§4). The native provider layer is not deleted: it **shrinks to the
providers we can actually live-test** (Databricks, OpenRouter), and gains
automated cost tracking and OTel tracing that benefit every transport (§6).

The deciding constraint is **key coverage and trust** (§2): we hold API keys
for only a subset of providers and never will hold all of them. Provider code
we cannot exercise against a live endpoint is unverifiable liability — whether
we wrote it or a 2-month-old project did. The long tail must therefore be
delegated to the upstream with the strongest verifiable trust posture and the
broadest real-world test exposure. Among the candidates (§5), that is the
Pydantic organization: PEP 740 build attestations, a written V1
stability policy, ~weekly releases with a large contributor base, and a stack
we already build on (PenguiFlow is Pydantic-v2-native). It also happens to
solve the "streamline the native layer" wishlist directly: `genai-prices`
(cost tables, maintained by the same org) and OTel GenAI-semconv tracing
(MLflow-ingestable) come with the model layer.

The architecture from the first revision of this analysis is unchanged and
transport-agnostic: integrate behind the existing `Provider` ABC, opt-in flag,
parity gate before any default flip, and ship structured final answers +
multimodal inputs as provider-agnostic PenguiFlow features that do not depend
on which transport wins.

---

## 2. The deciding constraint: key coverage and trust

- We hold live credentials for **Databricks and OpenRouter** (the live
  validation matrix used for the 3.10.0 robustness work). We do not and will
  not hold keys for every provider the native layer claims to support
  (Bedrock, Google, NIM, native Anthropic/OpenAI in all configurations).
- Consequence 1: hand-maintained provider code for key-less providers can
  only be tested against recorded fixtures. Provider APIs drift; fixtures
  don't. Each such provider is an unbounded maintenance liability with no
  verification path — "streamlining" it doesn't change that.
- Consequence 2: the same logic applies to immature third-party transports.
  A library is only a trust upgrade if its providers are exercised by a large
  user base and its supply chain is verifiable. This is the bar liter-llm
  (2 months of stable releases, single primary maintainer, no build
  attestations found) does not yet meet, and litellm structurally failed
  (§4).
- Consequence 3: the providers we **can** test (Databricks above all — the
  production route, with quirks we already fixed in-house: GPT-5 fixed
  temperature, Claude Opus 4.7 no-temperature + reasoning mapping) are
  exactly where in-house code is cheapest and safest to keep. The trust
  argument runs in opposite directions for the head and the tail of the
  provider distribution.

This reframes the goal: **not** "replace the native layer", but "stop owning
provider code we cannot verify, keep owning the provider code only we can
verify."

---

## 3. Current state (what must not break)

### 3.1 The client contract

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

### 3.2 The stack underneath

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

### 3.3 What already exists but is dormant

- `ImagePart` exists in `penguiflow/llm/types.py` (`ContentPart = TextPart |
  ToolCallPart | ToolResultPart | ImagePart`) but the planner path is
  text-only: `NativeLLMAdapter._convert_messages()`
  (`penguiflow/llm/protocol.py:552`) maps `{"role", "content"}` dicts to a
  single `TextPart`.
- `StructuredOutputSpec` exists in the native layer, but the planner's final
  answer is free text inside `FinalPayload.raw_answer` — formatting is
  prompt-trusted, not schema-enforced.

---

## 4. The litellm exit, grounded (verified 2026-06-11)

The exit decision is no longer a judgment call. Verified record:

- **March 24, 2026 — litellm 1.82.7 / 1.82.8 on PyPI were backdoored**
  (GHSA-5mg7-485q-xm76; attacker "TeamPCP", downstream of the March 19 Trivy
  CI-action compromise that stole litellm's PyPI publishing credentials).
  v1.82.8 shipped a `litellm_init.pth` that executed **on every interpreter
  startup** — SDK-only users were fully exposed; ~47k downloads in the ~40
  minutes before quarantine. Payload harvested env vars, SSH keys, cloud
  credentials, CI/CD secrets. Confirmed by litellm's own advisory, Datadog,
  Snyk, Wiz, Sonatype.
- **April–June 2026 — two server-side CVEs exploited in the wild**: pre-auth
  SQLi in proxy key verification (CVE-2026-42208, CVSS 9.3, exploited within
  ~36h of disclosure, leaks upstream provider keys) and MCP command injection
  (CVE-2026-42271, on **CISA KEV** since 2026-06-08). Proxy-side, so they
  don't hit our SDK-only usage — but they are the 24th and 23rd advisories in
  ~26 months, with recurring vulnerability classes (SQLi and SSTI each
  appearing in both 2024 and 2026).
- Footprint: ~13 direct deps (including the full `openai` SDK, `aiohttp` and
  `httpx` both), 94 `requires_dist` entries across extras, ~15 MB wheel,
  Python `<3.14` ceiling.

Note our current pin is `litellm>=1.77.3,<=1.82.6` (`pyproject.toml:84`) —
the upper bound stops **exactly one release before** the malicious 1.82.7.
That was luck, not protection. Action regardless of transport choice:
deprecate the `litellm` extra; until removal, hold the pin below 1.82.7 or
move it to `>=1.83.0` (post-rebuild pipeline) with hash-pinned locks.

---

## 5. Candidate assessment

Hard requirements: async completion + async streaming **with reasoning
deltas** (the Databricks Opus 4.7 work depends on them), OpenAI-style
`json_schema` response format, typed/classifiable errors (429 detection feeds
`FallbackLLMClient`), coverage of OpenAI / Anthropic / Gemini / Bedrock /
Databricks / OpenRouter / NIM + generic OpenAI-compatible, multimodal input,
supply-chain trust, light dependency footprint, Pydantic v2 compatibility.

| | litellm | liter-llm | any-llm (Mozilla) | **pydantic-ai-slim (direct)** | openai-SDK consolidation | streamlined native |
|---|---|---|---|---|---|---|
| Async + streaming | ✓ | ✓ (binding fixed only in v1.4.0) | ✓ | ✓ | ✓ | ✓ (ours) |
| Reasoning deltas | partial | unverified in Python | normalized, **but Databricks-Claude path verified broken** (flag enabled untested, PR #556) | **`ThinkingPartDelta` — best surveyed** | not in OpenAI spec; per-provider sniffing forever | ✓ (we wrote it) |
| `json_schema` output | ✓ | ✓ | ✓ (gaps, e.g. #542) | ✓ native mode + prompted fallback | varies by compat layer (Anthropic compat silently drops it) | ✓ (ours) |
| Typed 429 | ✓ | ✓ | ✓ `RateLimitError(retry_after)` | ✓ `ModelHTTPError.status_code` | ✓ per-SDK | ✓ (ours) |
| Provider coverage | ~100 | 143 | good; no NIM provider | broad; **no Databricks provider** (issue #2947, PR #4036 open) | ~70% of our matrix; Bedrock-Claude and Anthropic-compat not production-viable | 7, mostly untestable by us |
| Multimodal input | ✓ | ✓ image+audio | image ✓, audio unverified | ✓ `ImageUrl`/`AudioUrl`/`BinaryContent` | ✓ where compat layer allows | would build ourselves |
| Cost tracking | ✓ | ✓ in core | ✓ | ✓ `genai-prices` (same org, standalone-usable) | manual | would build ourselves |
| Tracing | callbacks | unconfirmed in Python | ✓ | ✓ OTel GenAI semconv built in; MLflow-ingestable | manual | would build ourselves |
| Supply chain | **backdoored 2026-03** | 1 maintainer, 2.5 months, no attestations found | Mozilla AI, attestations, 2-maintainer bus factor | **Pydantic Inc, PEP 740 attestations, written V1 stability policy** | OpenAI/Anthropic-grade | ours (trusted but unverifiable long tail) |
| Install weight | ~15 MB wheel, 13+ deps | ~0 Python deps (native binary; **no musl wheels**) | 24 pkgs (bundles openai+anthropic SDKs always) | 32 pkgs (`[openai,anthropic]`), no provider SDKs in base | 16 pkgs/SDK | 0 new |
| Maturity risk | mature but compromised + 3.4k open issues | **high** | medium (breaking changes in minors) | low-medium (**2.0 betas in flight → pin `<2`**) | low | n/a |

Eliminated outright: **aisuite** (sync-only, no streaming, dormant since
2025-11), **instructor** (structured-output layer, not a transport — could
still sit on top later), **llm**/CLI-first, **mirascope** (3 providers),
**unify** (pivoted away), **LangChain** (weight).

### Why pydantic-ai-slim (direct) wins

- `pydantic_ai.direct.model_request` / `model_request_stream` are documented
  precisely for "building your own abstractions" without adopting the Agent
  framework. `ModelRequestParameters` supports `output_mode='native'` +
  `output_object` (json_schema) with a `prompted_output` fallback — i.e. the
  downgrade chain from §7-finding-2 exists upstream.
- Streaming events include `ThinkingPartDelta` with provider details — the
  only surveyed library with a first-class reasoning-delta model, which is
  what `on_reasoning_chunk` parity requires.
- `FallbackModel(default, *fallbacks, fallback_on=...)` exists upstream, but
  **we keep our `FallbackLLMClient`** — ours carries cooldown semantics,
  key rotation, and revert-to-primary that production already depends on.
- Trust: the same organization whose validation library is already
  PenguiFlow's core dependency; attestations; stability policy; 17.7k stars;
  weekly releases; 3 OSV advisories ever, all in optional features, all
  patched.
- **The gap is Databricks — and that is the least bad place to have one.**
  pydantic-ai has no Databricks provider, and the generic
  OpenAI-compatible workaround fails Pydantic validation on Databricks-served
  Claude content-block responses (verified at source level). But Databricks is
  the one provider where we hold keys, production traffic, and fresh expertise
  (3.10.0a3 reasoning fix). We keep our native `DatabricksProvider` (or write
  a small custom `Model` subclass — the plug-point is a documented ABC) and
  track upstream PR #4036 for eventual retirement.

### Why not the others

- **liter-llm** (first revision's candidate): genuinely impressive
  engineering, and its provider breadth is unmatched — but every red flag
  lands on the surface we'd depend on: the Python binding (async semantics
  broken until v1.4.0, renames in v1.4/v1.5, fallback and hooks not clearly
  exposed to Python), single primary maintainer, ~2.5 months of stable
  history, no musl wheels, and a compiled core we cannot patch when the next
  Databricks-route quirk appears. **Watch-list: re-evaluate ~2026-12** (≥9
  months of binding stability, attestations, multi-maintainer).
- **any-llm**: closest philosophical fit (wraps official SDKs, unified errors,
  normalized reasoning deltas, dedicated Databricks provider) — but its
  Databricks-Claude reasoning support is non-functional, **verified live
  2026-06-11** against our Databricks workspace (v1.17.0, models
  `databricks-claude-opus-4-7/4-8`, `databricks-gpt-5-5`,
  `databricks-gpt-5-4-mini`):
  - `DatabricksProvider` is an empty `BaseOpenAIProvider` subclass with
    `SUPPORTS_COMPLETION_REASONING = True` flipped on (the flag was enabled
    untested — upstream PR #556). There is no per-model request translation:
    the unified `reasoning_effort` param is forwarded verbatim and Databricks
    Claude rejects it with **400 "reasoning_effort: Extra inputs are not
    permitted"** (both stream and non-stream). PenguiFlow's provider
    translates per family (`databricks.py:558–593`); any-llm does not.
  - Hand-crafting the correct request (`extra_body={"thinking":
    {"type": "adaptive"}}`) gets past Databricks, but **streaming then
    crashes inside any-llm's own types**: `ValidationError: 2 validation
    errors for ChatCompletionChunk — usage.completion_tokens must be int,
    got None` on Databricks' usage chunk. Hard failure, not silent drop.
  - Reasoning normalization only reads top-level string fields
    (`reasoning_content`/`thinking`/`think`/`chain_of_thought`) and
    `<think>`-style XML tags; Databricks Claude's list-shaped reasoning
    content blocks (`type: "reasoning"`, `summary[].text` — what
    `databricks.py:454–463` parses) have no conversion path.
  - GPT-5 family on Databricks works through any-llm (`reasoning_effort`
    accepted), with no visible reasoning stream — consistent with the
    provider hiding reasoning; not a defect.
  Re-evaluate alongside liter-llm.
- **openai-SDK consolidation** (official `openai` SDK against compat
  endpoints): maximal supply-chain trust, but the compat layers are leaky
  precisely where we need them — Anthropic's is documented non-production and
  silently ignores `json_schema` and thinking; Bedrock-Claude's chat
  completions support is contradicted within AWS's own docs; reasoning deltas
  are not in the OpenAI spec at all, so every provider diverges
  (`reasoning_details`, `reasoning_content`, `<think>` tags, content blocks).
  Choosing this means owning per-provider normalization forever — the burden
  we're trying to shed.
- **streamlined native only** (cost + trace automation on the current layer):
  assessed in §6 — the automation is worth shipping, but it does not solve
  the §2 trust problem for the provider long tail.

---

## 6. The "streamlined native" track — adopted in reduced scope

The idea of keeping the native layer but automating cost tracking and tracing
is sound and is **partially adopted** — not as the whole answer, but as
hardening for the providers we keep:

1. **Native layer shrinks to what we can verify.** Keep: `DatabricksProvider`,
   `OpenRouterProvider` (live keys, production traffic). Deprecate-and-delegate
   to the new transport: OpenAI, Anthropic, Google, Bedrock, NIM (NIM is
   OpenAI-compatible and reachable through the transport's compat path).
   Result: 7 hand-maintained providers → 2 we actually test.
2. **Cost tracking via `genai-prices`** (Pydantic's standalone price
   database) replaces our hand-maintained pricing tables as the source of
   truth for both the native providers and the transport; our
   `calculate_cost`/`register_pricing` API stays as the stable facade
   (override-capable for private Databricks rates).
3. **Auto-trace at the adapter edge, transport-agnostic.** One OTel
   GenAI-semconv span per `Provider.complete()` /
   `JSONLLMClient.complete()` call (model, tokens, cost, latency,
   finish_reason; never raw prompts by default), emitted through the existing
   `metrics.py`/`middlewares.py` hooks. MLflow ingests OTel traces, and
   MLflow's tracing integrations list PydanticAI autologging (verify exact
   MLflow version in the spike). This lands once at our seam and covers every
   transport, including the remaining native providers.

What this track does **not** do alone: make untestable provider code
trustworthy. Hence the hybrid in §1.

---

## 7. Findings incorporated (prior internal architecture research)

Internal research on LLM-client architecture for agent runtimes (validated
against 20+ providers in a separate production system) settled several
principles we adopt here rather than re-derive:

1. **The client is transport; the runtime owns intent.** Keep the LLM client
   to one completion method. Parsing, validation, repair, and tool dispatch
   live in the runtime, never in the client. PenguiFlow already conforms
   (`JSONLLMClient` + planner repair loop). The migration must not leak the
   transport's tool-calling or typed-response shapes into the planner.
2. **Structured-output modes need a downgrade chain.** Providers fail
   `json_schema` in diverse ways. The proven chain is
   `json_schema → json_object → prompted-text`, triggered by invalid-schema
   error classification, with an event emitted on each downgrade. PenguiFlow's
   output strategies (`penguiflow/llm/output/`) already implement the modes;
   pydantic-ai's `output_mode='native'` + `prompted_output` matches this
   shape upstream.
3. **Validator-driven retry with a corrective turn.** For schema-enforced
   responses, on validation failure append a corrective message containing the
   validation error and retry, bounded per model profile (default 1 retry).
   This is the mechanism for the new structured final answer — it lives in
   PenguiFlow regardless of transport.
4. **Per-provider quirks are a correction layer, not client flags.** Message
   reordering, schema sanitization, reasoning-effort routing, temperature
   support — all keyed off model profiles. PenguiFlow's profile system is the
   same idea; it must keep working when the transport is third-party code we
   cannot patch.
5. **Multimodal inputs as a content-part sum type with size discipline.**
   Image/audio/file parts with three supply forms (URL, inline data, artifact
   reference), and a hard threshold above which inline data must not reach the
   LLM edge raw. Inputs first; generation/transcription outputs are tools,
   not client methods.
6. **Prompt-engineered and native tool calling can coexist** behind a planner
   opt-in, because both reduce to the same decision shape (`PlannerAction`).
   Native tool calling is explicitly **out of scope** for this branch but the
   provider seam should not preclude it.

---

## 8. Decisions (proposed, revised)

1. **Integration point: the `Provider` ABC, not the adapter.** A
   `PydanticAIProvider` in `penguiflow/llm/providers/pydantic_ai.py` wraps
   `pydantic_ai.direct` behind the existing `Provider` interface. Everything
   above — `NativeLLMAdapter`, output strategies, temperature recovery,
   `FallbackLLMClient`, profiles, pricing facade, telemetry — is reused
   verbatim. *(Unchanged from first revision except the transport.)*
2. **Opt-in first, default later.** Transport selection via
   `create_native_adapter(transport="native" | "pydantic-ai")`, defaulting to
   `"native"`. Default flips per model-family only after the §11 parity gate;
   Databricks and OpenRouter stay on the kept native providers until upstream
   support is proven equivalent. `litellm` extra deprecated immediately (§4
   pin action now, removal on schedule).
3. **Pin `pydantic-ai-slim<2`** with per-provider extras only
   (`[openai,anthropic,google,bedrock]`), hash-pinned lockfiles. Re-evaluate
   the 2.x line after it stabilizes.
4. **Fallback stays ours.** `FallbackLLMClient`/`CooldownStore` semantics
   unchanged; the transport's typed errors (`ModelHTTPError.status_code ==
   429`) become the single classification path. Upstream `FallbackModel` is
   not used.
5. **Structured final answer is a PenguiFlow feature, provider-agnostic.**
   New opt-in `ReactPlanner(final_response_model: type[BaseModel] | None =
   None)`: schema injected into the final-response action, `model_validate`
   on the result, bounded corrective-turn retry (finding 3). `FinalPayload`
   gains `structured: BaseModel | None`; `raw_answer` keeps today's text.
   Default unset → byte-for-byte today's behavior.
6. **Multimodal inputs are a PenguiFlow feature, provider-agnostic.**
   `AudioPart` joins `ImagePart`; `JSONLLMClient.messages` widens
   `Sequence[Mapping[str, str]]` → `Sequence[Mapping[str, Any]]`
   (parameter-type widening — non-breaking). Profile capability flags
   (`supports_image_input`, `supports_audio_input`) fail loudly; 32 KB
   inline-data threshold at the adapter edge.
7. **Cost + trace automation per §6** ships regardless of transport outcome:
   `genai-prices` behind the existing pricing facade; OTel GenAI-semconv
   spans at the adapter seam through existing hooks.
8. **`JSONLLMClient` frozen** except the widening in (6) and formally adding
   the already-shipped `on_reasoning_chunk` as an optional kwarg.
9. **Native tool calling: out of scope, not precluded.**

---

## 9. Non-goals

- Replacing the planner's structured-output-as-intent loop with native tool
  calling (future branch).
- Adopting pydantic-ai's Agent, tools, or graph machinery — **only**
  `pydantic_ai.direct` + model classes + message types. PenguiFlow remains
  the agent runtime.
- Multimodal **outputs** (image generation, TTS, transcription) — tools, not
  client capabilities.
- Removing the kept native providers (Databricks, OpenRouter).
- liter-llm integration (watch-list, re-evaluate ~2026-12).
- Process-wide cooldown store (tracked in the robustness plan).

---

## 10. Phased delivery

### Phase 0 — Spike & go/no-go gate (this branch)

Throwaway harness (not shipped) driving `pydantic-ai-slim<2` via
`pydantic_ai.direct` against the routes we can live-test, with the native
layer as the control. Go/no-go checklist:

- [ ] OpenRouter route through upstream's OpenRouter support: streaming,
      `ThinkingPartDelta` → `on_reasoning_chunk` mapping, `json_schema`
      native mode + prompted fallback, 429 → `ModelHTTPError.status_code`
      mapping.
- [ ] OpenAI + Anthropic + Gemini through upstream model classes against the
      keys we do hold (or trial keys for the spike only): smoke parity on a
      fixed prompt set.
- [ ] Databricks via the kept native provider — confirm the §6 shrink plan
      leaves zero behavior change; additionally test upstream PR #4036's
      branch against Databricks Claude content blocks to size eventual
      retirement.
- [ ] Mid-stream 429 behavior under `model_request_stream` (must match the
      semantics in `docs/LLM_PROVIDER_ROBUSTNESS_PLAN.md`).
- [ ] `genai-prices` vs our pricing tables on production models (delta
      within tolerance; Databricks private rates overridable).
- [ ] OTel span emission at the adapter seam; ingest into MLflow; confirm
      MLflow version requirements.
- [ ] Image part + audio part round-trip on at least one provider each.
- [ ] Dependency audit: `pydantic-ai-slim[...]` lock diff, attestation
      verification, no version conflicts with penguiflow's pins.

**No-go** on any unfixable red item → fall back to the §6 streamlined-native
track alone (shrink + cost + trace), and Phases 2–3 proceed anyway — they
don't depend on the transport.

### Phase 1 — `PydanticAIProvider` (opt-in)

- `penguiflow/llm/providers/pydantic_ai.py` implementing the `Provider` ABC;
  optional extra `penguiflow[pydantic-ai]` with `<2` pin.
- Transport selection in `create_native_adapter` (default `"native"`).
- Error mapping + profile-driven request building (temperature, reasoning,
  schema sanitization) applied before the request enters the transport.
- `litellm` pin action (§4) + deprecation warning on the litellm path.
- Tests: provider conformance suite parameterized over `native` and
  `pydantic-ai` stub transports; mid-stream behavior; negative paths.

### Phase 2 — Structured final answers (provider-agnostic)

- `final_response_model` on `ReactPlanner` per Decision 5; corrective-turn
  retry bounded by profile (default 1).
- Streaming: structured mode keeps `_StreamingArgsExtractor` token streaming;
  validation on the assembled result.
- Tests: valid/invalid/retry-exhausted; default-off byte-parity with today's
  output; template stubs unchanged (protocol identical).

### Phase 3 — Multimodal inputs (provider-agnostic)

- `AudioPart`; planner-level input parts; message widening per Decision 6;
  capability flags on kept-native + transport providers; threshold
  enforcement.
- Tests: parts survive the planner trajectory (incl. trajectory summarization
  — parts must be summarized/stubbed, never inlined); loud failure on
  unsupported provider; threshold violation.

### Phase 4 — Cost + trace automation

- `genai-prices` behind the pricing facade; OTel GenAI spans at the adapter
  seam wired through `metrics.py`/`middlewares.py`; MLflow example.

### Phase 5 — Parity gate, per-family default flips, native shrink

- Full suite + live matrix (Databricks + OpenRouter) green under
  `transport="pydantic-ai"`.
- Flip defaults per model family where upstream is proven; mark OpenAI,
  Anthropic, Google, Bedrock, NIM native providers deprecated (≥2 minor
  releases); `litellm` extra removal scheduled; CHANGELOG migration notes.

### Phase 6 (future, separate branches)

- Native tool-calling planner mode; Databricks native-provider retirement if
  upstream #4036 lands and proves out; liter-llm / any-llm re-evaluation
  (~2026-12); process-wide cooldown store; multimodal outputs as tools.

---

## 11. Acceptance criteria (binding)

1. **Zero breaking changes:** the entire existing test suite passes unmodified
   on every phase; `JSONLLMClient` signature unchanged except documented
   widening; all `penguiflow.llm` / `penguiflow.planner` exports unchanged.
2. A production agent built on 3.10.0 (ReactPlanner + `llm="databricks-..."` +
   `llm_fallback=...`) runs identically with no code changes, with the new
   transport on and off.
3. New features are **opt-in**; defaults reproduce today's behavior exactly
   (including the structured-output prompt content when
   `final_response_model` is unset).
4. Every phase ships unit + negative-path tests and keeps coverage ≥ 84.5%.
5. Default flips (Phase 5) require the live validation matrix per family, not
   just unit tests.
6. Supply-chain hygiene: exact pins + hash-locked resolution for the
   transport; attestation check documented in CI notes.

---

## 12. Open questions

1. pydantic-ai 2.0 timeline and migration surface — how long is the `<2` pin
   sustainable, and does 2.x change `pydantic_ai.direct`? (Watch upstream;
   revisit at Phase 5.)
2. `opentelemetry-api` lands as a hard transitive dep via pydantic-ai-slim —
   acceptable, or does it need to stay behind the optional extra? (It aligns
   with the §6 tracing track, but penguiflow core today has no OTel dep.)
3. Per-model routing string mapping: penguiflow profile names
   (`databricks-claude-opus-4-7`) ↔ transport model identifiers — one mapping
   table in profiles, or adopt upstream naming for new models?
4. Flat `api_keys` in `ModelFallbackConfig` across mixed transports — the
   per-model key form (robustness plan open question) likely becomes
   necessary; resolve before Phase 5.
5. Should `DSPyLLMClient` get the structured-final-answer path too, or is it
   exempt? (It bypasses the native layer entirely.)
6. NIM: served through the transport's OpenAI-compatible path — does any
   production consumer rely on NIM-specific behavior (`reasoning_content`,
   `<think>` tags) that needs a profile correction?

---

## 13. Side finding (live verification 2026-06-11) — pre-existing bug

While verifying any-llm against our Databricks workspace, Databricks rejected
`thinking={"type": "enabled", "budget_tokens": N}` for
**`databricks-claude-opus-4-8`** with: *"thinking.type.enabled" is not
supported for this model. Use "thinking.type.adaptive" and
"output_config.effort"*. PenguiFlow's own provider routes only
`databricks-claude-opus-4-7*` to adaptive thinking (`databricks.py:563`);
Opus 4.8 falls through to the budget branch (`databricks.py:576–584`) and
would hit the same 400 whenever a caller sets `reasoning_effort`. Fix
independently of this branch: widen the adaptive-thinking routing (and the
corresponding profile) to Opus 4.8, with a live regression test.
