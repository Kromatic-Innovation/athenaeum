# Entity Page Template

Reference template for creating wiki entity pages. The librarian's Tier 3
uses this structure when writing new pages. Humans can also use it when
manually adding entities.

## Template

```markdown
---
uid: e7a3b1c4                   # 8-char hex, permanent, never changes
type: person                     # from _schema/types.md
name: Display Name               # human-readable, CAN be renamed
aliases: [Alt Name, Nickname]    # for dedup and search
access: internal                 # from _schema/access-levels.md
tags: [active]                   # from _schema/tags.md
related:
  - uid: f2d4e6a8               # cross-reference by UID
    role: employer               # freeform relationship label
created: 2026-01-01
updated: 2026-01-01
---

# Display Name

Opening paragraph: who/what this is and why it matters. Claims cite
sources via footnotes.[^1]

## Section (as appropriate for entity type)

- Key fact or observation[^2]
- Another fact from a different source[^1][^3]

## Open Questions

- [ ] Unresolved question about this entity

[^1]: [[src-entity-uid|Source Name]] context, date
[^2]: [[src-other-uid|Other Source]] context, date
[^3]: Direct observation, date
```

## Field Reference

### Required (every page)

| Field | Purpose |
|-------|---------|
| `uid` | 8-char hex from `uuid4`. Permanent. Never changes. |
| `type` | Entity type from `_schema/types.md`. |
| `name` | Human-readable display name. Can be renamed. |
| `access` | Access level from `_schema/access-levels.md`. |

### Recommended

| Field | Purpose |
|-------|---------|
| `aliases` | Alternate names. Librarian checks before creating dupes. |
| `tags` | Topical tags from `_schema/tags.md`. |
| `related` | Typed links to other entities by UID + role. |
| `created` | ISO date when page was first created. |
| `updated` | ISO date when page was last modified. |

### Schema version (declared kernel-field migrations, issue athenaeum#1628)

| Field | Purpose |
|-------|---------|
| `schema_version` | Integer: which declared kernel fields this page is expected to carry, per `athenaeum.schema_migrations`'s registry. Absent means version 0 — nothing has stamped this page yet. Advanced two ways: `athenaeum schema migrate --apply` for rule-based/eager migrations (a deterministic, no-model sweep), and the `athenaeum audit` pass for model-based/on-audit migrations (only once every field that migration names is filled or recorded in `audit_findings`). Never hand-set this field. |

### Set by the audit pass (`athenaeum audit`, issue athenaeum#1624)

Never hand-fill these — an `athenaeum audit` run stamps them, and only
that pass's write path may set them. Absent entirely on a page that has
never been audited; `last_audited` is what distinguishes "never checked"
from "checked, and every coordinate below is either filled or recorded
undeterminable." The three coordinate fields below are one MODEL-based,
on-audit schema migration (`schema_version` 1 -> 2) — see the section
above; they are declared in `athenaeum.schema_migrations`, not hard-coded
in the audit pass itself.

| Field | Purpose |
|-------|---------|
| `last_audited` | ISO-8601 UTC timestamp of the most recent audit pass. |
| `audit_version` | The audit prompt/schema version that pass ran against. |
| `valid_from` | Claim validity window, lower bound. Filled only when the page's own body/sources state it; open (absent) otherwise. |
| `valid_until` | Claim validity window, upper bound. Same fill rule as `valid_from`. |
| `claimed_scope` | Where the claim APPLIES (the `scope` kernel dimension's coordinate — distinct from `scope:`, a different, provenance-shaped legacy field). Same fill rule. |
| `audit_findings` | Per-field map recording WHY a coordinate above was left unfilled (e.g. `valid_from: "undeterminable: no dated validity window stated"`) — a decision, not a guess, and not silence. A coordinate the audit pass filled has no entry here; one it left populated (already set before the pass ran) has no entry either — coordinates are never overwritten once set. |

## Sections by Entity Type

| Type | Typical sections |
|------|-----------------|
| person | Role/Background, Relationship History, As Information Source, Contact |
| company | Overview, Relationship History, Key Outcomes, Contacts |
| project | Overview, Timeline, Outcomes, Lessons Learned |
| concept | Definition, Usage, Related Concepts |
| tool | Purpose, Configuration, Opinions |
| reference | Summary, Key Takeaways, Citations |
| preference | Current Setting, History, Rationale |
| principle | Statement, Evidence, Exceptions |

## Trust Model

Trust is NOT a frontmatter field. Instead:

1. Claims cite sources via Wikipedia-style footnotes
2. Sources are entities with their own pages
3. Source pages accumulate reliability notes over time
4. Trust is assessed by following the citation chain

This means trust is contextual: "Alice is reliable on product topics but
overstates timelines" — not a single confidence score.
