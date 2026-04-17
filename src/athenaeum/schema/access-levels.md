# Access Levels

Per-page access classification. Every wiki page must have exactly one access
level. If a page needs mixed access levels, split it into two pages and
cross-link them.

| Level | Who can see | Example content |
|-------|------------|-----------------|
| open | Anyone, safe to publish publicly | Blog content, public frameworks, open-source docs |
| internal | You and your personal agents | Workflow preferences, tool opinions, internal notes |
| confidential | You and specific business context | Client engagement details, pricing, strategy |
| personal | You and trusted individuals only | Home address, health, family matters |

## Rules

- Default new pages to `internal` unless the source material clearly indicates
  another level.
- Agents read all access levels for reasoning but must respect access level
  when producing output (e.g., never include `confidential` content in public-facing
  text).
- When publishing, only `open` pages are eligible for export.
