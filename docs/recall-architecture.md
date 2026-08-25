# Recall architecture

How the UserPromptSubmit hook surfaces wiki context — the hybrid FTS5 + vector pipeline, the optional LLM query-topic preprocessor, and the load-bearing invariants that a future "simplification" must not remove.

## Pipeline

```
┌─────────────────┐    ┌──────────────────┐    ┌────────────┐    ┌───────────┐
│ UserPromptSubmit│ -> │ query-topics     │ -> │ FTS5 + vec │ -> │ injected  │
│  (raw prompt)   │    │ (Haiku, optional)│    │ hybrid     │    │ context   │
└─────────────────┘    └──────────────────┘    └────────────┘    └───────────┘
                              │
                              ▼ (any failure)
                       regex + stopword
                       fallback extractor
```

1. **Source config.** `~/.cache/athenaeum/config.env` is sourced under `set -a` / `set +a` so that `ANTHROPIC_API_KEY` propagates to child processes. Without `set -a`, the LLM topic extractor silently runs without its key.

2. **Query-topic extraction (optional, LLM).** `athenaeum query-topics "$PROMPT" --timeout 3` calls a cheap Haiku classifier that returns a JSON array of substantive topics, ignoring meta-instructions like "quote verbatim" or "don't call tools". On any failure (missing CLI, missing API key, timeout, bad JSON), the hook falls back silently to a regex + stopword extractor.

3. **Hybrid search.**
   - **FTS5** (`wiki-index.db`) — lowercased-and-phrase-quoted OR query, top 3 by BM25 rank, excluding session-seen filenames.
   - **Vector** (`wiki-vectors/`, runs when `SEARCH_BACKEND=vector`) — embeds the *concatenated topics* (not the raw prompt; meta-instructions drift the embedding), queries chromadb, returns top 3.
   - **Merge** — FTS5 first, then vector, dedupe by filename, cap at 3.

4. **Session dedup.** `/tmp/knowledge-seen-${SESSION_ID}` accumulates already-surfaced filenames across turns.

5. **Emit.** `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}`. A flat `{"additionalContext":...}` payload is silently ignored by Claude Code.

## Why hybrid — and why both layers are load-bearing

**FTS5 alone** is fragile on synonym or paraphrase queries. Asking about "iterative feedback loops" won't match the wiki page titled "Innovation Accounting" because there's no lexical overlap.

**Vector alone** is fragile on short proper-noun queries. Concrete failure:

> Query: `"Return Path"`
> Nearest vector neighbour: `reference_local_paths.md`
> Distance from the actual entity page: larger than the collision

Short strings are dominated by their common-word components. Out-of-the-box sentence embeddings place "Return Path" closer to `local paths` (sharing "path") than to the sparse entity page for the company "Return Path". FTS5 phrase match on `"return path"` resolves this trivially — no embedding can out-match a literal phrase hit.

**Conclusion:** the hybrid merge is not defence-in-depth. **Each backend rescues a class of queries the other handles poorly.** Removing either collapses recall on its rescue class. When `SEARCH_BACKEND=vector`, the example SessionStart hook still builds FTS5 as a secondary index for the same reason — FTS5 rebuild is ~1s on a 3k-page wiki, cheap next to vector's ~45s.

## Why the LLM preprocessor exists

The regex+stopword fallback extractor sorts words alphabetically and takes the first 8. On a meta-heavy prompt like

> *"Without calling any tools, quote the block about Return Path verbatim."*

the word `return` lands 10th alphabetically and gets dropped. Vector embedding of the raw prompt drifts toward *"without tools / quote / verbatim"* — hook/tooling pages, not entities.

The LLM preprocessor returns `["Return Path"]`, ignoring the meta-wrapper, and both backends then land correctly. It's a cheap Haiku call (~200ms) with a 3s timeout and silent fallback.

## ANTHROPIC_API_KEY bootstrap (SessionStart)

Claude Code authenticates with `CLAUDE_CODE_OAUTH_TOKEN` (starts `sk-ant-o`), scoped to its inference endpoint. Passing that token to the general Anthropic Messages API returns:

```
401 OAuth authentication is currently not supported
```

So the LLM preprocessor needs a real console API key, which it reads from `ANTHROPIC_API_KEY`. The reference `session-start-recall.sh` fetches it from 1Password when:

- `ANTHROPIC_API_KEY` isn't already exported, AND
- the `op` CLI is signed in

```bash
op read "op://Agent Tools/Anthropic API Key/credential"
```

Override path via `ATHENAEUM_OP_KEY_PATH`. The fetched key is cached in `~/.cache/athenaeum/config.env` with `0600` perms. Every failure mode is silent — the recall hook then degrades to the regex fallback.

## Failure modes and diagnostics

