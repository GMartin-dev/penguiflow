# `ObservationGuardrailConfig` — Context Overflow Safety Net

This is a **reliability guardrail**, not a policy guardrail. It prevents one bad tool output from blowing the planner's context window. It's enabled by default and lives on a different axis from the `GuardrailGateway`.

## What it does

When a tool returns an observation (its result), the planner has to embed it in the next LLM prompt. A 50 MB tool output would explode the prompt.

The observation clamp:
1. Measures the observation's size.
2. If under threshold → pass through unchanged.
3. If over threshold:
   - If an `ArtifactStore` is available → store the observation, replace with `{"artifact_ref": ...}`.
   - Otherwise → truncate (optionally JSON-preserving).

The LLM sees either the full observation, an artifact ref it can dereference, or a truncated version with a note.

## Config

```python
from penguiflow.planner.models import ObservationGuardrailConfig

planner = ReactPlanner(
    ...,
    observation_guardrail=ObservationGuardrailConfig(
        enabled=True,
        max_chars=...,                # threshold for triggering clamp
        prefer_artifact=True,         # try artifact store first
        truncate_preserves_json=True, # try to keep valid JSON when truncating
        truncation_note="[truncated]",
    ),
)
```

Defaults are reasonable. Don't disable in production — even "small" tools can occasionally return huge outputs (an MCP server hiccup, an unintended query result).

## Why it's separate from policy guardrails

Different concerns:
- **Policy guardrails** decide *whether* to allow content based on rules. Output: decision.
- **Observation clamp** decides *how* to fit content into the prompt without overflow. Output: transformed content.

The policy gateway never sees the artifact ref — by the time it inspects `tool_call_result`, the observation has already been clamped. If you have a rule that needs to inspect the full content, do so server-side before the result reaches the planner (e.g., inside the tool itself).

## Artifact path

When `prefer_artifact=True` and an `ArtifactStore` is available:

1. Observation exceeds `max_chars`.
2. Clamp uploads the full observation as an artifact (binary or text).
3. Returns `{"artifact_ref": ArtifactRef(...)}` to the planner.
4. The LLM sees the ref. It can call `list_artifacts(...)` / `download_artifact(...)` if you've exposed those tools.

Benefits:
- Full data preserved.
- LLM can request specific slices via tools.
- Audit trail (artifact stored durably if your store is durable).

Requirements:
- `ArtifactStore` configured ([[penguiflow-statestore]] covers backends).
- Planner tool catalog includes artifact-reading tools (the rich-output `list_artifacts` is one example, but you may want a generic `read_artifact_slice(ref, offset, length)`).

## Truncation path

When artifact path is unavailable (no store, or `prefer_artifact=False`):

1. Observation exceeds `max_chars`.
2. Truncate to `max_chars`.
3. Append `truncation_note`.
4. If `truncate_preserves_json=True` and the observation parses as JSON, attempt to drop nested values to fit (truncate from the deepest values first).

This loses data. Use only when you can't run an `ArtifactStore`.

## JSON-preserving truncation

If `truncate_preserves_json=True`:
- Parse the observation as JSON.
- Walk the tree; sort fields by size.
- Truncate the largest leaf values first, replacing with `"<truncated>"`.
- Stop when the serialized form fits.

The LLM gets a valid-JSON observation it can reason about, even if some leaf values are stubbed. Better than blunt char-level truncation that breaks the structure.

## Tuning `max_chars`

| Use case | Suggested `max_chars` |
|---|---|
| GPT-4o (128k context) | 20,000 - 40,000 |
| Long-context Claude/Opus (200k+) | 40,000 - 80,000 |
| Small model (32k context) | 4,000 - 8,000 |

Balance: too low triggers the clamp on legitimate outputs (artifact refs everywhere); too high allows context bloat. Measure observation size distribution in your workload.

## Monitoring

Track:
- Observation clamp activation rate (clamps per tool call).
- Per-tool clamp rate — identifies tools with consistently large outputs (candidates for native artifact return).
- Artifact path vs truncation path ratio — should be near 100% artifact in production.
- Size distribution of clamped observations.

A persistent clamp rate >5% on a specific tool means the tool itself should return artifact refs natively, not raw blobs.

## Anti-patterns

- **Disabling the clamp** — saves one config line, costs you a context overflow incident.
- **Truncation mode in production** — lossy. Wire an artifact store.
- **Tools that paginate to evade the clamp** — they leak to the LLM as N small calls. Better: one call that returns an artifact ref + offset/length.
- **`max_chars` too low** — every observation becomes an artifact; LLM thrashes calling `read_artifact`.

## Interaction with policy guardrails

Order of operations on `tool_call_result`:
1. Observation clamp runs first.
2. Policy guardrail sees the clamped observation.

So policy rules that inspect tool output see either the original (small) content or an artifact ref. If your rule needs to enforce policy on the raw content, do it inside the tool, not via the gateway.

## When the clamp itself fails

If the artifact upload fails (store unavailable, network):
- The clamp falls back to truncation.
- A warning is logged.
- Execution continues.

If truncation also fails (corrupt JSON, encoding issue):
- The clamp returns a minimal stub: `{"error": "observation_clamp_failed", "size": N}`.
- A warning is logged.
- Execution continues.

Never blocks the run. This is reliability, not a hard fence.
