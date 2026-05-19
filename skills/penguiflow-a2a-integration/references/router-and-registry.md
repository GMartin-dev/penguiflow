# Router Topology: `AgentRegistry`, `RouterPolicy`, `A2ARouterToolset`

When a manager agent needs to choose among multiple specialist A2A agents, use the registry + router toolset together.

## `AgentRegistry`

In-memory registry of remote A2A agents.

```python
from penguiflow_a2a import AgentRegistry, RemoteAgentRecord, RemoteSkillRecord

registry = AgentRegistry()
registry.register(RemoteAgentRecord(
    agent_url="https://specialist-a.example.com",
    name="Specialist A",
    description="Handles finance queries",
    version="1.0.0",
    skills=(
        RemoteSkillRecord(
            agent_url="https://specialist-a.example.com",
            skill_id="answer",
            name="answer",
            description="Answer a finance question",
            tags=("finance",),
            input_modes=("text/plain",),
            output_modes=("application/json",),
        ),
    ),
    streaming=True,
    push_notifications=False,
    tenant_id="acme",
    trust_tier="standard",     # untrusted | standard | trusted
    latency_tier="standard",   # fast | standard | slow
    auth_schemes=("bearer",),
))
```

Or register from a card (auto-derives skills):

```python
registry.register_card(
    agent_url="https://specialist-b.example.com",
    card=card_dict,
    tenant_id="acme",
    trust_tier="trusted",
    latency_tier="fast",
    auth_schemes=("oauth",),
    metadata={"region": "us-east-1"},
)
```

### Declarative loading

```python
from penguiflow_a2a import load_agent_registry_config

registry = load_agent_registry_config("/etc/manager/agents.yaml")
```

Or in-memory:

```python
registry = AgentRegistry.from_config({
    "agents": [
        {"agent_url": "https://specialist-a/", "card": {...}, "tenant_id": "acme",
         "trust_tier": "standard", "latency_tier": "standard", "auth_schemes": ["bearer"]},
        {"agent_url": "https://specialist-b/", "card": {...}, "tenant_id": "acme",
         "trust_tier": "trusted", "latency_tier": "fast"},
    ],
})
```

The loader normalizes URLs (`_normalize_agent_url`), validates the shape, and coerces tier enums.

## `RouterPolicy`

Production guardrails applied **before** route scoring.

```python
from penguiflow_a2a import RouterPolicy

policy = RouterPolicy(
    allowed_agents=("https://specialist-a/", "https://specialist-b/"),
    denied_agents=(),
    max_candidates=5,
    require_same_tenant=True,
    min_trust_tier="standard",
    required_execution_mode=None,
    required_auth_schemes=("bearer",),
    fallback_agent_url="https://specialist-a/",
    fallback_skill="answer",
    timeout_s=30.0,
    poll_interval_s=0.25,
    max_poll_attempts=120,
)
```

The policy filters the candidate set:
- `allowed_agents` whitelist (empty = allow all).
- `denied_agents` blacklist.
- `require_same_tenant` enforces `tenant_id` match between request and agent.
- `min_trust_tier` excludes lower-tier agents.
- `required_execution_mode` filters by capability (`stream`, `task`, `blocking`).
- `required_auth_schemes` filters by supported auth.

If the filter empties the candidate set, the policy can fall back to `fallback_agent_url`/`fallback_skill`.

## `AgentRouteRequest`

Inputs the registry uses to score candidates.

```python
from penguiflow_a2a import AgentRouteRequest

request = AgentRouteRequest(
    query="What was Q3 revenue?",
    skill="answer",
    input_mode="text/plain",
    output_mode="application/json",
    tenant_id="acme",
    required_execution_mode="stream",
    auth_schemes=("bearer",),
    min_trust_tier="standard",
)

candidates = registry.score(request, policy=policy)
```

Scoring considers:
- Skill name match (exact > tag overlap > description fuzz).
- Input/output mode compatibility.
- Tenant scope.
- Trust and latency tiers (higher trust + faster latency = higher score).
- Auth scheme overlap.

Result is a list of `AgentRouteCandidate(agent, skill, score, reasons)` sorted by score, capped at `policy.max_candidates`.

The `reasons` field is human-readable — emit it via observability to debug routing decisions.

## `A2ARouterToolset`

Wraps the registry as a planner tool. The planner calls one of:
- `router.delegate(query=..., skill=...)` — picks the top candidate and delegates.
- Manual: planner reads candidates, then calls a specific `A2AAgentToolset.tool(...)`.

```python
from penguiflow_a2a import A2ARouterToolset

router_toolset = A2ARouterToolset(
    registry=registry,
    policy=policy,
    transport_factory=lambda agent_url: A2AHttpTransport(base_url=agent_url, ...),
)

node_spec = router_toolset.delegate_tool(
    name="route_and_delegate",
    desc="Route the user's query to the best specialist agent",
)
```

Inputs (`RouterDelegationArgs`):
- `query: str`
- `skill: str | None`
- `tenant_id: str | None`
- ... (mirrors `AgentRouteRequest`)

Output (`RouterDelegationResult`):
- `agent_url: str`
- `skill: str`
- `score: float`
- `reasons: tuple[str, ...]`
- `result: Any` — what the specialist returned.

The router toolset reuses `A2AAgentToolset` under the hood for the actual call, so conversation continuity, execution-mode selection, and cancellation propagation all work.

## Patterns

### Pattern 1: Strict policy + fallback

When uptime matters more than optimality:

```python
policy = RouterPolicy(
    allowed_agents=(),
    require_same_tenant=True,
    fallback_agent_url="https://always-on.example.com/",
    fallback_skill="answer",
)
```

If filtering empties the set, the fallback agent handles the request.

### Pattern 2: Explain-only routing

Planner reads `candidates` and explains its choice to the user before delegating:

```python
candidates = registry.score(request, policy=policy)
# planner inspects `reasons` and confirms with the user before calling delegate_tool
```

### Pattern 3: Multi-skill specialists

A single specialist can register multiple `RemoteSkillRecord`s. Score them independently with `request.skill` set.

## Operational rules
- `agent_url` is the routing key. Normalize URLs (trailing slash, scheme) at registration.
- `tenant_id` is the multi-tenant boundary. `require_same_tenant=True` is the production default.
- Treat `RouterPolicy` as authorization input, not the only check. Don't allow callers to override `denied_agents`.
- Log `reasons` for every route decision — it's the audit trail.
- Refresh the registry on a schedule if agent cards change; the in-memory `AgentRegistry` is mutable.