| Symptom | First check |
|---|---|
| Recall misses proper noun in meta-heavy prompt | `grep ANTHROPIC_API_KEY ~/.cache/athenaeum/config.env` — if missing, `op whoami` |
| Hook runs but model never references the injection | Shape mismatch — output must be `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",...}}`, not flat `{"additionalContext":...}` |
| Short entity name returns unrelated page | Vector collision — verify the FTS5 db exists and is being merged first |
| LLM extraction returns `[]` every time | `set -a` / `set +a` missing around `source config.env` — var not exported to child process |
| `athenaeum query-topics` hangs | 3s timeout should kick in; if not, check `ATHENAEUM_PYTHON` points to an env with the athenaeum CLI |

## Recall hit header (provenance/context) — athenaeum#325

Each `recall` hit renders a compact metadata header so a consuming agent can
judge a fact's **trust and currency without opening the page**. The header is
built at render time from the hit's FRESH on-disk frontmatter (the same `fm`
dict the athenaeum#312 Layer-C audience re-check already reads — no index change, no
reindex), and sits between the `**Tags:**` line and the snippet:

```
Deploy target (score: 12.3)
**Path:** wiki/deploy-target.md
**Source:** user-stated (2026-04-10) · **Updated:** 2026-06-30 · **Valid:** 2026-04-01 → open
**Status:** contradiction-flagged (see _pending_questions.md)

<snippet>
```

Fields:

| Field | Source frontmatter | Rendered when |
|---|---|---|
| `**Source:**` | `source_type` + the date part of `source_ref` (falling back to `created`) | `source_type` is a non-default, in-vocabulary origin (`user-stated` / `external` / `document`). The default `inferred` — and an absent/typo'd value — render nothing. |
| `**Updated:**` | `updated` (date part) | `updated` is present. |
| `**Valid:**` | `valid_from` → `valid_until` (via the shared `validity_bound_str` renderer; `open` for a missing bound) | EITHER `valid_from` or `valid_until` is present. |
| `**Status:**` | `status: contradiction-flagged` OR `contradictions_detected: true` | the page is contradiction-flagged. Points the reader at `_pending_questions.md`. |

`Source:`/`Updated:`/`Valid:` share ONE `·`-joined line; `Status:` is a second
line. Header is capped at ~2 lines per hit.

**Omit-at-default rule.** Every field is omitted at its default, so an
uncontested, unscoped page adds at most one extra line (usually just
`**Updated:**`). A page with NONE of source/updated/valid/status renders
exactly the pre-athenaeum#325 output — `**Tags:**`, blank line, snippet — with no blank
metadata line. There is never an empty `**Source:**`/`**Valid:**` segment.

**Why the `Status:` line is load-bearing.** Silently returning one side of a
contradiction-flagged pair — with no signal that the fact is disputed — is the
exact failure this header prevents. The `Status:` line is the reader's cue to
consult the pending-question queue before trusting the snippet.

## Currency-aware ranking and decay buckets (athenaeum#904)

A page may declare `bucket: daily | weekly | durable` (optionally alongside
a suggested `valid_until`) — see `docs/provenance-shape.md` §8.8 for the
full frontmatter contract. Recall uses it for exactly one thing: an EXPIRED
`bucket: daily` page is reordered to the bottom of the result set it would
already have occupied, rather than ranking equally alongside current
facts — `mcp_server._reorder_hits_by_currency` runs a stable partition over
the hits a backend already selected (never widening the candidate pool,
never dropping a hit). `weekly` / `durable` / unbucketed pages are
untouched, so a corpus with no `bucket:` anywhere sees byte-identical
output to before this feature existed.

