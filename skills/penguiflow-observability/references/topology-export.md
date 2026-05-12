# Topology Export: Mermaid and DOT

## `flow_to_mermaid(flow, direction="TD") -> str`

Returns a Mermaid `flowchart` representation of the flow's adjacency.

```python
from penguiflow import flow_to_mermaid
print(flow_to_mermaid(flow, direction="LR"))
```

`direction`:
- `"TD"` — top-down (default).
- `"LR"` — left-to-right (easier to read for wide graphs).
- `"BT"`, `"RL"` — less common.

Output:
```
flowchart LR
  Opensea --> parse
  parse --> route
  route --> handle_a
  route --> handle_b
  handle_a --> Rookery
  handle_b --> Rookery
```

Drop into a `.md` file inside triple backticks with the `mermaid` language hint — most renderers (GitHub, MkDocs Material, GitLab) render natively.

## `flow_to_dot(flow, rankdir="TB") -> str`

Returns a Graphviz DOT representation.

```python
from penguiflow import flow_to_dot
print(flow_to_dot(flow, rankdir="LR"))
```

`rankdir`:
- `"TB"` (top-to-bottom, default), `"LR"` (left-to-right), `"BT"`, `"RL"`.

Output:
```
digraph G {
  rankdir=LR
  "Opensea" -> "parse"
  "parse" -> "route"
  ...
}
```

Pipe through `dot -Tpng -o flow.png` or render with any Graphviz-compatible viewer.

## What's in the diagram

- Every `Node` with its `name=...`.
- Every edge declared in `create(...)` adjacency tuples.
- Synthetic `Opensea` (ingress) and `Rookery` (egress) endpoints.
- Routers shown as ordinary nodes with multiple outgoing edges (the actual predicate isn't visualized).
- Joins shown as ordinary nodes — `join_k` looks like any other node.

What's **not** in the diagram:
- Runtime state (queue depths, in-flight messages, latencies).
- `NodePolicy` (timeouts, retries, validate mode).
- `allow_cycles` / `allow_cycle` flags.
- Pause/resume boundaries.

For runtime state, use the observability dashboards. For policy info, generate annotations alongside the diagram in your docs build.

## Anonymous nodes

If you skipped `name=` on a node, the diagram shows a generated label that's not human-friendly. Always set `name=` on nodes you expect to appear in diagrams:

```python
# bad
node = Node(my_fn)

# good
node = Node(my_fn, name="my_fn")
```

## CI integration: as-built diagrams

Generate a diagram on every commit and check it in (or render in CI artifacts) to catch unintended topology changes.

```bash
uv run python -c "from app.flow import flow; from penguiflow import flow_to_mermaid; \
  print(flow_to_mermaid(flow, direction='LR'))" > docs/topology.mmd
git diff --exit-code docs/topology.mmd   # fail if changed without intent
```

Or generate PNG:
```bash
uv run python -c "from app.flow import flow; from penguiflow import flow_to_dot; \
  print(flow_to_dot(flow))" | dot -Tpng -o docs/topology.png
```

## "Why doesn't the diagram match what I built?"

Common causes:
- The flow you visualized isn't the flow you run (different factory, different module).
- A node was created inside a function and not connected via adjacency tuples.
- A router declares successors via the predicate but you forgot to wire them in `create(...)`. The diagram only shows declared adjacency — predicate-returned names must also be wired.

Fix: always generate the diagram from the **same `flow` instance** your runtime uses.

## Operational defaults

- Mermaid for README and PR review (renders inline on GitHub).
- DOT for high-fidelity rendering (publications, deep-dive docs).
- `direction="LR"` for wide graphs; `"TD"` for tall ones.
- Stable `Node(name=...)` so diff is meaningful across diagram regenerations.
- Pin Graphviz version in CI if you commit rendered PNGs (layout can shift between versions).

## Security

Diagrams can leak architecture details:
- Internal service names.
- Tool sources (MCP namespaces).
- Manager↔specialist routing topology.

If your repo is public but the topology is sensitive, generate diagrams only in private CI artifacts. Don't commit them.

## What goes well next to a diagram

Bundle a few things alongside the topology export:
- A table of `NodePolicy` per node (`timeout_s`, `max_retries`, `validate`).
- A list of registered models per node (`ModelRegistry`).
- A reference to the relevant SLI dashboard URL.

Together these give a reader the "as-built" picture: shape, policy, contracts, signals.
