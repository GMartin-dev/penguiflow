---
name: penguiflow-agent-reset-template
description: Clone an existing PenguiFlow test agent into a new folder, remove copied state, rewire MCP/tooling for a new idea, and validate it boots cleanly. Use when you want to spin up another experiment agent quickly from a working sibling — not for greenfield agents. For brand-new agents, prefer `penguiflow new <template>` (interactive) or `penguiflow generate <spec.yaml>` (declarative, recommended Jan 2026+); see `TEMPLATING_QUICKGUIDE.md`.
---

# Penguiflow Agent Reset Template

Clone a previous agent, strip copied state, and rewire it for a new experiment quickly and safely.

> **When NOT to use this skill:** For greenfield agents, use [[penguiflow-quickstart]] (`penguiflow new <template>` or `penguiflow generate <spec.yaml>`) — the CLI scaffolders ship the latest template wiring, all `--with-*` enhancement flags, and a known-good `agent.yaml`. Cloning is faster only when you have a *working* sibling agent whose wiring you want to inherit verbatim. After cloning, hand off to the subsystem skill the user actually wants (e.g., [[penguiflow-mcp-integration]] for MCP rewiring, [[penguiflow-reactplanner-config]] for planner knobs).

## Workflow

### 1) Duplicate the source agent folder

Use a full folder copy first so local context stays available:

```bash
cp -R <source-agent-folder> <new-agent-folder>
```

Use a new, idea-specific folder name (`slides-creation`, `github-triage-agent`, etc.).

### 2) Remove copied runtime state before editing code

Always reset copied state artifacts from the new folder:

```bash
cd <new-agent-folder>
rm -rf .venv .pytest_cache __pycache__ */__pycache__
```

Delete leftover output folders that belong to the old idea (downloads, generated docs, temp exports).

### 3) Rewire MCP/tool integrations for the new idea

Update the external tool wiring and planner instructions together:

1. Edit MCP connection/config in `src/.../external_tools.py`.
2. Edit system prompt + planning hints in `src/.../planner.py`.
3. Update `.env` and `.env.example` to keep only relevant variables for the new MCP/tools.
4. Update demo query in `src/.../__main__.py` so smoke run exercises the new domain.

Keep tool namespace names stable and explicit (`slides`, `github`, `youtube`) so prompts and logs are readable.

### 4) Keep `agent.yaml` accurate for Playground frontend

Always edit `agent.yaml` after rewiring code.

Update at least:

1. `agent.name`
2. `agent.description`
3. `agent.template` and relevant `agent.flags`
4. `tools` section to match what the agent actually exposes
5. `planner` section (limits/rich output settings/guidance) to reflect real behavior

Do not leave stale tool descriptions from the copied agent. The Playground frontend reads this file for UI metadata, so drift here causes misleading UX.

### 5) Refresh Python environment from scratch

Recreate environment in the new folder:

```bash
uv venv
uv sync --extra dev
```

If sync fails after a copy, remove `.venv` again and rerun. Never trust copied virtualenvs.

### 6) Validate end-to-end

Run minimum validation pass:

```bash
uv run python -m compileall src tests
uv run --extra dev pytest -q
uv run penguiflow dev --project-root . --port <port> --no-browser
```

Confirm startup logs show connection to the intended MCP endpoint and expected tool discovery.

### 7) Final cleanup and consistency pass

Before finishing:

1. Remove stale references to old agent names in README/docs/tests.
2. Ensure tests import current package/module names.
3. Confirm prompt examples in README match new tools.
