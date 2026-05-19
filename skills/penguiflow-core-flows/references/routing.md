# Routing in the Core Runtime

Routing is how the **graph** (not a planner) makes decisions. Two helpers + an optional policy hook.

## `predicate_router(name, predicate, policy=None)`

Returns a `Node` that:
1. Calls `predicate(message)` to compute targets.
2. Emits the **same** message to each target.

`predicate` can be sync or async. It returns:
- a single `Node`, or
- a string (successor `Node.name`), or
- a sequence of `Node` / `str`, or
- `None` to drop the message.

If a returned name doesn't match any outgoing successor, the runtime raises `KeyError: No successor named ...`.

```python
router = predicate_router("route", lambda msg: "a" if msg.route == "a" else "b")

flow = create(
    router.to(a, b),
    a.to(),
    b.to(),
)
```

## `union_router(name, union_model, policy=None)`

Returns a `Node` that:
1. Validates the input against a Pydantic discriminated union.
2. Reads `kind` from the validated model (falls back to the class name).
3. Routes to the successor whose `Node.name` matches that string.

```python
class CreateUser(BaseModel):
    kind: Literal["create_user"]
    payload: ...
class DeleteUser(BaseModel):
    kind: Literal["delete_user"]
    payload: ...

UserOp = Annotated[CreateUser | DeleteUser, Field(discriminator="kind")]

router = union_router("user_op", UserOp)
flow = create(
    router.to(create_user_node, delete_user_node),
    create_user_node.to(),
    delete_user_node.to(),
)
```

If no successor matches the discriminator, raises `KeyError`.

## `RoutingPolicy` hook

Both routers accept an optional `policy` (sync or async). It runs **after** the predicate/union decision and can override or refine it. Signature:

```python
async def policy(req: RoutingRequest) -> Node | str | Sequence[Node | str] | None: ...
```

`RoutingRequest` exposes:
- `message` — the in-flight message
- `context` — the `ctx` of the router node
- `node` — the router `Node` itself
- `proposed` — what the predicate/union returned
- `trace_id` — current trace id (if envelope flow)

Return `None` to drop, return a different decision to override, return `proposed` to accept.

## `DictRoutingPolicy`

Built-in config-driven policy:

```python
from penguiflow import DictRoutingPolicy

policy = DictRoutingPolicy(
    mapping={"acme": "premium_path", "globex": "standard_path"},
    default="standard_path",
    key_getter=lambda req: req.message.headers.tenant,
)

router = predicate_router("route", predicate=lambda msg: msg.kind, policy=policy)
```

Load mappings from JSON or env:

```python
policy = DictRoutingPolicy.from_json("/etc/agent/routing.json", default="standard_path", key_getter=...)
policy = DictRoutingPolicy.from_env("ROUTING_MAP_JSON", default="standard_path", key_getter=...)
```

## Operational defaults

- Keep routing decisions **pure**. Emit side effects in dedicated nodes downstream.
- Keep routing keys low-cardinality (`kind`, `tenant`, a feature flag).
- One routing layer per decision point. Nested routing gets hard to reason about under retries.
- Don't treat routing policies as authorization — they're easy to bypass with crafted messages. Authz at ingress.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `KeyError: No successor named ...` | Predicate returned a name not connected as successor | Connect the target in `create(...)` and check `Node(name=...)` |
| Message disappears silently | Predicate or policy returned `None` | Log the drop path; ensure intentional |
| Random routing behavior | Predicate uses global mutable state | Make the predicate pure; pass state through `message.meta` |
| Union routes unexpected branch | Discriminator implicit / ambiguous | Make `kind: Literal[...]` explicit on each branch |
| Cross-tenant leak | Routing key doesn't account for tenant | Include `Headers.tenant` in `key_getter` |

## When to use a planner instead

If you find yourself building deeply nested routing with many branches and ad-hoc rules, that's a planning problem. Switch to `ReactPlanner` and let the LLM choose tools — see [[penguiflow-reactplanner-config]].

Rule of thumb: more than 2 routing layers or routes that depend on synthesis → planner. Static branching with ≤2 layers → routers.
