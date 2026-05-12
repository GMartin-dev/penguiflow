# Pack Formats and Schemas

Four formats. Pick by audience and tooling.

## Format: Markdown (`*.skill.md`)

Most readable. Recommended for in-repo packs that humans review.

```markdown
---
name: pack.demo.handle_incident          # globally unique within the pack
title: Handle a production incident      # human-readable
trigger: When an on-call incident is declared and you need a repeatable response.
task_type: domain                        # domain | tooling | safety | meta
required_tool_names: [pagerduty_ack, slack_post]
required_namespaces: [pagerduty, slack]
required_tags: [oncall]
tags: [incident, oncall]
inputs:
  - "Incident summary"
  - "Affected tenants"
outputs:
  - "Mitigation steps taken"
  - "Root cause hypothesis"
steps:
  - Confirm impact and affected tenants/projects.
  - Identify the failing dependency and roll back if needed.
  - Mitigate user impact first, then diagnose root cause.
  - File a follow-up incident review issue.
failure_modes:
  - If metrics are missing, check telemetry pipeline health first.
  - If the rollback fails, escalate to platform on-call.
notes: |
  Keep the public status page updated. Do not share customer names externally.
---
```

The body of the file (below the frontmatter) is optional supplementary text. Most fields are exposed via the frontmatter; the body is rarely needed.

## Format: YAML (`*.skill.yaml` / `*.skill.yml`)

Equivalent to Markdown frontmatter; no body.

```yaml
name: pack.demo.handle_incident
title: Handle a production incident
trigger: When an on-call incident is declared.
task_type: domain
steps:
  - Confirm impact.
  - Identify failing dependency.
  - Mitigate before diagnose.
```

Use when:
- Editing in a YAML-aware editor / pipeline.
- Generating skills from a script.

## Format: JSON (`*.skill.json`)

Single skill or a list of skills in one file.

```json
{
  "name": "pack.demo.handle_incident",
  "title": "Handle a production incident",
  "trigger": "When an on-call incident is declared.",
  "task_type": "domain",
  "steps": [
    "Confirm impact.",
    "Identify failing dependency.",
    "Mitigate before diagnose."
  ]
}
```

Or list:
```json
[{...}, {...}, {...}]
```

Use when:
- Importing from a system that emits JSON.
- Generating skills programmatically with a schema validator in the loop.

## Format: JSONL (`*.skill.jsonl`)

One skill per line. Bulk import friendly.

```
{"name": "...", "trigger": "...", "steps": [...]}
{"name": "...", "trigger": "...", "steps": [...]}
```

Use when:
- Bulk-loading thousands of skills from an export.
- Working with stream processors.

## Field reference

### Required
- `trigger: str` — when the skill applies (used by retrieval).
- `steps: list[str]` — the playbook.

### Recommended
- `name: str` — globally unique within the pack. Use dot-namespaced form (`pack.area.action`).
- `title: str` — human-readable.
- `task_type: str` — `"domain"` | `"tooling"` | `"safety"` | `"meta"` (extensible).
- `tags: list[str]` — free-form tags.

### Applicability gating
- `required_tool_names: list[str]` — tool names that must be allowed for the skill to surface.
- `required_namespaces: list[str]` — tool namespaces that must be present (`github`, `slack`).
- `required_tags: list[str]` — tags that must be present in the request's allowed tag set.

All three are AND-combined. Empty/absent = no constraint on that axis.

### Optional
- `inputs: list[str]` — what the user/agent provides.
- `outputs: list[str]` — what the skill produces.
- `failure_modes: list[str]` — what can go wrong; how to recover.
- `notes: str` — free-form guidance.
- `version: str` — semantic version. Useful for tracking changes.

### Internal (don't set unless you know why)
- `id: str` — auto-derived from `name` + pack.
- `pack: str` — auto-set from `SkillPackConfig.name`.

## Validation rules

The loader will:
- Reject skills with empty `trigger` or `steps`.
- Reject skills with invalid YAML frontmatter (for `*.skill.md`).
- Skip files that don't match the recognized extensions.
- Deduplicate by `name` within a pack (last wins; warn on collision).

Across packs, name collisions are handled by precedence: runtime providers win, then earlier `skill_packs` entries.

## Layout in a project

```
skills/
  packs/
    ops/                               # SkillPackConfig(name="ops", path="skills/packs/ops")
      handle_incident.skill.md
      rollback_deployment.skill.md
      escalate_to_oncall.skill.md
    rich_output/
      report_layout.skill.md
      chart_selection.skill.md
    domain/
      finance_quarter_close.skill.yaml
      hr_offboarding.skill.yaml
```

One skill per file is the common convention — easier to review and version. JSONL is the exception when you must bulk-import.

## Authoring tips

### Crisp triggers
The `trigger` field is what the retrieval embedding compares against. Write it in user-intent terms ("when an incident is declared", not "when sev > 0"). Include the verbs the user will say.

### Concrete steps
Each step is a short imperative. Avoid hedging ("maybe consider doing X"). Avoid implementation detail unless it's universal.

### Failure modes
Cover the top 2-3 ways the skill goes wrong and what to do. The planner sees them as part of the skill text and can apply them.

### Tool references
If your skill mentions tools by name, declare `required_tool_names` to gate the skill's visibility. Otherwise the planner may attempt to call a tool that isn't allowed in this request.

### Version your skills
Treat skills like code. PR them. Run tests against skill content if you have a verification pipeline (LLM-as-judge on "does this skill correctly say to do X?").

### Don't put secrets in skills
Skills enter `llm_context`. Never include:
- API keys, tokens.
- Internal-only URLs or hostnames.
- Customer-identifying data.
- Production-specific secrets.

`redact_pii=True` is a defense in depth, not a license to store secrets.
