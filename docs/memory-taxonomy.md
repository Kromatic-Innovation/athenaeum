# Memory Taxonomy — Data Model (issue #424)

> **Status:** data model + validation only. This document locks the shape;
> enforcement of the merge-vs-cite semantics described in §3 is
> [#433](https://github.com/Kromatic-Innovation/athenaeum/issues/433) and has
> not shipped yet. Governance over the `axiom` class
> ([#434](https://github.com/Kromatic-Innovation/athenaeum/issues/434):
> explicit human-approved promotion/demotion ledger + assignment audit) has
> shipped — see `src/athenaeum/axiom_governance.py`. Tier
> usage of `memory_class` is [#423](https://github.com/Kromatic-Innovation/athenaeum/issues/423)
> / [#432](https://github.com/Kromatic-Innovation/athenaeum/issues/432).

## 1. Goal

Give every memory a **class** — what KIND of thing it is epistemically
(a fact vs. a guideline vs. a standing decision), as opposed to what entity
it describes or what intake channel produced it.

## 2. Axis reconciliation — three orthogonal axes, not one

Athenaeum already has two type axes on a wiki page. `memory_class` is a
**third, layered** axis, not a replacement for either:

| Axis | Frontmatter key | Values | Defined in | Answers |
|---|---|---|---|---|
| Entity schema | `type:` | `person`, `company`, `project`, `concept`, `source`, + `FALLBACK_TYPES` (`auto-memory`, `tool`, `reference`, `principle`, `feedback`, `preference`, `user`) | `src/athenaeum/schemas.py` (`KNOWN_TYPES`) | "What kind of *entity* does this page describe?" |
| Intake type | `memory_type:` | `feedback`, `project`, `reference`, `user`, `recall` | `src/athenaeum/models.py` (`AutoMemoryFile.memory_type`) | "What intake channel / auto-memory shape produced this?" |
| **Memory class (new)** | `memory_class:` | `fact`, `guideline`, `axiom`, `reference`, `entity`, `decision`, `procedure` | `src/athenaeum/schemas.py` (`MEMORY_CLASSES`) | "What EPISTEMIC kind of memory is this?" |

Both existing axes are **untouched** — their validation behavior is
byte-identical before and after this change (see
`tests/test_memory_taxonomy.py::TestExistingAxesUnchanged`). A person page
keeps `type: person` and simply gains `memory_class: entity`; nothing is
retyped or replaced.

Rationale for layering instead of replacing `KNOWN_TYPES`: replacing it would
break validation of every existing wiki page. Layering is additive and
reversible. It also matches the settled taxonomy's own framing that `entity`
is "already de facto" the class most existing wiki pages belong to — this
axis makes that classification explicit rather than inventing a new entity
taxonomy.

`open-question` and `hypothesis` classes are deliberately **deferred** —
the taxonomy does not over-mint classes ahead of a concrete consumer needing
them.

### Validation behavior

- One of the 7 `MEMORY_CLASSES` values → accepted silently.
- A non-empty value **outside** the 7 → **flagged**: `WikiBase`'s field
  validator emits a `UserWarning` (matching the existing `type:` /
  `KNOWN_TYPES` precedent from issue #93) — recoverable, not a hard raise.
- **Absent** `memory_class` → **tolerated**. Legacy/untyped pages must not
  fail to validate. `athenaeum.schemas.is_untyped_memory_class` and
  `athenaeum._lint.lint_untyped_memory_class` are the predicates a
  lint/report pass calls to surface these pages as "untyped" rather than
  letting them disappear silently.

## 3. Merge-vs-cite semantics (documented here; enforcement is #433)

The reason `memory_class` exists as a distinct axis is that different
classes should be reconciled DIFFERENTLY when new, possibly-overlapping
memory arrives:

- **Within the same class, on the same topic/entity → MERGE.** Two `fact`
  pages about the same entity's headcount consolidate into one page (the
  existing dedupe/merge pipeline's job, unchanged by this issue). Two
  `guideline` pages saying "always squash-merge" and "prefer squash merges"
  are the same guideline and should fold together.
- **Across classes → CITE, NEVER DESTROY.** A `guideline` does not
  overwrite, absorb, or delete the `fact` page(s) that justify it — it
  **cites** them (e.g. via a wikilink or an `## Inference` block's
  `basis:` list, see §4). The facts survive independently, so that:
  - a fact can be corrected or retracted without silently invalidating an
    unrelated guideline that happens to reuse the phrase,
  - a guideline's justification stays traceable and auditable back to the
    specific facts it depended on,
  - a `decision` similarly cites the facts/guidelines that motivated it
    rather than swallowing their content.

This is a **should-merge-here / must-cite-there** rule pair, not a single
merge algorithm — enforcing it (routing a resolver decision through the
right one of the two paths depending on whether the pair being reconciled
shares a `memory_class`) is explicitly **out of scope for this issue** and
is tracked as #433. Nothing in the merge/recall/embed code paths changes as
part of #424.

## 4. Inference blocks — schema + parser (retraction machinery is #433)

A `memory_class: fact` page may derive some of its claims from OTHER fact
pages rather than from direct observation. Such a derived claim is written
as an `## Inference` block in the page body:

```markdown
## Inference
**Basis**: [[fact-a]], [[fact-b|Fact B alias]]
**Confidence**: 0.8
The derived claim goes here, in prose.
```

- `**Basis**:` — one or more Obsidian-style `[[slug]]` / `[[slug|alias]]`
  wikilinks to the fact page(s) the inference is derived from.
- `**Confidence**:` — a float in `[0, 1]`.

Each block parses to an addressable unit (`athenaeum.inference_blocks.InferenceBlock`)
with a stable content-derived `id`, exposing its `basis` list and
`confidence` value — "addressable" so a future retraction pass (#433) can
name a specific inference block and re-evaluate or invalidate it when one of
its `basis` facts is retracted. **That re-evaluation/invalidation logic does
not exist yet** — this issue ships only the schema + parser
(`athenaeum.inference_blocks.parse_inference_blocks`).

A block missing `**Basis**:`, missing/unparseable `**Confidence**:`, or
whose `**Basis**:` line has no recoverable wikilink is **flagged**
(`InferenceBlock.malformed` / `.errors`) rather than silently dropped or
silently accepted.

## 5. Staleness axis — `observed_at`

A standing-state fact (e.g. "Acme has 40 employees") is true **when
observed**, not necessarily **currently true** — headcount changes.
`observed_at` is a THIRD date-ish frontmatter field, distinct from both:

- `created` / `updated` — write-time bookkeeping (when the PAGE was
  written/touched), and
- `valid_from` / `valid_until` — the claim-VALIDITY window (issue #308;
  when the resolver or a human has explicitly bounded how long a claim
  holds).

`observed_at` records the observation date without itself asserting
anything about current validity. The validator (`WikiBase.observed_at`,
`schemas.py`) accepts and round-trips it; `athenaeum.models.parse_observed_at`
reads it back as a `date` (fail-open: absent/unparseable → `None`, mirroring
`parse_valid_from` / `parse_valid_until`). Round-trip through
`render_frontmatter` is asserted in `tests/test_memory_taxonomy.py`.

No reader in this issue treats a stale `observed_at` as grounds to
deactivate a fact — that policy decision, if wanted, belongs to a future
consumer issue (most naturally #433), not this data-model issue.

## 6. Explicitly out of scope for #424

- Any change to `recall`, `merge`, or `embed` behavior.
- Enforcing merge-vs-cite semantics (§3) — #433.
- Inference-block retraction machinery (§4) — #433.
- Axiom governance (elevated review/approval for the `axiom` class) — shipped
  separately in #434 (`src/athenaeum/axiom_governance.py`; see that module's
  docstring for the promotion/demotion ledger + assignment-audit design).
- Tier (compile pipeline) usage of `memory_class` — #423 / #432.