Pass `recall(..., history=True)` (or the MCP tool's `history` parameter) to
skip the reorder for one call and get plain relevance order — the
conservative, explicit opt-in for "I am asking about the past, not the
current state." No query-text keyword inference, no LLM intent
classification: both would risk misfiring in either direction, and the
issue's own scope forbids an LLM anywhere near the expiry decision.

An expired `bucket: daily` page stays *findable* in ordinary (non-history)
recall too — it does not disappear, it just ranks lower. This required a
small divergence from the pre-existing "expired claims are hard-filtered by
default" behavior (`docs/provenance-shape.md` §8.3):
`athenaeum.search._is_recall_inactive` is a recall-scoped sibling of
`athenaeum.models.is_inactive_memory` that exempts an expired
`bucket: daily` page from that hard filter (every other case — a
superseded/deprecated page, an expired non-daily page — behaves exactly as
before). The C3 merge-compile's own member-activeness check is deliberately
untouched by this: an expired daily-bucket raw member still stops
contributing to the compiled page, which is what makes a rapidly-rewritten
status page collapse to its latest value instead of accumulating stale
content.

The wiki itself never grows an in-tree "archived" marker page for this —
`athenaeum decay-sweep` (`athenaeum.decay_sweep`) periodically `git rm`s an
expired `bucket: daily` page from the live tree (never `weekly`/`durable`/
unbucketed), leaving it recoverable from git history. Dry-run by default,
same shape as `athenaeum auto-memory prune`. Every archived page also gets a
durable sweep-ledger record (`_decay_sweep_records.jsonl`, cache dir, never
the wiki corpus) — which page, why, when, and the recovering commit SHA —
and the sweep refuses to archive at all if that ledger write fails (issue
athenaeum#969).

**The expired-`daily` exemption is the SOLE exemption from the fail-closed
expiry filter (issue athenaeum#969).** `_is_recall_inactive`'s divergence
from `is_inactive_memory`, above, is scoped to exactly one condition —
`bucket: daily` AND expired — and nothing else. Every other case the §8.3
"currently-valid-by-default" filter governs (`docs/provenance-shape.md`
§8.3) — a superseded/deprecated page, an expired `weekly`/`durable`/
unbucketed page — stays hard-excluded exactly as before. There is no second
carve-out anywhere in the recall path; a future one needs its own issue and
its own review of this invariant, not a quiet extension of this one.

**Swept ≠ cold.** Do not conflate this section's sweep with the memory
model's planned cold tier (`embedded: false`, the storage-adapter
`corpus_policy` bit — `docs/whole-store-adapter-design.md` §8 "Surface 1").
A COLD page stays on disk in the live wiki tree: it is excluded from the
FTS5/vector index, but it remains reachable through `KeywordBackend`'s
full-corpus scan (`cheap_local_scan` — a deep-recall path that does not
depend on the index). A SWEPT page has been `git rm`'d out of the live tree
entirely (above): it has no recall path at all, indexed or not — the only
way back is `git show`/`git log` against the recovering commit the sweep
ledger records, never a `recall()` call of any kind.

## Handle-shaped queries — exact reverse lookup, not similarity search (athenaeum#907)

A query that is **handle-shaped** — a bare address (`alex@example.org`), or a
registry handle framed as a question ("who owns kromatic.example?", "is this
address still current?") — never reaches the FTS5/vector/keyword pipeline
above. `athenaeum.identity_resolution.resolve_handle_query` answers it by
exact reverse lookup instead, and both `recall` entry points
(`mcp_server.recall_search`, `athenaeum recall` / `_cmd_query.cmd_recall`)
call that one resolver rather than each re-deriving it.

Detection is deliberately conservative: exactly one email-shaped token in the
query, or the whole query (framing stripped) exactly matching an existing
`registry.json` handle value. Anything else is not handle-shaped and falls
through to the pipeline above completely unchanged — same output as if this
feature did not exist.

The response is a JSON document (`json.dumps(..., indent=2, sort_keys=True)`,
nothing else) carrying the person's `uid`, `display_name`, `entity_class`, and
per-value fact fields — usage/provenance classification, bounce history,
validity dates. Excluded values are gated by `with_pii` exactly as they are
elsewhere in `recall`, and the excluded-surface lookup runs strictly after the
same audience and `recallable` drops documented below.

**Facts only, never an eligibility predicate.** This is a boundary, not an
implementation detail: the response never carries `outreach_eligible` or
anything shaped like "may this address be used to contact them" — that
decision is the caller's own policy, applied over the facts this module
returns. Access control (who may set `with_pii`, or call `recall` at all) is
a separate, deferred question (athenaeum#864); this module implements
neither.

## Type filter, config-derived tool schema, and entity-class discovery (athenaeum#964)

`recall` (the MCP tool and the `athenaeum recall` CLI) accepts an optional
**`type`** filter that narrows the search to one or more entity classes (a
page's `type:`). It is the alternative to a per-kind API (a `people`
endpoint, a `companies` endpoint, ...) — one generalizable argument instead
of a proliferating set of typed interfaces, matching the ratified
`athenaeum-one-ladder-no-typed-interfaces` position already applied to the
resolve/read half of the interface (athenaeum#883/#885/#886).

**Contract:**

- Omitting `type` (the default, `None`) searches every class — byte-identical
  to the pre-athenaeum#964 behavior.
- The value is an **opaque, operator-defined string** — it is NEVER validated
  against `wiki/_schema/types.md`. A corpus can carry a class that isn't
  declared there at all (`auto-memory` is exactly such a class on the live
  corpus, per the issue's own evidence), and a filter on it still works.
- An unrecognized value does NOT error and does NOT read as a silent "nothing
  matched": the response names the deployment's actual entity classes
  alongside the empty result, so a typo is always diagnosable from the
  response itself.
- The predicate is pushed **INSIDE** every backend's query — before
  ranking/top-k is selected, never post-filtered on the result list — the
  same rule athenaeum#312's `caller_audience` predicate already follows. A backend
  that only post-filtered would silently return too few (or wrongly-ranked)
  results once a filtered class fell outside the unfiltered top-k.

**Per-backend implementation:**

| Backend | Storage | Predicate |
|---|---|---|
| `keyword` | none (scans frontmatter live) | checked before scoring, so a non-matching page never enters the candidate list |
| `fts5` | a `type UNINDEXED` column (same shape as the existing `audience` column) | `AND type IN (...)` in the `WHERE` clause, before `ORDER BY rank LIMIT` |
| `vector` | a `type` key in chromadb metadata | a native `where=` clause, composed with the existing `filename` exclusion via `$and` so a call passing both honors both |

**Frontmatter precedence.** `type` appears both top-level (`type: person`,
the documented shape) and, on some pages, nested under `metadata:`
(`metadata: {type: person}`). `athenaeum.models.resolve_page_type` is the
ONE place this precedence is decided — top-level wins, `metadata.type` is
the fallback — and every `type`-column/metadata writer (FTS5's `_row_for`,
the vector backend's `_add_records`) and the entity-class resolver all call
it, so a page authored either way is found identically by every backend.

**Index rebuild.** Because the athenaeum#370 stat pre-filter skips re-reading an
unchanged file's frontmatter, adding a filterable field to the metadata
contract does not "just work" on the next ordinary incremental build — an
untouched page would silently keep serving its old (type-less) metadata
forever. Both indexed backends carry a metadata-schema version stamp checked
before every incremental build: FTS5 reuses its existing
`_SCHEMA_VERSION`/`PRAGMA user_version` mechanism (bumped 2 → 3); the vector
backend gained an equivalent `metadata_schema_version` manifest key (new at
2). A mismatch — including a pre-athenaeum#964 manifest carrying no such key at
all — forces a FULL rebuild rather than being incrementally reused, so every
page picks up the new column/key exactly once. The operator's own ratified
direction for this migration: *"I genuinely don't care if we have to
rebuild ... plan for the long term and generalized use case"* — a full
rebuild is CPU-only (the embedding model runs locally, no API spend) and is
the sanctioned migration path, not a fallback.

**Full-rebuild wall time — PENDING host-side measurement.** The issue asks
for a measured (not predicted) full-rebuild wall time on the live corpus
(22,797 wiki pages at the time the issue was filed) on the vector backend.
This PR was built in a sandboxed lane with no access to the operator's real
`~/knowledge` corpus and no network egress to chromadb's embedding-model
download endpoint, so that measurement could not be produced here — a
fabricated number would be worse than an honest gap. Run once, post-merge,
against the live corpus:

```
time athenaeum reindex --full --backend vector
```

and replace this paragraph with the observed wall time.

**Related-record identity.** Each recall hit now always renders `**Uid:**`
and `**Type:**` (unlike the omit-at-default athenaeum#325 header — "go dig
further" is the point, so these are never hidden), plus a `**Links:**` line
listing the page's outbound `[[wikilink]]` targets when it has any (omitted
entirely when it has none). This closes the identity gap the issue's own
evidence named: previously the only way to reach `read_entity` from a hit was
to string-parse the `<uid>-<slug>.md` filename. Inbound backlinks are
explicitly OUT of scope — serving them needs a new index this issue does not
build; see the issue's own "Out of scope" section.

**Entity-class discovery — the `entity_schema` MCP tool.** The ONE new MCP
tool this issue adds (deliberately the only addition — "the only other
endpoint or MCP tooling we should be adding is schema queries," per the
operator's ratified direction). Call it before narrowing with `type=...` when
the deployment's classes aren't already known. It reports, per class:

- `count` — live pages this caller may read (fail-closed audience-scoped,
  same predicate as `recall` itself).
- `declared` — present in `wiki/_schema/types.md`.
- `observed` — at least one live page carries this class.
- `fields` — the union of frontmatter KEYS its pages carry. Keys only, never
  values, and any key routed to an excluded surface (e.g. inline `emails`)
  is omitted entirely rather than listed — the tool can never become a
  "which PII fields exist" oracle.

`declared` and `observed` are reported independently rather than reconciled:
the two CAN legitimately drift (an operator adds a class to the corpus before
updating `types.md`, or the reverse), and this tool's job is to make that
drift visible at the protocol level, not to paper over it — reconciling
`types.md` itself is a separate, out-of-scope corpus-repair job.

`queryable_fields` reports exactly the fields `recall`'s filter arguments
implement today — `["type"]`. It must never advertise a field no filter
actually honors.

**Config-derived tool schema, computed once.** `recall`'s `type` parameter
description — and `entity_schema`'s whole answer — are computed from the SAME
resolver (`athenaeum.entity_schema.resolve_entity_classes`) at
`create_server()` time, from THIS deployment's own `wiki/_schema/types.md`
and corpus, not from a hardcoded literal in source. A deployment with no (or
an empty) `types.md` degrades to the observed classes / the collapsed
fallback set (`athenaeum.schemas.KNOWN_TYPES` — see below) rather than
failing to register the tool at all.

This is computed **once**, at server construction — not per call. A
`types.md` edit takes effect on the **next server start**. This is a
deliberate choice, not a limitation: the installed protocol
(`mcp` at `2025-11-25`) supports `notifications/tools/list_changed` for live
schema invalidation, and nothing in this implementation precludes wiring that
up later — it is simply out of scope for this issue.

**One fallback source of truth.** Two independently-drifted "fallback entity
types" lists used to exist — `athenaeum.librarian.FALLBACK_TYPES` (a list,
used when `types.md` is missing at compile time) and
`athenaeum.schemas.FALLBACK_TYPES` (a frozenset, used to build
`KNOWN_TYPES` for the athenaeum#93 unknown-type warning). They are collapsed to
one: `athenaeum.schemas.KNOWN_TYPES`, which the entity-class resolver AND
`librarian.py`'s compile-time fallback both now read directly. `librarian.py`
no longer defines its own copy.

## Generalized ENUMERATION primitive (athenaeum#965)

`recall` narrows a **relevance-ranked** search — it always needs query text
to rank against. Even with athenaeum#964's `type` filter, it structurally
cannot answer "give me every entity of type X whose field Y matches,
ordered by field Z": there is no X to rank. `enumerate_entities` (the MCP
tool) / `athenaeum enumerate` (the CLI) is that different primitive —
**why a separate primitive, not an argument on `recall`**: every one of
`recall`'s three backends (`keyword`, `fts5`, `vector`) either returns
nothing or returns meaningless neighbours for empty/no query text (see the
"Type filter..." section above and issue athenaeum#965's own evidence) —
enumeration is not a missing argument, it is a code path that must never
touch query-text ranking at all. It takes a declared entity type, zero or
more field predicates, a sort key, and a limit — and no query string.

**Contract:**

- `entity_type` is **required** — there is no "enumerate everything" mode.
  An unrecognized value does not error: the response's `known_classes`
  names what this deployment DOES declare/observe (the exact same "escalate
  rather than reject" rule `recall`'s `type` filter already follows),
  computed from the SAME resolver (`athenaeum.entity_schema.resolve_entity_classes`,
  issue athenaeum#964) — never a second, independently-drifting list.
- Zero or more `predicates`, AND-combined. Each names one field — or an
  **ordered fallback list of fields**, OR-combined (the generalized form of
  `athenaeum people --company`'s `current_company` /
  `linkedin_company_at_connect` shape) — and a match kind: `eq` (exact),
  `substring`, or `regex`, all case-insensitive. `ne` is `eq` negated (e.g.
  `do_not_email != true`, usable without `with_pii` since athenaeum#1122 —
  see the scoping note below), not a fourth independent kind.
- A caller-named `sort_key` (any frontmatter field), descending by default.
  Ties — including an entire class sharing one sort value — are always
  broken by `uid` ascending, regardless of sort direction. Values that parse
  as numeric sort numerically; everything else (including a missing value)
  sorts as its lowercased string form.
- `limit` with a sane default (50, matching `athenaeum people`'s own
  default), `0` = unlimited (matching that same command's `--limit 0`).
  Pagination is an opaque continuation `cursor` from a prior call's
  `next_cursor`; a cursor is only valid for the exact
  `(entity_type, sort_key, descending)` triple it was minted under.
- Every hit carries `uid`, `type`, `name`. A caller may request additional
  declared `fields` per hit; a field absent on a given page is included as
  `null`, never silently omitted, so every hit has the same shape.
- `google_contact_*` is usable as a predicate AND as a requested output
  field, gated behind `with_pii=True` — the SAME flag contract
  `recall(with_pii=...)` already uses. Referencing it without the flag
  raises (CLI: nonzero exit + message; MCP: an `error` key in the response)
  rather than silently omitting the field. `do_not_email` (and its
  `_reason` / `_date` companions) is usable the same way but is **not**
  gated (athenaeum#1122 removed it from `_PII_GATED_EXACT_FIELDS`, which is
  now empty) — see the scoping note below for why the two fields are no
  longer treated alike.
- Fail-closed audience scoping (issue athenaeum#538), identical to every other
  read tool: a restricted `caller_audience` never enumerates a page it may
  not read.

**Backend.** Enumeration reads the converged filterable-metadata store
athenaeum#964 built: `FTS5Backend.candidates_by_type` runs a plain indexed
`WHERE type = ?` against the SQLite `wiki` table's `type UNINDEXED` column —
the SAME column `recall`'s `type_filter` predicate already applies — never
FTS5 `MATCH`/BM25 ranking. That table has only seven columns (`filename`,
`name`, `tags`, `aliases`, `description`, `audience`, `type`); it does not,
and per the issue's "no new index structures" constraint must not, carry
arbitrary frontmatter fields like `current_company` or `do_not_email`. So
the type column narrows the **candidate set** to pages of the requested
type — bounded, not a corpus-wide scan — and `athenaeum.enumeration` reads
each candidate's frontmatter fresh from disk for predicate evaluation, the
sort key, output field selection, and the fail-closed audience re-check.
This is the same "trust the index for narrowing, re-read fresh frontmatter
for content and authorization" pattern every other read layer in this
codebase already uses (`recall`'s own Layer C, `cmd_recall`,
`entity_schema`'s field-key scan) — not a second, independently-drifting
full-corpus scanner duplicating the `keyword` backend's traversal (the
issue's original Plan step 2, superseded by the 2026-08-20 AC amendment that
named this backend explicitly).

**Pagination cursor.** An opaque, base64-encoded JSON payload:
`{entity_type, sort_key, descending, sort_tuple, uid}` — the exact sort
position of the last-returned hit. Stable because ordering is always fully
determined by `(sort_tuple, uid)`: primary sort ascending/descending by the
caller-named field, secondary always `uid` ascending (a stable double sort —
sort by `uid` first, then by the primary key with a stable algorithm,
so ties retain their `uid`-ascending relative order regardless of
direction). Resuming re-derives the full candidate/predicate/sort
computation and skips forward to the position after the cursor's
`(sort_tuple, uid)` — a best-effort continuation over a live corpus (not a
frozen snapshot): a stale cursor (its row no longer matches, or was
deleted) resumes from the start rather than raising. A cursor presented
against a different `(entity_type, sort_key, descending)` than it was
minted under raises/errors rather than silently returning nonsense.

**PII-gated fields — scoping note.** `google_contact_*` and `do_not_email`
are BOTH read from the SAME on-page frontmatter as every other field — this
is NOT `recall(with_pii=True)`'s excluded-surface RECORD JOIN (which
resolves values from an off-corpus contact store for one hit at a time via
`pii.assemble_excluded_read`). Enumeration's job is discovery — which
`uid`s match — not the deep per-entity contact read; a caller that needs
the full excluded-surface record for an enumerated hit still follows up
with `recall(with_pii=True)` or `read_entity` by the returned `uid`.

Only `google_contact_*` is actually gated. The gate targets fields whose
VALUE is a durable, cross-system join key — `google_contact_*` lets a
holder join this wiki page to an out-of-band contact system — not fields
that merely relate to email. `do_not_email` was gated on that broader,
looser theory since this module's introduction (athenaeum#965 AC amendment 1)
until athenaeum#1122, when the operator
ruled it wrong: the field is a plain suppression-opt-out boolean with no
excluded-surface join and no durable identifier value, the same shape as
`current_company` or any other ungated field, and gating it made the
*safest* possible question (`do_not_email != true`, the `ne`-predicate
example above) require a *broader* grant than an unrelated one. athenaeum#1122
removed `do_not_email` from `_PII_GATED_EXACT_FIELDS` (now empty) and
deliberately left `do_not_email_reason` / `do_not_email_date` ungated too,
for the identical reason. This is unrelated to the separate `recall` /
`read_entity` reverse-lookup path, where `with_pii=True` stays required
because there the lookup KEY is an email address on the excluded surface
(`docs/authorized-reader-contract.md`) — enumeration never looks anything up
by address, so that constraint never applied here.

**Capability parity with `athenaeum people`.** `athenaeum people` was
deprecated by athenaeum#966 in favour of the generalized primitive below and
has since been REMOVED (athenaeum#1079) now that every reproducible surface
had a landed `enumerate` equivalent — same disposition athenaeum#888 applied
to the athenaeum#887 `person` parity precedent. The table below is kept as a
historical record of the mapping; per surface, as `athenaeum people` stood
before removal:

| `athenaeum people` surface | Generalized `enumerate` expression | Notes |
|---|---|---|
| `--company SUBSTR` (repeatable, AND; matches `current_company` OR `linkedin_company_at_connect`) | `--where current_company,linkedin_company_at_connect:substring:SUBSTR` (repeatable) | Reproduced exactly — the ordered-fallback-field predicate generalizes this shape by construction. |
| `--tag VALUE` (repeatable, AND; exact match) | `--where tags:eq:VALUE` (repeatable) | Reproduced — `tags` is a list field; `eq` matches if any list element equals `VALUE`. |
| `--tier VALUE` (sugar for `--tag tier:VALUE`) | `--where tags:eq:tier:VALUE` | Reproduced, minus the shorthand — the caller spells out `tier:VALUE` itself; no special-cased flag. |
| `--title-regex PATTERN` (repeatable, AND) | `--where current_title,linkedin_position_at_connect:regex:PATTERN` (repeatable) | Reproduced exactly. |
| `--company-regex PATTERN` (repeatable, AND) | `--where current_company,linkedin_company_at_connect:regex:PATTERN` (repeatable) | Reproduced exactly. |
| Default order: `warm_score` desc | `--sort warm_score` (descending is `enumerate`'s own default) | Reproduced exactly, with ZERO special-casing — `warm_score` is itself a plain stored frontmatter field (`cmd_people` reads it with a bare `meta.get("warm_score")`); `enumerate`'s generic sort mechanism handles it identically to any other field. This is the intended generalization, not a coincidence. |
| `--top-touch N` (switches order to `meeting_count_24mo*3 + sent_count_24mo`, top N) | **Dropped, without replacement (athenaeum#1079).** | A genuinely COMPUTED composite with no single backing frontmatter field — the exact shape AC amendment 3 names as out of scope ("computed orderings ... are out of scope here"). The athenaeum#1079 caller search found no in-repo caller depending on it. |
| `--limit N` (`0` = unlimited) | `--limit N` (`0` = unlimited) | Reproduced exactly, same semantics. |
| `--format table\|tsv` | **Dropped, without replacement (athenaeum#1079).** `enumerate` prints one JSON document. | Presentation-layer concern outside a generalized primitive's scope; JSON is the strictly more general shape a caller can render as either table or TSV itself. |

## Off-corpus federation (athenaeum#984)

A SEPARATE mechanism from the pipeline above — the shell-hook hybrid merge
this page otherwise documents is the `UserPromptSubmit` convenience path
only. Off-corpus federation lives in the Python `recall` MCP tool
(`athenaeum.mcp_server._recall_via_backend`), the interface an agent calls
explicitly, not the hook: `athenaeum.off_corpus.query_off_corpus` queries a
second, independent index shard (its own FTS5 db + vector collection, under
`<cache_dir>/off-corpus/`, never git-tracked — see
[`docs/configuration.md`](configuration.md#off-corpus-store-athenaeum984--off-by-default))
with the SAME `backend_name` the primary corpus query just used, and
`athenaeum.off_corpus.merge_ranked_hits` sorts the two hit lists together by
score before the existing render pipeline (Layer C authorization, the
`recallable` policy check, currency reordering) runs — uniformly, over the
merged list, with no off-corpus-specific carve-out in any of those checks.

**Why same-backend-name is required, not incidental.** Two hit lists are
only comparable by score when they came from the identical scorer (BM25 vs
BM25, or cosine vs cosine) — mixing an FTS5 score with a vector score would
make the merge sort meaningless, the exact reason the shell hook above
concatenates FTS5-then-vector instead of sorting across backend types.
Federation sidesteps this by always querying the off-corpus shard with
whatever `backend_name` the primary query used.

**Erasure boundary.** A hit resolved from the off-corpus shard is tagged
`off-corpus/<relpath>` (a fixed literal prefix, not derived from the
off-corpus root's on-disk name) and resolved back to a path via
`_resolve_hit_path`'s `off_corpus_root` branch — the same function every
other hit shape (`wiki/`, extra-root) already resolves through, not a
second resolution path. `athenaeum.off_corpus.erase_off_corpus_record`
deletes the content key and incrementally rebuilds both off-corpus index
shards in the same call, so a caller that erases a record and then calls
`recall` again observes it gone from BOTH content and the federated result
set — see [`docs/security-posture.md`](security-posture.md#off-corpus-erasure-boundary-athenaeum984)
for the full erasure-boundary account.

**Off by default.** With `off_corpus.enabled` unset, `query_off_corpus`
returns `None` immediately and `_recall_via_backend` behaves byte-identically
to before athenaeum#984 — no second query, no merge, no `off-corpus/`-prefixed
hit ever appears.

## Load-bearing invariants

Do not simplify any of these without reading this page and the related commit history. Every one of them is a **silent failure mode** — no exception, no log, just degraded recall quality. The "What breaks" column is what forces a future reviewer to think twice before deleting the guard.

| Invariant | Why it's load-bearing | What breaks if removed |
|---|---|---|
| `set -a` around `source "$CONFIG_ENV"` | `source` without `-a` sets vars only in the hook shell; child processes inherit a clean env. | `athenaeum query-topics` silently runs keyless. The regex fallback runs on 100% of prompts. Named-entity recall on instruction-heavy prompts degrades to 0. No error logged — the Haiku call just returns empty. |
| FTS5 maintained even when vector is primary | Short proper-noun queries ("Return Path") embed closer to generic pages containing the common word than to the sparse entity page. | Queries like "Return Path" return `reference_local_paths.md` instead of the entity. The model still gets *an* injection, so the hook looks healthy — the wrong page just quietly crowds out the right one. |
| `hookSpecificOutput.hookEventName` wrapper | Claude Code discards flat `{"additionalContext":...}` payloads without logging; the hook runs CI-green, `bash -n` is clean, stdout is valid JSON. | `additionalContext` never reaches the model. Zero recall signal. Only detectable by asking the model "did you receive a knowledge context block?" or by reading Claude Code source. |
| Console API key vs `CLAUDE_CODE_OAUTH_TOKEN` | Messages API rejects OAuth tokens with `401 OAuth authentication is currently not supported`. The tokens look similar (both start `sk-ant-`). | `query-topics` returns empty on every call. Silent fallback to regex extractor. Detectable only by reading the 401 body, which the hook swallows to avoid noisy stderr. |
| Audience filter applied INSIDE each backend query AND re-checked at render (athenaeum#312) | The filter must gate ranking/top-k, not just titles. If moved to a post-hoc filter over already-selected hits, a forbidden page still consumes a BM25/kNN slot and starves a permitted page — and a title-only filter leaks the body. | A restricted routine silently receives fewer permitted pages (forbidden ones ate the slots) or a forbidden page's snippet/body. No error — recall "works", it just leaks or under-serves. Do NOT collapse the three layers (index-time audience column, in-query predicate, fresh-frontmatter re-check) into a single post-hoc title filter. |
| Reads reach a caller through the recall/read interface — never by opening a store path (athenaeum#863) | The interface is the one place a rule about the data can be stated, changed or enforced. A store is an ordinary directory: going around the interface *works*, so nothing signals that it happened. This is doubly true of the off-corpus excluded surfaces, which are excluded from recall by construction but are not access-controlled. Since athenaeum#883/#885/#886 they are reachable THROUGH the interface for every entity class — `recall(with_pii=True)` when searching, `pii.read_entity` / `read_entities` (or the `read_entity` tool / `athenaeum query entity`) when holding a uid — so there is no longer a question the interface cannot answer. The person-shaped `pii.read_person` / `read_people`, the `read_person` tool and `athenaeum query person` were DEPRECATED wrappers over that same read (athenaeum#887) and have been REMOVED (athenaeum#888) now that every known consumer migrated to the generic path above. See [`docs/one-way-in-one-way-out.md`](one-way-in-one-way-out.md). | Every caller grows its own read path. Audience scoping, redaction markers, and any later authorization decision become unenforceable — not because they were removed, but because they are no longer on the path the data actually takes. Silent by nature: the direct read returns correct bytes, so nothing looks broken. |
| `with_pii` joins excluded fields at RENDER only — after both Layer-C drops (athenaeum#885) | Excluded values are never indexed and are not searchable; the flag attaches a record to a hit the corpus already produced and already authorized. It runs strictly after (1) the fail-closed `is_page_authorized` re-check and (2) the athenaeum#532 `recallable` drop, so a hit either removes never triggers an excluded-surface lookup at all. Layers A (index build) and B (in-query predicate) are untouched by it. | Moved into Layer A, an excluded value lands in the FTS5/vector store and becomes searchable — permanently, until the index is rebuilt. Moved into Layer B, or run BEFORE either drop, the flag becomes an existence oracle: a restricted caller learns whether a record exists behind a page it may not read, by timing or by the lookup itself. Both fail silently — recall still returns plausible results. |
| Off-corpus federation always queries with the SAME `backend_name` the primary corpus query used (athenaeum#984) | `merge_ranked_hits` sorts two hit lists by raw `score` — valid only when both scores come from the identical scorer. A future caller that queries the off-corpus shard with a different backend than the primary would silently rank incomparable scores against each other. | Off-corpus hits would rank randomly relative to corpus hits (an FTS5 BM25 score compared against a vector cosine-distance score has no shared meaning) — recall still "works" (returns *a* result), it just orders the merge nonsensically, with no error to signal it. |

The shared failure mode — "ships CI-green, degrades silently" — is why
each of these warrants a "what breaks" column rather than a one-line
description. If a future PR proposes removing one, the reviewer should
ask: *how would we detect that this broke?* If the answer is "we
wouldn't, until recall quality drops and someone complains", keep the
invariant.

## References

- Reference implementation: `examples/claude-code/user-prompt-recall.sh` — the recall-on-turn hook shipped with this repo.
- Related PRs: athenaeum athenaeum#40 (JSON shape), athenaeum#42 (query-topics CLI)
