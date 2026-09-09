# Field constraints

**Reference page.** For the task-shaped version, see
[Field constraints](../guides/field-constraints.md).

## What it does

`athenaeum.field_constraints` reads an operator-declared, per-type field
constraint table from `wiki/_schema/field-constraints.md` and checks a
page's frontmatter and body against it. It is the missing member of the
`wiki/_schema/` family `types.md` / `tags.md` / `access-levels.md` already
establish: the operator declares a vocabulary by hand, this module
applies it. It ships no constraint of its own — an absent or empty
`field-constraints.md` makes every function in this module an
unconditional no-op.

Three rules are supported per declared `(type, field)` row: `forbidden`
(the field must not appear at all), `allow-pattern` (every value must
match a declared regex), and `deny-pattern` (no value may match a
declared regex). `field` is normally a frontmatter key, or the reserved
value `body`, which checks the page's rendered body text instead — the
only way a Tier-3 write is checked at all, since a Tier-3 create or merge
only ever emits body text, never a new frontmatter key.

Two consumers use this module: a write-boundary guard
(`guard_entity_field_constraints`), called from
[librarian](librarian.md)'s Tier-3 write path for both a new-entity create
and a merge into an existing page, and a standalone detector
(`scan_field_constraint_violations`) that walks the whole corpus once and
reports every violation of a currently-declared constraint, cheaply
enough to run on a routine schedule.

## What it reads

- `wiki/_schema/field-constraints.md` — the declared constraint table.
  Absent, unreadable, or carrying no valid data row all resolve to "no
  constraints," identically.
- The page under check: its frontmatter dict and rendered body text,
  always re-derived from the exact bytes about to be written (or, for the
  detector, the exact bytes on disk) rather than a caller-supplied copy.

## What it writes

- `wiki_root/_field_constraint_rejected/<filename>` — the full rendered
  content of a write the guard refused, byte-for-byte, so nothing about
  the refused write is lost.
- `wiki_root/_field_constraint_violations.jsonl` — one durable, append-only
  record per violation the guard refused, naming the type, field, rule,
  offending value, uid, name, and which call site (`tier3-create` /
  `tier3-merge`) it came from.

Neither path is ever touched when no constraint is declared.

## What it refuses

- **A refused write never lands.** Both the new-entity create path and
  the merge-into-existing-page path skip their normal write when the
  guard reports a violation; a refused merge leaves the pre-existing page
  on disk byte-for-byte untouched.
- **Never a silent repair.** A violation is reported and the write is
  refused whole — the offending field or sentence is never stripped out
  and the rest of the write applied. Deleting a value that exists nowhere
  else is exactly the failure mode this module exists to avoid.
- **A malformed declared row enforces nothing, never something else.** An
  unrecognized rule, a pattern rule with no pattern, or an invalid regex
  is skipped with a warning — it is never coerced into a different rule
  or treated as `forbidden` by default.
- **No semantic ownership inference.** `allow-pattern` / `deny-pattern`
  are literal string-shape tests. This module does not attempt to infer
  whether an address is personal or organizational from its shape, and
  does not claim to — see the guide's "What this can and cannot tell
  apart" section for the documented limit.

## See also

- Guides — [Field constraints](../guides/field-constraints.md)
- Modules — [librarian](librarian.md) · [routing](routing.md)
