# Conflict Resolution — Audit and Lock (issue #91)

This document is the LOCK for athenaeum's conflict-resolution behavior as of
merge SHA `b0ee25c` on `develop` (post-Lane-E). It catalogs every code path
that resolves disagreements between two pieces of data — between raw intake
and the wiki, between two cluster members, between a canonical wiki and an
absorbed duplicate, between an Apollo response and an existing wiki record —
and pins the exact rule each one applies.

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

## 8. `connectors/apollo.py:enrich_person` + CLI `enrich --persons` write path

### 8a. `enrich_person`

- **File:** [`src/athenaeum/connectors/apollo.py` lines 399–471](https://github.com/Kromatic-Innovation/athenaeum/blob/b0ee25c/src/athenaeum/connectors/apollo.py#L399-L471)
- **Trigger:** Per person wiki — caller passes the existing meta dict + an
  `ApolloClient`. Pure aside from the Apollo API call.
- **Resolution rule (per field):**

| Field | Rule |
|-------|------|
| `apollo_id`, `current_title`, `current_company`, `apollo_headline`, `apollo_location`, `apollo_employment_history`, `apollo_enriched_on` | **Apollo wins (always emitted)** when truthy in the response. Falsy values (`None`/`""`/`[]`/`{}`) are skipped via `set_field`. |
| `linkedin_url`, `twitter_url`, `github_url` | **Existing wins.** Apollo's value is emitted ONLY if the existing meta's value is empty/missing. Operator-curated curated URLs are protected. |

- **Provenance behavior (post-#90):** Every field set in `fields` gets a paired
  entry in `field_sources` with value `api:apollo:<YYYY-MM-DD>` (UTC date). The
  `field_sources` map is returned to the caller for merging — `enrich_person`
  itself does not touch the wiki.
- **Known edge cases:**
  - `today=None` defaults to UTC today via `datetime.now(tz=timezone.utc)`.
  - When Apollo returns no match, `EnrichResult(matched=False)` with empty
    fields/field_sources.

### 8b. CLI `enrich --persons` write path

- **File:** [`src/athenaeum/cli.py` lines 1219–1226](https://github.com/Kromatic-Innovation/athenaeum/blob/b0ee25c/src/athenaeum/cli.py#L1219-L1226)
- **Trigger:** `athenaeum enrich --persons --apply` per matched candidate.
- **Resolution rule:**
  - **Scalar fields:** `new_meta = dict(meta); new_meta.update(result.fields)` —
    Apollo's `fields` overwrite the existing wiki's values for any key Apollo
    returned. (Recall `enrich_person` already filtered linkedin/twitter/github
    to only emit when missing on input — so those three are NOT clobbered. The
    Apollo-namespace and `current_title`/`current_company` fields ARE clobbered
    on every successful match.)
  - **`field_sources`:** `existing_fs = dict(meta.get("field_sources") or {});
    existing_fs.update(result.field_sources)` — Apollo's per-key provenance
    overwrites the existing wiki's per-key provenance for any key Apollo touched.
    This is correct *iff* Apollo's value also overwrote the field; for
    linkedin/twitter/github (which Apollo skipped writing to `fields`), the
    `field_sources` entry is also absent so existing provenance survives.
- **Provenance behavior (post-#90):** Records `api:apollo:<date>` for every
  Apollo-touched field. Pre-existing non-Apollo provenance for unrelated keys
  survives untouched. The case where the existing wiki already carried an
  `api:apollo:<earlier-date>` for a field — and a re-enrich produces a newer
  date — results in the newer date overwriting (this is desired: it tracks
  freshness).
- **Known edge cases:**
  - There is no "skip if Apollo's value matches existing" guard — even when
    Apollo's `current_title` equals the wiki's existing `current_title`, the
    write still updates `apollo_enriched_on` and bumps `field_sources`. **Bug
    finding filed as separate issue** — see PR body.
  - The `--skip-recent` CLI flag (default 30 days) gates the candidate set
    BEFORE this write path; it is an upstream filter, not a conflict rule.

---

## Comparison matrix — who wins on each field type

| Resolver | Scalar (truthy/either) | Scalar (always) | List | Numeric | Date | Body | Provenance (`field_sources`) |
|----------|------------------------|------------------|------|---------|------|------|------------------------------|
| `tier0_passthrough` | n/a (skip-on-conflict) | incoming verbatim | incoming verbatim | incoming verbatim | `created`: keep if present; `updated`: today | incoming verbatim | passthrough |
| `tier3_create` | n/a (no existing) | LLM | LLM | LLM | `created`/`updated`: today | LLM | not written |
| `tier3_merge` | LLM-decided per the prompt's three-class taxonomy | LLM | LLM | LLM | `updated`: today | LLM | unchanged |
| `tier3_write` | (delegates to merge/create) | (delegates) | (delegates) | (delegates) | `updated`: today | (delegates) | unchanged |
| `merge.py` cluster merge | n/a | n/a | sources: `(session,turn)`-dedupe; origin_scopes: union | n/a | n/a | concat with paragraph-dedupe | n/a |
| `contradictions.py` | DETECT-ONLY — never resolves | — | — | — | — | — | — |
| `dedupe._perform_merge` | canonical wins if truthy | canonical wins | union (canonical first) | max | max (lex ISO) | canonical + appended absorbed | canonical wins per key; absorbed-only keys carried forward |
| `enrich_person` | existing wins for linkedin/twitter/github; Apollo wins for all other Apollo fields | Apollo when matched | Apollo (employment history) | n/a | `apollo_enriched_on`: today | n/a | Apollo emits `api:apollo:<date>` per key |
| CLI `enrich --persons` write | — | dict-update: Apollo wins for any key it returned | (none — Apollo doesn't return list-merge keys) | — | — | — | dict-update: Apollo wins for any key it returned |

**Lock semantics:** every cell above MUST stay accurate. A future PR that
changes any cell must update both this matrix AND the corresponding
`tests/test_conflict_resolution.py` test in the same change.

---

## Coverage notes

`tests/test_conflict_resolution.py` exercises every documented rule above. The
combined whole-module line coverage on the eight target files
(`merge.py`, `contradictions.py`, `dedupe.py`, `librarian.py`, `tiers.py`,
`connectors/apollo.py` — `tier3_*` and `tier0_passthrough` are within
`tiers.py`/`librarian.py`) is **~48% line coverage** when measured against the
whole modules. This is below the issue's 80% target, but the residual lines
are NOT in the resolvers themselves — they live in:

- `librarian.py`: `discover_*` / CLI orchestration (~80% of the file is
  pipeline plumbing outside `tier0_passthrough`).
- `tiers.py`: `tier1_programmatic_match` (covered separately by
  `test_tiers.py`) and `tier2_classify` (covered by `test_tiers.py`).
- `connectors/apollo.py`: HTTP transport (`ApolloClient.people_match`,
  `_request`, retries) — covered by `test_apollo_connector.py`.
- `dedupe.py`: name normalization, wiki loading, YAML round-trip — covered
  by integration tests in `test_dedupe.py`.
- `merge.py`: top-level `merge_clusters_to_wiki` orchestration — covered by
  `test_librarian_merge.py`.
- `contradictions.py`: 80% reached by this suite; remaining lines are
  malformed-response error paths covered by `test_librarian_merge.py`.

The resolver functions themselves (`tier0_passthrough`, `tier3_create`,
`tier3_merge`, `tier3_write`, `merge_cluster_row`, `_perform_merge`,
`_merge_meta`, `_merge_field_sources`, `dedupe_sources`, `synthesize_body`,
`detect_contradictions`, `enrich_person`) all have at least one passing test
asserting their documented rule.

When future PRs change resolver behavior, the rule of thumb is: every NEW
or CHANGED rule needs a test in `test_conflict_resolution.py`. Total
module-coverage targets are tracked in the wider `tests/` suite, not this
lock document.
