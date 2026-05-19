# `penguiflow apply`

## What it is / when to use it

`penguiflow apply` reconciles an existing agent project with an agent spec without replacing implemented tool code.

Use it after a scaffolded project has real code in `tools/`, `planner.py`, or orchestration modules and you want to add spec-declared tools or refresh managed wiring.

## Contract surface

```bash
penguiflow apply --spec agent.yaml [--output-dir=.] [--check] [--diff] [--force]
```

Default behavior:

- create missing tool stubs from `tools:` entries in the spec
- create missing tool tests
- update managed tool registry blocks in `src/<package>/tools/__init__.py`
- update managed prompt constants in `src/<package>/planner.py`
- refresh the project-root `agent.yaml` used by the playground
- skip existing tool implementation files
- skip markerless wiring files unless `--force` is passed

## Ownership model

PenguiFlow treats generated project files as one of three categories:

| Category | Examples | Apply behavior |
| --- | --- | --- |
| Managed blocks | tool imports, tool registry entries, prompt constants | Updated in place |
| Create-once files | `tools/<name>.py`, tool tests | Created if missing, never overwritten by default |
| User-owned code | tool implementations, custom clients, custom orchestration logic | Never touched |

Managed files use markers such as:

```python
# <penguiflow:managed tool-registry>
registry.register("search_documents", SearchDocumentsArgs, SearchDocumentsResult)
# </penguiflow:managed tool-registry>
```

Code outside managed markers is developer-owned.

## Adding a tool to an existing project

1. Add the tool to `agent.yaml`:

```yaml
tools:
  - name: normalize_data
    description: Normalize fetched records
    side_effects: pure
    args:
      items: list[str]
    result:
      normalized: list[str]
```

2. Preview the reconciliation:

```bash
penguiflow apply --spec agent.yaml --check --diff
```

3. Apply the missing scaffold and managed wiring:

```bash
penguiflow apply --spec agent.yaml
```

4. Implement `src/<package>/tools/normalize_data.py`.

5. Update `planner.system_prompt_extra` in `agent.yaml` so the model knows when to use the tool, then run `penguiflow apply --spec agent.yaml` again.

## Options

| Flag | Behavior |
| --- | --- |
| `--check` | Preview changes without writing files; exits non-zero if changes are needed |
| `--diff` | Print unified diffs for managed updates |
| `--force` | Replace markerless generated wiring files; tool implementation files are still preserved |
| `--output-dir` | Directory containing the project directory; if it already looks like a generated project root, `apply` uses it directly |
| `--quiet` | Suppress output |
| `--verbose` | Print progress context |

## Relationship to `generate`

Use `penguiflow generate` to bootstrap a project from a spec. Once a project contains implemented tools or custom planner/orchestrator code, use `penguiflow apply` for ongoing spec changes.

Do not rerun `penguiflow generate --force` to add a tool to an existing customized project; that can replace generated planner/config/wiring files.
