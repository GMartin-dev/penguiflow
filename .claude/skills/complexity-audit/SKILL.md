---
name: complexity-audit
description: Measure cyclomatic complexity across a Python repo to find refactor opportunities and track code health over time. Runs three analyses — ruff C901 violations (functions above a CC threshold), radon repo-wide average and grade distribution (A–F), and a reference-weighted hotspot ranking that prioritizes complex code that's actually used. Use when the user asks about cyclomatic complexity, code complexity, where to refactor, repo hygiene metrics, complexity hotspots, technical-debt prioritization, complexity scores, or wants to add a complexity gate to CI/CD. Outputs human-readable tables plus an optional JSON report suitable for trend tracking and CI artifacts.
---

# Complexity Audit

Quantify how branchy a Python codebase is, identify the worst offenders, and weight the result by how heavily each function is referenced so you focus refactors where they pay back most. The skill produces three complementary views, runnable individually or together via `audit.py`.

## When to use

- "What's our cyclomatic complexity?" / "Where should we refactor?" / "Where's the technical debt?"
- "Add a complexity gate to CI" / "How do I track code health over time?"
- "Which complex functions are actually used?" (hotspot prioritization)
- Reviewing a large PR for added complexity, or a new repo for baseline hygiene.

**Skip when** the user wants line-coverage, runtime profiling, or static-analysis bug-finding — those are different tools.

## What cyclomatic complexity means

CC ≈ "number of independent paths through a function." Every `if`/`elif`/`for`/`while`/`and`/`or`/`except`/`case` adds 1 to a baseline of 1. Standard grade bands:

| CC range | Grade | Reading |
|---------:|:-----:|---------|
| 1–5      | A     | Simple, low risk |
| 6–10     | B     | Manageable |
| 11–20    | C     | Complex — refactor candidate |
| 21–30    | D     | Very complex |
| 31–40    | E     | Extreme |
| 41+      | F     | Effectively untestable |

CC is a *proxy*, not truth. It misses cognitive load from naming, indirection, and state. But it's cheap, deterministic, and tracks well over time.

## The three views

### 1. Violations (ruff `C901`)

Lists every function above the project threshold (default 10). Fast, deterministic, fits in CI. Use this for the **gate**.

```bash
uv run ruff check --select C901 --exclude tests --no-cache <pkg dirs...>
```

Output: each violation prints `is too complex (N > 10)` with file:line. Count them; look at the long tail (CC > 20).

### 2. Repo-wide distribution (radon)

Computes CC for every function/method/class (regardless of threshold) and reports an overall average and grade distribution. Use this as the **trend metric** in CI.

```bash
uvx --from radon radon cc <pkg dirs...> --exclude "**/tests/**" -s -a
```

Output ends with `Average complexity: <Grade> (<float>)`. A healthy repo sits at A (1–5).

### 3. Reference-weighted hotspots (custom)

Combines the two by counting word-boundary references to each function name across the source tree, then ranks by `CC × refs`. This surfaces the functions whose complexity matters most because they're called from many places. Use this for **prioritization**.

The provided `audit.py` does all three views in one pass and can emit JSON for CI ingestion.

## Workflow for the agent

1. **Identify the scope.** Default to the top-level package directory(ies) plus `examples/` and `scripts/` if present. Exclude `tests/`, `.venv/`, `node_modules/`, `build/`, `dist/`, `__pycache__/`. If unsure which dirs to include, ask once or run `ls` and infer.
2. **Run `audit.py`** with the project's scope. Prefer `uv run --with radon python audit.py ...` if the repo uses `uv`; otherwise fall back to `python audit.py ...` after ensuring `radon` and `ruff` are available (`pip install radon ruff` or `uvx`).
3. **Present three sections** in your reply: violation count + top offenders, repo-wide average + grade distribution, and weighted hotspots. Keep tables short (top 10–20 rows).
4. **State the caveats** (see below) before any recommendation — they materially affect interpretation.
5. **Offer follow-ups**: focused refactor on a specific module, a CI gate config, or a trend-tracking job.

## How to interpret the numbers

- **Plain average CC < 5 (Grade A)**: healthy overall. Most refactoring should be targeted, not sweeping.
- **Plain average CC 5–10 (Grade B)**: drift; pick the worst module and reduce.
- **Plain average CC > 10**: systemic — push for architectural review, not function-by-function cleanup.
- **Weighted average meaningfully lower than plain average**: complex code is rarely called → long-tail debt, lower urgency. Focus on Phase 3 hotspots.
- **Weighted average ≈ plain average or higher**: complexity sits on hot paths → high-impact refactors available; address immediately.
- **Violation count growing PR-over-PR**: add the CI gate (next section) before the slope worsens.
- **Functions with CC > 40 and refs > 20**: top-priority refactor targets. Almost always there's a state-machine, dispatcher, or god-function that can be split.

## CI integration

Two patterns, often used together:

### Pattern A — Hard gate (blocks merge)

Fail CI if any function exceeds a chosen ceiling. Start lenient (e.g., 30) and ratchet down each quarter so existing code doesn't have to be fixed all at once.

```yaml
# .github/workflows/complexity.yml (excerpt)
- name: Cyclomatic complexity gate
  run: |
    uvx ruff check --select C901 --config 'lint.mccabe.max-complexity = 30' \
      --exclude tests <pkg>
```

Configure the threshold in `pyproject.toml` so it lives in code review, not workflow YAML:

```toml
[tool.ruff.lint.mccabe]
max-complexity = 30  # ratchet down over time
```

### Pattern B — Trend artifact (informational)

Run `audit.py --format json` on every push to `main`, upload as a workflow artifact, and chart `weighted_average` and `violation_count` over time. No build break; visibility only.

```yaml
- name: Complexity report
  run: |
    uv run --with radon python .claude/skills/complexity-audit/audit.py \
      --scope <pkg dirs> --format json > complexity.json
- uses: actions/upload-artifact@v4
  with:
    name: complexity-report
    path: complexity.json
```

Combine: gate at the ceiling, trend at the average. Both numbers live in the same `audit.py` run.

## Caveats (state these honestly)

1. **Static refs ≠ runtime call frequency.** A function with one call site in a hot loop matters more than one with 50 cold call sites. For ground truth, profile with `scalene`/`cProfile` on representative workloads.
2. **Name collisions inflate reference counts.** Method names that overlap with builtins (`list`, `get`, `set`) or recur across classes (`run`, `step`, `complete`, `main`, `__init__`) get conflated. `audit.py` filters a default noise list; extend it via `--noise-names`.
3. **No type/import resolution.** The reference count is a regex over identifiers, not a real call graph. Precise resolution needs `jedi`/`pyright`/`pyan3`; trade-off is speed and portability.
4. **CC ≠ readability.** A 30-CC dispatcher with a clean lookup table may be easier to maintain than a 12-CC function with nested closures. Use the numbers as a signal, not a verdict.
5. **Test code excluded by default.** That's deliberate — tests are repetitive and would skew averages downward. If you want test usage to count as a "this is exercised" signal, pass `--include-tests`.

## Quick start (one-liner)

From the repo root:

```bash
uv run --with radon python .claude/skills/complexity-audit/audit.py \
  --scope src --exclude tests
```

Adapt `--scope` to the project's top-level package dir(s). The script auto-installs `ruff` and `radon` if missing under `uv`.

## Companion artifact

- `audit.py` — runs all three views, prints human-readable tables, supports `--format json` for CI. Self-contained: only requires `ruff` (installed) and `radon` (auto-pulled via `uv run --with radon` or `pip`).
