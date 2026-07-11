# Provenance Shape — Per-Value Attribution and the MCP `remember` API

This document is the DESIGN LOCK for athenaeum's per-claim provenance
on-disk shape. It settles the questions that #102, #97, and #96 each
implement against, so those three issues land against ONE decided shape
and don't drift.

Companion to `docs/conflict-resolution.md` — that doc locks how
disagreements are RESOLVED; this one locks how attribution is REPRESENTED
on disk and at the MCP boundary.

This is a design document. No code changes ship with it. The
implementation issues remain open and reference this doc as their target
contract.

---

## 1. Background

### Already shipped

- **#90 — per-claim provenance primitives** (PR #94): `WikiBase.source`
  and `WikiBase.field_sources` round-trip on disk. `provenance.py` parses
  the scalar `<type>:<ref>` form and the structured `{type, ref, ts?,
  confidence?, notes?}` dict form. (A legacy single-token form
  — `extended-tier-build`, `warm-network-detect` — was accepted on read
  until #97 migrated the live tree on 2026-05-09; see §5.)
- **#95 — Tier 3 emits `field_sources`**: when Tier 3 creates or merges
  a person/company wiki, the relevant Apollo-namespace fields are
  attributed via `field_sources.<key> = "api:apollo:<date>"`.
- **dedupe coalesce coverage** (#100, Lane E of the foundation refactor):
  `_merge_field_sources` carries `field_sources` forward across a
  duplicate-pair merge. Canonical wins per key; absorbed-only keys are
  carried forward. Pruned when the underlying field is gone. Locked in
  `docs/conflict-resolution.md` §7.

### Not yet decided (this doc decides them)

- **Per-VALUE attribution for list fields** (#102). Today
  `field_sources.emails` is one source for the whole list. If a contact
  has `emails: [a@x.com, b@y.com]` where `a@x` came from Apollo and `b@y`
  came from a LinkedIn export, the second source is lost.
- **MCP `remember(sources=...)` shape** (#96). Three on-the-wire shapes
  collide today and the type-based disambiguation has a pathological case.
- ~~**Legacy slug → typed migration** (#97).~~ Resolved 2026-05-09:
  `athenaeum repair --legacy-source-slugs --apply` migrated 15,403 wikis
  from `<bare-slug>` to `script:<slug>`. The `_LEGACY_SCALAR_RE` branch
  in `provenance.parse_source` retired in the follow-up PR.

---

## 2. Decision: per-value `field_sources` for list fields

### 2.1 The shape — list-of-plain-values

**Decision: per-value attribution is a list of `{value, source}` records,
co-indexed (by value) with the underlying field's list, NOT positionally
indexed.** Co-indexing by value is robust to list-reordering during
merges; positional indexing is not.

On-disk YAML:

```yaml
emails:
  - tristan@kromatic.com
  - tristan@trikro.com
field_sources:
  emails:
    - value: tristan@kromatic.com
      source: api:apollo:2026-04-29
    - value: tristan@trikro.com
      source: linkedin:tristankromer
```

`source` follows the existing `provenance.py` contract — scalar
`<type>:<ref>` OR a structured `{type, ref, ts?, confidence?, notes?}`
dict.

### 2.2 List of dicts (employment_history)

**Decision: per-value attribution attaches to the WHOLE dict, NOT to
each scalar inside.** Sub-field attribution would force a recursive shape
that current readers would reject; the value is the dict; the source
applies to the dict as a unit.

```yaml
apollo_employment_history:
  - company: Kromatic
    title: Founder
    start_date: 2014-01
  - company: SECUDE
    title: Director of Product
    start_date: 2010-06
field_sources:
  apollo_employment_history:
    - value:
        company: Kromatic
        title: Founder
        start_date: 2014-01
      source: api:apollo:2026-04-29
    - value:
        company: SECUDE
        title: Director of Product
        start_date: 2010-06
      source: linkedin:tristankromer
```

Match key for "is this the same value": `repr(value)` of the dict, the
same identity used by `dedupe._perform_merge`'s list-union (see
`docs/conflict-resolution.md` §7 known-edge-cases). Two semantically-equal
dicts with different YAML key order would NOT match; this is a known
limitation accepted at #100 and out of scope here.

If a future requirement needs sub-field attribution inside a dict, the
caller should split the dict into separate scalar fields rather than
nest `field_sources` recursively.

### 2.3 Backward compatibility — legacy field-keyed shape

**Decision: readers accept BOTH shapes; writers emit ONLY the new shape.**
The legacy field-keyed shape (one source for the whole list) stays
loadable and round-trippable forever — no forced upgrade pass.

Legacy shape (still accepted on read):

```yaml
emails:
  - tristan@kromatic.com
  - tristan@trikro.com
field_sources:
  emails: api:apollo:2026-04-29
```

Disambiguation rule at parse time:

- `field_sources.<list_field>` is a `str` or a dict with `type`/`ref`
  → legacy single-source-for-the-whole-list. Applies to every value.
- `field_sources.<list_field>` is a `list` of `{value, source}` records
  → new per-value shape.

Migration path:

- **Tier 0 passthrough**: NEVER auto-upgrades. Renders whichever shape
  the raw frontmatter contained, byte-for-byte. (See §3.)
- **Tier 3 / dedupe / any write path that already touches the field**:
  when WRITING, emit the new per-value shape. If the existing on-disk
  value was legacy, the writer treats it as "every existing list value
  shares the legacy source" and merges the new attributions in.
- **No standalone migration script.** The shape transitions field-by-field
  as wikis are organically updated. The legacy reader branch stays
  forever — the cost is one isinstance check per parse.

### 2.4 Validator changes (informational; implementation is #102)

`provenance.validate_field_sources` will need to accept either:

- a `str | dict` value (legacy), validated by `parse_source`, OR
- a `list[dict]` value where each entry is `{"value": <any>, "source":
  <str | dict>}`, with `source` validated by `parse_source` and `value`
  type-unconstrained.

Co-indexing alignment between `field_sources.<k>` entries and the actual
`<k>` list is NOT enforced at validation time — a stale entry that no
longer matches any list value is dropped at write time, mirroring the
"prune dangling attributions" rule already used by
`_merge_field_sources`.

---

## 3. tier0_passthrough byte-for-byte contract

`tier0_passthrough` (see `docs/conflict-resolution.md` §1) renders raw
frontmatter verbatim. The per-value shape changes nothing about that
contract — it strengthens it.

**Rule: if raw frontmatter contains `field_sources` in EITHER the legacy
field-keyed shape OR the new per-value shape, render it back unchanged.**
Tier 0 NEVER auto-upgrades. The only mutation Tier 0 makes to incoming
frontmatter remains stamping `updated` (and `created` if missing); see
`conflict-resolution.md` §1 known-edge-cases.

This holds the existing `project_athenaeum_tier0_passthrough` invariant
(byte-for-byte preservation when raw has `uid` + `type` + `name`) without
amendment.

---

## 4. MCP `remember(sources=...)` API

Resolves #96.

### 4.1 Status quo and the pathological case

The current `remember` accepts `sources` as ANY of three shapes,
disambiguated by inspecting keys:

1. scalar `"<type>:<ref>"` → wiki-level `source`.
2. dict containing `type` + `ref` (and possibly `ts`/`confidence`/`notes`)
   → wiki-level structured `source`.
3. dict whose keys are field names → per-field `field_sources`.

Pathological case: a caller wanting `field_sources={"type": "api:x",
"ref": "linkedin:y"}` (i.e. the wiki has frontmatter fields literally
named `type` and `ref`) is misclassified as shape (2).

### 4.2 Decision — explicit wrapper keys

**Decision: introduce explicit wrapper keys. Keep the bare scalar
shorthand for the common case; require `_source` or `_field_sources` for
structured input. The bare-dict heuristic is removed.**

Three accepted shapes:

```python
# (a) scalar shorthand — wiki-level default. No ambiguity.
remember(text="...", sources="api:apollo:2026-05-09")

# (b) wiki-level structured — explicit wrapper.
remember(text="...", sources={"_source": {
    "type": "api",
    "ref": "apollo:2026-05-09",
    "confidence": 0.9,
}})

# (c) per-field map — explicit wrapper.
remember(text="...", sources={"_field_sources": {
    "current_title": "api:apollo:2026-05-09",
    "linkedin_url":  "linkedin:tristankromer",
}})
```

Rules:

- `sources is None` → no provenance attached (today's behavior).
- `sources is str` → wiki-level `source` scalar (today's behavior).
- `sources is dict` → MUST contain exactly one of `_source` or
  `_field_sources` at the top level. Anything else is a `ValueError`.
- The two wrappers may NOT both appear in one call. If a caller wants a
  wiki-level default AND per-field overrides, they pass `_field_sources`
  with the desired per-field map; `WikiBase.source` is set separately
  via the wiki's existing source field, not via `remember(sources=...)`.
  (Parsimony — the MCP surface is for new wiki creation, not retroactive
  field attribution.)

The pathological `{"type", "ref"}` case now writes:

```python
remember(text="...", sources={"_field_sources": {
    "type": "api:internal:2026-05-09",
    "ref":  "linkedin:somehandle",
}})
```

…which is unambiguous because the disambiguation no longer inspects
inner keys.

### 4.3 Per-value attribution at the MCP boundary

The MCP API does not yet accept per-VALUE attribution for list fields.
The `_field_sources` value is `{<field>: <source>}` only — same shape as
today's on-disk legacy `field_sources`. `remember()` callers that want
per-value attribution should pass the structured form via the future
typed `RememberPayload` shape (deferred; not in #96 scope).

Rationale: `remember` is the boundary where the LLM agent dictates a
single source for a single creation event. Per-value attribution arises
during MERGE (dedupe, organic updates), which is a different code path
that doesn't go through MCP.

---

## 5. Legacy slug migration (#97)

### 5.1 Live-tree inventory

Run on the live tree (`~/knowledge/wiki`) on 2026-05-09:

| bare-slug | count |
|-----------|-------|
| `extended-tier-build` | 15,117 |
| `warm-network-detect` | 286 |

No other bare-slug values exist. The legacy regex was permissive —
`[a-z][a-z0-9_-]*` — but only these two slugs were ever written by
in-tree scripts.

### 5.2 Mapping table

Both observed slugs map cleanly to the `script:<slug>` convention:

| Legacy bare-slug | Typed equivalent |
|------------------|------------------|
| `extended-tier-build` | `script:extended-tier-build` |
| `warm-network-detect` | `script:warm-network-detect` |

Rationale: both are athenaeum-internal librarian scripts. `script:` is
the most honest type — these are local-process attributions, not API
calls (no `api:`), not human curation (no `claude:`), not external
profile references (no `linkedin:`).

**Decision: a bare-slug-to-typed-form lookup table lives in the migration
script as a fixed dict. Unknown slugs (none exist today, but defensive)
abort the migration with a loud error rather than guessing.** This is
deliberately conservative — adding a guess-rule for a slug that didn't
exist in the inventory pass is how data gets corrupted.

### 5.3 Per-wiki context lookup — NOT REQUIRED

Both observed slugs are context-free. No `linkedin` bare-slug exists
that would need a username pulled from elsewhere in the wiki. If one
ever appears (it would fail the inventory check above and abort the
migration), the implementation issue gets reopened with a discussion of
context-extraction rules. **OPEN QUESTION — Tristan to decide before
#97 lands**: only if a non-context-free bare slug appears in a future
inventory.

### 5.4 Idempotency and dry-run

The `athenaeum repair --legacy-source-slugs` command (per #97) MUST:

1. Run by default in DRY-RUN. Print: `would migrate <N> wikis: <slug>
   → <typed>` per slug class. Exit 0.
2. With `--apply`: rewrite each wiki's `source:` line in place.
   Re-running the command after a successful apply MUST find zero wikis
   needing migration and exit 0 — typed forms (`script:extended-tier-build`)
   no longer match `_LEGACY_SCALAR_RE` so they're skipped.
3. Validate each migrated wiki via `validate_wiki_meta` after the rewrite
   and abort the WHOLE run on the first validation failure, rolling
   back unwritten files. (Files already written stay written; the user
   re-runs after fixing the validation issue.)
4. Touch only the `source:` line. NEVER reformat surrounding YAML, NEVER
   re-render the body.

After a successful migration of the live tree:

- `_LEGACY_SCALAR_RE` and its branch in `provenance.parse_source` retire.
- Tests that exercise the legacy branch retire with it.

**Status (2026-05-09):** the live-tree migration ran and rewrote 15,403
wikis to the `script:<slug>` typed form. The `_LEGACY_SCALAR_RE` constant
and the legacy branch in `provenance.parse_source` were retired in the
follow-up PR; `parse_source` now raises `ValueError` on bare-slug input
with a pointer to the typed form. The migration tool itself
(`repair.migrate_legacy_source_slugs`) keeps its own internal slug regex
and ships unchanged for any future tree that needs it.

---

## 6. NOT in this doc

Out of scope, by design:

- **Enricher Protocol.** Apollo was extracted from the OSS package in
  Phase 0 (cwc#235 / athenaeum#112). Future paid-API integrations live
  in their host repositories (e.g. cwc/scripts/knowledge-librarian/).
  athenaeum's contract stops at the on-disk shape and the MCP API.
- **Conflict resolution semantics.** `docs/conflict-resolution.md` is the
  lock for which-source-wins. This doc only specifies how attribution is
  represented; resolution rules already documented there extend
  unchanged to the per-value shape (dedupe's "canonical wins per key,
  absorbed-only keys carried forward" generalizes naturally to "canonical
  wins per (key, value), absorbed-only entries carried forward").
- **Body-level provenance.** Per-claim attribution for assertions in the
  markdown body is a separate problem — Tier 3's footnote convention is
  the current answer and is not affected by this doc.
- **Cross-wiki provenance graph.** Tracking which wiki cites which other
  wiki is separate from per-field source attribution and is out of scope
  for the implementations of #96/#97/#102.

---

## 7. Declared memory relationships (`refines` / `supersedes`)

Issue #167 (Lane 1 of #166). Two optional frontmatter fields on
auto-memory files declare an explicit relationship to another memory.
They sit alongside `source:` / `field_sources:` and round-trip through
tier0 passthrough byte-for-byte.

### 7.1 `refines:`

Shape: a list of memory `name:` slugs this memory narrows.

```yaml
---
type: feedback
name: open-csv-in-numbers
refines:
  - open-files-in-sublime
---
```

Semantics: general + exception. BOTH memories remain active. The pair
is NOT a contradiction — the second memory is a documented refinement
of the first.

### 7.2 `supersedes:`

Shape: a list of `{name, as_of, reason}` records declaring this memory
replaces another. The superseded memory stays on disk for audit but is
no longer active guidance.

```yaml
---
type: project
name: voltaire-inbox-ea-umbrella
supersedes:
  - name: voltaire-old-nanoclaw-ea
    as_of: 2026-04-23
    reason: "Voltaire renamed; old nanoclaw EA dead."
---
```

The `name` key is required. `as_of` and `reason` are optional and stored
as empty strings when missing.

### 7.3 Conflict-detector + resolver behavior

- **Detector short-circuit** (`merge.py`): when every pair in a cluster
  declares the other via `refines` or `supersedes`, the cluster
  short-circuits to `detected=False` with a rationale of
  `declared-refinement` or `declared-supersession`. The Haiku call is
  skipped entirely.
- **Resolver auto-prefer** (`resolutions.py`): on the rare path where a
  declared pair reaches the resolver (e.g. via the similarity sweep on a
  pair that wasn't fully covered by the primary-pass filter), the
  resolver returns a synthetic proposal WITHOUT an LLM call:
  - `supersedes` → `keep_<superseder>` at `confidence=1.0`.
  - `refines` → `not_a_conflict` at `confidence=1.0`; `merge.py` then
    drops the escalation entirely.
- **Name matching uses `slugify()`** on both sides at compare time, so
  case- or punctuation-mismatched declarations (`Memory-A` vs
  `memory-a`) still match. Trailing/leading whitespace was already
  stripped by the parsers — slugification is the stronger contract.
- **Precedence when both sides declare conflicting relationships:**
  - **Mutual `supersedes`** (A says supersedes B AND B says supersedes
    A) is itself a declared contradiction. Neither side wins
    deterministically — `merge._declared_relationship` returns `None`
    (the pair falls through to the detector/resolver), and
    `resolutions._declared_winner` returns `None` (the pair falls
    through to the LLM). Both sites emit a `WARNING` log so the
    contradiction is auditable.
  - **Mixed `refines` + `supersedes`** (A `refines` B, B `supersedes`
    A) resolves to `keep_b` / `declared-supersession`. Supersession is
    the stronger statement and wins over the weaker refinement claim
    from the other side. No warning — the resolution is well-defined.
  - **Mutual `refines`** (A `refines` B AND B `refines` A) resolves to
    `not_a_conflict`. Both memories asserting "I narrow the other" is
    treated as a benign declaration of co-membership in a refinement
    cluster; the cluster simply stays active.
- **Detector short-circuit refuses on underspecified detector output:**
  when `ContradictionResult.members_involved` has fewer than two
  entries, `resolutions._declared_winner` returns `None` (falls through
  to the LLM). The earlier draft filled the missing slots from the
  start of the supplied member list, which silently evaluated
  declarations against a pair the detector never flagged. The
  resolver now only short-circuits when both detector-named members
  resolve to entries in the supplied member list.

### 7.4 Parser contract

Both fields are parsed by `models.parse_refines` and
`models.parse_supersedes`. Missing or `None` → empty list. Any shape
violation (scalar instead of list, missing `name` on a `supersedes`
entry, empty-string entry on `refines`) raises `ValueError`. The
auto-memory loader and the merge-pass shim catch and log the error and
default to empty lists so one malformed file does not crash the whole
ingest; downstream tooling (a future lint pass) can surface them.

### 7.5 Out of scope (deferred lanes)

- Migrating existing memories to declare relationships — a one-off
  script lane.
- MCP / agent-facing tooling to declare from the agent side — separate
  lane.
- Resolver input expansion (Lane 2, #168), prompt rewrite (Lane 3,
  #169), threshold tuning (Lane 4, #170).

---

## 8. Claim-level temporal validity (`valid_from` / `valid_until`)

Issue #308 (slice 1). Two optional frontmatter fields on auto-memory members
declare the real-world window over which a claim is true. They sit alongside
`source:` / `field_sources:` / `refines:` / `supersedes:` and round-trip through
tier0 passthrough byte-for-byte (same contract as §3 and §7).

### 8.1 Shape and semantics

Both are optional ISO-8601 **dates** (`YYYY-MM-DD`, no time component in slice 1):

```yaml
---
type: project
name: deploy-target
valid_from: 2026-04-01     # optional; open lower bound when absent
valid_until: 2026-06-30    # optional; open upper bound (still valid) when absent
source_type: user-stated
source_ref: <session>:<turn>
---
```

- A claim is valid over the window `[valid_from, valid_until]`. Both bounds are
  optional. `valid_until` is the **last date the claim was valid (inclusive)**;
  a claim is inactive when `as_of > valid_until`. The **active predicate keys on
  the upper bound only** — `valid_from` is parsed and round-tripped, and feeds
  #324's disjoint-window comparison, but does NOT gate activeness (§8.3).
- **Orthogonal to `source:`.** `source_type` / `source_ref` (#260) answer *where
  the claim came from* and *when it was ingested*; `valid_from` / `valid_until`
  answer *over what real-world window the claim is true*. This is the bi-temporal
  split (ingestion time vs. valid time) — they sit **beside** `source:`, never
  inside it.
- **Augments, does not replace, `superseded_by` / `deprecated` (#191).** Those
  remain the pointer (who won) and the both-stale flag; `valid_until` is the
  interval close. As of **slice 2** the resolver auto-stamps this interval on a
  temporal supersession (see §8.4) — the `superseded_by` mark and the
  `valid_until` close are written together, the close never replacing the mark.

### 8.2 Default-open interval (backward compatibility)

**Rule: absent `valid_until` ⇒ open upper bound ⇒ the claim is currently valid
(active).** Every existing page has no `valid_from` / `valid_until`, so no
existing file changes visibility — no migration, no backfill. The field
transitions organically as humans or the resolver (slice 2, §8.4) close
intervals. This mirrors §2.3's "no forced upgrade, legacy shape loadable
forever" stance.

A **malformed / unparseable** date **fails OPEN**: it is logged and treated as
absent (the claim stays active). Silently hiding a claim on a bad date is worse
than keeping it visible for a knowledge base — same "must not crash the nightly
compile" contract as `coerce_source_type`.

### 8.3 Currently-valid-by-default filter

Recall and the C3 compile filter to **currently-valid claims by default**. The
single shared helper `models.valid_until_expired(meta, as_of=None)` (default
`as_of = date.today()`) is wired into BOTH inactive predicates so they stay in
lockstep:

- `is_inactive_memory(meta, as_of=None)` (dict path — recall's three `search.py`
  gates, `recurring_claims.py`) gains a third disjunct: inactive if
  `superseded_by` OR `deprecated` (as before) OR `valid_until` in the past.
- `AutoMemoryFile.is_inactive(as_of=None)` (dataclass path — the C3 compile in
  `merge.py`) delegates the same temporal check to the same helper.

Because every live-knowledge read already routes through those predicates, the
past-`valid_until` disjunct filters expired claims everywhere with no call-site
changes.

**Upper bound only — `valid_from` stays ungated.** The active predicate keys on
`valid_until`, NOT `valid_from`. Gating on the lower bound would hide a
future-dated claim, which collides with §7.3 / #324: the disjoint-validity
detector short-circuit relies on a member whose `valid_from` is after today
staying **active** so a sequential (disjoint) pair can form — a not-yet-valid
claim is a recorded FUTURE state, not a hidden one. Slice 3's as-of rewind
therefore views history through the upper bound and the #191 tombstones, which is
exactly where the supersession-as-interval value lives (the slice-2 resolver
closes intervals by stamping `valid_until`).

### 8.5 As-of view (slice 3)

The `as_of` parameter — designed in at slice 1 — is now threaded out to an
operator-facing **as-of view** that answers *"what did we believe on DATE?"*.
It is **read-only**: no resolver decision, no wiki mutation. Two surfaces:

- **`athenaeum recall <query> --as-of YYYY-MM-DD`.** Returns the wiki as it stood
  on that date. Indexed backends (fts5/vector) filter at BUILD time, so recall
  builds a **throwaway as-of index** under `<cache-dir>/_asof/<date>/` and queries
  that — the live index is never touched. The `keyword` backend scans on query
  and honors `as_of` directly. A final temporal backstop re-checks each hit's
  fresh on-disk frontmatter against `as_of`, so the printed view is exact
  regardless of backend build state.
- **`athenaeum rebuild-index --as-of YYYY-MM-DD --cache-dir <scratch>`.** Builds a
  persistent as-of index in a scratch dir (`search.build_index(..., as_of=...)` on
  all backends; the `build_fts5_index` / `build_vector_index` convenience wrappers
  accept an ISO string). Point `--cache-dir` at a scratch path so the live index
  is not overwritten, then `recall --cache-dir <scratch>`.

The as-of rewind operates through the **upper bound** (`valid_until`) and the
#191 tombstones: a claim valid on DATE but expired now is **included** (its
`valid_until` had not yet passed on DATE). Because `valid_from` is ungated
(§8.3), a claim not yet valid on DATE is NOT excluded by this view — that is the
deliberate cost of keeping #324's disjoint detector working, and the
supersession-as-interval value (slice-2 `valid_until` closes) is unaffected. The
MCP `recall` tool and the C3 *compile* view stay at today (default `as_of`) — a
compile-as-of would write a historical view over the live wiki, so it is deferred
to a later slice.

### 8.4 Resolver interval-close (slice 2)

**Slice 2 (#308) makes `resolutions.enact_resolution` auto-stamp the loser's
`valid_until` when a resolution establishes a TEMPORAL supersession** — the
loser is *valid-then-replaced* history, not a wrong claim. The stamp AUGMENTS,
never replaces, the existing mark (§8.1): the loser stays `superseded_by` the
winner and is still filtered by `is_inactive_memory`.

Which verdicts trigger a close:

- **`keep_a` / `keep_b`** — the loser is valid-then-replaced. Its interval
  closes at the **winner's `valid_from`** when known, else at the **resolution
  date** (`date.today()`).
- **Sequential-snapshot `not_a_conflict`** — two dated snapshots of the same
  fact (older → newer). The **older** member's interval closes at the newer's
  lower bound. Ordering is by `valid_from`, else ingestion date (`created_at` →
  `updated_at`); with **no reliable ordering signal, nothing is stamped**. This
  verdict is deliberately NOT in `ENACTING_ACTIONS`, so the merge-pass
  suppress/drop routing is unchanged — the close fires only when a caller routes
  the pair through `enact_resolution`.
- **Do NOT close** for `correct_*` / `forget_*` (loser was WRONG, never validly
  true), `deprecate_both` (both stale), `retain_both_with_context`, `merge`,
  `propose_merge`.

**Value & only-close-never-widen.** The stamped value is the inclusive
last-valid date (`YYYY-MM-DD`; a claim is inactive iff `as_of > valid_until`,
§8.1). If the loser already carries an EARLIER `valid_until`, it is preserved —
a resolution must not EXTEND validity; only the earlier of (existing, new bound)
is kept.

**Boundary reconciliation with #324.** `models.validity_windows_disjoint` uses a
STRICT `<` on the inclusive `valid_until`, so a loser ending on date X and a
winner starting on date X SHARE day X and are **not** disjoint. Stamping
`loser.valid_until = winner.valid_from` therefore leaves the pair non-disjoint
at the boundary day **by design** — no minus-one-day is subtracted (§8 does not
specify one). This is safe because the superseded loser is ALSO marked
`superseded_by` and hence inactive: it never re-surfaces as a live claim
regardless of the one-day window overlap. The exact stamped value is pinned by
`tests/test_conflict_resolution.py::TestIntervalCloseSlice2`.

### 8.6 Per-claim compiled validity (slice 4)

Slices 1–3 carry `valid_from` / `valid_until` on **raw members** and filter
them at the member (page) grain: an expired member is dropped whole from recall
and from the C3 compile (`is_inactive_memory` / `AutoMemoryFile.is_inactive`).
Slice 4 pushes validity **into the compiled entry**, per claim rather than per
page.

**The claim is the member; its window travels with it into the entry.** A C3
compile blends many raw members (each one claim) into one wiki entry whose
`sources:` list is the per-claim provenance record (§3, §7; #262 already carries
per-source `claim` / `verdict`). Slice 4 stamps each surviving member's
`valid_from` / `valid_until` onto the source records that member contributes
(`merge._stamp_member_validity`), so the compiled entry records **which window
each claim is valid over**, instead of the whole page being one valid/invalid
unit. All sources a member cites share that member's window (the window belongs
to the claim, applied to each of its citations).

- **Only-fill-never-override.** A bound the source already declares (a future
  explicit per-source window) is preserved; the member value fills only an
  absent bound. An open/malformed member bound (`""`) adds no key.
- **Round-trips like `claim` / `verdict`.** `valid_from` / `valid_until` serialize
  into the entry's frontmatter `sources:` list and re-parse through
  `_parse_one_source` byte-for-byte (bounds normalized via `validity_bound_str`).
- **Rendered for humans.** `render_source_footnotes` appends a
  `— **Valid:** <window>` clause (`X to Y` / `from X` / `until Y`); a source with
  no window renders exactly as before (back-compat).
- **No new page-level filter, no regression to slices 1–3.** Members are still
  filtered whole by `is_inactive` BEFORE sources are built, so a compiled entry
  only ever carries currently-active (or future-dated, §8.3) claims; slice 4
  surfaces each surviving claim's window (e.g. a still-active claim's
  `valid_from`, or a future `valid_until`) without changing what is compiled.
  The #324 disjoint detector and the slice-2 resolver interval-close are
  untouched — they operate on raw members, upstream of the compile.

Pinned by `tests/test_temporal_validity.py::TestPerClaimCompiledValiditySlice4`.

## 9. Multi-dimensional scoped claims (`scope: {org, locale}` + time)

Issue #329 (buildable subset of the design pass). Generalizes §8's TIME
dimension to a small **product poset** over `{org, locale, time}`. Two claims
can BOTH be true when separated by organizational scope, specificity, or locale;
the detector/resolver represents that so scope-separated claims stop surfacing as
false contradictions. Lives in `src/athenaeum/scoped_claims.py`; the resolver
short-circuit + `scope_*` actions in `resolutions.py`.

### 9.1 Shape

```yaml
---
type: reference
name: deploy-policy
valid_from: 2026-04-01        # TIME dimension (§8, #308) — unchanged
scope:
  org: kromatic/platform      # node in the versioned org tree; absent = org-wide
  locale: en-US               # absent = everywhere
---
```

- `scope:` is an optional nested block. `org` / `locale` are single string
  coordinates; the time dimension stays the top-level `valid_from` / `valid_until`
  from §8. All optional and round-trip through tier0 byte-for-byte (§3 contract).
- Each dimension is a **poset with TOP = unscoped** (absent coordinate). `org` /
  `locale` are **trees** — a descendant node ⊑ its ancestor
  (`kromatic/platform ⊑ kromatic`; `en-US ⊑ en`), "lower" = more specific = smaller
  region. Time is **intervals under inclusion**. The **meet** (region
  intersection) is componentwise: interval intersection for time, "lower-if-
  comparable, else empty" for the trees.

### 9.2 Versioned tree config — authors may not mint scope values

The org/locale node sets are a small **versioned config** in `athenaeum.yaml`:

```yaml
scope:
  org:    [kromatic, kromatic/platform, kromatic/marketing]
  locale: [en, en-US, de-DE]
```

A coordinate value **not in the tree** normalizes to *unscoped* (TOP) with a
debug breadcrumb — the hard lesson from Cyc's microtheory proliferation. This
**fails open toward detection**: a typo adds no constraint rather than silently
carving a phantom scope that could hide a claim. No `_DEFAULTS` seed (#231): a
fresh install has empty trees, so every declared org/locale value is inert and
single-user behavior is unchanged.

### 9.3 Three-way overlap verdict

`scoped_claims.scope_comparison` replaces the binary conflict/no-conflict split
(wired into `resolutions._scope_verdict_proposal`, before the declared/LLM path):

- **DISJOINT** — empty meet in some dimension → `not_a_conflict` (conf 1.0, no
  Opus call). Generalizes §8/#324's disjoint-time pre-filter to org/locale.
- **OVERRIDE** — one context strictly below the other in the **org/locale trees**
  → `not_a_conflict` (conf 1.0): the specific claim is an exception, the general
  claim governs the remainder, **both stay active** (defeasible specificity).
- **OVERLAP** — same-context or incomparable-but-overlapping → falls through to
  the resolver as a genuine contradiction.

**Deferred (ADR).** Time-interval NESTING does **not** trigger OVERRIDE — only
org/locale tree-specificity does — so §8/#324's shipped "nested time still reaches
the resolver" semantic is preserved. Whether a bounded-time exception should
override an always-valid claim is left to the #329 ADR.

### 9.4 Scope-edit resolver actions (`scope_a` / `scope_b`)

NARROW the named side's scope until the meet is empty, converting an apparent
contradiction into two durably-true **scoped** claims that never re-enter
detection. BOTH members stay **active** — the minimal-information-loss choice,
preferred over `keep_*` (retires the loser) / `correct_*` / `forget_*` (delete)
whenever both sides were true somewhere/somewhen.

Enactment (`enact_resolution`) narrows the **TIME** dimension: closes the named
side's `valid_until` to the day BEFORE the other side's `valid_from` (strict-`<`
disjoint, **minus one day** so both stay live — unlike §8.4's boundary-day share,
which relies on `superseded_by` for inactivity). Only-close-never-widen. Org/locale
coordinate PINNING is deferred to the ADR; when the pair has no time boundary,
`scope_*` no-ops (escalates to the human) rather than guessing a coordinate. In
`ENACTING_ACTIONS` + `flip_action`; auto-apply threshold 0.90.

The broader team/multi-tenant scope-IDENTITY system and the recall
`serve --scope` caller-context filter are out of scope (#314), deferred design.

## 10. Channel split, model recording, IdP-compatible asserter identity

Issue #326. Extends `source_type` (#260) — which collapsed three materially
different AI channels into `inferred`, recorded nothing about WHICH model
asserted a claim, and had no person identity for `user-stated` beyond a
session ref — with a channel split, a `model:` recording field, and an
IdP-compatible `asserter:` block. Round-trips through tier0 byte-for-byte
via the existing `WikiBase.model_config = ConfigDict(extra="allow")` path;
no schema tightening.

### 10.1 Channel split — extend the `source_type` vocabulary

The `source_type` vocabulary gains two values (see
`models.SOURCE_TYPES`):

| value | meaning |
|---|---|
| `user-stated` | human utterance in a session (unchanged from #260) |
| `agent-observed` | **new** — AI derived it from in-session artifacts (file contents, tool output); verifiable against the transcript |
| `inferred` | AI leap without artifact backing (unchanged; stays the coercion default) |
| `model-prior` | **new** — asserted from training-data knowledge with no session evidence |
| `external` / `document` | unchanged (external citation / permanent document) |

`coerce_source_type` keeps its fail-open contract: a typo / unknown value
downgrades to `inferred` with a debug breadcrumb; missing/None passes
through to `inferred` quietly (legacy pages).

**Resolver precedence.** In the source-precedence taxonomy exposed to
the resolver LLM (`_RESOLVE_SYSTEM` in `resolutions.py`),
`model-prior:<model-id>` ranks BELOW `script:<slug>` — a pipeline slug
at least names a repeatable in-tree process; a training prior names
only the model that guessed, and dates from that model's cutoff.
`unsourced` remains the last tier. The full ranking becomes:

1. `user:<conversation-ref>` (highest)
2. `linkedin:<username>` / `twitter:<username>`
3. `api:apollo` / `api:<vendor>`
4. `wikipedia:<page>`
5. **`agent-observed:<model>:<session-ref>` (new — issue #328)**
6. `claude:tier3-...`
7. `script:<slug>`
8. **`model-prior:<model-id>` (new — issue #326)**
9. `unsourced` / empty (lowest)

`agent-observed` (issue #328) ranks BELOW `wikipedia:<page>` — it is not
a curated public authority — but ABOVE `claude:tier3`/inferred: it is
grounded in a real in-session artifact the agent READ (file contents or
tool output), verifiable against the transcript, not an unsupported
leap. It is written by the `repair --backfill-sources` pass when it
re-classifies a DEFAULTED `claude:inferred` memory and finds the claim
in a tool-result block; the scalar becomes
`agent-observed:<model>:<session-ref>` (the `<model>` segment is omitted
when the transcript carries none).

Lock discipline: any change to this taxonomy MUST update
`docs/conflict-resolution.md` (which cross-links to this section), the
`_RESOLVE_SYSTEM` prompt + its `tests/data/resolve_system.txt` snapshot,
AND the corresponding test in `tests/test_conflict_resolution.py` in the
same change.

### 10.1a Source backfill — `repair --backfill-sources` (issue #328)

A memory written through `remember()` with no `sources` gets the
DEFAULTED scalar `source: claude:inferred` (`mcp_server._DEFAULT_INFERRED_SOURCE`).
The `repair --backfill-sources` pass re-examines each such memory against
its origin transcript (located via `originSessionId` + `originTurn`,
scope = the auto-memory parent dir) and matches THE CLAIM (`name`/`title`,
fallback first non-frontmatter line) as a normalized substring:

1. **User said it → `user-stated`.** The `source:` scalar is rewritten to
   `user:<session>#turn<N>` (resolver tier 1 — precedence keys on the
   `source:` SCALAR, not `source_type`) and `source_type: user-stated` /
   `source_ref` are set. `on_behalf_of` is populated from the owner's
   configured `asserter` (§10.3) ONLY when it yields a durable identity
   key; transcripts carry no OIDC identity, so it is usually absent.
2. **Derived from a tool-result artifact → `agent-observed`.** The scalar
   is rewritten to `agent-observed:<model>:<session-ref>` (tier 5 above)
   and `source_type: agent-observed` / `source_ref` / `model` are set.
3. **No support found → confirm inferred.** A boolean
   `inferred_verified: true` marker is stamped; precedence is UNCHANGED.
   The marker makes the pass idempotent — a confirmed memory is skipped on
   every subsequent run, as is any already-upgraded memory (its scalar is
   no longer `claude:inferred`).

The pass touches ONLY provenance keys (body + all other frontmatter lines
are byte-for-byte preserved, per the §3 tier0 discipline), runs under the
#309 run lock on `--apply`, writes atomically (`atomic_io`), and SKIPS
(never guesses) when the transcript is missing/rolled off. Dry-run
(default) prints per-memory proposed upgrades and writes nothing.

### 10.2 Model recording

AI-attributed claims (`source_type` in
`models.AI_ATTRIBUTED_SOURCE_TYPES` — `agent-observed`, `inferred`,
`model-prior`) SHOULD carry a top-level `model:` frontmatter field with
the model-id that asserted them:

```yaml
---
type: feedback
name: caching-strategy-guess
source_type: model-prior
model: claude-opus-4-7            # optional but SHOULD be set for AI channels
on_behalf_of: alice               # optional W3C PROV actedOnBehalfOf principal
---
```

Optional at the schema level (fail-open — a missing `model:` is not a
validation failure); RECOMMENDED at the write path so downstream
audits can trace a stale `model-prior` claim to a specific model
cutoff. `on_behalf_of:` names the responsible human principal (model
asserted, human accountable) and is also optional.

### 10.3 Asserter identity for humans — enterprise-IdP compatible

`user-stated` claims MAY carry an `asserter:` block naming the human
who made the claim. Keys on the OIDC-guaranteed stable pair
(`iss`, `sub`) with the Microsoft Entra pairwise-`sub` trap handled:

```yaml
asserter:
  type: person                              # person | software_agent | organization
  iss: "https://accounts.google.com"        # durable key part 1 (verbatim from token)
  sub: "1076..."                            # durable key part 2
  provider_ids:                             # optional per-provider extras
    entra_oid: "..."                        # Entra's stable per-tenant object id
    entra_tid: "..."                        # Entra's tenant id
  email: user@example.com                   # display snapshot; NEVER a key
  name: "Alice Example"                     # display snapshot; NEVER a key
```

Semantics (locked by `models.asserter_identity_key`):

- **Standard branch (Google, Okta, most OIDC providers):** identity key
  is `(iss, sub)`. `email` and `name` are display-only snapshots — an
  email change does NOT orphan the identity, since the key is unchanged.
- **Microsoft Entra branch:** Entra's `sub` claim is PAIRWISE per app
  (OIDC-Core §8.1 — a client sees a DIFFERENT `sub` for the same user
  in a different app), so `sub` is UNUSABLE as a cross-app identity
  anchor. When `provider_ids.entra_tid` and `provider_ids.entra_oid`
  are both set, the identity key becomes `(iss, "entra", tid, oid)`
  and `sub` is IGNORED.
- **Single-user / degraded:** `iss: local`, `sub: <configured-owner>`
  is a valid identity for single-user deployments.
- **No durable identifier extractable:** identity key is `()`; the
  caller should treat that as "no identity declared" and fall back to
  owner-only defaults.

Maps to SCIM (RFC 7643) for future provisioning correlation:
`type: person` ≙ SCIM `User`; `type: organization` ≙ SCIM `Group` (via
tenant); `provider_ids.entra_*` are the Entra-tenant Object identifiers
SCIM already uses.

Google's own docs are explicit: never key on email. This lock exists so
we can't drift toward the tempting-but-broken shortcut.

### 10.4 MCP `remember(sources=...)` surface

The MCP `remember` tool's `sources=` argument accepts the new
channel-split payloads via additional wrapper keys alongside the
pre-existing `_source` and `_field_sources` (§4):

```python
remember(text="...", sources={
    # SourceRef surface (from §4) — unchanged
    "_source": "user-stated:session-abc#turn-5",

    # Channel-split extras (issue #326)
    "_source_type": "user-stated",                  # coarse channel
    "_source_ref":  "session-abc#turn-5",           # ULTIMATE reference
    "_model":       "claude-opus-4-7",              # AI model-id (optional)
    "_on_behalf_of": "alice",                       # human principal (optional)
    "_asserter": {                                  # IdP identity for user-stated
        "type": "person",
        "iss":  "https://accounts.google.com",
        "sub":  "1076...",
        "email": "alice@example.com",
    },
})
```

Every extra is OPTIONAL; provide only the ones the caller can honestly
attest to. Validation stays fail-open (matches
`coerce_source_type`'s "must not crash the nightly compile" contract):
a typo'd `_source_type` downgrades to `inferred` on read; a
non-dict `_asserter` is rejected loudly (it's a corruption signal, not
a normal input path). Extras land as frontmatter keys with the same
name as the wrapper key minus its leading underscore
(`_source_type` → `source_type`, `_asserter` → `asserter`, etc.) so
the read-side parsers (`models.parse_asserter`,
`models.coerce_source_type`, `models.parse_model`) find them.

### 10.5 What this doc does NOT decide

- **Rendering `model:` / `asserter:` into the resolver's per-member
  passages.** Issue #326 does the source-precedence prompt update,
  but the resolver already receives each member's frontmatter dict — a
  future lane can surface `model:` / `asserter:` in the rendered
  passage without a lock change.
- **Auto-detecting the asserter from an ambient identity provider.**
  The MCP server does not read OIDC tokens from the environment; the
  caller supplies the `_asserter` block. A future middleware lane
  (deferred) can populate it automatically.
- **Preserving asserter identity across a rename of the display email.**
  The identity key doesn't change; but there is no separate index that
  maps old email → new email. This is deliberately out of scope —
  `email` is a snapshot for humans reading the frontmatter, not a
  lookup key.

## 11. Implementation issue mapping

| Section | Issue | What the issue implements |
|---------|-------|---------------------------|
| §2 per-value `field_sources` | #102 | Validator + readers accept the new list-of-records shape; writers emit it; legacy shape stays loadable. |
| §3 tier0 byte-for-byte | (no issue — already invariant) | Existing tier0 passthrough already satisfies the rule. Add a regression test asserting per-value shape round-trips. |
| §4 MCP `remember(sources=...)` | #96 | Replace the bare-dict heuristic with the `_source`/`_field_sources` wrapper keys; update docstring + integration test. |
| §5 legacy slug migration | #97 | `athenaeum repair --legacy-source-slugs` with dry-run/apply, the fixed mapping table, and post-migration validation. |
| §8 claim-level temporal validity | #308 | Slice 1: `valid_from` / `valid_until` parse + round-trip; shared `valid_until_expired` helper; currently-valid-by-default filter. **Slice 2 (shipped): resolver interval-close (§8.4) — `enact_resolution` stamps the loser's `valid_until` on `keep_a`/`keep_b` and sequential-snapshot `not_a_conflict`, only-close-never-widen.** Slice 3 (`--as-of`) deferred. #329 generalizes the close to non-time scopes. |
| §9 multi-dimensional scoped claims | #329 | Buildable subset: `scope: {org, locale}` poset (trees) + time, versioned tree config (`scope.org`/`scope.locale`), three-way `scope_comparison` verdict (DISJOINT / OVERRIDE / OVERLAP) wired into `resolutions._scope_verdict_proposal`, and `scope_a`/`scope_b` resolver actions (time-dimension narrowing enactment). **Deferred (ADR):** time-nesting OVERRIDE, org/locale coordinate pinning enactment, recall `serve --scope` filter, team/multi-tenant scope-identity (#314). |
| §10 channel split + model + asserter | #326 | Extend `SOURCE_TYPES` with `agent-observed` and `model-prior`; add `model:` / `on_behalf_of:` / `asserter:` claim-level frontmatter fields; extend `remember(sources=...)` with `_source_type` / `_source_ref` / `_model` / `_on_behalf_of` / `_asserter` wrapper keys; drop `model-prior:<model-id>` into the resolver's precedence taxonomy below `script:`. |
| §12 claim kind + opinion attribution | #327 | Add `claim_kind:` (`fact`/`observation`/`opinion`/`decision`/`policy`/`definition`) classified once at intake (`claim_kind.classify_claim_kind`, tier2-style), round-tripped by tier0; add `compare_asserters` (`same`/`different`/`unknown`) over the §10.3 identity key; add the `attribute_both` resolver action + `_stance_attribution_verdict` short-circuit + detector `stance` conflict type. An opinion is NEVER resolved by precedence; unknown asserter → keep-both fallback. |

## 12. Claim kind + opinion attribution (`claim_kind:`, `attribute_both`)

**Status: locked, implemented (issue #327).** Adds an EPISTEMIC classification
orthogonal to `source_type`, and an asserter-comparison rule so evaluative
claims are never resolved by source precedence.

### 12.1 `claim_kind:` frontmatter field

One of `fact | observation | opinion | decision | policy | definition`.
Classified ONCE at intake by a cheap LLM pass (`claim_kind.classify_claim_kind`,
same pattern / model knob as tier2 classification), stored in frontmatter, and
round-tripped byte-for-byte by tier0 passthrough (§3). **Absent / unrecognized
→ `""` (unclassified), fail-open** via `models.parse_claim_kind` — an
unclassified claim keeps pre-#327 behavior. `claim_kind` classifies the SHAPE
of the claim (is it evaluative?); `source_type` classifies its ORIGIN channel.
The two are independent.

### 12.2 Asserter comparison — `same` / `different` / `unknown`

`models.compare_asserters(a, b)` reuses the §10.3 OIDC-durable
`asserter_identity_key` and returns:

- **`unknown`** when EITHER side yields an empty identity key. This is the
  COMMON case — a Claude session carries no OIDC identity. Identity is
  CAPTURED only when a caller/`remember()` supplies an `_asserter` block; it is
  NEVER fabricated from a transcript.
- **`same`** — both keys non-empty and equal (email change does not matter).
- **`different`** — both keys non-empty and unequal.

### 12.3 `attribute_both` resolver action + `stance` routing

For an evaluative pair (both `claim_kind: opinion`, or detector
`conflict_type: stance` with neither side an explicit non-opinion kind),
`resolutions._stance_attribution_verdict` short-circuits WITHOUT an Opus call:

- **different** asserters → `attribute_both` (keep both, explicit attribution).
- **unknown** asserter on either side → **`attribute_both` (REQUIRED keep-both
  fallback)** — never supersede or delete an opinion by precedence when
  identity is missing.
- **same** asserter + a distinguishing date → supersession (keep the newer);
  undated → `attribute_both`.

`attribute_both` is non-destructive: `enact_resolution` stamps
`attributed: true` on BOTH members (both stay active; each keeps its own
`asserter:` block). It is in `ENACTING_ACTIONS`, orientation-agnostic
(`flip_action` → `None`), auto-apply threshold 0.90. `merge._emit_escalation`
drops the pending-question escalation for `attribute_both`, so the pair never
re-queues to the human. Full behavior lock: `docs/conflict-resolution.md` §13.
