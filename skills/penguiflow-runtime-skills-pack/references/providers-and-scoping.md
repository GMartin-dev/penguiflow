# Providers, Scoping, and Composition

The `SkillProvider` protocol lets host apps supply skills at runtime, bypassing the static pack loader. Use for tenant- and user-specific skills.

## `SkillProvider` protocol

Implement (signatures):

```python
from penguiflow.skills.provider import SkillProvider

class MyProvider(SkillProvider):
    async def list(
        self,
        *,
        tenant_id: str | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        **kwargs,
    ) -> Sequence[Skill]: ...

    async def get(
        self,
        names: Sequence[str],
        *,
        tenant_id: str | None = None,
        **kwargs,
    ) -> Sequence[Skill]: ...

    async def search(
        self,
        query: str,
        *,
        tenant_id: str | None = None,
        limit: int = 6,
        **kwargs,
    ) -> Sequence[Skill]: ...
```

The protocol is duck-typed. Implement on any object.

## Wiring

```python
provider = MyProvider()
planner = ReactPlanner(
    ...,
    skills_provider=provider,
    skills=SkillsConfig(
        enabled=True,
        skill_packs=[...],     # static packs still compose
    ),
)
```

Or with a factory (one provider per planner instance):

```python
def factory(planner) -> SkillProvider:
    return MyProvider(planner=planner)

planner = ReactPlanner(
    ...,
    skills_provider_factory=factory,
    skills=SkillsConfig(enabled=True),
)
```

Use the factory when:
- The provider needs a reference to the planner (introspection).
- You're forking planner instances and need per-fork provider state.

## Composition rules

When both static packs and a runtime provider are configured:

1. The provider is called first (`list`/`search`/`get`).
2. The static pack is called next.
3. Results are merged.
4. **Runtime provider wins on `name` collision.** Local packs are fallback.

This lets runtime customize without rewriting the base pack: tenant-specific override of `pack.demo.handle_incident` while keeping the rest of the pack intact.

## Hard rule: enabled flag

If you pass a `skills_provider` while `SkillsConfig.enabled=False`, **the planner raises a configuration error**. The skills subsystem must be enabled to consume any provider.

If you pass a `skills_provider` and omit `SkillsConfig`, the planner internally enables skills with defaults.

## Tenant scoping

The provider methods receive `tenant_id`, `project_id`, `user_id` from `tool_context`. Use them for:
- DB filtering (`WHERE tenant_id = $1`).
- ACL checks ("does this user have access to this skill?").
- Per-tenant overrides ("tenant A uses a custom version of the email skill").

### Pattern: tenant override
```python
class TenantProvider(SkillProvider):
    def __init__(self, db):
        self.db = db

    async def list(self, *, tenant_id=None, **kw):
        if tenant_id is None:
            return []     # fail-closed in multi-tenant
        rows = await self.db.fetch(
            "SELECT * FROM skills WHERE tenant_id = $1", tenant_id
        )
        return [Skill.from_row(r) for r in rows]

    async def search(self, query, *, tenant_id=None, limit=6, **kw):
        if tenant_id is None:
            return []
        rows = await self.db.fetch(
            "SELECT * FROM skills WHERE tenant_id = $1 AND search_index @@ to_tsquery($2) LIMIT $3",
            tenant_id, query, limit,
        )
        return [Skill.from_row(r) for r in rows]

    async def get(self, names, *, tenant_id=None, **kw):
        if tenant_id is None or not names:
            return []
        rows = await self.db.fetch(
            "SELECT * FROM skills WHERE tenant_id = $1 AND name = ANY($2)",
            tenant_id, list(names),
        )
        return [Skill.from_row(r) for r in rows]
```

Fail-closed (return `[]` when `tenant_id` is missing) is the multi-tenant default.

### Pattern: user personalization
```python
class UserPersonaProvider(SkillProvider):
    async def list(self, *, user_id=None, **kw):
        prefs = await load_user_preferences(user_id)
        return build_skills_from_prefs(prefs)
```

## Host-side ACL patterns

Skills can encode operational power — a "rollback prod" skill in the wrong hands is dangerous.

Layer authz at the provider:

```python
async def get(self, names, *, user_id=None, **kw):
    user = await load_user(user_id)
    skills = await self.repo.get_skills(names)
    return [s for s in skills if user.can_access(s)]
```

Don't rely on the planner or applicability metadata to enforce ACLs. The provider is the enforcement point.

## Caching

The planner caches skill content per run (the SQLite store handles static packs; runtime providers see direct calls). If your provider is expensive:

- Cache `list`/`search` results per `(tenant_id, query)`.
- Invalidate when skill content changes (LISTEN/NOTIFY on Postgres; pub/sub on Redis).
- Don't cache `get` — it's already by name and rare.

Beware staleness — a user updating a skill expects to see it immediately, not after a cache TTL.

## Versioning

If skills evolve, track `version` per skill. The provider can:
- Return only the latest version (default).
- Return a specific version if the caller asks.
- Track when a skill was last shown to a user (avoid re-showing recently-seen guidance).

For audit, log `(skill_id, version)` per retrieval event.

## Composition with [[penguiflow-rich-output]]

A common pattern: skills steer rich-output behavior.

A skill says: "When the user asks for quarterly numbers, build a chart with `build_chart_echarts` then a table with `build_table`, compose into `render_grid`."

The planner retrieves the skill, follows the steps. Use applicability gating (`required_tool_names: [render_grid, build_chart_echarts, build_table]`) so the skill only surfaces when those tools are active.

## Anti-patterns

| Anti-pattern | Why it's wrong |
|---|---|
| Provider without ACLs in multi-tenant | One mis-scoped query leaks tenant data |
| Caching `get` results indefinitely | Updates invisible to users |
| Provider returning all skills regardless of tenant | Catastrophic in multi-tenant |
| Embedding secrets in dynamic skill text | Same risk as static packs — LLM-visible |
| Composing without name precedence awareness | Confusion when runtime overrides local pack silently |
