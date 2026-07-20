# Conflict Resolution — Audit and Lock (issue #91)

This document is the LOCK for athenaeum's conflict-resolution behavior as of
merge SHA `b0ee25c` on `develop` (post-Lane-E). It catalogs every code path
that resolves disagreements between two pieces of data — between raw intake
and the wiki, between two cluster members, between a canonical wiki and an
absorbed duplicate — and pins the exact rule each one applies.

The companion test suite at `tests/test_conflict_resolution.py` asserts each
documented rule. **Any future PR that changes a documented rule MUST update
this document AND the corresponding test in the same change.** Schema-tightening
PRs that surface new conflict surfaces MUST add a new section here.

The audit is read-only — no behavior changes shipped with #91. Bug findings are
filed as separate `moscow:could` (or `moscow:should` when correctness-critical)
issues; see the PR body for the live list.

GitHub permalinks below all anchor on `b0ee25c`.

---

## 1. `librarian.py:tier0_passthrough`

- **File:** [`src/athenaeum/librarian.py` lines 304–388](https://github.com/Kromatic-Innovation/athenaeum/blob/b0ee25c/src/athenaeum/librarian.py#L304-L388)
- **Trigger:** Runs first in `process_one()` for every raw-intake file. Promotes
  pre-structured raw markdown (already valid wiki schema with `uid` + `type` +
  `name`) verbatim to `wiki/` without invoking any LLM tier.
- **Resolution rule:** **Skip-on-conflict.** This path does NOT resolve conflicts
  at all. It enforces a strict eligibility gate and bails to `None` (caller falls
  through to Tier 1/2/3) if any of these are true:
  - frontmatter does not parse,
  - `uid`, `type`, or `name` is empty,
  - `type` is not in the schema's allowlist,
  - the uid already exists in the index (idempotent re-run guard),
  - a file with the target filename already exists on disk.
- **Provenance behavior (post-#90):** Passes raw frontmatter through byte-for-byte.
  If the raw carries `field_sources`, it survives intact. If it does not, no
  attribution is synthesized — no Apollo call runs in this path.
- **Known edge cases:**
  - Custom-namespace fields (`relationship:`, `apollo_*`, `linkedin_url`, etc.)
    round-trip via `render_frontmatter`'s `extra="allow"` schema — the LLM tiers
    would drop them.
  - `created` is stamped only if missing; `updated` is always overwritten with
    today's date. This is the one mutation tier0 makes to incoming frontmatter.

## 2. `tiers.py:tier3_create`

- **File:** [`src/athenaeum/tiers.py` lines 305–353](https://github.com/Kromatic-Innovation/athenaeum/blob/b0ee25c/src/athenaeum/tiers.py#L305-L353)
- **Trigger:** Tier 3 write phase — when a `ClassifiedEntity` from Tier 2 has
  `is_new=True` and Tier 1 found no existing match.
- **Resolution rule:** **No conflict possible by construction.** Tier 3 create
  runs only after Tier 1 (programmatic match) AND Tier 2 (LLM dedupe via the
  "skip these" allowlist) have agreed the entity is new. The only failure mode
  is API-level — `messages.create` exceptions propagate to the caller.
- **Provenance behavior (post-#90):** Sets `created` and `updated` to today.
  Does not write `field_sources` — `field_sources` is an Apollo-namespace
  contract, not a schema-wide one.
- **Known edge cases:**
  - The LLM is instructed to write footnotes citing `source_ref`, but does not
    populate `field_sources`. Per-claim provenance for Tier 3 creates lives in
    the body, not the frontmatter.

## 3. `tiers.py:tier3_merge`

- **File:** [`src/athenaeum/tiers.py` lines 356–398](https://github.com/Kromatic-Innovation/athenaeum/blob/b0ee25c/src/athenaeum/tiers.py#L356-L398)
- **Trigger:** Tier 3 write phase — when a `ClassifiedEntity` matches an
  existing wiki page (`existing_uid` set).
- **Resolution rule:** **LLM-mediated, three-class resolution** dictated by the
  `MERGE_SYSTEM` prompt:
  - **Factual contradiction** (verifiable fact): the LLM is told to "keep the
    more reliable source, note the discrepancy" — the LLM picks which side
    wins, athenaeum does not enforce a rule.
  - **Contextual difference** (opinions, preferences): "capture both with context"
    — both retained in body.
  - **Principled tension** (values, axioms): the LLM emits `ESCALATE: <description>`
    optionally followed by `---\n<merged body>`. `tier3_merge` returns
    `(body_or_None, EscalationItem(conflict_type="principled"))`.
- **Provenance behavior (post-#90):** Stamps `meta["updated"]` with today's date
  inside `tier3_write` (line 446). Does not touch `field_sources`. Existing
  body is passed in plain; the merged body is written verbatim from the LLM.
- **Known edge cases:**
  - When the LLM returns `ESCALATE:` without a `---` separator, `body=None` is
    returned and the caller does NOT update the existing page — the escalation
    is queued, the wiki is unchanged.
  - There is no deterministic "incoming-wins" or "existing-wins" rule. Behavior
    is fully delegated to the LLM and the prompt's three-class taxonomy. **Bug
    finding filed as separate issue** — see PR body.

## 4. `tiers.py:tier3_write`

- **File:** [`src/athenaeum/tiers.py` lines 401–457](https://github.com/Kromatic-Innovation/athenaeum/blob/b0ee25c/src/athenaeum/tiers.py#L401-L457)
- **Trigger:** Wraps the per-action loop for a single raw file.
- **Resolution rule:** **No write-time locking. Last-write-wins on disk.** All
  Tier 3 LLM calls run first and accumulate `pending_updates`; only after every
  call succeeds does the loop write to disk (lines 454–455). This gives an
  all-or-nothing semantic per raw file but does NOT coordinate concurrent writers.
  Two pipelines processing different raw files that target the same wiki entity
  will race on the filesystem.
- **Provenance behavior (post-#90):** `meta["updated"]` is set to today before
  rendering. `field_sources` is preserved if it exists on the existing page;
  it is neither added to nor pruned.
- **Known edge cases:**
  - All-or-nothing applies WITHIN one raw file. If raw file A and raw file B
    both produce `update` actions for the same wiki entity, A's full update is
    written, then B's full update overwrites — no per-field merge across raw
    files; whichever runs second wins. **Documented but not flagged as a bug**:
    this is the deliberate single-writer assumption baked into the librarian.
  - Schema validation runs on the merged frontmatter via `render_frontmatter`
    only (no `validate_wiki_meta` call here, unlike tier0).

## 5. `merge.py` — auto-memory cluster merge

`merge.py` is **NOT** a field-level wiki merger. It consolidates auto-memory
cluster JSONL into one `wiki/auto-<topic-slug>.md` per cluster. The "merge" verb
overlaps with the dedupe path's per-field merge but the surfaces are disjoint.

### 5a. `merge_cluster_row`

- **File:** [`src/athenaeum/merge.py` lines 387–527](https://github.com/Kromatic-Innovation/athenaeum/blob/b0ee25c/src/athenaeum/merge.py#L387-L527)
- **Trigger:** Per cluster row from the canonical cluster JSONL.
- **Resolution rule (per field):**
  - `topic_slug`: derived from member filenames; collisions resolved by
    `merge_clusters_to_wiki` via cluster_id suffixing (lines 619–627).
  - `origin_scopes`: union with first-seen ordering preserved (line 477).
  - `sources`: union with `(session, turn)` dedupe key, **first occurrence
    wins** (`dedupe_sources` line 305). `(session, date)` is explicitly NOT
    used as a dedupe key.
  - `body`: concatenate-with-paragraph-dedupe — every member's body is appended
    in cluster input order, paragraphs seen verbatim earlier are dropped
    (`synthesize_body` line 333). **Exact-match string compare on whitespace-trimmed
    paragraphs**; variant phrasings are kept.
  - `cluster_centroid_score`: comes from the JSONL row, not merged.
- **Provenance behavior (post-#90):** `sources[]` carries `origin_scope` per
  entry. The new entity-schema `field_sources` map is irrelevant here —
  auto-memory entries do not use it.
- **Known edge cases:**
  - Members that fail to resolve to a live file are dropped with a warning
    (line 419); the cluster proceeds with its surviving members.
  - When a cluster member has no `sources[]` at all, a synthetic source is
    built from `originSessionId`+`originTurn` (`_am_as_implicit_source`).

### 5b. `merge_clusters_to_wiki`

- **File:** [`src/athenaeum/merge.py` lines 557–683](https://github.com/Kromatic-Innovation/athenaeum/blob/b0ee25c/src/athenaeum/merge.py#L557-L683)
- **Trigger:** Top-level entry point. Reads cluster JSONL, runs C4 contradiction
  detection, writes `wiki/auto-*.md`, queues escalations.
- **Resolution rule:**
  - **Slug collisions:** first wins; subsequent get a `-<cluster_id>` suffix.
  - **Contradictions detected:** does NOT block the write. The merged entry is
    still rendered with `contradictions_detected: true` + `status:
    contradiction-flagged` in frontmatter. An `EscalationItem` is queued to
    `_pending_questions.md` for human review.
- **Provenance behavior (post-#90):** N/A — same as 5a.

## 6. `contradictions.py:detect_contradictions`

- **File:** [`src/athenaeum/contradictions.py` lines 247–304](https://github.com/Kromatic-Innovation/athenaeum/blob/b0ee25c/src/athenaeum/contradictions.py#L247-L304)
- **Trigger:** Called by `merge_clusters_to_wiki` once per cluster (every cluster,
  including dry-run).
- **Resolution rule:** **DETECT-ONLY, NEVER AUTO-RESOLVE.** This module's
  contract is to surface contradictions for human review; it returns a
  `ContradictionResult` and the caller writes both the merged entry AND an
  escalation. The cluster's body is unchanged regardless of detector output.
- **Resolution boundary:** Two contradiction types are distinguished — `factual`
  (incompatible facts about the same thing) and `prescriptive` (opposing guidance).
  Tier 3's `principled` class is intentionally separate: it lives in the
  entity-wiki path, not the auto-memory path.
- **Provenance behavior (post-#90):** N/A — operates over auto-memory bodies.
- **Known edge cases:**
  - Singleton clusters return `detected=False` without a network call.
  - No client (`ANTHROPIC_API_KEY` unset) returns
    `ContradictionResult(detected=False, rationale="llm-unavailable")`.
  - Any API error degrades to `detected=False` (`rationale="llm-unavailable"`).
  - Detector returning an invalid `conflict_type` literal degrades to
    `detected=False`.
  - The detector echoes member refs that are validated against the input
    cluster's known refs; basename-fallback matches are accepted; unknown
    refs are dropped silently.

## 7. `dedupe.py:_perform_merge` (and `_merge_meta`)

- **File:** [`src/athenaeum/dedupe.py` lines 365–471](https://github.com/Kromatic-Innovation/athenaeum/blob/b0ee25c/src/athenaeum/dedupe.py#L365-L471)
- **Trigger:** Called per `DuplicatePair` from `find_duplicate_persons`. Merges
  the absorbed person wiki into the canonical, then deletes the absorbed file.
- **Resolution rule (per field type — THIS IS THE LOCK):**

| Field class | Keys | Rule |
|-------------|------|------|
| List union | `emails`, `tags`, `aliases` | Canonical entries first, then absorbed entries not already seen. Order preserved within each side. |
| Coalesce (canonical wins if truthy) | Apollo namespace (`apollo_id`, `apollo_headline`, `apollo_location`, `apollo_employment_history`, `current_title`, `current_company`); LinkedIn namespace (`linkedin_url`, `linkedin_position_at_connect`, `linkedin_company_at_connect`, `linkedin_connected_on`); Google namespace (`google_contact`, `google_contact_kromatic`, `google_contact_tristankromer`) | If canonical is truthy, canonical wins. Else absorbed wins. **`None`/`""`/`[]`/`{}` count as falsy.** |
| Max numeric | `warm_score`, `meeting_count_24mo`, `sent_count_24mo` | Higher number wins. Non-numeric → `-inf`. `None` on one side → other wins. |
| Max date (lexicographic ISO compare) | `last_touch`, `updated`, `apollo_enriched_on` | Later date wins by string comparison. Empty/None on one side → other wins. |
| Implicit alias | `aliases` | If absorbed's `name` differs from canonical's `name`, absorbed's `name` is appended to `aliases`. |
| Audit trail | `merged_from` | List append (deduped). `absorbed_uid` is added on every merge. |
| Audit trail (source) | `merged_from_sources` | Dict map `{absorbed_uid: absorbed_source}`. Canonical's wiki-level `source` always wins; absorbed's `source` is archived under this map. |
| Stamp | `updated` | Always set to today (overrides max-date result above). |
| Body | (markdown body) | If absorbed body is empty: canonical wins. If canonical body is empty: absorbed wins. If absorbed body is a substring of canonical: canonical wins. Else: canonical body + `\n\n## Merged from <absorbed_uid>\n\n` + absorbed body. |

- **`field_sources` resolution (`_merge_field_sources` lines 330–362):**
  - **Canonical's `field_sources.<key>` always wins.**
  - Keys present on absorbed but not canonical are carried forward — preserves
    per-value attribution for list fields whose values originated on absorbed.
  - Any `field_sources` entry whose key is no longer present in the merged
    frontmatter is pruned (no dangling attributions).
- **Provenance behavior (post-#90):** This is the reference implementation
  for #90 provenance carry-forward. The `_merge_field_sources` helper is the
  contract.
- **Known edge cases:**
  - When BOTH wikis have a non-empty `google_contact` and they differ, both
    sub-keys (`google_contact_kromatic` + `google_contact_tristankromer`) are
    coalesced from absorbed into canonical (lines 383–390) — keeps both sides'
    Google account attribution.
  - List union uses `repr(item)` as the dedupe key for dict/list members — a
    shallow but stable identity. Two semantically-equal dicts with different
    key order would NOT dedupe.
  - Idempotence guard lives in `merge_duplicate_persons` (`already_merged`
    counter when absorbed file is gone), NOT `_perform_merge` itself.

> **Note (2026-05-09):** A previous "section 8" documented the
> `connectors/apollo.py:enrich_person` resolver and the CLI
> `enrich --persons` write path. Both were extracted from the OSS package
> in athenaeum#112 and now live in a separate private toolkit.

## 9. Disjoint temporal validity — sequential states are not conflicts (issue #324)

- **Files:** `src/athenaeum/models.py` (`validity_windows_disjoint`),
  `src/athenaeum/merge.py` (`_all_pairs_disjoint`, `_detected_pair_disjoint`),
  `src/athenaeum/resolutions.py` (`_disjoint_validity_verdict`),
  `src/athenaeum/contradictions.py` (`_member_scope_header`).
- **Trigger:** Two claims carry `valid_from` / `valid_until` frontmatter (#308
  slice 1) whose windows do not overlap in time — e.g. A is true through
  2026-03-31 and B is true from 2026-04-01. These are *sequential states of the
  world*, not a contradiction, but the cheap C4 detector (which strips
  frontmatter) cannot see the windows and re-flags them every compile.
- **Shared predicate:** `validity_windows_disjoint(meta_a, meta_b)` parses each
  side with the fail-open `parse_valid_from` / `parse_valid_until`. Windows are
  disjoint **iff** one side has a CLOSED upper bound ending strictly before the
  other side's lower bound: `a_until is not None and b_from is not None and
  a_until < b_from` (or the symmetric `b_until < a_from`). `valid_until` is the
  INCLUSIVE last-valid date, so the comparison is strict `<` — a window ending
  2026-04-01 and one starting 2026-04-01 SHARE that day and are NOT disjoint. A
  missing or malformed bound coerces to `None` (open) and therefore OVERLAPS by
  default (fail-open → detection proceeds).
- **Resolution rules (four surfaces):**
  1. **Pre-detection short-circuit (`merge.py`).** In the primary detector loop,
     after `_filter_declared_pairs` prunes declared pairs, if
     `_all_pairs_disjoint(filtered)` (EVERY surviving pair disjoint; any
     overlapping/open pair falls through), the Haiku call is skipped and the
     cluster records `ContradictionResult(detected=False,
     rationale="disjoint-validity")`. The similarity-sweep path applies the same
     skip per 2-member pair. Mirrors the declared-relationship short-circuit
     (§ #167).
  2. **Post-detection guard (`merge.py`).** When the cluster is only partially
     disjoint the detector still runs, and may flag a specific disjoint pair.
     `_detected_pair_disjoint(result, filtered)` re-checks the two flagged
     members (guarding for the detector's 0/1-member echo) and downgrades to
     `detected=False, rationale="disjoint-validity"` BEFORE the escalation /
     pending-question write.
  3. **Resolver synthetic (`resolutions.py`).** If a flagged disjoint pair still
     reaches `propose_resolution`, `_disjoint_validity_verdict` returns
     `not_a_conflict` at `confidence=1.0` with NO Opus call — checked FIRST,
     before the declared-winner short-circuit.
  4. **Scope header (`contradictions.py`).** `_member_scope_header` renders a
     single TRUSTED `scope:` line per member (`valid: <from> → <until> · source:
     <source_type> · updated: <date>`, each segment omitted at its default)
     OUTSIDE the untrusted `<memory>` block. `_DETECT_SYSTEM` marks it as trusted
     temporal/provenance metadata so the detector can reason about overlapping
     windows too. The memory BODY stays untrusted inside `<memory>` tags.
- **Provenance behavior:** None of the four surfaces mutate frontmatter or
  bodies. The `disjoint-validity` rationale is recorded on the in-memory
  `ContradictionResult` / `ResolutionProposal` only.
- **Known edge cases:**
  - Both bounds absent on either side ⇒ open window ⇒ never disjoint ⇒ detection
    proceeds (a claim with no window is treated as always-valid).
  - Touching boundary (A `valid_until = B valid_from`) ⇒ shares that inclusive
    day ⇒ NOT disjoint.
  - A cluster > 2 is short-circuited only when EVERY pair is disjoint; one
    overlapping pair sends the whole (declared-pruned) remainder to the detector,
    where the post-guard still catches any individually-flagged disjoint pair.

---

## 10. Resolver interval-close on temporal supersession (issue #308 slice 2)

- **Files:** `src/athenaeum/resolutions.py` (`enact_resolution` and the
  `_close_interval` / `_sequential_snapshot_close` / `_member_ingestion_date`
  helpers), reusing `models.parse_valid_from` / `models._coerce_iso_date`.
- **Trigger:** a resolution establishes a **temporal supersession** — the loser
  is *valid-then-replaced* history, not a wrong/transient claim. `enact_resolution`
  stamps the loser's `valid_until` in ADDITION to the existing supersession mark
  (§8 provenance-shape: "Augments, does not replace, `superseded_by` /
  `deprecated`"), so the loser stays `superseded_by` the winner and is filtered
  by `is_inactive_memory`.
- **Which verdicts close an interval:**
  - `keep_a` / `keep_b` — close the loser at the **winner's `valid_from`** when
    known, else the **resolution date** (`date.today()`). Enacted via the
    existing #191 marking branch, now augmented with the close.
  - Sequential-snapshot `not_a_conflict` — two dated snapshots (older → newer);
    the **older** member closes at the newer's lower bound. Ordering: `valid_from`,
    else ingestion date (`created_at` → `updated_at`); **no reliable ordering
    signal ⇒ no stamp**. Deliberately NOT in `ENACTING_ACTIONS` — the merge-pass
    suppress/drop routing is byte-identical; the close fires only when a caller
    routes the pair through `enact_resolution`.
  - **Never** for `correct_*` / `forget_*` (loser was WRONG), `deprecate_both`
    (both stale), `retain_both_with_context`, `merge`, `propose_merge`.
- **Only-close-never-widen:** if the loser already carries an EARLIER
  `valid_until`, it is preserved; a resolution must not EXTEND validity. The
  stored value is the inclusive last-valid date (`YYYY-MM-DD`).
- **Boundary reconciliation with §9 / #324:** `validity_windows_disjoint` uses a
  STRICT `<` on the inclusive `valid_until`, so `loser.valid_until =
  winner.valid_from` leaves the pair **non-disjoint at the boundary day by
  design** (they share that day). Safe because the loser is also `superseded_by`
  and hence inactive — it never re-surfaces regardless of the one-day overlap. No
  minus-one-day is subtracted (§8 specifies none).
- **Provenance behavior:** best-effort frontmatter write via
  `_mark_member_frontmatter`; a read/write error is logged and swallowed
  (enactment must never crash the merge pass). Winner file is never mutated.
- **Follow-up (#329):** generalized this interval-close to non-time scopes
  (org/locale) via the three-way scope verdict + `scope_*` resolver actions —
  see Section 12.

---

## 11. Resolver source-precedence taxonomy — channel split (issue #326)

- **File:** [`src/athenaeum/resolutions.py`](https://github.com/Kromatic-Innovation/athenaeum/blob/develop/src/athenaeum/resolutions.py)
  — the `_RESOLVE_SYSTEM` prompt's `SOURCE-PRECEDENCE TAXONOMY` block.
- **Trigger:** the resolver LLM applies this taxonomy when a
  keep_a/keep_b/correct_a/correct_b decision needs to name a winner and
  the two sides carry different `source:` shorthand types.
- **Resolution rule (unchanged for existing tiers):** the taxonomy is
  ordered highest-to-lowest; ties break by newer source date. The tiers
  are the SourceRef `<type>:<ref>` shorthand's type component (`user:`,
  `linkedin:`, `api:`, `wikipedia:`, `agent-observed:`, `claude:`,
  `script:`, `model-prior:`, `unsourced`).
- **Change (#326):** issue #326 introduced a NEW tier `model-prior:<model-id>`
  ranked BELOW `script:<slug>`. Rationale is locked in
  `docs/provenance-shape.md` §10.1 — a training prior is unverifiable and
  silently stale past the model cutoff, while a pipeline slug at least names
  a repeatable in-tree process.
- **Change (#328):** issue #328 inserts a NEW tier
  `agent-observed:<model>:<session-ref>` at **rank 5** — BELOW
  `wikipedia:<page>` (it is not a curated authority) and ABOVE
  `claude:tier3`/inferred (it is grounded in a real in-session artifact the
  agent READ — file contents or tool output — verifiable against the
  transcript, not an unsupported leap). This shifts `claude:`→6, `script:`→7,
  `model-prior:`→8, `unsourced`→9. The tier is written by the
  `repair --backfill-sources` pass (issue #328) when it re-classifies a
  memory whose source was DEFAULTED to `claude:inferred` and finds the claim
  in a tool-result block. This is a §10.1 LOCK-DISCIPLINE change — the
  taxonomy in `resolutions.py` (`_RESOLVE_SYSTEM`), the
  `tests/data/resolve_system.txt` snapshot, and the Section 11 test class in
  `tests/test_conflict_resolution.py` are updated together.
- **Cross-reference:** the channel-split source_type vocabulary (with
  the parallel `agent-observed` and `model-prior` claim-level values)
  is locked in `docs/provenance-shape.md` §10; the two docs cross-link
  so a future change to either the vocabulary OR the precedence rank
  updates both plus `tests/test_conflict_resolution.py` in the same
  change (Section 11 test class).
- **Provenance behavior:** the taxonomy is enforced at PROMPT time only
  — no deterministic winner-picker runs in-process. The LLM's returned
  `source_precedence_used` field records the comparison it used.

---

## 12. Scoped claims — three-way overlap verdict + scope-edit actions (issue #329)

- **Files:** `src/athenaeum/scoped_claims.py` (poset model + verdict);
  `src/athenaeum/resolutions.py` (`_scope_verdict_proposal` short-circuit,
  `scope_a` / `scope_b` actions, `_narrow_scope_interval` enactment);
  `src/athenaeum/contradictions.py` (`_member_scope_header` org/locale segment).
- **Companion tests:** `tests/test_scoped_claims.py` (poset, verdict, resolver
  short-circuit, enactment) plus the `ENACTING_ACTIONS` lock in
  `tests/test_enact_resolution.py`.
- **Model.** #308 gave claims a TIME dimension; #329 generalizes to a small
  **product poset** over `{org, locale, time}`. Each dimension has TOP =
  *unscoped*; org/locale are **trees** (`kromatic/platform ⊑ kromatic`;
  `en-US ⊑ en`), time is **intervals under inclusion**. The org/locale tree is a
  **versioned config** (`scope.org` / `scope.locale` node lists in
  `athenaeum.yaml`); a value **not** in the tree normalizes to *unscoped* and
  logs a breadcrumb — authors may not mint scope values (the Cyc-microtheory
  lesson), and the fail-open direction is toward detection.
- **Three-way verdict** (`scope_comparison`, replaces binary conflict/no-conflict):
  - **DISJOINT** — the componentwise meet is empty in some dimension (incomparable
    org/locale subtrees, or disjoint time windows). The contexts never co-apply
    → `not_a_conflict`, confidence 1.0, **no Opus call**. Generalizes Section 9's
    disjoint-time pre-filter.
  - **OVERRIDE** — one context is strictly below the other in the **org/locale
    trees**: the specific claim is an exception carving out its region; the
    general claim governs the remainder. Both stay active → `not_a_conflict`,
    confidence 1.0 (defeasible specificity — the false positive that every
    org-rule/team-exception pair would otherwise become).
  - **OVERLAP** — same-context or incomparable-but-overlapping → the scope path
    returns `None` and the pair falls through to the declared / LLM resolver as a
    genuine contradiction.
- **Wiring.** `propose_resolution` calls `_scope_verdict_proposal` right after the
  #324 disjoint-time check and before the declared-relationship + LLM paths. On a
  fresh install with **no `scope:` config** the org/locale coordinates normalize
  to unscoped, so the verdict reduces to time-only and returns `None` for anything
  Section 9 did not already catch — **no default-behavior change**.
- **Scope-edit actions `scope_a` / `scope_b`.** NARROW the named side's scope until
  the meet is empty, converting an apparent contradiction into two durably-true
  **scoped** claims that never re-enter detection. BOTH members stay **active** —
  unlike `keep_*` (retire loser), `correct_*` / `forget_*` (delete). Enactment
  (`enact_resolution`) narrows the **TIME** dimension: closes the named side's
  `valid_until` to the day BEFORE the other side's `valid_from` (strict-`<`
  disjoint, minus-one-day so both stay live — contrast Section 10's boundary-day
  share, which relies on `superseded_by` for inactivity). Only-close-never-widen.
  Added to `ENACTING_ACTIONS` + `flip_action`; auto-apply threshold 0.90.
- **Deliberately deferred (design-only, to the #329 ADR):**
  - **Time-interval NESTING does not trigger OVERRIDE** — only org/locale
    tree-specificity does. Section 9/#308 shipped a semantic where nested-but-
    overlapping time windows still reach the resolver; auto-promoting a
    sub-interval to a silent override would change that, so it is left to the ADR.
  - **Org/locale coordinate PINNING enactment** — `scope_*` narrows only the time
    dimension. Pinning an org/locale coordinate as the narrowing edit needs the
    caller-context write path; when the pair has no time boundary, `scope_*`
    no-ops (escalates to the human) rather than guessing a coordinate.
  - **Recall `serve --scope` caller-context filter/ranking** and the broader
    team/multi-tenant scope-IDENTITY system (#314) are out of scope.

---

## 13. Opinion attribution — evaluative claims are never resolved by precedence (issue #327)

- **Files:** `src/athenaeum/models.py` (`CLAIM_KINDS`, `parse_claim_kind`,
  `compare_asserters`, `AutoMemoryFile.claim_kind`); `src/athenaeum/claim_kind.py`
  (intake-time classifier `classify_claim_kind` / `stamp_claim_kind`);
  `src/athenaeum/resolutions.py` (`_stance_attribution_verdict` short-circuit,
  `attribute_both` action, `enact_resolution` attribution stamp);
  `src/athenaeum/contradictions.py` (`stance` conflict type);
  `src/athenaeum/merge.py` (`_emit_escalation` attribute_both drop path).
- **Companion tests:** `tests/test_claim_kind.py` (classify + tier0 round-trip +
  fail-open), `tests/test_models.py::TestCompareAsserters`,
  `tests/test_resolutions.py::TestOpinionAttribution`,
  `tests/test_conflict_resolution.py::TestOpinionAttributionLock`,
  `tests/test_librarian_merge.py::TestOpinionAttributionMerge`, plus the
  `ENACTING_ACTIONS` lock in `tests/test_enact_resolution.py`.
- **`claim_kind` (epistemic shape).** Orthogonal to `source_type` (origin
  channel). One of `fact | observation | opinion | decision | policy |
  definition`, classified ONCE at intake by a cheap LLM pass (tier2-style,
  routed through the `models.classify` knob), stored in frontmatter, and
  round-tripped byte-for-byte by tier0 passthrough. **Absent → unclassified
  (`""`), fail-open** — the stance short-circuit does not fire and the pair
  resolves exactly as pre-#327.
- **Asserter comparison.** `compare_asserters(a, b)` reuses the OIDC-durable
  `asserter_identity_key` (§10 of `provenance-shape.md`) and returns
  **`same` / `different` / `unknown`**. `unknown` when EITHER side has no
  durable identity — the common case, since a Claude session carries no OIDC
  identity. Identity is CAPTURED when a caller/`remember()` supplies an
  `_asserter` block (issue #326); it is NEVER fabricated from a transcript.
- **`_stance_attribution_verdict` short-circuit** (deterministic, **no Opus
  call**). Engages when BOTH sides carry `claim_kind: opinion`, OR the detector
  routed the pair as `conflict_type: stance` AND neither side is EXPLICITLY a
  non-opinion kind. Then, by asserter comparison:
  - **different** (both known, distinct) → `attribute_both`, confidence 1.0.
    Both stay active with explicit attribution.
  - **unknown** (missing identity on either side) → `attribute_both`,
    confidence 1.0. **REQUIRED fallback** — an opinion is NEVER superseded or
    deleted by precedence when identity is missing.
  - **same** + a distinguishing date → supersession (`keep_a`/`keep_b`, keep the
    newer), as with any dated same-author update.
  - **same** + undated → `attribute_both` (cannot order; keep both).
  If EITHER side is explicitly `fact`/`decision`/`policy`/`observation`/
  `definition`, the guard returns `None` and the normal precedence path runs —
  an opinion-vs-fact pair is not an attribution case.
- **Wiring.** `propose_resolution` calls `_stance_attribution_verdict` right
  after the declared-relationship check (an explicit `supersedes` still wins)
  and before the LLM path. `merge._emit_escalation` treats `attribute_both`
  like the suppress/refines short-circuit: it enacts the non-destructive
  attribution stamp and **drops** the pending-question escalation, so the pair
  **never re-queues** to the human. Both members stay **active** (no
  `superseded_by`/`deprecated`; each keeps its own `asserter:` block). A
  re-detected pair hits the deterministic guard again next run (cheap, no Opus
  call) and is dropped identically.
- **`attribute_both` action.** Added to `ENACTING_ACTIONS`; enactment stamps
  `attributed: true` on BOTH members (non-destructive — nothing deleted or
  retired). Orientation-AGNOSTIC (symmetric), so `flip_action` returns `None`
  (applied unchanged, like `deprecate_both`). Auto-apply threshold 0.90 (the
  deterministic guard emits confidence 1.0; the threshold matters only for an
  LLM-returned `attribute_both`).

---

## Comparison matrix — who wins on each field type

| Resolver | Scalar (truthy/either) | Scalar (always) | List | Numeric | Date | Body | Provenance (`field_sources`) |
|----------|------------------------|------------------|------|---------|------|------|------------------------------|
| `tier0_passthrough` | n/a (skip-on-conflict) | incoming verbatim | incoming verbatim | incoming verbatim | `created`: keep if present; `updated`: today | incoming verbatim | passthrough |
| `tier3_create` | n/a (no existing) | LLM | LLM | LLM | `created`/`updated`: today | LLM | not written |
| `tier3_merge` | LLM-decided per the prompt's three-class taxonomy | LLM | LLM | LLM | `updated`: today | LLM | unchanged |
| `tier3_write` | (delegates to merge/create) | (delegates) | (delegates) | (delegates) | `updated`: today | (delegates) | unchanged |
| `merge.py` cluster merge | n/a | n/a | sources: `(session,turn)`-dedupe; origin_scopes: union | n/a | n/a | concat with paragraph-dedupe | n/a |
| `contradictions.py` | DETECT-ONLY — never resolves (disjoint-validity pairs skip the LLM entirely, §9) | — | — | — | — | — | — |
| `dedupe._perform_merge` | canonical wins if truthy | canonical wins | union (canonical first) | max | max (lex ISO) | canonical + appended absorbed | canonical wins per key; absorbed-only keys carried forward |

**Lock semantics:** every cell above MUST stay accurate. A future PR that
changes any cell must update both this matrix AND the corresponding
`tests/test_conflict_resolution.py` test in the same change.

---

## Coverage notes

`tests/test_conflict_resolution.py` exercises every documented rule above. The
combined whole-module line coverage on the in-tree target files
(`merge.py`, `contradictions.py`, `dedupe.py`, `librarian.py`, `tiers.py` —
`tier3_*` and `tier0_passthrough` are within
`tiers.py`/`librarian.py`) is **~48% line coverage** when measured against the
whole modules. This is below the issue's 80% target, but the residual lines
are NOT in the resolvers themselves — they live in:

- `librarian.py`: `discover_*` / CLI orchestration (~80% of the file is
  pipeline plumbing outside `tier0_passthrough`).
- `tiers.py`: `tier1_programmatic_match` (covered separately by
  `test_tiers.py`) and `tier2_classify` (covered by `test_tiers.py`).
- `dedupe.py`: name normalization, wiki loading, YAML round-trip — covered
  by integration tests in `test_dedupe.py`.
- `merge.py`: top-level `merge_clusters_to_wiki` orchestration — covered by
  `test_librarian_merge.py`.
- `contradictions.py`: 80% reached by this suite; remaining lines are
  malformed-response error paths covered by `test_librarian_merge.py`.

The resolver functions themselves (`tier0_passthrough`, `tier3_create`,
`tier3_merge`, `tier3_write`, `merge_cluster_row`, `_perform_merge`,
`_merge_meta`, `_merge_field_sources`, `dedupe_sources`, `synthesize_body`,
`detect_contradictions`) all have at least one passing test
asserting their documented rule.

When future PRs change resolver behavior, the rule of thumb is: every NEW
or CHANGED rule needs a test in `test_conflict_resolution.py`. Total
module-coverage targets are tracked in the wider `tests/` suite, not this
lock document.
