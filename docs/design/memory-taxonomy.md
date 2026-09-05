# Memory Taxonomy — Data Model (issue athenaeum#424)

> **Status:** data model + validation only. This document locks the shape;
> enforcement of the merge-vs-cite semantics described in §3 is
> [#433](https://github.com/Kromatic-Innovation/athenaeum/issues/433) and has
> **shipped** — see `src/athenaeum/merge_type_gate.py` (module docstring:
> "This module is that enforcement"), consumed by the merge/proposal engine
> (`athenaeum.merge`, `athenaeum.wiki_dedupe`). Governance over the `axiom` class
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
| Entity schema | `type:` | `person`, `company`, `project`, `concept`, `source`, + `FALLBACK_TYPES` (`auto-memory`, `tool`, `reference`, `principle`, `preference`, `incident`) | `src/athenaeum/schemas.py` (`KNOWN_TYPES`) | "What kind of *entity* does this page describe?" |
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
  `KNOWN_TYPES` precedent from issue athenaeum#93) — recoverable, not a hard raise.
- **Absent** `memory_class` → **tolerated**. Legacy/untyped pages must not
  fail to validate. `athenaeum.schemas.is_untyped_memory_class` and
  `athenaeum._lint.lint_untyped_memory_class` are the predicates a
  lint/report pass calls to surface these pages as "untyped" rather than
  letting them disappear silently.

## 2a. Assignment — rule map, backfill, and where this axis sits in its lifecycle (athenaeum#996)

**Lifecycle position: BACKFILL, not enforced.** `memory_class` is currently a
*populated-but-optional* dimension. Absence is tolerated by validation (see
"Validation behavior" above), nothing refuses a page for lacking it, and no
writer is required to set it. The sequence this axis moves through is:

1. **Writable** (shipped, athenaeum#996) — `models.WikiEntity` carries `memory_class`
   and `render()` emits it, so newly created pages land classed. Before this
   the field existed only on the READ model (`schemas.WikiBase`), so corpus
   coverage could not be anything but zero.
2. **Backfilled** (this stage) — `athenaeum memory-class backfill` assigns the
   field to existing pages that lack it. Running it against the live store is
   an operator act, tracked separately.
3. **Enforced** (NOT scheduled) — a future decision could make the field
   required at write time or make `_lint.lint_untyped_memory_class` fail a
   gate. Nothing does that today; do not write code that assumes the field is
   present.

### The deterministic `type:` → `memory_class:` rule map

Source of truth: `schemas.TYPE_TO_MEMORY_CLASS` / `schemas.memory_class_for_type`.
Adopted on athenaeum#972 after a live-corpus scan; it decides ~97% of pages at zero
LLM calls.

| `type:` | `memory_class:` |
|---|---|
| `person`, `company`, `concept`, `tool`, `project`, `source`, `user` | `entity` |
| `reference` | `reference` |
| `principle` | `guideline` |
| `auto-memory`, `preference`, `feedback`, `incident`, `issue` | *(no rule — classifier residual)* |
| anything else | *(no rule — reported as `unmapped-type`, never guessed)* |

The residual types are intake/lifecycle markers rather than entity kinds, so
they split across `fact`/`decision`/`procedure`/`guideline` on CONTENT. The
backfill command's opt-in `--classifier` mode handles them in batched calls
(~20 pages each, routed through the `classify` model knob); pages carrying
`retired: true` are excluded by default.

**No machine may mint `axiom`.** The rule map has no `axiom` target, and the
classifier's output is filtered against `schemas.MACHINE_ASSIGNABLE_MEMORY_CLASSES`
(= `MEMORY_CLASSES` minus `axiom`) in code, not merely discouraged in the
prompt. Axiom status requires the human-approved promotion record §6 defers to
athenaeum#434.

Two further guarantees the backfill holds, both because a taxonomy pass must be
re-runnable without an operator auditing 20k+ files: an existing non-empty
`memory_class` is never overwritten, and a page with no YAML frontmatter block
is skipped and counted — never given a synthetic one. The write is a textual
insertion of one line into the existing frontmatter block rather than a
parse/re-render round trip, so a second run is a byte-level no-op.

## 3. Merge-vs-cite semantics (documented here; enforcement shipped in athenaeum#433)

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
shares a `memory_class`) was explicitly out of scope for THIS issue (athenaeum#424)
and shipped separately as athenaeum#433: `src/athenaeum/merge_type_gate.py`
(`cross_class_precheck` rejects cross-class merge proposals at proposal
time; `build_cite_proposal` builds the non-destructive cite path in their
place), consumed by `athenaeum.merge` and `athenaeum.wiki_dedupe`. Nothing
in the merge/recall/embed code paths changed as part of athenaeum#424 itself — the
routing logic landed in the follow-up athenaeum#433 change.

## 4. Inference blocks — schema + parser (retraction machinery shipped in athenaeum#433)

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
`confidence` value — "addressable" so a retraction pass can name a specific
inference block and remove it when one of its `basis` facts is retracted.
This issue (athenaeum#424) ships only the schema + parser
(`athenaeum.inference_blocks.parse_inference_blocks`); the retraction
primitive shipped in the follow-up athenaeum#433:
`athenaeum.inference_blocks.retract_inference_block` removes a targeted
`## Inference` block by `id` as a pure text transform (byte-identical
elsewhere, no basis re-evaluation, no cascading). Cross-record cascading —
notifying dependent merges when a retraction removes a fact a merge relied
on — is separately shipped in issue athenaeum#435's `src/athenaeum/retraction_cascade.py`,
which emits a human-review item (never an auto-unmerge) naming the
dependent merge, the retracted observation, and the retraction reason.

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
- `valid_from` / `valid_until` — the claim-VALIDITY window (issue athenaeum#308;
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
consumer issue (most naturally athenaeum#433), not this data-model issue.

## 6. Explicitly out of scope for athenaeum#424

- Any change to `recall`, `merge`, or `embed` behavior as PART OF athenaeum#424 itself.
- Enforcing merge-vs-cite semantics (§3) — out of scope for athenaeum#424, shipped
  separately in athenaeum#433 (`src/athenaeum/merge_type_gate.py`).
- Inference-block retraction machinery (§4) — out of scope for athenaeum#424, shipped
  separately in athenaeum#433 (`athenaeum.inference_blocks.retract_inference_block`)
  and athenaeum#435 (`src/athenaeum/retraction_cascade.py`).
- Axiom governance (elevated review/approval for the `axiom` class) — shipped
  separately in athenaeum#434 (`src/athenaeum/axiom_governance.py`; see that module's
  docstring for the promotion/demotion ledger + assignment-audit design).
- Tier (compile pipeline) usage of `memory_class` — athenaeum#423 / athenaeum#432.
