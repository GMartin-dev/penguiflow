# Providers, Scoping, and Composition

The `SkillProvider` protocol lets host apps supply skills at runtime, bypassing the static pack loader. Use for tenant- and user-specific skills.

## `SkillProvider` protocol

The actual protocol (`penguiflow.skills.provider.SkillProvider`) is wider than a basic list/get/search trio. All methods accept typed query objects from `penguiflow.skills.models` plus a keyword-only `tool_context` (`Mapping[str, object]`) and optional `capability_context: SkillCapabilityContext | None`:

```python
from collections.abc import Mapping, Sequence
from penguiflow.skills.models import (
    RetrievalResponse, SkillCapabilityContext, SkillDirectoryEntry,
    SkillListRequest, SkillListResponse, SkillQuery, SkillResultDetailed,
    SkillSearchQuery, SkillSearchResponse, SkillsDirectoryConfig,
)
from penguiflow.skills.provider import SkillProvider

class MyProvider(SkillProvider):
    async def get_relevant(self, query: SkillQuery, *, tool_context, capability_context=None) -> RetrievalResponse: ...
    async def search(self, query: SkillSearchQuery, *, tool_context, capability_context=None) -> SkillSearchResponse: ...
    async def get_by_name(self, names: list[str], *, tool_context, capability_context=None) -> list[SkillResultDetailed]: ...
    async def list(self, req: SkillListRequest, *, tool_context, capability_context=None) -> SkillListResponse: ...
    async def directory(self, config: SkillsDirectoryConfig, *, tool_context, capability_context=None) -> Sequence[SkillDirectoryEntry]: ...
    async def format_for_injection(self, skills: Sequence[SkillResultDetailed], *, max_tokens: int) -> tuple[str, int, int, bool]: ...
```

`tool_context` is the scoping vehicle — read `tenant_id`/`project_id`/`user_id` from it. `capability_context` (built by `build_skill_capability_context(...)`) is what the provider should apply for applicability filtering when present.

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

Or with a factory — `SkillProviderFactory = Callable[[SkillsConfig], SkillProvider]`:

```python
def factory(config: SkillsConfig) -> SkillProvider:
    return MyProvider(config=config)

planner = ReactPlanner(
    ...,
    skills_provider_factory=factory,
    skills=SkillsConfig(enabled=True),
)
```

The factory receives the live `SkillsConfig`. Use it when the provider needs to look at the planner's configuration (cache_dir, top_k, packs) at construction time.

## Composition rules

When both static packs and a runtime provider are configured:

1. The runtime provider is called first.
2. The local SQLite store (loaded from `skill_packs`) is called next.
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
Read scoping fields from `tool_context` inside each method. Return the typed response models from `penguiflow.skills.models` — empty responses are fail-closed in multi-tenant deployments.

```python
class TenantProvider(SkillProvider):
    def __init__(self, db): self.db = db

    async def search(self, query, *, tool_context, capability_context=None):
        tenant_id = tool_context.get("tenant_id")
        if not tenant_id:
            return SkillSearchResponse(results=[])         # fail-closed
        rows = await self.db.fetch(
            "SELECT * FROM skills WHERE tenant_id = $1 AND search_index @@ to_tsquery($2) LIMIT $3",
            tenant_id, query.query, query.limit,
        )
        return SkillSearchResponse(results=[SkillSearchResult.from_row(r) for r in rows])

    async def get_by_name(self, names, *, tool_context, capability_context=None):
        tenant_id = tool_context.get("tenant_id")
        if not tenant_id or not names:
            return []
        rows = await self.db.fetch(
            "SELECT * FROM skills WHERE tenant_id = $1 AND name = ANY($2)",
            tenant_id, list(names),
        )
        return [SkillResultDetailed.from_row(r) for r in rows]
```

Return empty responses (rather than raising) when `tenant_id` is missing — this is the multi-tenant fail-closed contract.

### Pattern: user personalization
```python
class UserPersonaProvider(SkillProvider):
    async def list(self, req, *, tool_context, capability_context=None):
        prefs = await load_user_preferences(tool_context.get("user_id"))
        return SkillListResponse(entries=build_entries_from_prefs(prefs))
```

## Host-side ACL patterns

Skills can encode operational power — a "rollback prod" skill in the wrong hands is dangerous.

Layer authz at the provider:

```python
async def get_by_name(self, names, *, tool_context, capability_context=None):
    user = await load_user(tool_context.get("user_id"))
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
