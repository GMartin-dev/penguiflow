# `ObservationGuardrailConfig` — Context Overflow Safety Net

This is a **reliability guardrail**, not a policy guardrail. It prevents one bad tool output from blowing the planner's context window. It's enabled by default and lives on a different axis from the `GuardrailGateway`.

## What it does

When a tool returns an observation (its result), the planner has to embed it in the next LLM prompt. A 50 MB tool output would explode the prompt.

The observation clamp:
1. Measures the observation's size.
2. If under the configured thresholds → pass through unchanged.
3. If over threshold:
   - If `auto_artifact_threshold > 0` and an `ArtifactStore` is available → store the observation, return an artifact ref + preview.
   - Otherwise → truncate by field (`max_field_chars`), preserving structure when possible, with `truncation_suffix` appended.

The LLM sees either the full observation, an artifact ref it can dereference, or a structure-preserved truncated version.

## Config (actual fields)

```python
from penguiflow.planner.models import ObservationGuardrailConfig

planner = ReactPlanner(
    ...,
    observation_guardrail=ObservationGuardrailConfig(
        max_observation_chars=50_000,        # default; size threshold for triggering the clamp (>= 1000)
        max_field_chars=10_000,              # default; max chars per field when truncating (>= 100)
        truncation_suffix="\n... [truncated: {truncated_chars} chars]",
        preserve_structure=True,             # default; keep JSON structure (only truncate values)
        auto_artifact_threshold=20_000,      # default; store as artifact if larger (0 disables artifact path)
        preview_length=500,                  # default; preview length included in truncated refs (>= 0)
    ),
)
```

There is no `enabled` flag. The clamp is active whenever an `ObservationGuardrailConfig` is present on `ReactPlanner`; pass `observation_guardrail=None` to opt out (or accept the default by omitting the kwarg — `ReactPlanner` instantiates one for you). Don't disable in production: even "small" tools can occasionally return huge outputs (an MCP server hiccup, an unintended query result).

## Why it's separate from policy guardrails

Different concerns:
- **Policy guardrails** decide *whether* to allow content based on rules. Output: decision.
- **Observation clamp** decides *how* to fit content into the prompt without overflow. Output: transformed content.

The policy gateway never sees the artifact ref — by the time it inspects `tool_call_result`, the observation has already been clamped. If you have a rule that needs to inspect the full content, do so server-side before the result reaches the planner (e.g., inside the tool itself).

## Artifact path

When `auto_artifact_threshold > 0` and an `ArtifactStore` is available, observations larger than that threshold are uploaded; the planner sees a reference plus a short preview (length controlled by `preview_length`).

Benefits:
- Full data preserved.
- LLM can request specific slices via artifact-reading tools.
- Audit trail (artifact stored durably if your store is durable).

Requirements:
- `ArtifactStore` configured ([[penguiflow-statestore]] covers backends).
- Planner tool catalog includes artifact-reading tools (the rich-output `list_artifacts` is one example, but you may want a generic `read_artifact_slice(ref, offset, length)`).

Set `auto_artifact_threshold=0` to disable the artifact path; the clamp will then always truncate.

## Truncation path

When the artifact path is unavailable or disabled:

1. Observation exceeds `max_observation_chars` (or any single field exceeds `max_field_chars`).
2. Truncate field-by-field to `max_field_chars`.
3. Append `truncation_suffix`. The format string supports `{truncated_chars}` interpolation.
4. If `preserve_structure=True` (default) and the observation parses as JSON, drop or stub the largest leaf values first while keeping the tree intact.

This loses data. Use only when you can't run an `ArtifactStore`.

## Structure-preserving truncation

`preserve_structure=True` makes the clamp:
- Parse the observation as JSON when possible.
- Walk the tree; sort fields by size.
- Truncate or stub the largest leaf values first.
- Stop when the serialized form fits.

The LLM gets a valid-JSON observation it can reason about, even if some leaf values are stubbed. Better than blunt char-level truncation that breaks the structure.

## Tuning `max_observation_chars`

| Use case | Suggested value |
|---|---|
| GPT-4o (128k context) | 30,000 - 60,000 |
| Long-context Claude/Opus (200k+) | 50,000 - 100,000 |
| Small model (32k context) | 4,000 - 8,000 |

Balance: too low triggers the clamp on legitimate outputs (artifact refs everywhere); too high allows context bloat. Measure observation size distribution in your workload.

`max_field_chars` is a finer knob: even with a high observation cap, individual fields will be truncated if they exceed `max_field_chars`. Useful when one giant field overshadows the rest.

## Monitoring

Track:
- Observation clamp activation rate (clamps per tool call).
- Per-tool clamp rate — identifies tools with consistently large outputs (candidates for native artifact return).
- Artifact path vs truncation path ratio — should be near 100% artifact in production.
- Size distribution of clamped observations.

A persistent clamp rate >5% on a specific tool means the tool itself should return artifact refs natively, not raw blobs.

## Anti-patterns

- **Setting `observation_guardrail=None`** in production — saves one line of config, costs you a context-overflow incident.
- **Truncation-only mode** (`auto_artifact_threshold=0` without a store) — lossy by design.
- **Tools that paginate to evade the clamp** — they leak to the LLM as N small calls. Better: one call that returns an artifact ref + offset/length.
- **`max_observation_chars` too low** — every observation becomes an artifact; the LLM thrashes calling `read_artifact`.
