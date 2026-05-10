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

## 7. Implementation issue mapping

| Section | Issue | What the issue implements |
|---------|-------|---------------------------|
| §2 per-value `field_sources` | #102 | Validator + readers accept the new list-of-records shape; writers emit it; legacy shape stays loadable. |
| §3 tier0 byte-for-byte | (no issue — already invariant) | Existing tier0 passthrough already satisfies the rule. Add a regression test asserting per-value shape round-trips. |
| §4 MCP `remember(sources=...)` | #96 | Replace the bare-dict heuristic with the `_source`/`_field_sources` wrapper keys; update docstring + integration test. |
| §5 legacy slug migration | #97 | `athenaeum repair --legacy-source-slugs` with dry-run/apply, the fixed mapping table, and post-migration validation. |
