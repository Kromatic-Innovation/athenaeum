**Reference:** [Field constraints module](../modules/field-constraints.md)

# Per-type field constraints

Athenaeum ships no opinion about which frontmatter fields — or which body
content — may appear on which entity `type`. If you want to declare a
rule (for example, "a company page may not carry a personal contact
address"), you write it yourself in `wiki/_schema/field-constraints.md`.
Nothing is enforced until you create that file; a deployment with no such
file behaves exactly as before this mechanism existed.

This sits alongside the other operator-declared vocabularies in
`wiki/_schema/` (`types.md`, `tags.md`, `access-levels.md`): you declare a
table by hand, the librarian reads it.

## Declaring a constraint

Create `wiki/_schema/field-constraints.md` with a table:

```markdown
# Field Constraints

| Type    | Field  | Rule          | Pattern           |
|---------|--------|---------------|-------------------|
| company | emails | forbidden     |                   |
| company | body   | deny-pattern  | [\w.+-]+@[\w-]+\.[\w.-]+ |
```

Columns:

- **Type** — an entity type, matched against a page's resolved `type:`.
- **Field** — a frontmatter key (`emails`, `phones`, anything your pages
  carry), OR the reserved value `body`, which checks the page's rendered
  body markdown text instead of a frontmatter key. `body` matters because
  a Tier-3 create or merge write is LLM-authored body text — the writer
  never emits a new frontmatter key on its own — so a rule aimed at
  catching what an LLM writes usually wants `body`, not a frontmatter
  field name.
- **Rule** — one of:
  - `forbidden` — the field must not appear (non-empty) at all.
  - `allow-pattern` — every value must match `Pattern` (`re.search`); a
    non-matching value is a violation.
  - `deny-pattern` — any value matching `Pattern` is a violation.
- **Pattern** — a Python regex, required for `allow-pattern`/`deny-pattern`,
  ignored for `forbidden`. A literal `|` inside a pattern (for
  alternation, e.g. `^(info|sales)@`) must be escaped as `\|` so it is
  not read as a table-cell separator.

A malformed row (unrecognized rule, a pattern rule with no pattern, or an
invalid regex) is skipped and logged — it never crashes a run, and it
never falls back to a different rule.

## What this can and cannot tell apart

`allow-pattern` / `deny-pattern` are string-shape tests only. They have no
notion of *who owns* an address — a person versus an organisation. A rule
like `^(info|sales|support)@` will treat any value with that shape as
permitted, including a coincidentally-shaped value that is not actually a
role address, and will flag a genuine role address it didn't anticipate
(`billing@`, `hr@`, …) as a violation. A short local-part heuristic
(`^[a-z]{2,4}@`, aimed at "looks like a role account") also matches a
real person's initials at the same domain — those are indistinguishable
from a role address by shape alone. If your policy genuinely needs to
distinguish "owned by a person" from "owned by the organisation," this
mechanism cannot make that call for you; it can only check the pattern
you give it. Write the rule you can defend as a shape rule, and expect it
to have both false positives and false negatives on short or ambiguous
local parts.

## Detecting violations that already exist

`athenaeum.field_constraints.scan_field_constraint_violations(wiki_root)`
walks the corpus once and reports every page whose frontmatter or body
violates a declared constraint. With no `field-constraints.md`, this
costs one file-existence check and returns immediately — safe to call on
every routine sweep.

## What happens to a violating write

A Tier-3 create or merge that would violate a declared constraint is
refused, not silently repaired: the write never lands, the page that
would have violated the rule is parked under
`wiki_root/_field_constraint_rejected/`, and a record is appended to
`wiki_root/_field_constraint_violations.jsonl` so you can review and
disposition it by hand. For a refused merge, the existing page is left
untouched — nothing is deleted.
