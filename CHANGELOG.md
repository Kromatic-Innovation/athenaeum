# Changelog

All notable changes to Athenaeum are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

- **Design note: standing sensitive-value filter at raw-sweep intake
  (athenaeum#949).** New `docs/sensitivity-value-routing.md` answers the
  issue's AC1–AC13 — placement and pointer contract (AC1/AC2), the uid
  problem and the proposed record-keyed read-path disposition (AC3), the
  raw-tree observability gap stated as open rather than solved (AC4),
  disposition of the existing `screen_intake` stage (AC5), per-write-path
  redaction mechanics (AC6), precedence and fail-closed behavior (AC7/AC10),
  usage-classification default (AC9), idempotency (AC11), the correlation
  trade (AC12), the migration story relative to athenaeum#437 (AC13), and
  the relationship to `pii.RedactionMarker` left as an explicit open
  question (AC8). Cites a working spike (branch
  `prototype/949-sensitivity-routing-spike`, not merged and not part of
  this change) as verification evidence for specific mechanical claims —
  the note's decisions are proposals for review, not settled by the spike
  having been built. Implementation is explicitly deferred to the
  follow-on slices the note's §10 lists — athenaeum#1022 (config resolver),
  athenaeum#1023 (routing/redaction mechanism), athenaeum#1024
  (record-keyed read path), athenaeum#1025 (wire into the librarian raw
  sweep) — filed as separate issues against this note (AC14's filing half;
  review of the note itself is routed separately). Docs-only; no code
  changed.

### Fixed

- **The librarian's per-raw-file LLM-call/wall-clock bounds rejected
  ordinary files instead of catching loopers, and an over-bound file's
  completed work was discarded wholesale (athenaeum#994).**
  `resolve_raw_file_max_api_calls` (default `8`) and
  `resolve_raw_file_max_runtime_seconds` (default `120`, seconds) assumed an
  ordinary raw file cost roughly 1-3 LLM calls and well under two minutes.
  Measured reality on the live deployment (2026-08-15/16 nightly logs, api
  provider) put an ordinary file at 20-46 calls and 300-690 seconds —
  un-batched `tier3_write` spends one call per entity action — so both
  bounds sat 3-6x below the median file and rejected normal input rather
  than catching genuine loopers; two consecutive nights spent over 3,000s
  and 240+ calls each on `created=0 updated=0 files=0`, redoing the same
  files at full cost both nights. Defaults are recalibrated to `60` calls
  and `900` seconds, covering the measured distribution with headroom.
  Enforcement also moved from post-hoc (checked once, after a whole file's
  actions had all already run and paid for) to pre-emptive/incremental:
  `tier3_derive_actions` now checks both bounds after EACH entity action, so
  a file whose Nth action pushes it over the bound never starts action N+1.
  The action(s) that completed before the trip now land as durable partial
  progress — written to disk before the over-budget error propagates,
  via the new `RawFileOverBudgetError.new_entities` /
  `pending_updates` / `updated_uids` / `escalations` payload and
  `librarian._apply_tier3_results` — instead of the entire file's spent
  work being discarded every time, which is what caused the same file to be
  redone in full on consecutive nights. The existing quarantine ledger
  (athenaeum#898) is unchanged and still backstops a file that keeps
  tripping the bound after it is quarantined out of the discovery set, so a
  chronically over-bound file is still never retried identically forever.

## [0.19.0] - 2026-08-15

_Supersedes six untagged, unpublished patch bumps (0.18.2–0.18.7) that never shipped to PyPI; the last published release was v0.18.1._

### Added

- **Recall identity resolution: handle-shaped queries answer by exact reverse
  lookup (athenaeum#907).** A handle-shaped query — a bare address, or a
  registry handle framed as a question ("who is this address?", "is this
  address still current?") — now answers by exact reverse lookup instead of
  similarity search, via the new `athenaeum.identity_resolution` module.
  Detection is deliberately conservative (exactly one email-shaped token, or
  the whole query with a small closed set of interrogative framing stripped
  exactly matching a `registry.json` handle value); any other query falls
  through to the existing search pipeline byte-identically. The response is a
  JSON document carrying the person's `uid`, `display_name`, `entity_class`,
  and per-value fact fields — usage/provenance classification, bounce
  history, validity dates — never an eligibility, permission, or action
  predicate of any kind (access control remains a separate, deferred question,
  athenaeum#864). Excluded values are gated by `with_pii` exactly as
  elsewhere in `recall`, with the excluded-surface lookup running strictly
  after the same audience and `recallable` drops (athenaeum#885/#532) —
  either drop performs zero excluded-surface scans. Wired through both
  `recall` entry points: the MCP `recall` tool and `athenaeum recall`.

- **Unified decision resolution as intake (athenaeum#908).** `list_pending_decisions`
  already joins questions/merges/retractions/audits/quarantine into one
  outbound queue; the path back IN was still per-type. New module
  `athenaeum.decision_answers` extends the existing `raw/answers/*.md`
  raw-intake convention with `decision_id` / `decision_type`
  (`question`/`merge`/`audit`/`proposed-rule`, required) / `verdict` /
  `note` fields, and applies pending decision-answer files
  deterministically at tier 0 — no LLM call, ever — dispatching per type
  to `answers.resolve_by_id`, `pending_merges.resolve_merge`, or
  `calibration.record_audit_review`. Wired into the existing `athenaeum
  ingest-answers` run-locked tick (not a new CLI command): decision
  answers apply first, then the legacy question-answer pass completes the
  write-back/archival for any question answered this way. Applying is
  fail-soft and idempotent by construction — an unknown id, an
  already-resolved id, an invalid verdict, or a schema-malformed answer
  file is logged and skipped (never deleted, so it stays its own audit
  trail) without corrupting state or halting the rest of the batch, and
  since each underlying resolver already refuses to re-mutate an id it
  has settled, no separate "applied" bookkeeping is needed for
  idempotency. A record with no `decision_id` (the pre-existing
  `pending_question_answer` provenance format) parses exactly as before —
  hard back-compat requirement. `proposed-rule` is REGISTERED (the format
  round-trips) but has no store yet: it fails closed with a structured,
  logged `decision_type_unavailable` outcome and zero state mutation,
  referencing athenaeum#905 (open, blocked by athenaeum#901/athenaeum#903) rather than
  inventing that store here. See
  [`docs/contradiction-detection.md`](docs/contradiction-detection.md#decision-answer-files-unified-decision-resolution-as-intake-athenaeum908)
  for the full format and dispatch table.

- **The athenaeum#691 PII-restore script is now a tracked, tested repo script instead
  of an untracked hand-patched file (athenaeum#844).** The script that
  mutates the live PII-bearing `~/knowledge` wiki lived untracked at
  `~/Desktop/athenaeum-691-restore_pii.py` and had been hand-patched twice
  across separate sessions with no changelog, no review, and no pointer from
  the issue it serves; its `classify()` safe/unsafe boundary — the only thing
  preventing over-restoration of real client PII — had zero test coverage.
  Relocated into the repo verbatim except for one deliberate change: the
  operator-specific half of `SAFE_EMAIL_EXACT` moves out of source and into
  live config (`pii.restore.safe_email_exact` in `athenaeum.yaml`), since this
  repo is public and the hardcoded set contained a real address — a script
  whose job is keeping contact data out of a published corpus must not itself
  publish one. Generic entries (`git@github.com`, `root@example.com`) remain
  code defaults; a missing or malformed config block fails CLOSED to those
  defaults, since an address absent from the allowlist stays redacted, the
  safe direction under this codebase's over-restoring-is-worse-than-under-
  restoring rule. `classify()`'s logic is unchanged; the resolved allowlist
  was verified byte-identical to the previous hardcoded set. This relocation
  does not yet add an automated test for `classify()`'s safe/unsafe boundary
  — verification here was manual (byte-identical allowlist, a real address
  still classifying as `None`, and a clean `--limit 40` dry run against the
  live wiki) rather than a `tests/` regression test; that gap is tracked
  separately and not closed by this entry.

- **`docs/north-star.md`** — canonical statement of the project's purpose and
  operating principles, linked from the README.

- **Per-file size/cost bound + quarantine for poison raw-intake files (athenaeum#898).**
  A single pathological raw file could silently own the entire nightly entity
  budget: `RawFile.content` read a whole file with no size guard (athenaeum#843
  documented this and scoped the guard out), and a file that exhausted the
  phase budget was deferred and retried on the next run indefinitely (athenaeum#800
  documented failed files retrying nightly with no escape). Measured
  consequence: one 9.7MB dry-run artifact accounted for 93% of timed
  entity-phase LLM calls for roughly three months before it was retired.
  Three per-file bounds are now enforced: a **byte bound** at `RawFile.content`
  itself (`RawFileTooLargeError`, checked via `stat()` so an oversized file
  costs one syscall to reject, never a full read — `librarian.raw_file_max_bytes`,
  default 5 MiB), and a **LLM-call bound** plus a **wall-clock bound**, both
  checked by the entity phase runner after each file's `process_one` call
  completes (`librarian.raw_file_max_api_calls` default 8,
  `librarian.raw_file_max_runtime_seconds` default 120). A file that exceeds
  ANY bound on `librarian.quarantine_threshold` (default 2) consecutive runs
  on the same content is **quarantined**: physically moved from
  `raw/<source>/` to `wiki/_quarantine/<source>/` (so it drops out of
  `discover_raw_files`'s discovery set with no change to that function),
  recorded in a new append-only audit ledger (`wiki/_quarantine.jsonl`, new
  `athenaeum.quarantine` module — mirrors the `athenaeum.calibration` /
  `athenaeum.retraction_cascade` JSONL-ledger house style), and surfaced as a
  `type: "quarantine"` item in `athenaeum decisions` / `list_pending_decisions`
  via the new `quarantine_to_decision` mapper. Quarantine is reversible ONLY
  by an operator decision (`athenaeum.quarantine.release_quarantine`) — there
  is no automatic un-quarantine path. The consecutive-violation counting
  mirrors the athenaeum#663 stuck-file ledger's shape exactly (content-hash-keyed,
  so an edited file starts a fresh count; corrupt/missing ledger fails open)
  but is tracked in its own manifest (`wiki/_quarantine_candidates.json`) —
  a bound violation is a measured resource fact, not a processing exception,
  and quarantine's disposition (physical removal) is materially heavier than
  the stuck-file skip-in-place. The consecutive-count hash deliberately
  fingerprints `(size, mtime)` via a single `stat()` rather than reading full
  content the way the stuck-file ledger does — reading a file large enough to
  violate the byte bound just to hash it would defeat the bound's own
  cost-avoidance purpose.

- **A person email handle resolves to a uid at tier 0, through the PII surface
  (athenaeum#884).** A `{"type": "person", "handle": {"email": "…"}}` correction
  target now resolves by reverse-lookup — `email → contact record → record uid
  → wiki page` — inside the correction applier, with no LLM call, so **no
  external system ever needs to read the excluded surface to correlate an
  address to a person**. This implements the answers recorded on
  athenaeum#858/#859 (operator, 2026-08-13): the alternative was a uid-only
  reverse-lookup read API that a caller would hit before submitting; putting
  the resolution inside the librarian instead means the caller never needs the
  uid, no new caller gains contact-surface access, and there is one resolution
  path rather than two to keep in sync. voltaire's shipped writer already emits
  this exact target shape and needs no change.

  `email` is deliberately **not** a `SOURCE_HANDLE_KEYS` member and must never
  become one: `registry.json` is compiled from wiki frontmatter, and the
  athenaeum#502/#507 migrator preserves only `DURABLE_IDENTIFIER_FIELDS` and
  explicitly folds `alt_emails` onto the excluded record — so an address seeded
  as a registry handle is migrated off the page on the next `storage
  migrate-pii` run and its registry entry evaporates. Every step of the
  resolution goes through `athenaeum.pii` (new `resolve_contact_records`, the
  all-matches sibling of `resolve_contact_record`, and `uid_on_record`); the
  applier never constructs a contacts-surface path itself. **The librarian is
  not an exception to the one-way-out rule, it is an implementation of it** —
  `docs/one-way-in-one-way-out.md` gains a section saying so, and another
  recording the athenaeum#858 answer durably in the docs rather than only in an
  issue comment.

  All-matches rather than first-match is the point: `resolve_contact_record`
  returns the deterministic first and logs the ambiguity, which is right for
  `mark_bounced` (a deliverability fact must land somewhere, and a shared
  address is legitimate) and wrong for identity resolution, where quietly
  picking one of several people is exactly the guess this layer must refuse.
  Several **distinct** persons raises a tier; several records carrying the SAME
  uid do not, since that is one person described twice. `ExcludedRecordIndex`
  grows the matching `all_by_identifier` accessor — a different read of
  athenaeum#883's index, not a second scan.

  **A zero-match email handle raises a tier and NEVER creates**, notwithstanding
  athenaeum#865's tier-0 create branch. voltaire's ordinary conversation-intake
  path emits this target shape for every triaged correspondent with no
  significance gate in front of it, so a create-capable email handle would
  auto-create a person page per correspondent — cold senders and sales
  sequences included — which is the "write everything and let the librarian
  decide" firehose the operator rejected. The guard is written **explicitly**
  rather than left to rest on `email` being absent from `SOURCE_HANDLE_KEYS`,
  so a future widening of that tuple cannot silently open the create branch;
  a test pins that too.

  Beyond the four cases the issue enumerates (matched / zero-match / ambiguous
  / cross-type), the **orphan-uid** branch its acceptance-criteria amendment
  adds is handled and distinguishable: a record whose uid has no wiki page (a
  measured ~47 of 12,960 records) raises a tier, never creates, never crashes,
  and records `email-handle-orphan-uid` rather than `email-handle-no-match`.
  Zero-match means *the address is unknown*; orphan-uid means *the address is
  known and its person page is missing* — a store-consistency signal worth
  surfacing rather than filing away as an unknown address. Since both are
  `raised-tier` (a closed §5.3 vocabulary this does not reopen), the
  distinction is carried by a new machine-readable `reason` on
  `TargetResolution`, `None` for every pre-existing outcome.

- **The generic excluded read reaches MCP and the shell (athenaeum#886).** After
  athenaeum#883/#885 the generic path existed in Python, but a caller reaching
  athenaeum over MCP or the CLI could still only ask the person-shaped
  question — the same asymmetry that produced the original problem, since an
  interface that answers one entity type well and every other type not at all
  is one that callers route around. The `recall` MCP tool and `athenaeum
  recall` gain `with_pii` (plus `--usage-class` on the CLI, matching `query
  person`'s existing flag). A new `read_entity` MCP tool and `athenaeum query
  entity --uid ... [--class ...] [--include-excluded] [--usage-class ...]`
  read any entity class by uid, printing the same JSON object shape `query
  person` prints today. **`read_person` and `athenaeum query person` are
  kept**, unchanged in name, arguments, defaults, output and exit codes, and
  become thin wrappers over the generic path — parity is asserted by test
  across all four inclusion/record cells AND for a `usage_classes`-filtered
  case, with byte-identical stdout on the CLI side. The generic tool carries
  ALL of `uid`, the entity class, `include_excluded` **and** `usage_classes`:
  dropping the last would not have been a smaller version of the same tool, it
  would have silently removed a filter `docs/security-posture.md` §2.3 depends
  on. `mcp_server.person_read`'s fail-closed audience check applies
  identically on the generic path — the same check, so a restricted caller
  cannot obtain via one tool what the other refuses.

  `athenaeum recall --with-pii` is **not** a bare flag addition:
  `_cmd_query.cmd_recall` is a second implementation of the same layer
  ordering (it constructs its own backend and its own `is_page_authorized` /
  `storage_policy_configured` / `is_recallable` drops rather than calling
  `recall_search`), so athenaeum#885's layer-ordering guarantees are re-derived
  in that function — the join is placed after the audience drop, the
  athenaeum#308 temporal drop and the `recallable` drop — and asserted by that
  command's OWN layer-ordering tests, which athenaeum#885's do not cover.
  `athenaeum query people` (the separate multi-flag listing command) is
  deliberately untouched, and a test pins that it grew no excluded-field flag.
  `docs/one-way-in-one-way-out.md` §5's integrator checklist now points at the
  one read path in its two shapes; `docs/security-posture.md` §2, §2.1 and
  §2.3 are updated so the audience-scoping AND usage-class descriptions still
  match the tool inventory.
- **`recall(with_pii=True)` — resolve excluded fields for any entity class
  through the read you are already making (athenaeum#885).** Excluded-field
  access used to be expressible only as "call `read_person` with a uid you
  already have". A caller who found an entity through `recall` and wanted that
  entity's excluded fields had to make a second, differently-shaped call — and
  only if the entity happened to be a person. That is the shape that makes
  callers reach past the seam:
  `docs/one-way-in-one-way-out.md` §3 already recorded one agent session
  reading the excluded surface directly because nothing said otherwise and no
  alternative existed. `recall_search` / `_recall_via_backend` now take
  `with_pii` (default `False`) plus an optional `usage_classes` threaded to the
  join exactly as `read_entity` accepts it, so a caller that must not receive a
  provider-sourced address (`docs/security-posture.md` §2.3) can filter here
  too. **The default path is byte-identical and free** — with the flag unset,
  ZERO excluded-surface scans are performed. New config key
  `storage.excluded_read_mapping` maps a page `type:` onto the surface class
  whose record holds its excluded fields: identity by default, with the single
  shipped non-identity entry `person: pii`, operator-overridable. The join is
  gated by `storage.is_excluded(surface_class, config)` *before* it is
  attempted — a page class whose mapped surface class is NOT excluded (every
  class on a base that maps only `pii: excluded`) performs no join and returns
  nothing, never an error, and critically never scans the wiki root as if it
  were an excluded surface. One index per call, shared across all `top_k` hits;
  a hit with no `uid` performs no join and produces no marker.
  `docs/one-way-in-one-way-out.md` §3 is rewritten as one path — table *and*
  prose — and `docs/recall-architecture.md`'s load-bearing-invariants table
  gains the layering statement.

  **How it layers with audience scoping (athenaeum#312/#538) — unchanged.**
  `with_pii` touches only Layer C. Layer A (index build) is untouched: excluded
  values are never indexed and never become searchable, only resolvable on a
  hit the corpus already produced. Layer B (the in-query predicate) is
  untouched: the flag is not a search-time predicate and never widens the
  candidate set — it cannot make an excluded page become a hit. At Layer C the
  join runs strictly AFTER (1) the fail-closed `is_page_authorized` re-check
  and (2) the athenaeum#532 `recallable` drop, so a hit either removes never
  triggers an excluded-surface lookup at all — a restricted caller cannot use
  the flag to probe whether a record exists behind a page it may not read. Both
  orderings are asserted by test. Authorization is deliberately unchanged and
  identical to `person_read`'s: the audience check decides whether the hit
  exists, and for a hit that survives it the flag yields values. Who may SET
  the flag remains the deferred athenaeum#864 question.

  The join reuses athenaeum#883's public, `EntityIndex`-free assembly seam and
  never `read_entity`/`read_entities`: the render loop already holds the hit's
  fresh frontmatter from its own Layer-C re-read, so building an `EntityIndex`
  there would be a defect rather than an optimization detail — 25.2s of the
  measured 28.1s per-call cost IS that construction. Asserted by test, by
  monkeypatching `EntityIndex` to raise on the `with_pii=True` path.
- **`pii.read_entity` / `read_entities` — the excluded-record read, for ANY
  entity class (athenaeum#883).** The excluded/redacted read path was
  entity-shaped only by accident of how it was built: two things were
  hardcoded and both were narrow. `contacts_surface_root()` always resolved
  the `pii` class, even though the `storage.surface_root_for_class` layer it
  delegates to has been fully class-parameterized since athenaeum#429 — the
  caller simply never got to say which class. And `CONTACT_DATA_FIELDS` was a
  fixed four-name allowlist, so an excluded record for any non-person class
  read back as if it had no fields at all. Everything else was already
  generic (`EntityIndex` indexes every entity type by uid; the record join is
  uid-keyed; `RedactionMarker` is field-generic), so this is a
  parameterization, not a schema migration — there is no pointer-field
  frontmatter contract to invent. New `excluded_surface_root(entity_class,
  knowledge_root, config)` takes the **surface class** (the `storage.mapping`
  key, e.g. `pii`) — deliberately NOT the wiki page's `type:`, since a person
  page is `type: person` while its record lives on the `pii` surface; these
  functions are told the surface class and never guess. New
  `resolve_excluded_fields` replaces the fixed allowlist with a per-class
  policy: explicit `storage.excluded_fields.<surface_class>` config first,
  then `pii`'s built-in `CONTACT_DATA_FIELDS` verbatim, then — for any other
  class — every frontmatter field on the record minus a bookkeeping denylist.
  That last default is the deliberate choice: an allowlist for a class nobody
  has enumerated makes the redaction marker **dishonest by omission** (a
  forgotten field is reported neither as a value nor as a marker, collapsing
  "withheld" into "absent" — the exact failure the marker exists to prevent),
  whereas a denylist-complement fails only toward noise, never toward a silent
  hole. `PersonRead` is renamed `EntityRead` field-for-field with
  `PersonRead = EntityRead` kept as a true alias, so `read_person` and
  `read_entity` return the identical type and parity is by construction rather
  than by test; the `contact` / `contact_included` / `contact_record_path`
  payload keys keep their names, since renaming them would change JSON live
  consumers already read. **`read_person` / `read_people` are unchanged** —
  they become thin wrappers passing `surface_class="pii"`, with identical
  signatures, defaults, laziness, return values and JSON shape, and
  `read_people`'s positional convention (`apollo-enrich`'s exact call shape)
  neither reorders nor becomes keyword-only.
- **`pii.assemble_excluded_read` — the field/marker logic, without an
  `EntityIndex` (athenaeum#883).** The assembly that was private inside
  `_person_read_from_indexes` (which required an `EntityIndex` it needed only
  to find the page) is now public and takes an already-resolved page path +
  frontmatter + the matched record's frontmatter directly, returning
  `(fields, redactions, classifications)`. `read_entity`/`read_entities` are
  callers of it, not the only way to reach it. This is the seam a caller that
  already HOLDS the page — `recall`, next in this chain — needs: rebuilding an
  `EntityIndex` there would be a defect, not an optimization detail, since
  25.2s of the measured 28.1s single-call cost IS the `EntityIndex`
  construction.

### Changed

- **BEHAVIOR CHANGE (athenaeum#908): `resolve_question` / `resolve_merge` /
  `review_audit_item` now defer their state mutation.** Each MCP tool
  still validates the id against CURRENT state immediately (unknown id,
  already-resolved id, or invalid verdict/decision still fails right away
  with the same `error_code` as before, and nothing is written on that
  path), but a successful call now writes a decision-answer file instead
  of mutating `_pending_questions.md` / `_pending_merges.md` / the
  calibration ledger directly. The actual state change happens on the
  next `athenaeum ingest-answers` tick. A successful response now
  includes `deferred: true`, `answer_file`, and `decision_id` so a caller
  can tell the difference; `resolve_merge`'s response no longer carries
  `folded_sources` / `aliases_added` / `links_rewritten` synchronously
  (those only exist once the next tick applies the fold). `athenaeum
  calibration review` (the CLI twin of `review_audit_item`) is a
  deliberate exception — it still calls `record_audit_review` directly
  and immediately; athenaeum#908 named only the three MCP mutators.

- **The ingest stamp advances per file, not all-or-nothing (athenaeum#895).**
  `librarian.ingest` now records a content hash for exactly the raw files a run
  drained from the intake queue — compiled by the entity loop, or retired by
  the move-then-retire pass — merged into the existing stamp. A run truncated
  by `max_files` stamps its compiled subset and leaves the remainder unstamped;
  the next run skips the stamped subset and picks up the rest.

  athenaeum#530 established the invariant that a file which was not compiled
  must never be stamped (stamping it makes the next ingest take the no-op fast
  path, and those notes are silently never compiled and never recallable), but
  expressed it as a single whole-run boolean: stamp nothing unless the run
  drained the whole backlog. With a steady backlog above `max_files` that
  condition never holds, so the stamp never advanced and every SessionEnd
  rediscovered the same head — measured at 86 SessionEnd runs producing 26
  compiled files over 7 days with no stamp movement. The invariant is per file,
  and this enforces it per file: a truncated run makes real, durable progress
  while the uncompiled remainder stays unstamped and discoverable.

  "Left the intake queue" is deliberately the signal, rather than a phase-level
  report of what a run believed it compiled: it is observable ground truth and
  can only ever under-approximate the drained set, so a mis-report upstream
  cannot cause an uncompiled file to be stamped. One consequence worth naming:
  a file that failed or was skipped as stuck (athenaeum#663) is no longer
  stamped by an otherwise-clean run, so it is retried instead of being recorded
  as seen and never compiled again. The manifest format is unchanged and reads
  back-compatibly — a manifest written by the old code loads and is extended in
  place.

### Deprecated

- **`pii.read_person` / `read_people` — deprecated in favour of
  `recall(with_pii=True)` and the generic entity read (athenaeum#887).** Now
  that athenaeum#885 and athenaeum#886 have shipped the general replacement, the
  person-shaped entry points are marked deprecated: both emit a
  `DeprecationWarning` (`stacklevel=2`, so it points at the CALLER's line —
  otherwise it names `pii.py` and tells a consumer nothing about which of
  their call sites to migrate) naming the replacement and the removal issue.
  The `read_person` MCP tool logs a ONE-TIME notice per process, and
  `athenaeum query person` prints one to **stderr** — never to stdout, which
  is a JSON object a script parses and where a notice would be a breaking
  output change dressed up as a deprecation. `docs/one-way-in-one-way-out.md`
  §3/§5 and `docs/recall-architecture.md` now document them as deprecated
  wrappers rather than the sanctioned entry point.

  **This changes zero behaviour** — both functions keep working identically,
  including `read_people`'s laziness and its positional calling convention,
  and every existing result is unchanged (asserted against the generic path
  rather than assumed). One implementation detail was required to make the
  warning honest: `read_people` is no longer a generator function. A
  `warnings.warn` inside a generator body does not run until the generator is
  first advanced, so a caller that built the iterator and passed it elsewhere —
  or never consumed it — would have got the warning at a confusing point or
  not at all. It now warns at CALL time and RETURNS `read_entities`' generator,
  which preserves laziness exactly: nothing is read, not even the indexes,
  until the first pair is pulled.

  Actual removal is a separate, later issue (athenaeum#888), gated on a real
  deprecation window and on known consumers having migrated — **not** on this
  one closing. `read_person`/`read_people` are documented public API that this
  repo's own history proves is depended on (apollo-enrich#37/#49 followed the
  documented path, and apollo-enrich's weekly job calls `read_people` today);
  deleting a documented public function the same day its replacement lands is
  the identical mistake in reverse — a caller finds the path broken with no
  warning, exactly the silent failure `docs/one-way-in-one-way-out.md` exists
  to prevent on the read side. Per the version-bump policy this issue is a
  **patch** (a warning, no behaviour change); the removal will be a breaking
  change and must bump **minor** at minimum, with a `### Removed` entry.

### Fixed

- **The handle-shaped `recall` path silently dropped `usage_classes`,
  returning every usage class instead of the requested subset (athenaeum#907
  follow-up; found by Sentry Seer on athenaeum#919).** Both `recall` entry
  points accept `usage_classes` to restrict which excluded contact values
  come back, and the similarity-search path already threaded it to
  `pii.assemble_excluded_read` — but `identity_resolution.resolve_handle_query`
  never received it, so a handle-shaped query (a bare address, or a registry
  handle framed as a question) with `usage_classes` set returned values of
  every class regardless. `resolve_handle_query` now takes a keyword-only
  `usage_classes` and threads it through `_finish` /
  `_assemble_contact_values` to `pii.assemble_excluded_read`, exactly
  mirroring the similarity-search path; both entry points
  (`athenaeum.mcp_server.recall_search` and `athenaeum._cmd_query.cmd_recall`)
  pass their existing value through. The filter narrows WITHIN the
  athenaeum#885 excluded-value join and cannot widen it, and does not perturb
  the `with_pii=False` redaction-marker path.
- **A graceful internal deadline stop no longer collides with a hard external
  kill on the same exit code (athenaeum#897).** `run()` returned `124` for BOTH
  its own wall-clock deadline trip AND a delivered SIGTERM/SIGINT — a
  consumer keyed on `rc == 124` (e.g. the SessionEnd wrapper) could not tell
  a clean, resumable, partial-progress run apart from a hard kill without
  parsing log text. New `EXIT_GRACEFUL_PARTIAL = 75` (BSD `EX_TEMPFAIL`
  semantics: partial progress committed, resumable) is now returned by every
  internal deadline-check path — `RunContext.stop_on_deadline`, the
  entity-loop `deadline_tripped` finalize branch, and every
  `RunDeadlineExceeded` catch site. `EXIT_EXTERNAL_KILL = 124` is unchanged
  and now reserved exclusively for the opt-in SIGTERM/SIGINT handler
  (`_commit_partial_and_exit`), matching coreutils `timeout`(1) semantics —
  athenaeum's own checks never return it. Both constants live in
  `src/athenaeum/librarian.py`; the full contract is documented in the new
  `docs/exit-codes.md`, linked from `docs/configuration.md` and the README
  docs index. **Two of the issue's acceptance criteria — the SessionEnd
  wrapper's own handling of the two codes and its `INGEST_DEGRADED` signal —
  live in `scripts/hooks/knowledge-rebuild-index.sh` in a different repo
  (`Kromatic-Innovation/code-workspace-config`) and are relocated to
  cwc#2556; until that lands the wrapper does not yet recognise `75` and
  falls through to its generic non-zero-rc handling instead of a dedicated
  resumable-partial path (no data loss — partial progress is still committed
  and the run is still resumable).**
- **SessionEnd's inner deadline is derived from the outer kill timer, so the
  graceful-stop path can actually win the race (athenaeum#896).** `cmd_session_end`
  passed no `max_runtime`, so the inner deadline fell through to
  `DEFAULT_MAX_RUNTIME` (3600s) — four times the SessionEnd wrapper's 900s
  outer `timeout --signal=TERM` — so every budget-tripped SessionEnd run was
  externally SIGTERM'd instead of exiting through the athenaeum#337/athenaeum#396
  graceful-stop path (partial-progress commit, resumable deferred intake). New
  `session_end_max_runtime()` derives `inner = outer - margin` from
  `KNOWLEDGE_REBUILD_TIMEOUT` (the SAME env var, same 900s default, the
  wrapper script — `code-workspace-config/scripts/hooks/knowledge-rebuild-index.sh`,
  a different repo — reads for its own `timeout` wrap, so the env var is the
  single definition both sides read), clamped so the result is always
  strictly positive and strictly less than a configured outer of 2 or more.
  `cmd_session_end` also now passes explicit, session-scoped-incremental-sized
  `max_files`/`max_api_calls` (`SESSION_END_MAX_FILES` = 20,
  `SESSION_END_MAX_API_CALLS` = 100) instead of the nightly-run defaults (50 /
  800). Mirrors the nightly path's existing outer-minus-margin derivation
  shape (cron-fleet#95) applied to the SessionEnd wrapper's own outer value;
  the nightly path's own derivation is unchanged.
- **`pii.resolve_contact_record` is answerable from an index, and no longer
  goes stale mid-batch (athenaeum#883).** athenaeum#877/#879 fixed the by-uid
  batch path; the by-ADDRESS sibling still paid a full `iter_contact_records`
  scan **per call**, across three callers (`bounce_join.join_identifier`,
  `classify_contact_value`, and `mark_bounced` via the librarian's compile
  loop), each paying it independently. New `ExcludedRecordIndex` builds
  `by_uid` + `by_identifier` in ONE pass, holding the existing discipline
  exactly — deterministic first-match-in-sorted-order wins, `log.warning`
  (never raise) on collision, missing root yields an empty index, identical
  `str(...).strip()` coercion — so moving a caller onto it can never resolve
  to a different record than the unindexed function would. The librarian's
  compile loop builds ONE index for its whole `ctx.raw_files` pass and threads
  it down through `process_one`'s new `excluded_index=` parameter to
  `tier0_bounce_mark` and `mark_bounced`; building it inside
  `tier0_bounce_mark` would rebuild it once per raw file and defeat the fix.
  The index's load is lazy (first lookup, not construction) so a compile run
  with no conforming bounce note pays nothing at all. Every new `index=`
  parameter is optional and defaults to `None`, so every existing caller keeps
  today's behaviour *and* today's cost. `join_identifier` deliberately keeps
  the unindexed call and says why: it is a single-identifier entry point with
  no batch above it, and building an index to answer one lookup is strictly
  slower than the scan it replaces.
- **A batch of hard-bounce marks can no longer mint a duplicate record
  (athenaeum#850 regression guard).** `mark_bounced` MINTS a record when
  resolution returns `None` and merges new identifiers onto existing ones, so
  any process-lifetime index that is never invalidated would answer a stale
  `None` on the second lookup of one address in a batch — reintroducing
  athenaeum#850's exact failure. `ExcludedRecordIndex` takes `EntityIndex`'s
  architectural answer instead of leaving it as a documented hazard: a one-shot
  load plus an explicit `register(path)` the WRITER calls after every write.
  `register` re-indexes the record **wholesale** (re-reading its uid and its
  full identifier list) because a single-key insert would miss the merge case,
  and it appends rather than prepends, so registering can never change which
  record an already-indexed identifier resolves to — batch resolution stays
  stable regardless of how writes interleave with reads. Both properties are
  pinned by test, alongside the two-marks-one-batch-one-record regression.

## [0.18.1] - 2026-08-13

### Added

- **`pii.read_people` — read N persons paying the store's O(corpus) scans
  ONCE, not once per uid (athenaeum#877).** `read_person` rebuilds two
  full-corpus indexes on every call — the wiki `EntityIndex` and a complete
  `iter_contact_records()` scan — which is unremarkable for the occasional
  single lookup and quadratic in practice for a loop: ~28s per uid against the
  live 16,928-page store, or ~37 hours for the 4,696-person population
  `apollo-enrich`'s weekly warm-tier job resolves, before a single provider
  API call. `read_people(knowledge_root, config, uids, *, include_contact=...,
  usage_classes=...)` builds each index exactly once per batch and streams
  `(uid, PersonRead | None)` pairs. Measured on a synthetic corpus of the same
  shape (16,928 pages / 4,696 candidates): **3.05 hours projected → 3.3
  seconds actual**, 0.71ms per uid after the fixed cost. Both O(corpus) halves
  had to go: the contacts scan is the one the issue names, but the
  `EntityIndex` rebuild is the larger share (a bare frontmatter pass over the
  wiki measured 25.2s of the 28.1s), so fixing only the named half would have
  left ~33 of the 37 hours in place. **This is a cost change, not a semantic
  one** — `read_people` returns exactly what `read_person` returns for each
  uid, including redaction markers, usage-class filtering and `None` for an
  unresolved uid, and both entry points share one assembly body so they cannot
  drift; parity is asserted directly in `tests/test_person_read_batch.py`.
  `read_person` itself is deliberately unchanged in cost and behavior — a
  single read has nothing to amortize — so the loop-shaped caller is what
  moves. Two smaller properties fall out: the indexes are built on the FIRST
  uid rather than at call time, so an empty batch (a quiet week for the weekly
  job) costs nothing rather than one full pass to read zero people; and the
  stream is lazy, so a 4,696-uid run never holds thousands of full page bodies
  in memory at once. The two-path invariant is untouched
  (`docs/one-way-in-one-way-out.md` §3, updated): `read_people` resolves
  `contacts_surface_root` itself, so a caller still supplies only uids and
  flags and never constructs a surface path — keeping the batch shape *inside*
  the seam is precisely what stops a caller from routing around an interface
  too slow for its real workload. New `pii.build_contact_record_uid_index`
  builds the `uid -> record` mapping in one pass, holding the same
  first-match-wins/warn-never-raise discipline as
  `resolve_contact_record_for_uid` so a batch read never resolves a uid to a
  different record than a single read would.

## [0.18.0] - 2026-08-13

### Added

- **`athenaeum bounce-divergence` — report bounce-mark divergence across BOTH
  surfaces, not a count of one (athenaeum#853).** A count of one surface cannot
  tell a healthy system from a broken one here, and this feature has already
  been misread twice by exactly that shape of check — once in each direction
  (a grep of the wiki surface returning 0 where the answer was 189 and being
  used as corroboration; a grep of the contacts surface for a `bounced:` key
  that is never written there returning 0 after a fully successful mark). New
  `athenaeum.bounce_divergence` takes a **store root as a parameter** and
  emits, for that store: the count of wiki pages carrying `bounced:`, the
  count of contacts-surface records carrying a mark — identified by the field
  the mark **actually writes** (`identifier_validity:` or a top-level
  `valid_until`), never by a `bounced:` key — and the **set difference in both
  directions**, computed over the `uid` join key athenaeum#852 defines rather
  than re-derived. Three properties are deliberate. *Empty and unreadable
  never render identically*: each surface carries a status (`read` / `missing`
  / `unreadable`) alongside its count, a partially-read surface reports how
  many paths it could not read, and the CLI exits 2 rather than 0 when a
  surface could not be read — that conflation is the root of both prior false
  negatives. *Output is safe to paste into a public issue*: aggregate counts
  and at most opaque handles (a page `uid`, or a truncated SHA-256 of an
  address), never an address, a name, or a record path — a contacts-surface
  filename embeds a slugified address, so paths are withheld too, and no
  `--verbose` mode exists that could leak detail. *Every number is re-derived
  at run time* from the store passed in; no figure from athenaeum#849 or
  athenaeum#853 is hard-coded as an expected value. Items carrying a bounce
  fact but no join key (a wiki page with no `uid`, a mark on a record with no
  `uid`) are counted and reported separately rather than hidden, so the report
  never overstates how much of the store it actually compared. Read-only:
  nothing writes to either surface.

- **The pii bounce mark and the wiki `bounced:` field consumers read are now
  joined (athenaeum#852).** The two surfaces recorded bounce facts
  independently of one another and shared no key: a wiki page carries a `uid`
  and no address, and the athenaeum#765 slug-keyed bounce record carried an
  address and no `uid`, so a pii mark could not reach the field consumers
  actually read. The excluded person record is the only object holding both
  halves — which is why athenaeum#850 (putting the mark onto that record) is a
  hard dependency rather than a tidiness fix. New `athenaeum.bounce_join`
  implements the chain `identifier -> person record -> uid -> wiki page`:
  `join_identifier` walks it forwards, `deliverability_for_page` walks it
  backwards from the page a consumer holds, and both stop at the first missing
  link and report how far they got rather than raising, because a broken chain
  is the ordinary case on a real store. Per P6 the direction is **read-time**:
  athenaeum does not write `bounced:` onto wiki pages — that would contradict
  the standing `wiki-contacts-no-email` principle, add a second writer to a
  field an external producer already writes, and converge the surfaces by
  copying. The evidence classes are kept **separate** rather than collapsed
  into one boolean: `Deliverability.hard_bounced` is the pii mark (`5.x.x`
  only) and `Deliverability.wiki_verdict` is whatever the wiki field says,
  verbatim and unclassified. That is what stops a transient `4.x` or a
  list-verification verdict (`MailboxDoesNotExist`, `DomainHasNullMx`) from
  being promoted to a hard bounce — even a wiki verdict that *would* match the
  detector is carried, never re-recognized, because convergence is a join on a
  shared key and not a re-recognition pass. No new state is recorded on either
  surface; the join only reads. New `docs/bounce-surface-convergence.md`
  (linked from `docs/deprecated-email-tracking.md`) states the surfaces, the
  key, the asymmetry, the direction and the alternative that was rejected, and
  the re-report contract — including why last-writer-wins is intended for a
  re-bounce and what first-writer-wins would have cost.

- **`athenaeum bounce-contract` — check a candidate bounce note against the
  Tier-0 gate before submitting it (athenaeum#854).** The deterministic
  hard-bounce gate (`librarian.tier0_bounce_mark`, athenaeum#765) is
  deliberately narrow: it fires only when the note's own frontmatter carries a
  non-empty `observed_at` and `source` and the body holds exactly one
  email-shaped token plus an RFC 3463 `5.x.x` code. A note missing any of
  those is **not rejected** — nothing is rejected — it falls through to the
  Tier 1/2/3 reasoning path and compiles as an ordinary free-text memory. For
  one note that is the design. At backfill volume it is a contamination
  vector: a producer emitting a few hundred near-miss notes compiles a few
  hundred addresses-plus-diagnostics into the corpus and never sees a failure
  signal. (The near-miss is not hypothetical: a survey of historical bounce
  evidence found only 13 of 189 entries carrying a `5.x.x` code at all.) Until
  now the required shape was discoverable only by reading the gate and
  inferring it — which a producer in another repository cannot depend on. New
  `docs/tier0-bounce-note-contract.md` documents the shape as an implementable
  contract, and new `bounce_contract.check_tier0_bounce_conformance` answers
  "would Tier 0 recognize this?" for a candidate note **without writing
  anything** — no mark, no intake submission, no store mutation, no network,
  no LLM call — reporting every unmet condition with a stable machine token
  (`missing_observed_at`, `missing_source`, `several_email_identifiers`,
  `missing_hard_bounce_code`, …) rather than a bare boolean, so a producer can
  fix a batch. Shipped as a CLI (`--text` / `--file` / stdin, `--json`, exit 2
  on decline) because the consumer is a producer in another repository and the
  nearest one is TypeScript. Drift between the contract and the gate is
  prevented structurally, not by convention: `tier0_bounce_mark` now calls the
  same check for its whole recognition decision and does nothing but write the
  mark on top of it, the body predicates are the production ones called
  directly, and tests pin both the check-vs-gate seam (against the real gate
  in `dry_run`) and the doc-vs-code reason table. No rejection path is added
  and no runtime behaviour on the intake path changes — a non-conforming note
  still falls through exactly as before.
- **`librarian.non_intake_sources` — exclude a `raw/<source>/` directory from
  entity intake (athenaeum#843).** `discover_raw_files` globbed every `*.md`
  and `*.jsonl` under each `raw/<source>/` and treated it as ordinary entity
  intake, with exactly one hardcoded exception (`answers`, athenaeum#414).
  There was no operator-controlled way to mark a whole source directory as
  "not memory content, do not classify" — so any tool that writes its own
  operational artifacts into `raw/<source>/` (the only tree `remember()`
  content also uses) silently became nightly LLM-classification load, with no
  remedy short of another patch to `discover_raw_files`. The live instance:
  `raw/contact-sync/` holds the contact-sync tool's `sync-*.jsonl` action logs
  (several multi-MB, one 9.7MB), `dedup-*.log` and state files. Those `.jsonl`
  logs match the glob and are not batch envelopes, so they entered
  `tier2_classify` → `tier3_write` as memory content — and `RawFile.content`
  has no size guard, so a multi-megabyte log was read whole and handed to the
  classifier. A new `sync-*.jsonl` lands daily, so the backlog grew nightly
  whether or not the entity phase ever reached it (155 deferred files in that
  one directory as of 2026-08-08, oldest since 2026-05-10). The new knob is
  resolved by `config.resolve_non_intake_sources` with the same tolerant
  contract as the other `librarian.*` list knobs (missing / non-list /
  non-string members degrade to empty, never raise), and `discover_raw_files`
  now takes an optional `config` and skips a listed `source_dir.name` before
  any glob work — the general form of the `answers` skip, which is unchanged.
  Default-empty: an unconfigured install discovers exactly what it did before.
  `config` is threaded at all five call sites (`librarian.run` ×2,
  `_cmd_drain`, `drain`, `status`) so every backlog count agrees on what will
  actually be processed. Which directories to exclude in a given store stays
  an operator decision — this ships the mechanism, not a policy.

### Changed

- **CI Python matrix pinned to 3.13 only; `requires-python` and trove
  classifiers now match (athenaeum#815).** The `test` job in
  `.github/workflows/ci.yml` dropped its three-leg
  `["3.11", "3.12", "3.13"]` matrix to a single `["3.13"]` entry, and the
  `types` (mypy) job's `Set up Python` step and `[tool.mypy] python_version`
  in `pyproject.toml` moved from 3.11 to 3.13 to match. This is a product and
  maintenance decision, **not a cost saving** — athenaeum is a public repo, so
  GitHub-hosted Linux runner minutes are free and unlimited here; the org paid
  nothing for the two extra legs. What the extra legs cost is maintenance
  surface: every dependency bump had to satisfy three interpreters, every
  version-conditional path had to be kept alive, and every red leg had to be
  triaged even when the others were green, for a compatibility promise this
  single-maintainer project never committed to keeping. `requires-python` in
  `pyproject.toml` moved from `>=3.11` to `>=3.13`, and the `Programming
  Language :: Python :: 3.11` / `:: 3.12` trove classifiers were removed
  (`:: 3.13` and the generic `:: 3` remain), so packaging metadata no longer
  advertises support CI does not verify. `.github/workflows/oss-readiness.yml`
  and `.github/workflows/release.yml` each pinned a standalone
  `python-version: "3.12"` for packaging/build steps unrelated to the test
  matrix; both moved to `"3.13"` to stay consistent with the new policy.
  `.github/workflows/evals.yml` (pins 3.12) and
  `.github/workflows/scorecard.yml` were left untouched — explicitly out of
  scope per the issue. A new "Supported Python version" section in
  `CONTRIBUTING.md` documents the one-version-at-a-time policy and states
  that bumping it means changing the matrix, `requires-python`, and the
  classifiers together. **Bumped MINOR (0.16.40 -> 0.17.0), not PATCH:**
  raising `requires-python` to `>=3.13` means `pip install athenaeum` now
  refuses to install this release on 3.11/3.12, which is a break for any
  consumer still on those interpreters.

### Fixed

- **The hard-bounce mark now annotates the record that already lists the
  address, instead of minting a duplicate beside it (athenaeum#850).**
  `pii.mark_bounced` resolved an incoming address to a record by email SLUG
  alone, so an address that already had a person record on the excluded
  surface (migrated by athenaeum#427/#437, listing it under `emails:`) got a
  *second*, slug-keyed record — with the deliverability fact on the one
  nothing else reads. That is a silent-wrong-answer shape rather than a
  duplicate-file annoyance: a consumer reading deliverability off the person
  record got a confident "not bounced" for an address that demonstrably
  bounced. The mark now asks `pii.resolve_contact_record` which existing
  record lists the address — across `emails:` / `former_emails:` /
  `alt_emails:`, case-insensitively and deterministically — and only mints
  the slug-keyed record when none does. On a person record the close is
  recorded **per identifier** (a `identifier_validity:` entry carrying
  `identifier`, `valid_until`, `observed_at`, `bounce_diagnostic`, `source`)
  rather than as a top-level `valid_until`, because that record holds several
  addresses and closing it as a whole would assert the *person* expired —
  trading one silent-wrong-answer for another. Both shapes are the same
  representation (athenaeum#308's valid-time close, not a new status enum),
  differing only in scope; new `pii.is_bounced_identifier` is the address-level
  read predicate that reads either, and never lets a record answer for a
  neighbouring address. Pairs the old resolution already created are
  repairable without a destructive step: `pii.fold_orphaned_bounce_marks`
  finds a slug-keyed record whose identifier some person record lists, replays
  the mark onto that record through the same `mark_bounced` path a live bounce
  takes, and reports a count — deleting nothing (the slug-keyed record keeps
  its own mark and gains a `folded_into:` stamp) and idempotent on a re-run.
  `docs/deprecated-email-tracking.md` gains the section a verifier actually
  needs: the mark is a valid-time close, **not** a `bounced:` field, so
  `grep '^bounced:'` over the contacts surface returns 0 even after a fully
  successful mark — and the mirror-image error, where the same grep over the
  wiki surface returned 0 where the correct answer was 189 and was used as
  corroboration. Both directions are recorded, because neither absence is
  evidence.

- **`examples/claude-code/` (the installable Claude Code hook kit) now ships
  in the built wheel, not just in git and the sdist (athenaeum#793).** A top-
  level `examples/` lives outside `packages = ["src/athenaeum"]`, so
  hatchling silently dropped it from the wheel — the same files-allowlist
  trap `pyproject.toml` already documents (and had already fixed) for
  `skills/`. Since `pip install athenaeum` resolves a wheel, the hook kit —
  `settings-snippet.json`, the four hook scripts, and the env-var reference —
  was invisible to essentially every installing user, including the
  `user-prompt-recall.sh` correctness fix from athenaeum#792. Force-included
  `examples` -> `athenaeum/examples` in `pyproject.toml`, mirroring the
  `skills` force-include, with the Option A vs. Option B reasoning recorded
  in the comment block next to it. New coverage in
  `tests/test_skill_packaging.py::test_built_wheel_contains_claude_code_hook_kit`
  builds a wheel and asserts the kit is present, mirroring the existing
  `test_built_wheel_contains_skill` for `skills/`.

- **Qualify three bare issue references that failed `public-safe-lint`
  (athenaeum#768).** The reconciliation pass landed two references to
  athenaeum#765 in `docs/deprecated-email-tracking.md` and one to
  athenaeum#712 in `CHANGELOG.md` without a repo prefix, tripping the
  `bare-issue-ref` rule on `develop`. All three now carry the `athenaeum`
  prefix the rest of the document already uses.

- **Sweep stale pointers to the golden files deleted by athenaeum#561
  (athenaeum#799).** athenaeum#561 replaced the single-prompt golden
  snapshot with the multi-prompt golden set, deleting
  `tests/data/resolve_system.txt` and `tests/test_resolve_system_snapshot.py`
  — but seven in-tree comments/docstrings still pointed at the deleted
  pair, including a DRIFT GUARD comment in `src/athenaeum/resolutions.py`
  that a doc author transcribed faithfully into `docs/field-corrections.md`
  §6.1 (athenaeum#796), shipping a normative guard citing two nonexistent
  files (caught and corrected in athenaeum#798). Corrected all seven sites
  — the module docstring and `_RESOLVE_SYSTEM` drift-guard comment in
  `src/athenaeum/resolutions.py`, `docs/conflict-resolution.md`,
  `docs/provenance-shape.md`, `tests/regression/test_live_prompt_regression.py`,
  and two docstrings in `tests/test_parse_response_routing.py` — to point at
  the live pin, `tests/data/prompts/resolutions.resolve_system.txt`
  (`tests/test_prompt_goldens.py::test_prompt_matches_golden`). The
  `resolutions.py` drift-guard comment now also names which sites are
  INDEPENDENT (hand-edited, bound by a test) versus DERIVED (regenerated
  and already pinned), matching the split `docs/field-corrections.md` §6.1
  uses, so the golden is no longer described as something to hand-maintain.
  Left the intentional historical mentions alone (the `docs/field-
  corrections.md` §6.1 parenthetical that exists to warn readers off the
  stale pair, and `tests/test_prompt_goldens.py`'s own "both removed"
  header). New coverage in `tests/test_drift_guard_paths.py` parses every
  DRIFT GUARD comment under `src/athenaeum/` and asserts each referenced
  `tests/*.py` path actually exists, so a stale guard fails CI instead of
  silently misleading the next editor.

### Documentation

- **Correct `docs/deprecated-email-tracking.md` — Voltaire is the reporter,
  drop the bespoke schema, mark Q1/Q2 superseded (athenaeum#768).** Three
  corrections to the athenaeum#565 design doc, verified against the live code
  2026-08-05: (1) the doc's Q3 named maecenas as the bounce-fact reporter,
  contradicting its own line describing maecenas as "a campaign tool" that
  shouldn't need to know about librarian bookkeeping — the actual chain is
  Voltaire detects (`voltaire/src/tiers/bounce.ts`) → updates Google Contacts
  → reports the fact to athenaeum as ordinary raw intake → the librarian
  marks the address bounced; maecenas only hosts the `handle_bounce.py`
  script Voltaire invokes and consumes suppression state at send time
  (maecenas#42 tracks fixing that script). (2) Q3/Q5 specified a dedicated
  `type: email_bounce` YAML schema and a special-cased librarian intake
  path — not generalizable for an OSS knowledge system with many fact types;
  corrected to the existing free-text `remember()` contract, and Q5 now
  notes the gcontacts read moved to the Voltaire side (voltaire#117). (3) Q1
  (the `_deprecations.jsonl` ledger) and Q2 (the archive-vs-delete
  disposition knob) are marked superseded in place, dated 2026-08-05, rather
  than deleted — pointing at athenaeum#712 (the v6 verdict ledger) and
  athenaeum#709's "zero destructive operations without a ledger entry"
  respectively. Also records the outcome-vocabulary gap (Voltaire's
  three-valued detection vs. Q1's two-valued `status`, voltaire#81) and notes
  Q4's optional lint issue (athenaeum#767) was closed as not planned because
  its premise (diffing the now-superseded ledger) no longer holds. **Reconciled
  a second time the same day, once athenaeum#765 actually merged (PR
  athenaeum#826):** the doc's Q1 and Q3 previously described the eventual
  mark as "whatever athenaeum#712's verdict ledger produces" — the shipped
  code doesn't wait on athenaeum#712 at all. It reuses athenaeum#308's existing
  `valid_until` claim-validity close directly (`pii.mark_bounced` /
  `pii.is_bounced`), gated by a new LLM-free Tier 0 branch
  (`librarian.tier0_bounce_mark`) that requires the raw note's own
  `observed_at` + `source` frontmatter and a body matching
  `pii.detect_hard_bounce_fact` (exactly one email token, an RFC 3463 `5.x.x`
  code). Q3's `remember()` contract example was also corrected: per-claim
  provenance rides `sources`, not `source` (the latter only selects the
  `raw/<session>/` landing directory) — verified end-to-end against
  `remember_write()` + `tier0_bounce_mark()`, matching
  `tests/test_bounce_mark.py`. Docs-only; no code changed.

### Fixed

- **Merge-proposal suppression messages print threshold comparisons at a
  precision that makes the printed comparison decidable (athenaeum#804).**
  The cohesion-floor suppression log line asserted `mean pairwise 0.60 <
  min_merge_mean_similarity=0.60` — false as printed, because both values
  were rounded to 2dp for the message while the comparison itself ran on
  the unrounded floats (e.g. an actual 0.5996 < 0.6000). The underlying
  comparison was confirmed correct all along; this is a reporting-only fix,
  not a behaviour change. `_merge_proposal_suppression_reason`'s
  `single-linkage chain`, `low cohesion`, and `low confidence` messages now
  print their compared values at 4 decimal places instead of 2 (the
  `over-cluster` message was already exact — it compares integers). New
  tests in `tests/test_merge_proposal_gates.py`
  (`TestSuppressionReasonPrecision`) pin the new format and assert a value
  just below a floor renders distinguishably from the floor itself; this
  unblocks the operator adjudication athenaeum#697's AC4 asks for.

- **Wiki-page dedup formation is confirmed complete-linkage, and the stale
  comments claiming otherwise are fixed (athenaeum#803).** `wiki_dedupe`'s
  `find_wiki_page_clusters` / `propose_wiki_page_merges` already routed
  through the shared `clusters.cluster_auto_memory_files` — the SAME
  formation routine athenaeum#681 made complete-linkage on the raw-source
  clusterer — but `wiki_dedupe.py`'s module docstring and an inline comment
  in `propose_wiki_page_merges` still described it as "the SAME
  single-linkage clusterer," which is exactly the drift athenaeum#803 asked
  to guard against: a reader trusting that stale comment could "fix" an
  already-fixed path a second, divergent way. Both are corrected to
  describe complete-linkage formation and explicitly call out that this
  logic is intentionally shared with the raw-source clusterer, not forked.
  New tests in `tests/test_wiki_dedupe.py`
  (`TestWikiClusterFormationIsCompleteLinkage`) pin the property through the
  wiki-page entry points specifically: a synthetic single-linkage chain of
  wiki pages does not form one giant cluster, every multi-member wiki
  cluster's `min_pairwise_score` (not just `centroid_score`) clears the
  threshold, and a 7-page chain that would have been one over-cap
  single-linkage component instead reaches `wiki/_pending_merges.md` as
  legitimate small proposals with zero `over-cluster` suppressions and no
  member of a legitimate pair lost.

- **`athenaeum push-metrics baseline` no longer writes an unconditional
  snapshot, and refuses to write a placeholder against a dead instrument
  (athenaeum#795).** The write to `docs/memory-model-measurements.md`
  previously happened before the `--json`/text branch and regardless of
  whether the baseline had anything meaningful to say — `--json` only
  redirected stdout, so there was no read-only way to check whether a
  baseline was computable. That caused a real incident: a run with `--json`
  meant purely as a check silently appended a dated placeholder snapshot,
  went unnoticed for ~2 hours, and had to be reverted with a correction
  comment on athenaeum#711. Two fixes: (1) a new `--dry-run` flag computes
  and displays the baseline (text or `--json`) without touching
  `--docs-path` at all — `--json` itself is unchanged, a stdout-format
  concern only, and does not by itself suppress the write; (2) a baseline
  with zero reference-determination records (precision not computable) is
  now refused — `athenaeum.push_metrics.write_snapshot` raises `ValueError`
  naming what was missing, the CLI reports it on stderr and exits `1`, and
  nothing is written. Covered by
  `tests/test_push_metrics_cli.py::test_baseline_dry_run_does_not_write`,
  `test_baseline_dry_run_inspects_invalid_baseline_without_writing`,
  `test_baseline_json_alone_still_writes`,
  `test_baseline_empty_ledger_is_honest` (rewritten to assert the refusal),
  `test_baseline_default_docs_path_not_written_for_invalid_baseline` (the
  default relative `docs_path` — the thing that actually wrote into the
  repo during the incident — was previously exercised by no test), and
  `tests/test_push_metrics.py::TestWriteSnapshot::test_creates_new_file`
  (also rewritten to assert the refusal).

- **`examples/claude-code/user-prompt-recall.sh` no longer gates the LLM
  topic extractor on `ANTHROPIC_API_KEY` (athenaeum#792).** `query-topics`
  routes through `build_llm_client`, which honors `llm.provider` — under
  `claude-cli` it authenticates via the ambient Claude Code login and needs
  no key at all, and any client-build failure already falls through to the
  regex+stopword extractor, so the shell-side key check added nothing that
  failure path didn't already do. Its only effect was silently disabling
  the extractor for every `claude-cli` user. Removed the gate (with a
  comment explaining why it must not come back), corrected two stale prose
  claims in the same file, updated `README.md`'s 1Password section and
  troubleshooting table to name the `claude-cli` case, and recorded the
  back-port in `AUDIT.md`'s `knowledge-recall-on-turn.sh` row (fixed
  upstream in the maintainer's private fork by cwc#2177, 2026-08-06). New
  coverage in `tests/test_shell_hooks.py::TestUserPromptRecall` proves the
  extractor is attempted with no `ANTHROPIC_API_KEY` set, via a stub
  `$ATHENAEUM_CLI` so the assertion never risks reaching a real LLM.

- **Test suite no longer writes synthetic records into the real
  `~/.cache/athenaeum` cache-dir artifacts (athenaeum#791).** A prior fix
  (athenaeum#776) isolated the spend ledger with a session-scoped autouse
  *fixture* — but fixtures only wrap test EXECUTION, which runs after
  collection, so a module that resolves the cache dir at COLLECTION time (a
  module-level statement, or `pytest.mark.parametrize` decorator arguments
  pytest evaluates while importing the module) ran before any fixture could
  fire. `tests/conftest.py` now redirects `ATHENAEUM_CACHE_DIR` /
  `ATHENAEUM_SPEND_LEDGER` in a `pytest_configure` hook instead, which runs
  before collection begins — closing the gap structurally for every test
  module. `tests/test_thinking_seam.py`'s bespoke module-level workaround for
  this same gap is deleted, now redundant. Added: a grep-guard
  (`tests/test_cache_dir_resolver.py::test_env_literal_setting_home_also_sets_cache_dir`)
  that scans `tests/` for hand-built `env = {...}` dicts that set `HOME`
  without also setting `ATHENAEUM_CACHE_DIR` — `tests/test_shell_hooks.py`'s
  five such dicts (protected only incidentally, by `HOME` already being a
  tmp dir) now set it explicitly too. The single-artifact pollution canary
  (`tests/test_llm_schemas.py::TestObservationsPathIsolation::test_nested_pytest_run_does_not_pollute_real_ledger`)
  is generalized into
  `test_nested_pytest_run_does_not_pollute_real_cache_dir_artifact`,
  parametrized over all four cache-dir artifacts (`_llm_schema_observations.jsonl`,
  `spend.jsonl`, `_push_records.jsonl`, `_push_references.jsonl`) instead of
  naming one. New `tests/test_push_metrics.py::TestPushRecordsPathIsolation`
  pins that `push_records_path()`/`reference_records_path()` resolve outside
  the real cache dir under the suite fixture. `athenaeum push-metrics
  baseline` gained `--exclude-session SESSION_ID` (repeatable): an explicit
  operator denylist for known-synthetic sessions (e.g. one that ran the test
  suite and leaked fixture pushes into the live ledger — the athenaeum#791
  evidence: 75 of 120 push records at filing, all from one such session).
  Excluded sessions and their record counts are always reported on
  `BaselineWindow`/the CLI output/the committed snapshot
  (`excluded_sessions`/`excluded_push_records`/`excluded_reference_records`),
  even when empty, so a contaminated window is visible rather than silently
  folded into a clean-looking total. No change to the push-record schema or
  to what the recall path writes at runtime.

- **Three provider/cost hygiene fixes from the LLM-provider/cost audit
  (athenaeum#780, findings L7/L8/L10).** (1) `scripts/measure_contradiction_baseline.py`'s
  `_build_client` was the only direct `anthropic.Anthropic(...)` construction
  outside `provider.py::build_llm_client` in the whole repo — it silently
  could never use the `claude-cli` subscription backend. It now routes
  through `build_llm_client(config, max_retries=3)`, the same factory every
  other call site uses; `grep -rn 'anthropic\.Anthropic(' src/ scripts/`
  returns zero hits outside `provider.py`. The `api` path is unchanged
  byte-for-byte (`max_retries=3`, `None` when `ANTHROPIC_API_KEY` is unset) —
  covered by existing `provider.py` tests plus four new tests in
  `tests/test_measure_contradiction_baseline.py`. (2) `spend.py`'s module
  docstring wrongly described the per-turn `query-topics` recall extractor as
  one that "always talks to the SDK directly"; `query_topics.py` actually
  calls `build_llm_client(config, timeout=timeout, max_retries=0)` like every
  other call site, so it routes through the provider seam and is metered only
  when the resolved provider is `api`. Docstring reworded only — the ledger
  rows themselves were already correct (`build_record` derives `billing_mode`
  from the resolved provider argument, never from this prose), so no code
  changed. (3) Setting `ATHENAEUM_REASONING_T1_MODEL` / `T2_MODEL` (env, or
  `models.reasoning_t1` / `reasoning_t2` yaml) while
  `ATHENAEUM_REASONING_TIER_AUDITING_ENABLED` is off had no effect, with no
  signal at all. `get_t1_model` / `get_t2_model` now log a warning naming the
  flag to set whenever the knob is explicitly configured and auditing is
  off. This is **not** a claim that the tiers are dead: both T1 and T2 have
  real production callers in `merge.py` (athenaeum#518, athenaeum#602) that
  bypass the empty `DEFAULT_TIER_CHAIN` and run whenever an operator opts
  into that same flag — they are opt-in, not unwired. Six new tests in
  `tests/test_reasoning_tiers.py` cover the warning firing (env knob, yaml
  knob) and staying silent (knob unset; auditing on via env; auditing on via
  yaml).

### Added

- **Hard-bounce recognition + mark on the PII/contacts surface (athenaeum#765,
  implements athenaeum#565 as corrected by operator design direction
  2026-08-05).** A hard-bounce fact (identifier, diagnostic, observed date,
  source) arrives as an ORDINARY free-text raw-intake note via the existing
  `remember()` path — no new intake schema, `type:` field, or dedicated code
  path (a test asserts the raw frontmatter carries no `type` key at all).
  New `librarian.tier0_bounce_mark` is one more deterministic decline-or-apply
  branch in the SAME tier dispatch every raw file already goes through
  (`process_one`), mirroring `tier0_handle_upsert`'s shape: it requires the
  raw's own (pre-existing, generic) `observed_at` and `source` frontmatter
  fields plus a body recognized by new `pii.detect_hard_bounce_fact` — exactly
  one email-shaped identifier and an RFC 3463 `5.x.x` enhanced-status-code
  diagnostic. Deliberately excludes `4.x` (voltaire#81's "potentially stale"
  case) by construction, so a transient DSN can never be marked bounced; this
  issue is scoped to the hard-bounce case only. `pii.mark_bounced` upserts the
  mark onto the identifier's own per-address contact record on the excluded
  contacts surface — encoded as a valid-time close (`valid_until` = observed
  date, reusing athenaeum#308's existing claim-validity mechanism) rather than a
  new `bounced`/`deprecated` status enum, and never a new ledger file (no new
  `*.jsonl` under the contacts surface — reuse `_observations.jsonl`'s
  discipline if one is ever needed). Idempotent: re-reporting the identical
  fact is a byte-for-byte no-op; re-reporting a later bounce updates the same
  record in place. Nothing is deleted under any configuration. New
  `pii.is_bounced` is the single read-side predicate a consumer (recall, lint)
  calls to tell a bounced-but-present identifier apart from one never seen.
  Explicitly out of scope (cut, not deferred, per the issue): a new
  `_deprecations.jsonl` ledger, an archive-vs-delete disposition knob, and a
  `bounced`/`deprecated` status enum as the durable representation — each
  collides with in-flight work (athenaeum#689's "one ledger" decision, athenaeum#709's
  zero-destructive-ops rule, athenaeum#714's typed valid-time interval).

- **Deterministic field-correction fast path — the "Lane C" tier-0 conformance
  format (athenaeum#797, design lock athenaeum#794).** A writer that already knows the
  exact target entity, attribute, and value it wants changed (a
  delivery-status monitor, a bulk relationship-graph writer, a third-party
  enrichment service) can now drop a `.jsonl` batch into the ordinary
  `raw/<source>/` intake tree — no reserved subtree, recognized purely by
  shape (first line parses as `record: "batch"`) — and have it applied at
  tier 0, LLM-free, instead of paying prose compilation per fact. New
  `src/athenaeum/precedence.py` mirrors the existing 9-tier
  `SOURCE-PRECEDENCE TAXONOMY` prompt block in-process
  (`SOURCE_PRECEDENCE_TIERS` / `source_rank()`), bound to that prompt by a
  test that PARSES it rather than transcribing the expected tiers. New
  `src/athenaeum/corrections.py` is the tier-0 applier: target resolution
  (uid / type+name / registered handle via `registry.json`), the op
  semantics (`set`/`add`/`remove` with per-value `field_sources`
  attribution), the delta gate (re-applying an unchanged correction is
  byte-for-byte a no-op), the conflict policy (source-rank comparison,
  `observed_at` tie-break, escalation on an undated tie), the `monotone`
  suppression rule (any permitted writer may set a safety flag; only
  `user:` tier may unset one), and routing (a sensitivity-classified
  attribute lands on its mapped surface regardless of the destination the
  correction named; an attribute with no schema slot is aliased, held for a
  schema-amendment proposal, or recorded as prose). `intake.discover_raw_files`
  now globs `*.jsonl` alongside `*.md` so a correction batch is visible to
  discovery at all — the pre-existing gap this closes: a `.jsonl` file was
  previously invisible to it, so a malformed batch would have been "seen by
  nothing," not merely skipped. `librarian.run()` gains a
  `_run_correction_phase`, ordered after the run deadline is armed and
  before the entity tier phase, with its own runtime share
  (`librarian.corrections.runtime_share`) and a hard zero-LLM-calls
  assertion. Every failure to conform — an unparseable source, an attribute
  off the allowlist, a target resolving to zero or several entities, an
  unknown `schema_version` — raises a tier rather than rejecting: conformant
  records in the same batch still apply, and a non-conformant record's raw
  text reaches ordinary intake via a `_corrections_applied.jsonl`-ledgered
  handoff file, never silently dropped. The attribute allowlist
  (`librarian.corrections.fields`) is **empty by default** — a fresh
  deployment writes nothing cheaply until an operator opts a specific
  attribute in. See `docs/field-corrections.md` (design) and
  `docs/configuration.md`'s new "Field corrections" section (every
  `librarian.corrections.*` key).

- **`athenaeum spend --reprice` — recompute historical rows at current rates
  (athenaeum#788, audit finding L3).** Schema v2 (athenaeum#487) added
  `tokens_by_model` on the stated rationale that "`tokens x model` is the
  fact; dollars are derived", so a mixed-model row stays repriceable — and
  then nothing consumed it. `athenaeum spend` read each row's stored
  `estimated_cost_usd` verbatim, so correcting a rate (athenaeum#777's 6.67x
  Fable/Mythos under-report) or making rates config-owned (athenaeum#783)
  bought nothing *retroactively*. `--reprice` is that missing door: it
  recomputes each row from its per-model attribution against the current,
  config-aware rate table and reports the recomputed total alongside the
  stored one, with the delta. **Read-only** — the ledger is append-only by
  design, so repricing reports a corrected figure and never rewrites history
  (a test asserts the file is byte-identical after a run; rewriting in place
  would be a separate, explicitly-destructive command, deliberately absent).
  Rows with no per-model attribution stay *unpriceable*: counted per bucket
  and at the top level, their stored dollars surfaced as
  `unpriceable_stored_usd` (and named in the human report), never dropped
  from `record_count` and never silently priced at zero. The
  subscription/API/unknown split survives repricing intact — a subscription
  row is `$0.00` stored **and** repriced (repricing never turns subscription
  draw into money owed; what it corrects there is the counterfactual
  `notional_usd`), and an `unknown` row (athenaeum#694) reprices inside its own
  bucket rather than folding into `api`. The delta is measured against
  `stored_usd_priceable` — the stored value of exactly the rows repriced —
  so unpriceable rows can never masquerade as a rate change. Per-model
  arithmetic is delegated to the new `models.cost_for_token_bucket`, which
  reuses `TokenUsage._cost_for` (athenaeum#239's cache multipliers,
  athenaeum#236's batch discount) so a reprice cannot drift from the formula
  that wrote the row. Strictly additive: without the flag the existing report
  and its `--json` consumer contract are unchanged.

- **`cache_control` breakpoints on the detector and resolver system prompts
  (athenaeum#790, buildable half of athenaeum#774).** The resolver's
  `_RESOLVE_SYSTEM` already carried an ephemeral `cache_control` breakpoint
  (athenaeum#230); the detector's `_DETECT_SYSTEM` (`contradictions.py`) did
  not, even though it is constant across every detector call in a run. Its
  `messages.create` call now sends the system prompt as a one-block list with
  `cache_control: {"type": "ephemeral"}`, mirroring the resolver's shape. Set
  unconditionally at the call site — declared backend capability, not the
  call site, decides whether it is honored:
  `capabilities_for("api").honors_cache_control` is `True` (the SDK passes it
  through unchanged) and `capabilities_for("claude-cli").honors_cache_control`
  is `False` (`_text_from_system` strips it, unchanged by this issue). No
  provider is switched by this issue — the `claude-cli` path and its existing
  golden/parity tests are unmodified, and the golden-pinned
  `tests/data/prompts/resolutions.resolve_system.txt` /
  `tests/data/prompts/contradictions.detect_system.txt` compare prompt TEXT
  only, so this request-metadata-only change leaves both goldens unchanged.

- **Weekly subscription token limit + max-percent-per-day ceiling
  (athenaeum#785).** `ceiling_tripped()`'s four existing ceilings are correctly
  unit-split by billing path (subscription bounded in TOKENS, metered API
  bounded in DOLLARS) but all four are absolute — there was no way to express
  "no more than N% of what I've allotted myself this week." Adding a
  percentage sooner was blocked on having a denominator at all: Claude Code
  subscription limits are rolling-window and are not exposed to athenaeum as
  a readable quota, so nothing in `provider.py::_parse_envelope` or the
  ledger schema carries one. New `config.resolve_spend_weekly_token_limit`
  (`ATHENAEUM_SPEND_WEEKLY_TOKEN_LIMIT` env > `spend.weekly_token_limit` yaml
  > `None`) lets the operator STATE that denominator; new
  `config.resolve_spend_max_pct_per_day` (`ATHENAEUM_SPEND_MAX_PCT_PER_DAY`
  env > `spend.max_pct_per_day` yaml > `None`) is the percentage taken of it.
  Both mirror the existing four `resolve_spend_max_*` resolvers' shape via
  the same shared `_resolve_optional_positive_number` helper, including its
  malformed-input handling (`bool` / non-numeric / non-positive all fall
  through to `None`, never a nonsensical ceiling). When both are set,
  `ceiling_tripped()` enforces a FIFTH, independent ceiling — an effective
  daily subscription token cap of `weekly_token_limit / 7 * (max_pct_per_day
  / 100)` — reusing `_start_of_utc_day` for the day boundary (matching all
  four existing per-day ceilings; a rolling 7-day window is explicitly
  deferred, per the issue's design notes) and including prior spend already
  committed earlier the same UTC day, exactly like `spend_today()`'s existing
  per-day accounting. The breach message names both the derived daily
  ceiling and the declared weekly limit it came from, so the operator can see
  what the percentage was taken of (for example: `"per-day subscription
  percent-of-weekly ceiling reached (51,000/50,000 tokens today, 50% of
  weekly limit 700,000 tokens)"`). **Strictly opt-in, and setting only ONE of
  the two knobs is a no-op** — a weekly limit with no percentage has nothing
  to apply, and a percentage with no weekly figure has nothing to take a
  percentage of — so there is no partial-config surprise; both cases are
  covered by tests. **The unit split stays load-bearing**: this ceiling is
  token-denominated and gates the `claude-cli` SUBSCRIPTION path only — it
  never reaches the metered API branch of `ceiling_tripped()`, so an
  API-path run is unaffected by it (asserted by test). No conversion rate
  was introduced to blend the two into one number: `subscription` notional
  tokens and `api` real dollars remain two metrics this ledger never sums
  (athenaeum#487, cwc#1629). The existing four ceilings and their tests are
  UNCHANGED. Per-area/per-knob halting (stopping one module while another
  continues) and a rolling 7-day window are both explicitly out of scope,
  deferred to the per-knob provider-routing issue and a future additive
  change respectively.

- **Per-MTok pricing is now config-owned, with a startup preflight that fails
  loudly on an unpriced model (athenaeum#783).** Model pricing was the only
  cost-relevant knob with no config resolver — the rate table was a bare
  module constant (`models._MODEL_RATES_USD_PER_MTOK`), so a vendor price
  correction needed a code edit, version bump, full suite run, and deploy
  (unlike every other cost knob, which resolves `env > yaml > code default`
  via `resolve_model` / `resolve_max_tokens` / `resolve_thinking` /
  `resolve_provider` / the four `resolve_spend_max_*`). New
  `config.resolve_model_rates(config)` reads `athenaeum.yaml`'s
  `pricing.<model-id-prefix>: [input_usd_per_mtok, output_usd_per_mtok]`
  section (malformed entries — wrong arity, non-numeric, `bool`, negative —
  WARN and are dropped, mirroring `resolve_max_tokens`'s convention, not a
  new one). **Design decision (settled, not an overlay):** yaml REPLACES the
  active rate table wholesale rather than merging on top of the code table as
  a floor — an overlay would keep the code table as an invisible second
  source of truth, and an omission there (the athenaeum#777 Fable/Mythos incident:
  two models silently priced at the blended average, under-reporting spend
  6.67x) would keep silently going unnoticed. New
  `models.configure_model_rates(rates)` installs the resolved table as the
  process-wide ACTIVE table `TokenUsage.estimated_cost_usd` reads (starts as
  the code default so every existing call site that never loads config is
  unaffected); `models.model_has_price(model)` is a real prefix-match lookup
  with **no** blended fallback. New `config.preflight_model_rates(...)` —
  mirroring the `preflight_provider()` pattern rather than discovering the
  gap per-file at cost-calculation time — is wired into `athenaeum run`'s
  precondition gate (`librarian._run_preconditions`, right after the
  provider preflight): it resolves the model each of the six LLM-serving
  knobs (`classify` / `write` / `topic` / `resolve` / `reasoning_t1` /
  `reasoning_t2`, the same set `prompt_registry.KNOBS` defines for athenaeum#781)
  will actually use for this run, and exits rc 1 — naming the model AND the
  `pricing.<model>` config key to set — if any is unpriced under the active
  table, rather than starting and silently under-reporting. The blended
  fallback is UNCHANGED for genuinely untagged tokens (its original athenaeum#247
  purpose) — only a *tagged* model used by a real run can no longer reach it
  silently. `athenaeum init`'s default `athenaeum.yaml` now ships an ACTIVE
  (uncommented) `pricing:` section pre-populated with the current rate table,
  generated at write-time from the same `models._MODEL_RATES_USD_PER_MTOK`
  literal a maintainer edits (never hand-copied), so a fresh install prices
  correctly out of the box. Schema contract, spelled out in
  `resolve_model_rates`'s docstring: one rate per prefix, no mode dimension —
  a time-boxed promo rate (Sonnet 5's introductory rate through 2026-08-31)
  or a per-request-mode rate (Opus 5's `speed: "fast"` rate) cannot be
  expressed and are explicitly out of scope, per the issue.

- **Per-knob spend attribution — `tokens_by_knob` (ledger v3) + `athenaeum
  spend --by-knob` (athenaeum#781).** The ledger recorded `run_type` and `models`
  but never the model KNOB, so "what does the contradiction resolver cost
  me?" was unanswerable — `run_type` is coarse (`librarian` covers classify,
  write, and resolve in one bucket) and `models` doesn't discriminate (the
  `classify` knob is shared by `tiers.classify`, `contradictions.detect_system`,
  and `claim_kind.claim_kind_system`, which all resolve to the same default
  model, so a row tagged `claude-haiku-4-5-20251001` could be any of the
  three). `TokenUsage.add` / `add_tokens` / `add_batch_tokens` gain a `knob=`
  kwarg — mirroring the existing `model=` kwarg and its `per_model`
  accumulation exactly — that accumulates into a new `per_knob` dict; each
  real call site threads it from the SAME knob string it already passes to
  `config.resolve_model` (`tiers.py`, `contradictions.py`, `claim_kind.py`,
  `query_topics.py`, `resolutions.py`, `reasoning_tiers.py`, and `batch.py`'s
  `execute_batch`, which tags a whole batch submission at once since the
  tier-2 classify batch and the tier-3 write batch are each submitted
  separately). `spend.tokens_by_knob()` mirrors `tokens_by_model()` into a
  new ledger field `tokens_by_knob`, bumping `LEDGER_VERSION` to 3. Per the
  issue's explicit override note: `tokens_by_model` is NOT reshaped — it
  stays the superset of hestia's `cost-ledger.ts` `{input, output, total}`
  shape that lets one reader serve both ledgers (athenaeum#487, cwc#1627/cwc#1629)
  — `tokens_by_knob` is a SIBLING field, additive and athenaeum-only (a knob
  has no hestia counterpart). Pre-v3 rows (and any v3 row that tagged no
  knob) stay readable and are counted `knob_unattributed_records` in
  `summarize()`, mirroring the existing pre-v2 `unpriceable_records`
  treatment exactly — never dropped, still counted in `record_count` and
  their billing bucket. `summarize()`'s existing `subscription`/`api`/
  `unknown`/`record_count` shape is unchanged (only additive keys), so the
  `cicero`/`good-morning` sub-skills do not regress. New `athenaeum spend
  --by-knob` mirrors the `--by-model` / `--by-provider` output shape and
  keeps the subscription/API split intact within each knob bucket — never a
  blended total. The six knobs (`classify`, `write`, `resolve`, `topic`,
  `reasoning_t1`, `reasoning_t2`) are derived from
  `prompt_registry._META_ROWS` (the single source of truth) via the new
  `prompt_registry.KNOBS` constant rather than duplicated, and every one is
  exercised by a real-call-site test in the new `tests/test_knob_attribution.py`,
  with a drift-guard test pinning that set against `KNOBS`. `docs/configuration.md`'s
  reasoning-tier-auditing cost note (added by athenaeum#779, which explicitly
  said `--by-knob` "does not exist yet") now points at `--by-knob` instead of
  the `--by-model`-keyed-by-model-id workaround it previously documented.

- **Entity-phase heartbeat, per-call LLM timing, and failure-reason logging
  (athenaeum#800, buildable half of athenaeum#764's Finding 0).** The entity
  phase (raw-file intake, tier1-4 routing) was the one librarian phase with
  ZERO `librarian-heartbeat` coverage — a nightly run (`631aaade`) spent 85%
  of its 3,446s window here with nothing logged between its `start` line and
  its eventual budget trip, so athenaeum#764's "where did the time go" question could
  not be answered from the logs. `librarian.py`'s per-file entity loop now
  emits `librarian-heartbeat phase=entity` lines on the same contract
  `merge-detect`/`merge-write`/`wiki-dedupe`/`reresolve` already have
  (`status=start|tick|done`, `done`/`total`, `compiled`/`unchanged`/`error`,
  `unit=<raw file ref>`, cumulative `elapsed=`), with exactly one `tick` per
  raw file processed — the same per-unit differencing property that made
  `merge-detect` diagnosable. Separately, every entity-phase LLM call
  (`tier2_classify` and its two retries, `tier3_create`, `tier3_merge`,
  `tier3_merge_full` in `tiers.py`) now logs its own wall-clock under the new
  `librarian-entity-llm-call` marker via a `_timed_llm_call` wrapper around
  the existing `with_retry` call — unchanged request/retry/parse behavior,
  just timed — so a run summary reader can distinguish "few slow calls" from
  "many fast calls", which the aggregate `entity secs`/`calls` figure alone
  cannot. A file failure now also logs the reason (exception type + message)
  at WARNING with the file path under a new `librarian-entity-file-failure`
  marker — run `631aaade` recorded three failed files with no error text
  captured at any level the operator's log sweep caught, and the trailing
  "Failed files" summary names only the filename. Finally, the entity-share
  budget-trip WARNING now states which resource tripped alongside both
  `api_call_budget` numbers (`631aaade` tripped at 28/1200 calls — 2.3% of
  the call budget — while the prior message named only "entity phase runtime
  share exhausted", reading as call-budget exhaustion and misleading the
  first pass of the athenaeum#764 diagnosis). Observability only: no change to
  provider routing, budgets, runtime shares, or deadlines. New
  `TestEntityHeartbeat` in `tests/test_librarian_heartbeat.py` and
  `TestEntityLLMCallTiming` in `tests/test_tiers.py` cover the contract.

### Changed

- **`claude-cli` provider path now passes `--strict-mcp-config` to every
  `claude -p` spawn (athenaeum#775).** `ClaudeCliClient.__init__` already runs
  the subprocess from a neutral `cwd` so it does not inherit a project
  `CLAUDE.md` / `.mcp.json` — but that guard only reaches *project*-scoped
  config. User-scoped MCP servers declared in `~/.claude.json` still booted on
  every call regardless of `cwd`, including all nine of the operator's
  globally-configured servers — notably athenaeum's own MCP server, so the
  compile process was booting the server that reads the corpus it is
  compiling. `_build_argv` now appends `--strict-mcp-config`, which suppresses
  that user-scoped load. Per the issue's host-measured figures (operator host,
  2026-08-06, not reproduced here): ~14.7s user CPU dropping to ~6.0s and
  roughly 12k fewer tokens per call, with output verified byte-identical
  between the two argv variants on an identical JSON-returning prompt. One
  argv element; `capabilities_for("claude-cli")` is unchanged — this alters
  what the subprocess loads, not what the backend can honor.

### Documentation

- **Document the reasoning-tier screen (T1/T2) in install/config — default
  stays OFF (athenaeum#779).** The cheap-to-expensive T1/T2 merge-proposal
  screen (`athenaeum.reasoning_tiers`) has had real production callers since
  athenaeum#518/#602, but was undocumented in the install/config docs, so an
  operator drowning in the human merge queue had no signal a built, tested
  screening cascade already exists. New `docs/configuration.md` section
  "Reasoning-tier screening (T1/T2) — off by default" covers what T1 (cheap
  model, bounded ~100-word excerpts, reject-or-pass-up only, no write
  authority) and T2 (expensive model, full bodies, T1 pass-ups only) do; the
  auto-apply consequence stated prominently — a T2 safe-class `approve` calls
  `pending_merges.resolve_merge(auto_applied=True)` (`merge.py:1408`), writing
  a merge into the wiki with **no human review**, which is the actual reason
  the default stays off; the recommendation to enable it once
  `athenaeum merges count` / `athenaeum decisions count` shows the human queue
  outgrowing manual triage; the single `ATHENAEUM_REASONING_TIER_AUDITING_ENABLED`
  env var / `librarian.reasoning_tier_auditing_enabled` yaml key that gates
  both tiers together; the added LLM spend, visible via `athenaeum spend
  --by-model` (the issue's proposed `--by-knob` flag does not exist — the real
  flag buckets by resolved model id, not by knob name, corrected in the docs);
  and the `wiki/_reasoning_tier_decisions.jsonl` decision log
  (`tier`/`decision`/`reason`/`reason_code`/`model`/`proposal_id` +
  timestamp). README gets a matching install-section pointer and a corrected
  "Note on reasoning tiers" (it previously said "exactly one production call
  site," stale since athenaeum#602 added the T2 caller). `docs/configuration.md`'s
  own reasoning-tier-model footnote and `docs/authority-manifest.md`'s
  "one gated production caller" note carried the same staleness and are
  corrected too — `reasoning_tiers.py`'s own docstring already warns against
  calling the tiers "unwired" or claiming "no production caller." The default
  is unchanged: `resolve_reasoning_tier_auditing_enabled` still defaults OFF,
  now asserted directly by `TestResolveReasoningTierAuditingEnabled` in
  `tests/test_config.py`. Docs and tests only — no change to tier logic, the
  safe-class definition, or the shipped default.

- **`runlock`: correct the docstrings that made a normal residual lockfile read
  as a leak (athenaeum#763).** There is no lock leak — a diagnosis established
  that `release()` never unlinks the lockfile (by design, always), that a
  dead-PID residual lockfile blocks nothing (mutual exclusion is the kernel
  `flock`, not the file's contents), and that the reported "leak" was the normal
  steady state. The misreading was invited by the code's own docs, so the fix is
  documentation: `release()`'s docstring now states plainly that it does not
  remove the lockfile and that a residual dead-PID file is expected and harmless,
  with a comment recording **why** it must not unlink (the athenaeum#526
  orphan-inode race); `stop_on_deadline`'s docstring no longer lets "the run-lock
  is released" be misread as "the file should be gone"; the module docstring
  leads with a prominent "Reading a residual lockfile" note so a debugger meets
  the kernel-`flock`-not-contents explanation first; and `is_stale()` plus the
  `--force` break message now distinguish a **benign** dead holder (safe to
  ignore) from the **actionable** alive-but-stalled one. No behavior change — in
  particular, `release()` still does NOT unlink (adding that would reintroduce
  athenaeum#526).

- **Record why the `decision_conflict_hosting_migration` resolver golden stands,
  and hand the boundary to v6 (athenaeum#760).** A comment on the case in
  `tests/evals/data/resolver/cases.yaml` now records that the golden stays
  `keep_pick_winner` **because** neither member carries `valid_from` (so
  `_narrow_scope_interval` returns `None` and the model's `scope_a` cannot
  enact) — *not* because `scope_*` is the wrong class for superseded decisions
  in general — and that the underlying trap (a verdict that silently no-ops when
  a coordinate is absent) is superseded by v6 (athenaeum#714/athenaeum#715), so
  the prompt is not to be edited for it. The resolver-side `xfail(strict=True)`
  and its `_DISPUTED` entry in `tests/test_recorded_fixtures.py` **stay** (removing
  them needs a re-record, which athenaeum#715 will do when it removes the
  resolver), now with a one-line comment pointing at athenaeum#760/athenaeum#715
  so the mark reads as tracked, not an untracked failure. Comment-only: no prompt,
  registered golden, recorded fixture, or `docs/prompts.md` edit; no `--record`.

### Fixed

- **`claude-fable-5` / `claude-mythos-5` were missing from
  `_MODEL_RATES_USD_PER_MTOK`, so tagged spend under-reported 6.67x
  (athenaeum#777).** Both ids matched no prefix in the rate table and fell
  through to the blended fallback ($1.50 input / $7.50 output per MTok)
  instead of their real $10.00/$50.00 catalog rate. The sibling
  `_SAMPLING_PARAMS_REJECTED_PREFIXES`
  table had listed `claude-fable-5` since athenaeum#577 — its own comment says
  "maintain the two tables together," and the two had drifted out of step.
  Because `models.<knob>` in `athenaeum.yaml` is operator-configurable,
  `models.resolve: claude-fable-5` was a supported one-line config change that
  would have silently let a $10/day spend ceiling not trip until roughly $67
  of real spend. Latent today (no `DEFAULT_*_MODEL` points at Fable or Mythos
  yet), but live in both `develop` and `main` (byte-identical for
  `models.py`). Fixed by adding explicit `_MODEL_RATES_USD_PER_MTOK` entries
  for `claude-fable-5`, `claude-mythos-5`, and — for legibility, not because
  the prefix match was wrong — `claude-opus-4-8`, `claude-opus-4-7`,
  `claude-opus-4-6`, `claude-sonnet-4-6`, and `claude-haiku-4-5`. Two new
  tests in `tests/test_model_rates.py` guard the fix and the drift: a
  parametrized sweep asserting every catalog id resolves to a real
  (non-blended) rate, and a one-directional invariant asserting every prefix
  `_SAMPLING_PARAMS_REJECTED_PREFIXES` declares also resolves to a real rate
  in `_MODEL_RATES_USD_PER_MTOK` (the two tables answer different questions,
  so identical key sets are not required — but the sampling table may never
  reference a model the rate table has never priced). The Sonnet 5
  standard-vs-introductory-rate decision (Occam, 2026-07-31) is unchanged.

- **Suite-wide isolation of the spend ledger, pinned by tests (athenaeum#776).**
  A test run that resolves `spend.resolve_ledger_path()` with no override lands
  on the operator's real `~/.cache/athenaeum/spend.jsonl`: fixture model ids
  `yaml-topic-model` / `yaml-classify-model` / `yaml-write-model` were measured
  in 30 live rows dated 2026-07-15 through 2026-08-02. That is not merely
  untidy — synthetic rows inflate the totals `athenaeum spend` and the
  `cicero/good-morning` sub-skills report, they are permanently unpriceable (no
  rate-table prefix matches them, so a repricing pass can never resolve them),
  and they contaminate a live guardrail: `spend_today()` feeds
  `ceiling_tripped()`, so a local suite run on the same UTC day as a real run
  moves a ceiling that bounds REAL spend. A **session-scoped, autouse** fixture
  in `tests/conftest.py` now redirects both `ATHENAEUM_CACHE_DIR` and
  `ATHENAEUM_SPEND_LEDGER` into a `tmp_path_factory` directory for the whole
  suite, and the function-scoped `_isolate_cache_dir` re-points the ledger into
  each test's own directory on top of it. Session scope is what this adds:
  athenaeum#750's function-scoped fixture already covers every write made
  *inside* a test function — measured, and it is why a suite run on the current
  tip leaves the live ledger byte-identical — but it cannot cover a
  session/module-scoped fixture, a collection-time import, or a config carrying
  `spend.ledger_path`, each of which still resolved to the live ledger. Five new
  tests pin the invariant end to end (path resolution, an actual `record_spend`
  write, both env vars, and resolution from session scope) so it cannot regress
  silently. Test-only change — no production code was touched.

- **The C4 contradiction-detector / resolver loop now ticks the run-lock
  heartbeat, so a long, healthy C4 phase no longer looks wedged (athenaeum#762).**
  `merge_clusters_to_wiki`'s C4 loop refreshed only the log-only `merge-detect`
  `PhaseHeartbeat`; it never called the run-lock heartbeat callable
  (`RunLock.heartbeat`, threaded from `run()` via `ctx.heartbeat`) — that
  callable was not even a parameter of `merge_clusters_to_wiki`. A run that
  spent 25+ minutes in C4 therefore stopped refreshing
  `~/knowledge/.athenaeum.lock`'s `heartbeat:` field and became
  indistinguishable from a wedged run to every consumer that reads heartbeat
  age (the athenaeum#397 contended-acquire auto-break). The callable is now
  threaded through `_compile_auto_memory` → `merge_clusters_to_wiki` and fired
  at the per-cluster **and** per-chunk boundaries (the finest granularity, right
  around the slow `claude -p` detector/resolver calls), plus per-pair in the
  cross-scope similarity sweep (the other long C4-family loop). The refresh is
  best-effort by contract: a failure is swallowed and never breaks or slows the
  run. The 21600s (6h) auto-break threshold is **left unchanged** — see the PR
  for the reasoning (the per-chunk tick delivers the observability benefit
  immediately; lowering the threshold is a separate tuning decision best made
  once real post-fix per-cluster cadence is observed over several nightly runs).

### Changed

- **Golden case `tool_choice_editor` adjudicated: `conflict_type` corrected
  `prescriptive` → `factual` (athenaeum#737).** The first live fixture recording
  (athenaeum#610) surfaced the detector returning `factual` where the golden
  expected `prescriptive`. On inspection the model is right: both members state
  *where* client-facing docs live (Notion vs Google Docs) rather than directing
  anyone to act, which is the detector prompt's own definition of `factual`, not
  `prescriptive` ("opposing guidance"). The golden in
  `tests/evals/data/detector/cases.yaml` is corrected with a comment recording
  *why*, and `tool_choice_editor` is removed from the `_DISPUTED` strict-xfail map
  in `tests/test_recorded_fixtures.py` — its detector replay now passes without an
  xfail. Offline/detector-only: no prompt edit, no `prompt_hash` movement, no
  re-record. The resolver-side `decision_conflict_hosting_migration` xfail is
  untouched (it is athenaeum#760's to record, and removing it here needs a
  re-record).

### Fixed

- **`stop_on_deadline` now performs the post-run push — the phase-boundary /
  C4 deadline exit no longer strands commits (athenaeum#761).**
  `RunContext.stop_on_deadline` committed partial progress and returned `124`
  to `run()`'s caller *before* `_run_finalize_phase`, so `librarian.push_after_run`
  never fired on the phase-boundary path (a `RunDeadlineExceeded` from the merge
  pass, or a between-phase deadline check — the C4 route). Every run that tripped
  the wall-clock deadline in that position committed locally and never pushed,
  silently defeating the move-then-retire recovery guarantee (athenaeum#284) —
  26 commits stranded on the operator host over three days (2026-08-02→05). The
  push now fires from inside `stop_on_deadline`, immediately after the partial
  commit; every `stop_on_deadline` call site (wiki-dedup boundary, `merge_only`
  catch, auto-memory catch, post-compile boundary) routes through it, so all
  phase-boundary exits are covered by one change. The push stays non-fatal (a
  `git push` failure never changes the `124` exit code), `--dry-run` still never
  pushes, and the entity-loop deadline path (which falls through to the
  finalize-phase push) still pushes exactly once, not twice. `_maybe_push_after_run`
  additionally logs a single line when an opted-in push is *skipped* (dry-run, no
  pre-run HEAD, or no new commits) so a skipped push is no longer silent — an
  operator can now tell a push that happened from one skipped (and why) from one
  that failed.

### Added

- **`claim_kind.stamp_claim_kind` wired into the nightly auto-memory intake
  phase (athenaeum#742).** The intake-time epistemic-kind classifier (Haiku,
  one cheap call per not-yet-classified raw auto-memory file) had no
  production caller — only tests exercised it — so
  `resolutions._stance_attribution_verdict`'s opinion-attribution
  short-circuit almost never fired. `librarian._stamp_unclassified_claim_kinds`
  now runs from `_run_auto_memory_phase` right after C1 discovery and before
  the C2 cluster pass, using the run's existing shared `anthropic` client
  (the same one C4 contradiction detection reuses) — no new client, no new
  config knob. Idempotent and fail-open throughout: an author-supplied
  `claim_kind:` is never re-classified or overwritten, a file with no client
  or a classification failure is left unstamped, and the classifier's own
  import stays lazy at the call site (never at `athenaeum.librarian` module
  scope) so it never reaches the 3-second-budget recall hot path
  (`query_topics.extract_topics`). Once stamped, a `claim_kind` round-trips
  through `discover_auto_memory_files` on the next read and is what the
  resolver's `attribute_both` action now sees on genuine opinion pairs.

- **`athenaeum merges propose-fold` — an operator CLI for the fold path (athenaeum#747).**
  Consolidating duplicate entities previously required hand-constructing a
  `_pending_merges.md` block through the Python API and calling `resolve_merge`
  directly — the exact route by which the athenaeum#748 `write_kind`
  misclassification was introduced. `merges propose-fold --into <canonical-page>
  --source <page> [--source ...]` now proposes a fold from named source pages
  into a named canonical page, deriving every field the mechanics depend on:
  `merge_target_name` is read from the canonical page's `name:` frontmatter (never
  taken as a string, so the target slug and the page cannot disagree), and
  `write_kind` is derived (athenaeum#748), never accepted as an argument. The draft
  defaults to the canonical page's current text **verbatim** — a fold's value is
  alias bookkeeping and source deletion, not a body rewrite — with `--draft-file`
  for a genuine content merge. Dry-run by default (prints the canonical page, each
  source, and the derived `write_kind`; writes nothing); `--apply` queues via the
  existing `write_pending_merge`, and approval stays the separate, unchanged
  `resolve_merge` step (no second write path). Refuses when `--into` is not an
  existing wiki page, when a `--source` equals `--into`, or when the canonical
  page's filename is not its own name-slug (which would classify as `create-merged`
  and silently not fold — rename it first).
- **`storage migrate-pii --rename-only` + target-scoped renames (athenaeum#745).**
  `--rename-name-email` (athenaeum#505's name-is-an-email slice) was reachable only
  through `--all`, which always ran the body-text contact-data migration in the
  same pass — so a rename could not be applied without also accepting every
  body migration that corpus-wide pass would perform. It was silently skipped
  under `--glob` and never ran at all under `--page`. This blocked real work:
  athenaeum#437's residue is entity pages whose `name:` **is** a person's address, but
  applying the rename required an `--all --apply` that would also have migrated
  57 phone "findings", almost all detector false positives — redacting real
  prose, the exact failure athenaeum#691 spent two restore passes repairing. Now
  `--rename-only` runs the slice alone; the slice is scoped by whichever
  selector is in use (`--page` / `--all` / `--glob`), with `--all` unchanged as
  the default corpus-wide set; and `--list-deferred` enumerates the pages
  athenaeum#505 deliberately refuses to name — the operator's manual-naming worklist,
  previously only a count. On the single-page path the rename runs **before**
  the body migration, since a rename moves the file and planning against the
  pre-rename path would read a target the rename is about to invalidate.

### Fixed

- **The test suite no longer pollutes the production observation ledger (athenaeum#750).**
  `record_observation()` (`athenaeum.llm_schemas`) resolves its ledger path via
  `resolve_cache_dir(cache_dir=None)`, whose precedence is `arg >
  ATHENAEUM_CACHE_DIR env > default (~/.cache/athenaeum)` — but `tests/conftest.py`
  isolated nothing, so any test that drove a parse site through
  `observe()`/`record_observation()` with no explicit `cache_dir` fell through to
  the real `~/.cache/athenaeum/_llm_schema_observations.jsonl`, silently appending
  test-run noise to the operator's production ledger on every local/CI run. A new
  autouse, function-scoped `tests/conftest.py` fixture (`_isolate_cache_dir`) now
  points `ATHENAEUM_CACHE_DIR` at a per-test tmp dir and defaults
  `ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED=0` for every test, so a newly added test
  cannot reintroduce the pollution without opting out explicitly; a nested-pytest
  regression test (`tests/test_llm_schemas.py`) spawns a real child `pytest`
  process and asserts the real ledger's line count is unchanged by the run.
  `tests/test_llm_schemas.py`'s own tests, which specifically assert observations
  ARE recorded, opt back in via its existing `_isolate_observation_ledger`
  fixture (now also re-enabling `ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED=1`
  explicitly rather than relying on unset-defaults-to-enabled). Bisecting a
  full-suite run surfaced one fixture-invisible leak the autouse fixture alone
  could not close: `tests/test_thinking_seam.py`'s `@pytest.mark.parametrize("label",
  sorted(_all_params()))` decorator arguments call real call sites
  (`classify_claim_kind`, `detect_contradictions`, …) at pytest COLLECTION time
  — before any fixture, autouse or otherwise, has run — so that module now sets
  `ATHENAEUM_CACHE_DIR` / `ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED` at module
  import time, ahead of the parametrize decorators. **The ledger is trustworthy
  (free of test-run pollution) only for records written from 2026-08-05
  onward** — rows already present in an existing ledger from before that date
  may include test-suite noise; no existing ledger file was moved, quarantined,
  or otherwise touched by this change.
- **Merge `write_kind` is derived and validated, not trusted from the caller (athenaeum#748).**
  `write_pending_merge` accepted `write_kind` as a caller-supplied string and
  stored it unvalidated; `resolve_merge` then dispatched on it and, for
  `fold-into-existing`, **deleted every source page**. A hand-set
  `fold-into-existing` whose target slug did not exist wrote the draft to a
  *new* page and deleted all sources — including the canonical page the fold
  was meant to fold into (the 2026-08-02 incident found while executing
  athenaeum#437). Three defenses now close this: (1) `write_pending_merge`
  **derives** `write_kind` from whether the target slug exists under the wiki
  root and keeps the parameter only as a validated override — a value that
  disagrees with the derived classification (or an unknown value) **fails
  closed** with a `ValueError` instead of storing a misclassified proposal;
  (2) `resolve_merge` **re-checks** target existence before the fold path and
  refuses with a distinct `fold_target_missing` error code (deleting nothing)
  when the target slug is absent, catching any legacy/hand-edited sidecar block
  that bypassed the write-time guard; (3) the fold's source-deletion step never
  deletes a source whose resolved path IS the canonical target page, as defense
  in depth beyond the existing slug filter. The classification now lives in a
  single source of truth (`pending_merges.classify_write_kind`), to which
  `merge._classify_merge_write_kind` delegates, so the proposal-time and
  write-time classifications cannot drift. Correctly-classified proposals behave
  exactly as before.
- **`transcript_verify` honors `CLAUDE_CONFIG_DIR` (athenaeum#723).**
  `DEFAULT_PROJECTS_ROOT` was hardcoded to `~/.claude/projects` and ignored the
  standard `CLAUDE_CONFIG_DIR` override (which relocates `~/.claude`), so in a
  custom-`CLAUDE_CONFIG_DIR` environment any transcript-root-dependent path
  silently read the wrong location and found nothing — flagged by Seer on
  athenaeum#722 (reference-determination under-reporting precision). A new
  `transcript_verify.default_projects_root()` resolves `<CLAUDE_CONFIG_DIR>/
  projects` (falling back to `~/.claude/projects` when unset/empty) at CALL
  time, and every caller — `verify_user_stated`, `classify_backfill_claim`, and
  `push_metrics.run_reference_determination` — routes through it, so all benefit
  automatically with no per-caller `projects_root` plumbing. `DEFAULT_PROJECTS_
  ROOT` is retained as an import-time snapshot for backward compatibility.
  Best-effort semantics are unchanged: a missing transcript is still an honest
  "cannot determine," never a fabricated figure.

### Security

- **`correct_a`/`correct_b` destructive auto-delete is gated on
  transcript-verified human authorship, not confidence (athenaeum#752).**
  `enact_resolution` DELETES the losing raw-memory member for these two
  actions — the resolver's most dangerous verdicts — and the only barrier was
  a confidence float (`resolve.auto_apply_threshold_per_action.correct_a`/
  `correct_b`, default `0.95`); models are poorly calibrated exactly at the
  top of their range, where a destructive auto-apply fires. The confidence
  gate is now **replaced** (not stacked) for `correct_a`/`correct_b`: at
  enact time, the WINNING member's claim (the side named by the resolver's
  `recommended_winner`) is classified against its origin-session transcript
  via `transcript_verify.classify_backfill_claim`, and auto-apply proceeds
  ONLY when the classified channel is `user-stated`. `agent-observed`,
  `inferred`, and `unavailable` (missing/rolled-off transcript — the most
  likely production path) all escalate to the human instead of deleting. The
  check re-derives the channel from the transcript on every call and
  **never** reads the member's own `source_type` frontmatter field — that
  field is self-declared by whoever wrote the memory (possibly the model
  itself), and trusting it would let a model grant itself deletion authority
  by emitting a string. The gate decision (channel + ref) is logged for
  every `correct_*` verdict, permit or refuse. `forget_a`/`forget_b` (a
  transient-member cleanup with no human assertion behind it) and
  human-ratified pending-question answers are explicitly out of scope and
  unaffected — see `docs/conflict-resolution.md` §14 for the full rule,
  scope boundaries, and known limits (substring match ≠ authorization;
  transcripts roll off; the transcript itself is not immutable).

### Added

- **LLM schema-observation is now measurable: durable ledger, denominators,
  parse-fail counting, class-tagged mismatches (athenaeum#724).** The M17
  observe-only instrument (athenaeum#570) could not measure what athenaeum#608
  needs: `observe()` sat *below* the parse guard (so total parse failures — the
  most severe mismatch class — were structurally invisible), nothing counted
  observations (so a `0` was indistinguishable from "nothing ran"), and the
  `query_topics` contract runs in the MCP server whose logging is retained
  nowhere. Now: every observation (clean or mismatched) is recorded to a
  durable append-only ledger `~/.cache/athenaeum/_llm_schema_observations.jsonl`
  (a real denominator); a total parse failure is counted as a `parse-fail`
  mismatch from the parse guard (`observe_parse_failure`, wired into
  `contradictions._parse_response` and `claim_kind.classify_claim_kind`); each
  mismatch carries its class (`extra-keys` / `missing-required` / `parse-fail` /
  `wrong-type`); and `aggregate_observations()` reports per-contract
  `{observations, mismatches, by_class, mismatch_rate}`, with a zero-observation
  contract surfaced as an explicit `no_data` row (rate `None`) — so "0 over 0"
  is never confused with "0 over 400". The ledger records only schema *shape*
  (field paths, error messages, unexpected key *names*) — never a field value.
  Observation stays strictly behavior-neutral and never raises; disable with
  `ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED=0`. `reasoning_tiers` T1/T2 parse
  boundaries are untouched. The `claim_kind` contract has no production caller
  (its stamper is unwired); it now surfaces as an explicit `no_data` row, and
  the wire-vs-remove decision is tracked in athenaeum#742.

### Fixed

- **push-metrics: read the session-id variable Claude Code actually exports
  (athenaeum#734).** athenaeum#711's push-record instrumentation read
  `CLAUDE_SESSION_ID` — a name Claude Code never sets (it exports
  `CLAUDE_CODE_SESSION_ID`). Because the guard is `if session_id:`, the id was
  always empty and **no push record was ever written**, silently burning
  athenaeum#711's one-shot pre-change baseline window. A single helper
  `push_metrics.resolve_session_id()` now resolves the id from
  `CLAUDE_CODE_SESSION_ID` first, then `CLAUDE_SESSION_ID` (fallback), then
  empty; the candidate-name tuple `push_metrics.SESSION_ID_ENV_VARS` is asserted
  explicitly in a test so a future rename is a visible diff, not a silent no-op.
  Both call sites — `mcp_server`'s push path and `query_topics.py:211`'s spend
  recording — route through the helper; no direct `os.environ.get(
  "CLAUDE_SESSION_ID")` remains in `src/`. Push records are unchanged in shape
  and still carry **no claim content and no personal data**. `docs/
  configuration.md` names the real variable and the fallback in both places.
- **`lint-pii` phone axis: exclude labeled identifiers and the residual shapes
  athenaeum#720 partially covered (athenaeum#732).** After athenaeum#720 the
  live phone axis still reported 187 findings, dominated by values the
  surrounding prose already types as record ids — `QBO realm 1008563730`, GA4
  `stream 5139685489`, `ISBN 9798183760910`. A **preceding-token** exclusion
  (`pii.LABELED_IDENTIFIER_PREFIXES`, a data list — a new label is a data entry,
  not a code path) retires that class with no model call, and **ISBN-13 is now
  excluded structurally** (13 digits with a `978`/`979` Bookland prefix) so an
  unlabeled ISBN needs no adjacent prose. Three smaller deterministic shapes are
  also closed: the **4-group single-dash** issue list (`410-414-416-412`) via a
  group-count bound expressed as a lower bound with **no upper limit** (a 5-/6-
  group list cannot reopen it), **datetime-with-space** (`2026-04-23 05`), and
  **four-part dates** (`2018-05-06-07`). The genuine numbers `917-231-6130` and
  `206-330-3783` stay flagged (pinned negative tests) and the **email axis is
  unchanged** (pinned with a count assertion). No live-store mutation; the
  operator confirms the new live count on athenaeum#726.

### Added

- **Recorded eval fixtures seeded for all five replay layers (athenaeum#610).** The
  replay layer was real but had nothing to replay: `tests/fixtures/recorded/`
  was empty, so three of four replay tests collected zero items and passed
  trivially (audit finding H13). `evals.yml` was dispatched with `record=true`
  against a live key (run `30760264305`), and its 34 fixtures are now committed
  across `classify` (6), `detector` (10), `merge` (4), `recall` (6) and
  `resolver` (8). Every fixture carries a non-empty `response_text`, including
  the eight `resolver` cases that recorded empty before athenaeum#684 fixed
  `RecordingClient`'s content-block read. `tests/fixtures/recorded/seeded-layers.yml`
  records the seeding date and workflow run per layer, so a later loss of any
  layer's fixtures fails loudly instead of passing as an empty directory
  (athenaeum#551's guard is now live). The replay suite runs 27 real assertions where
  it previously ran none, at zero network cost. Two golden-set cases whose
  stored expectation disagrees with the model are marked `xfail(strict=True)`
  and adjudicated in athenaeum#737 — neither the golden nor the model's answer is
  rewritten here, because silently editing an expectation to match observed
  output destroys the signal the layer exists to produce.

- **`athenaeum spend --json` documented as a stable consumer contract; unknown
  is now distinct from zero (athenaeum#694).** `spend --json` is the surface
  `/good-morning` reads, but its shape lived only in code. It is now documented
  in `docs/configuration.md` — every field's name, type, unit, and what it does
  and does not assert — as a stable contract (athenaeum emits facts; the caller
  computes ratios). The contract states explicitly that the `api` (dollars) and
  `subscription` (tokens) buckets are in **different units and must never be
  summed**, that subscription tokens are never rendered as dollars (its only
  dollar field is a hard `0.0`), and that athenaeum **does not assert account
  identity** — a consumer aggregating across accounts owns that itself.
  Behaviourally, a ledger row whose billing mode cannot be determined (no known
  `billing_mode` and no recognised `provider`) now resolves to a distinct
  **`unknown`** bucket — always present (blank when none) and never silently
  folded into `api` — so an undeterminable row can't be mistaken for API spend
  or for no activity (`spend.resolve_billing_bucket`; surfaced in both the JSON
  and the human report). Additive: the existing `subscription`/`api`/
  `record_count` keys are unchanged.
- **Public-safe OSS lint as a required CI check (athenaeum#693).** Athenaeum is
  periodically re-exported to this public tree, and a 2026-07-31 extraction
  shipped absolute home paths, bare internal issue references, and personal
  attributions into public git history (which is not revocable). This adopts
  the canonical, org-agnostic `public-safe-lint` template from
  `code-workspace-config` **byte-for-byte** — `public-safe-lint.sh` at the repo
  root (`git hash-object 8fa6671`) and `.github/workflows/public-safe-lint.yml`
  (`503f12a`), verified identical to cwc `develop` and never forked or
  hand-edited (fixes go to cwc). The scanner catches only generic *shapes* — a
  `/Users/`/`/home/` personal path, a `~/`-rooted path, a bare `#NNN` issue
  ref, a `+alias@` email, a personal-attribution phrase — carries zero
  org-specific data, and needs no secret, so it runs identically on a fork PR
  (the population most likely to leak by accident and the one CI secrets never
  reach). It reports `file:line` + rule name only (never the matched text, which
  would republish the leak), asserts a canary before trusting a clean verdict,
  and prints a files-scanned/rules-evaluated denominator. The check is folded
  into the existing **`CI Required`** aggregate (a new `public-safe-lint` job
  added to `needs:` + the aggregate result check) so a hit blocks
  merge/promotion — no new branch protection — while the standalone workflow
  additionally covers fork PRs; the same adoption shape as ideate-core#118. To
  make the tree green without editing the template: this repo's ~3,700 bare
  `#NNN` tracker citations were scripted to `athenaeum#NNN` (a cross-repo ref
  qualified to its own repo, e.g. a `code-workspace-config#340` fixture), the
  convention is now recorded in `AGENTS.md`/`CONTRIBUTING.md` so the next lane's
  first commit stays green, the two genuine findings (a `/Users/` doc path, a
  false-positive attribution phrase) were fixed, and the legitimate
  `~/`-config-path documentation (athenaeum's own storage surface) plus the PII
  leak-detector test fixtures are exempted via a committed
  `.public-safe-lintignore` (path+rule only, hits still reported) — leaving the
  genuine personal-path leak rules fully enforced.
- **Push-precision + coverage baseline instrumentation (v6 memory-model MVP
  (a), athenaeum#711).** The v6 epic's definition of done requires push precision
  ("referenced / pushed" per session) to improve over a baseline recorded
  BEFORE any later slice changes what `recall` pushes — this is that
  instrument, shipped first so the number starts accruing now. A **push
  record** (session id, timestamp, pushed page/claim ids, tier, matched
  scope, estimated token cost — no content, no personal data, person ids as
  the opaque `uid`, never a name-derived filename slug) is written every time
  the `recall` MCP tool renders a hit into a session
  (`mcp_server._recall_via_backend`'s block-assembly loop), to a new
  append-only `~/.cache/athenaeum/_push_records.jsonl` — durable, outside the
  wiki corpus, so it can never become a claim or enter the embedded index.
  At `session_end` (the SessionEnd-hook / nightly-after-librarian path), a
  **reference-determination** pass scans the originating session's transcript
  for each pushed id and records which were actually referenced afterward
  (`~/.cache/athenaeum/_push_references.jsonl`), computing
  `precision = referenced / pushed`. New CLI `athenaeum push-metrics
  {baseline,coverage-audit}`: `baseline` computes precision + coverage over a
  stated window and idempotently writes a dated snapshot into a new committed
  `docs/memory-model-measurements.md` (`docs/memory-model.md`, the design
  lock, is never touched); `coverage-audit` samples N sessions into a
  worksheet **file** listing each session's pushed set plus candidate ids
  that were NOT pushed, for a human reviewer to mark relevant-but-missed —
  the coverage-floor baseline a human must supply, never fabricated.
  Instrumentation is **on by default** (passive measurement) with a new
  `push_metrics.enabled` / `ATHENAEUM_PUSH_METRICS_ENABLED` config key to
  disable it; confirmed byte-identical recall output with instrumentation on
  vs off.

- **Prompt + schema-fragment byte attribution on the run summary and `status` (athenaeum#567).**
  A classify/create regression could not be attributed to *which* bytes a run
  used — the operator's (possibly edited) schema fragments vs. the shipped
  prompts. Two forensic surfaces now close that gap, adding no new phase, log,
  or exit-code behavior. (1) The one-line `librarian-run-summary` gains, on its
  head segment right after `total_secs`, a `schema_fragments=` key
  (`<name>:default` when the live fragment matches the bundled default,
  `<name>:<sha8>` when it diverges — one entry per operator-tunable fragment)
  and a single aggregate `prompt_manifest=<sha8>` key over the whole prompt
  set. (2) `athenaeum status` gains one divergence line per schema fragment
  (`default` vs `edited (sha8 …)`). Source of truth is unchanged —
  `tiers.schema_fragment_state` (athenaeum#563) for the fragment bytes and the new
  `prompt_registry.prompt_manifest_hash` aggregating `prompt_manifest` (athenaeum#561);
  no hashing is re-implemented. **No auto-update and no upgrade-time notice**
  were added: the write-once copy semantics stay, and the run-summary hash
  provides forensics regardless (per athenaeum#517's settled operator decision).

- **`models.resolve` — the resolver model joins the `models:` block (athenaeum#513).**
  athenaeum#232 introduced the `models:` yaml section and the shared
  `config.resolve_model()` precedence helper, but routed only three of the four
  model knobs through it; the contradiction resolver kept a hand-rolled
  env/yaml/default chain reading a standalone `resolve.model` key. That was a
  config-surface inconsistency (users set three model ids in one block and the
  fourth somewhere else) and a drift hazard (two copies of the same precedence
  logic, so a future change to the shared helper would silently skip the
  resolver). `resolutions._get_model` now delegates to `config.resolve_model`
  with knob `resolve`. **Backward compatible — no config edit required:** the
  pre-athenaeum#232 `resolve.model` key is still honored, threaded in as the helper's
  default so it sits below `models.resolve` but above the code default. Full
  precedence, highest first: `ATHENAEUM_RESOLVE_MODEL` env >
  `models.resolve` > `resolve.model` (legacy) > `DEFAULT_RESOLVE_MODEL`.
  The config template and `docs/configuration.md` now advertise
  `models.resolve` as the preferred key and mark `resolve.model` legacy.
  Not to be confused with athenaeum#234 (multi-provider support) — all four knobs
  remain free-form model-id strings passed to the Anthropic SDK.

- **Bulk PII migration + corpus-wide PII lint (athenaeum#495).** athenaeum#479 shipped
  `athenaeum storage migrate-pii` as a single-page tool, but the live corpus
  needs the transform over ~11.5k entity pages, and 790 pages carry an email in
  body text of `_`-prefixed queue/index/archive files the entity-page lint
  never opens. Two additions close both gaps:
  - `storage migrate-pii` gains bulk modes: `--all` (every top-level
    `wiki/*.md` entity page carrying contact data; `_`-prefixed queue/index
    files are deliberately excluded — they need per-file-kind operator
    decisions, not the entity-page transform) and `--glob PATTERN` (an
    operator-named set, e.g. one archive to redact in place). The single-page
    `--page` path is byte-for-byte unchanged. Bulk is **idempotent and
    resumable by construction** — a migrated origin page carries no contact
    data, so a re-run (after a clean finish or a crash halfway) skips it and
    applies only the remaining dirty pages, with no run ledger and no
    double-writes. Dry-run prints a summary (pages affected, records to
    create), not 11.5k diffs; apply reports progress every 500 pages.
  - `storage lint-pii` is a new corpus-wide gate: it scans the whole text of
    **every** file under `wiki/` — `_`-prefixed queue/index/archive files and
    stray `.bak` files included, recursing into subdirectories — for an inline
    email/phone token, and exits non-zero (2) on any finding so a body-text
    email cannot silently regrow after the sweep. Reuses the athenaeum#427/#455
    `find_inline_emails` / `find_inline_phones` detectors (one definition of
    "looks like contact data"). The excluded surface lives outside `wiki/` by
    construction, so migrated records are never scanned. Library entry points:
    `athenaeum.storage_migrate.iter_entity_pages` / `iter_glob_pages` and
    `athenaeum.pii.scan_corpus_pii` / `iter_corpus_files` /
    `CorpusPiiFinding`.
- **Permanent per-phase run summary (athenaeum#464, slice E of athenaeum#460).** Pure
  observability for the athenaeum#440 nightly-cost profiling epic: `run()` now emits
  ONE machine-greppable `librarian-run-summary` log line at the end of every
  exit path — the normal finalize AND every `_stop_on_deadline` (issue athenaeum#396)
  124 trip — covering the phases that actually ran (wiki-dedup, the entity
  loop, the auto-memory C2-C4 compile, retire, athenaeum#188 reresolve), with each
  phase's wall-clock seconds, LLM call counts, and work counts. Working
  hypothesis this unblocks measuring: C4's nightly cost is dominated by
  per-call latency of the `claude-cli` subprocess backend times the number
  of non-cached detector/resolver calls, so the detector/resolver call
  counts + phase seconds are the key signals.
  - `athenaeum.merge.merge_clusters_to_wiki` gains a keyword-only
    `out_stats: dict | None = None` out-param, populated immediately before
    both return sites (the dry-run return and the normal write return) with
    the counters the merge engine already tracks internally —
    `haiku_calls`, `resolve_calls`, `chunks_run`,
    `pairs_added_via_similarity`, `entries_merged`, `escalations_written` —
    so callers thread the existing counts up instead of recomputing them.
    Purely additive; every existing caller (`out_stats=None`) is
    byte-identical.
  - `athenaeum.librarian._compile_auto_memory` gains `out_merge_stats: dict
    | None = None`, threaded straight through as the merge call's
    `out_stats`.
  - `run()` owns a small `run_profile: list[tuple[str, float, dict]]`
    accumulator: each phase it controls is timed via `time.monotonic()`
    deltas at the call site, and per-phase LLM call counts come from a
    `usage.api_calls` before/after snapshot (the entity loop's delta is the
    entity-tier call count; the auto-memory delta cross-checks
    `out_merge_stats`). A new `_render_run_summary` helper renders the
    accumulated profile into one stable-prefixed
    (`RUN_SUMMARY_PREFIX = "librarian-run-summary"`), key=value line, e.g.
    `librarian-run-summary total_secs=12.3 | wiki-dedup secs=0.1 | entity
    secs=4.2 calls=6 created=2 updated=1 escalated=0 files=3 | auto-memory
    secs=7.8 detector_haiku=4 resolver_opus=1 sweep_pairs=0
    clusters_merged=2 escalations=0 | retire secs=0.1 | reresolve secs=0.05
    calls=0`. Only phases that actually ran are included — a phase never
    reached (e.g. after an early deadline trip) is simply absent, not
    zero-filled. Emitted via `log.info`; a `_summary_emitted` guard makes
    the emit idempotent (defense-in-depth — the `_stop_on_deadline` and
    normal-finalize emit sites are mutually exclusive on any single run, so
    in practice this fires exactly once).
  - Zero behavior change: no phase's logic, ordering, exit code, commits,
    or writes are affected. Additive log output + additive out-params only.

- **Live-client delta-scoped auto-memory compile + periodic full-compile
  cadence (athenaeum#463, slice D of athenaeum#460).** Extends the athenaeum#370 PR2 delta-scoped
  cluster/merge machinery — previously reachable ONLY on the deterministic
  `client is None` path (session_end / ingest tier0; the original D5
  fallback ALWAYS forced a whole-corpus compile under any live LLM client)
  — to the nightly LIVE-client run, so a night with no (or few) auto-memory
  changes no longer re-clusters and re-merges the entire corpus through the
  contradiction detector.
  - New config: `librarian.delta.live_client` (`resolve_live_delta_enabled`,
    default `true`) opts the live-client path into delta scoping, and
    `librarian.full_compile_every_days` (`resolve_full_compile_every_days`,
    default `7`, NOT under `librarian.delta`) sets the periodic whole-corpus
    reconciliation cadence that is delta's consistency backstop on the live
    path. `_compile_auto_memory`'s gate is now `delta_eligible = not dry_run
    and changed_paths is not None and delta_enabled and (client is None or
    (resolve_live_delta_enabled(config) and not full_compile_due))` — every
    existing fallback (F6 slug collision, the D2 affected-cluster/member
    caps, the empty-delta no-op) is unchanged and still applies on the
    live-client path.
  - Two new cache-dir stamps (siblings of `ingest-manifest.json`, outside the
    knowledge git repo): `auto-memory-manifest.json` (a content-hash
    snapshot of the auto-memory intake, the nightly run's own delta baseline
    — `ingest`/`session_end` already thread their own `changed_paths` and are
    unaffected) and `full-compile-stamp.json`, which records the LAST
    successful whole-corpus auto-memory compile as **`{"at": <ISO-8601 UTC
    timestamp>, "head": <knowledge_root HEAD sha or null>}`** — the
    timestamp drives the cadence; `head` is audit-only. `run()` computes
    `changed_paths` (deletion-only changes count for their prior cluster —
    a member removal must recompile that cluster) and `full_compile_due`
    (due when the stamp is absent, stale past the cadence, or
    `--full-compile`/`full_compile=True` forces it) before the auto-memory
    block, and — on a successful, non-dry-run compile — refreshes the
    auto-memory manifest and, ONLY when the compile that ran was actually
    whole-corpus (a new `_compile_auto_memory(out_delta_taken=...)`
    out-param reports this reliably, covering every internal fallback, not
    just the cadence flag), resets the full-compile stamp. A delta compile
    never resets the cadence clock. Both stamp reads/writes are best-effort
    (log + continue on failure), mirroring the ingest manifest's tolerance.
  - New `athenaeum run --full-compile` CLI flag (threaded into both the
    dry-run and locked `run()` call sites in `_cmd_run`) — the manual escape
    hatch for an immediate whole-corpus reconciliation.
  - **Implementer decision:** issue athenaeum#251 TTL expiry does **not**, by itself,
    force affected-cluster re-detection on an otherwise-eligible delta
    night — only the scheduled full-compile reconciliation re-enters
    TTL-decayed auto `not_a_conflict` suppressions. Keeping this out of the
    delta path avoids quietly widening the live-client delta scope beyond
    what the closure computation already proves affected.
  - `tests/test_delta.py`'s D5 suite is updated for the new default (a live
    client is delta-eligible unless `full_compile_due` or
    `librarian.delta.live_client: false`); new `tests/
    test_live_delta_cadence.py` covers the end-to-end cadence contract
    through the real `run()` entrypoint (detector call counts, not
    detector text, per an internal deterministic hashing-trick-embedding
    patch needed because the production fallback embedder's token bucketing
    uses Python's per-process-salted `hash()`).

- **Outbound-draft PII lint (emails/phones) — interim mitigation (athenaeum#455, split
  from athenaeum#428, epic athenaeum#422).** A reusable, offline, deterministic lint that scans
  outbound-destined text (email draft, Buffer post, public issue) for PII
  before it ships. Detects email addresses and phone numbers (in several
  formats), reports each finding's class + location (line/column + offsets),
  and supports a strip/redact mode distinct from flag-only. A configurable
  allowlist drops addresses already known to the recipient; where the caller
  cannot establish that, the default is to flag (fail safe, never
  silent-pass).
  - New `src/athenaeum/outbound_pii.py`: `scan_outbound_text` (flag),
    `redact_outbound_text` (strip), and the `lint_outbound_text` wrapper, plus
    an `Allowlist` helper. Reuses the athenaeum#427 `athenaeum.pii` email/phone patterns
    as the single source of truth (no second, driftable copy). No network, no
    live-store access, no LLM call.
  - New `athenaeum outbound-lint` CLI (`src/athenaeum/_cmd_outbound.py`): reads
    from `--text`/`--file`/stdin, `--allow`/`--allowlist-file`, `--redact`,
    `--json`; exits non-zero when PII is found in flag mode so a shell can gate
    a send on a clean scan. Delivers the mechanism only — wiring it into any
    specific outbound surface is separate per-surface work, and egress
    *refusal* stays parked on athenaeum#428.
- **Entity source-handle registry — schema + `registry.json` index builder
  (athenaeum#453, epic athenaeum#422).** Adds the source-handle frontmatter keys to the
  `person`/`company` scaffold templates and a deterministic CLI step that
  compiles them into an `entity uid → handle set` index for the fact-mining
  adapters.
  - New keys on `src/athenaeum/templates/{person,company}.md`: `domains`,
    `alt_emails`, `slack_channels`, `slack_user_ids`, `partner_domains`,
    `drive_folder_ids`, `mural_board_ids`, `handles_verified` (plus the
    pre-existing `linkedin_url`). No schema-class change needed — `WikiBase`
    is already `extra="allow"`, so the keys round-trip byte-for-byte through
    tier0 passthrough. Documented in `docs/source-handles.md`.
  - New `src/athenaeum/registry.py` + `athenaeum registry` command: walks
    `wiki/*.md` and emits `registry.json`, recording only entities with at
    least one populated handle. Deterministic (uid-sorted, canonical key
    order) and LLM-free. Emits a well-formed **empty** registry when no
    handles are populated yet — the operator-only seed (athenaeum#454) is never a
    precondition. Tooling only; no client data lands in this public repo.
- **Memory taxonomy — data model, validation, inference-block parser
  (athenaeum#424).** Adds `memory_class:` as a third, orthogonal frontmatter axis —
  `fact | guideline | axiom | reference | entity | decision | procedure` —
  layered BESIDE the existing entity-schema `type:` (athenaeum#93's `KNOWN_TYPES`)
  and intake `memory_type:`, both of which are untouched and remain
  byte-identical.
  - `WikiBase.memory_class` (`src/athenaeum/schemas.py`): a value outside
    the 7 is flagged via `UserWarning` (mirrors the athenaeum#93 `KNOWN_TYPES`
    precedent); an absent value is tolerated (legacy pages don't break) and
    reported via the new `schemas.is_untyped_memory_class` /
    `_lint.lint_untyped_memory_class` predicates.
  - New `src/athenaeum/inference_blocks.py`: schema + parser for
    `## Inference` body blocks (`**Basis**:` wikilinks + `**Confidence**:`)
    inside a `memory_class: fact` page, parsed to addressable
    `InferenceBlock` units. Malformed blocks are flagged, not dropped or
    silently accepted. Retraction machinery is out of scope — athenaeum#433.
  - `WikiBase.observed_at` + `models.parse_observed_at`: the staleness axis
    for standing-state facts (true-when-observed vs. currently-true),
    distinct from `created`/`updated` and from `valid_from`/`valid_until`
    (athenaeum#308). Round-trips through parse/render.
  - New [`docs/memory-taxonomy.md`](docs/memory-taxonomy.md): axis
    reconciliation, merge-vs-cite semantics (within-class merge,
    across-class cite-never-destroy — enforcement is athenaeum#433), and the
    inference-block grammar.
  - Data model + validation + parser + doc only — zero behavior change to
    merge/recall/embed.
- **PII off-corpus — excluded contacts surface, entity-page lint, observation
  log + supersession fold (athenaeum#427).** Code-only slice: keeps durable identity
  data on entity pages and archival contact data OUT of the
  embedded/recalled/merged corpus — corpus hygiene + ambient-egress
  reduction, NOT encryption (recall injects pages into arbitrary agent
  prompts; retrieval-layer exclusion is the cheapest egress reduction).
  Migrating live entity pages is operator task athenaeum#437; the retraction cascade
  is athenaeum#435 — both out of scope here.
  - New internal module [`athenaeum/pii.py`](../src/athenaeum/pii.py):
    `contacts_surface_root` (thin convenience over athenaeum#429's `excluded` adapter —
    no hardcoded path), `is_pii_flagged` (the `pii: true` belt-and-suspenders
    frontmatter flag, mirroring `authority.is_pointer_stub`'s coercion
    contract), and the entity-page lint (`has_inline_contact_fields` /
    `lint_inline_contact_fields` / `find_inline_emails` / `find_inline_phones`).
  - `search.py`'s FTS5/vector index build (`_scan_indexed_records`) and the
    keyword scan-on-query backend now also skip a `pii: true`-flagged page,
    same as an inactive memory — belt-and-suspenders for PII inline in
    narrative on a page not (yet) moved to the excluded surface.
    `wiki_dedupe.discover_wiki_dedupe_candidates` does the same for merge
    candidates. A page on the `excluded` storage surface was already outside
    every consumer's scan set by construction (athenaeum#429) — no code change needed
    there; this slice adds a test per consumer proving it.
  - `schemas.WikiBase` gains a model validator flagging inline `emails:` /
    `phones:` frontmatter on any entity page via `UserWarning` — mirrors the
    athenaeum#93 `KNOWN_TYPES` / athenaeum#424 `memory_class` precedent (recoverable, not a
    hard failure; migrating pre-existing pages is athenaeum#437). Silenced by
    `pii: true` (the explicit "every corpus consumer already excludes this"
    acknowledgment).
  - Append-only observation log `(identifier, person_id, observed_at,
    source_msg_id)` as JSONL (`_observations.jsonl`), mirroring
    `provenance.py`'s merge-provenance ledger discipline (`O_APPEND` + fsync,
    tolerant reader that skips a torn trailing line). Corrections are
    supersession records `(retracts: obs_id, reason, at)` in a separate
    append-only sidecar (`_observation_supersessions.jsonl`) — never edits or
    tombstones of the original observation.
  - `pii.fold_observations` resolves "latest uncontradicted" per identifier
    via a deterministic fold (sort by `observed_at` then `obs_id`; drop any
    observation a supersession retracts; keep one survivor per DISTINCT
    `person_id`) — deliberately no clustering/similarity step, so a
    genuinely shared address folds to ALL currently-attributed persons
    rather than only the newest write, and a correction (e.g. an address
    later reassigned to a different person) is expressed as an explicit
    supersession, never a fold-time guess.
  - New [`tests/test_pii_off_corpus.py`](../tests/test_pii_off_corpus.py):
    a test per corpus consumer (FTS5, vector, keyword, merge) proving both
    the excluded-surface by-construction exclusion and the `pii: true`
    belt-and-suspenders exclusion; lint tests for inline emails/phones;
    observation-log append/read/tolerant-reader tests; fold tests covering
    the shared-address and Jason/Janice correction shapes.
- **Pluggable storage-adapter layer — entity class → surface + corpus policy
  (athenaeum#429).** Generalizes "PII lives on an excluded path" (athenaeum#427) into a
  config-swappable layer: each entity class (the wiki frontmatter `type:`)
  resolves to a *storage surface* whose backing store AND corpus policy
  (embedded / recallable / merge-eligible) are a configuration choice,
  changeable later — not a hardcoded path.
  - New internal module [`athenaeum/storage.py`](../src/athenaeum/storage.py):
    `CorpusPolicy`, `StorageAdapter`, a built-in registry, `register_adapter`
    (the in-process extension point), `resolve_adapter_for_class`, and the
    consumer/writer helpers (`is_embedded` / `is_recallable` /
    `is_merge_eligible` / `is_excluded` / `surface_root_for_class`). Loud
    `StorageConfigError` on misconfiguration — never a silent fallback.
  - Two adapters ship built in: **`wiki-markdown-embedded`** (the default —
    today's flat `wiki/`, full corpus participation, so an unconfigured base is
    byte-identical) and **`excluded`** (a surface outside `wiki/`, no
    embed/recall/merge — what athenaeum#427's PII surface consumes). An excluded surface
    is excluded from the corpus **by construction** (its root is outside the
    scanners' search set), the fail-closed property athenaeum#427 requires — no change to
    the embed/recall/merge core.
  - Config-driven via a new `storage:` block in `athenaeum.yaml`
    (`storage.mapping` entity-class → adapter, `storage.adapters` custom
    definitions; `corpus_policy` keys fail closed). Adding a surface is config +
    an adapter with no core change — the seam athenaeum#426's deferred skill-file-sync
    surface will consume.
  - The wiki-dedup merge pass now consults the layer's `merge_eligible` policy
    (`wiki_dedupe.discover_wiki_dedupe_candidates`) so a class routed to a
    non-merge-eligible surface is dropped even if it sits in `wiki/` —
    fail-closed defense-in-depth, byte-identical when unconfigured.
  - New [`docs/storage-adapter-contract.md`](docs/storage-adapter-contract.md),
    explicitly disambiguated from the source → raw-intake adapter contract.
- **Documented the source → raw-intake adapter contract + a bundled
  adapter-authoring skill (athenaeum#419).** The raw-intake write API is the seam any
  external source uses to feed data into the wiki, but it wasn't documented or
  packaged as a reusable "adapter" contract for OSS consumers.
  - New [`docs/adapter-contract.md`](docs/adapter-contract.md) specifies the
    contract end to end: the two intake lanes, the `raw/<source>/<timestamp>-<uuid8>.md`
    location convention, the frontmatter/provenance shape, idempotency
    (write-once, append-only, atomic, compile-time dedupe), how compilation
    reconciles duplicates/updates, and the two write mechanisms (the MCP
    `remember` tool and direct file drop).
  - New bundled **`skills/adapter-authoring/`** skill — a self-contained,
    user-invocable walkthrough for building a custom adapter. It ships **inside
    the published wheel** (`athenaeum/skills/…`) via a
    `[tool.hatch.build.targets.wheel.force-include]` mapping, and in the sdist,
    so any consumer (human or agent) has it after `pip install athenaeum`.
  - New synthetic, runnable [`examples/adapters/minimal_adapter.py`](examples/adapters/minimal_adapter.py)
    (plus `examples/adapters/README.md`) — a generic Lane-A adapter, no private
    specifics or PII, usable as a starting point.

### Changed

- **Dropped the DCO sign-off requirement; inbound licensing is Apache-2.0 §5
  (athenaeum#678).** athenaeum#544 landed the DCO *documentation* and deferred the
  enforcement mechanism under an operator constraint ("do not land a check that
  wedges the overnight build conveyor"). Its premise — that the fleet's bot
  commit path emits a valid `Signed-off-by` — was verified false (none of the
  six most recently merged PRs' commits, nor dependabot's, carry the trailer),
  so a required DCO check would red every lane immediately. Since nearly all
  commits here are bot-authored through automated lanes, a sign-off ceremony the
  fleet performs on its own behalf certifies nothing a human asserted, and
  Apache-2.0 §5 already grants inbound contribution licensing without it. The
  DCO requirement is removed from `AGENTS.md` and `CONTRIBUTING.md` (the
  `git commit -s` guidance removed, not merely softened), `CONTRIBUTING.md` now
  states plainly that inbound contributions are licensed under Apache-2.0 §5,
  and **no** enforcement mechanism is added (no DCO app, no `dco` CI job). This
  is a decision recorded in the issue, not just a doc edit.

### Fixed

- **`prune-code-entities` no longer treats a bare `/` in an entity name as a
  file path (athenaeum#721).** athenaeum#680's retire sweep keyed on the entity name being
  file-shaped by *extension OR path separator*. The extension half works, but
  the path-separator half had no discriminating power in a corpus where slashes
  are ordinary punctuation in human and organization names — on the live store
  it put **140 real people, companies and concepts on a `git rm` kill-list**
  (44% of the 315 proposed deletions): a person matched on their pronouns
  (`Suzie Prince (she/her)`), companies (`Stora Enso / Chalmers University`,
  `ITHAKA/JSTOR`), an npm package (`@tanstack/react-query`), skill/label names
  (`dijkstra/arch-review`, `hestia/needs-human-promotion-review`), git branches
  (`feature/408-gateway-split-write`) and slash-commands (`/good-morning`).
  Classification (`tiers.classify_code_artifact_name`) is now **extension-only**:
  a slash contributes nothing. Confirmed against the real 140 — the narrower
  slash signals one might reach for (a slash-separated, space-free,
  uncapitalized, non-`@` token) still delete those legitimate skill/label/branch
  names, so a slash cannot be a signal at all. The 173 genuine extension-matched
  artifacts (`deploy-guard.sh`, `wiki_dedupe.py`, `crawlGovernance.ts`,
  `generate-repo-map.sh`) are still killed, and a full source path keeps being
  matched by its extension (`src/athenaeum/librarian.py`). **Decision (AC4):** an
  extension-less path-shaped name (`src/athenaeum`, `scripts/oss-export`) is
  **retained** — no mechanical slash signal separates it from the legitimate
  namespace/label/branch names above, and retention is the safe, reversible
  direction. The dry run now prints the matched rule per entry and an
  extension/non-extension split (`by rule: extension=N`) so an operator can
  audit the kill-list by class. Re-running the live dry run to confirm the new
  kill count and split is the operator step on athenaeum#695.
- **`lint-pii` no longer counts issue-number lists, non-ISO dates, or
  date-into-text bleed as phone-axis PII (athenaeum#720).** athenaeum#683 normalized a leading
  paren and cut `lint-pii` from 911 findings/107 files to 456/274, but four
  false-positive shapes survived because the fix only reached the *leading*
  paren: double/single-hyphen issue-number lists (`445--436--435--374`,
  `256-257-280`), non-ISO and dotted date orderings (`02-08-2018`), and a
  permissive character class that let a match run past a closing paren or
  across a newline into the next number (`2026-04-27)\n\n1`, `1778
  (2026-08-01`). The phone classifier is now **normalization + structural
  classification** rather than a literal blocklist: it segments a capture into
  digit groups and the separator runs between them and rejects line-spanning
  matches, unbalanced parens, dates in any ordering, year ranges in any
  separator style, multi-character separator runs, short unprefixed grouped
  runs (`<10` digits, no `+`), and lists of more than four groups — so a new
  separator style needs no new rule. Every athenaeum#500/#683 genuine-phone fixture
  (`+1-555-0100`, `(555) 010-0100`, `5551234567`, `+447911123456`,
  `917-231-6130`) is preserved, and the **email axis is untouched** (pinned by
  a count assertion). The live 456→residue drop is an operator-run step
  (`athenaeum storage lint-pii` against `~/knowledge/wiki`); the deterministic
  exclusions are pinned per-shape to the values in the issue's table.
- **The entity phase no longer starves the C4 contradiction detector of the
  whole run window (athenaeum#440).** `max_runtime` (athenaeum#396) is a single deadline shared
  by every phase, and the entity loop stopped only when that WHOLE budget was
  gone — so once the entity phase became slow enough, everything downstream of
  it (the auto-memory compile and the C4 contradiction detector, which athenaeum#461
  moved after it) got nothing. Measured on the live corpus: entity took 3690s
  of a 3944s window (93.6%) on 3 files, and C4 received **0 seconds on 10+
  consecutive nights**, so contradictions were not being detected slowly — they
  were not being detected at all. The entity phase now gets a *share* of
  `max_runtime` (`librarian.entity_runtime_share` /
  `ATHENAEUM_ENTITY_RUNTIME_SHARE`, default `0.6`) for claiming new files;
  when it is spent the phase defers the remaining intake to
  `wiki/_deferred_work.md` (resumable, exactly like the athenaeum#220 budget trip) and
  the run **continues into C2-C4** instead of exiting `124`. Deliberately not
  the `deadline_tripped` path — that flag skips the auto-memory block, which is
  the starvation being fixed. The real wall-clock deadline still takes
  precedence and still exits `124`. Checked at the per-file boundary, so a file
  already in flight may overrun the share by its own duration; any value
  outside `0 < share < 1` disables the reserve, and it is inert when
  `max_runtime <= 0`.

- **`storage migrate-pii`: detect PII in ANY frontmatter value, not only
  `emails:` / `phones:`, and invalidate the search index after `--apply`
  (athenaeum#502).** After the live bulk sweep (athenaeum#495) 690 pages still carried inline
  contact data the migrator never looked at — athenaeum#479's `plan_pii_migration` read
  only the `emails:` / `phones:` keys, but the residual lived in `aliases:`
  (dominant — and *more* exposed than `emails:`, since aliases are recall
  matching keys), `former_emails:` / `alt_emails:` / `source:` provenance
  strings, and body prose.
  - The migrator is now **detector-driven**: it scans every frontmatter value
    (and the body) with the shared `find_inline_emails` / `find_inline_phones`
    detectors, so a newly-invented contact key can't reopen the hole. Durable
    identifiers (`athenaeum.pii.DURABLE_IDENTIFIER_FIELDS` — name,
    `linkedin_url`, `handles_verified`, record IDs, `google_contact*`) are
    **preserved verbatim** per athenaeum#427; real aliases and non-PII provenance
    context survive (list entries that are pure contact data are dropped; an
    address embedded in a `source:` string is redacted in place).
  - The **name-is-an-email population** (~80 pages whose `name:` /
    `preferred_name:` is itself an address) is **excluded** from this automatic
    path — renaming an entity page changes its slug and breaks inbound
    `related:` / alias edges — and is filed as its own slice (athenaeum#505). Bulk mode
    reports the excluded count so it is visible, not silently dropped.
  - `--apply` no longer prints an unqualified success while the migrated data is
    still recallable: it now instructs a reindex (incremental suffices — a
    rewritten page's content hash changes, so the differ evicts the stale index
    entry), and a new `--reindex` flag runs it in one shot. Without this step
    the vector embeddings keep the pre-migration text and every migrated address
    stays reachable via `recall`.

- **Librarian: run the entity compile loop BEFORE the auto-memory block so a
  slow C2-C4 pass can no longer starve entity intake (athenaeum#461).** `run()` used
  to sequence the whole-corpus auto-memory block (C1 discover, C2 cluster,
  C3 merge, C4 detect) BEFORE the per-file entity tier loop. On a slow night
  the auto-memory block could consume the entire shared `max_runtime`
  deadline before the entity loop ever got a turn, deferring the whole
  entity intake even though it is typically far cheaper to compile.
  - The entity phase (raw-file discovery, `EntityIndex` load, the per-file
    tier loop and its SIGTERM/SIGINT partial-commit guard, the terminal
    commit) now runs immediately after the athenaeum#290 wiki-dedup pass and its
    deadline check (and after the `merge_only` early return), claiming the
    shared deadline and `max_api_calls` budget FIRST. The auto-memory block
    then runs after, consuming whatever budget/time remains — skipped
    entirely when the entity phase itself trips the deadline. `cluster_only`
    still skips the entity phase and returns after the auto-memory block's
    C2 cluster pass, unchanged; `merge_only` is unaffected (it already
    returns before either phase).
  - An empty entity/raw intake no longer short-circuits the whole run: the
    auto-memory block is independent of raw entity intake and always ran
    regardless in the pre-athenaeum#461 ordering, so the empty-intake path now falls
    through to it instead of returning early.
  - New `max_api_calls` guard on `merge_clusters_to_wiki` (both the primary
    per-cluster C4 detector call site and the cross-scope similarity sweep):
    once the shared run-level `usage.api_calls` has already reached the
    ceiling — which the entity phase, running first, can now do — the C4
    detector call is skipped with a deterministic `rationale=
    "budget-exhausted"` result, mirroring the existing declared-pair /
    disjoint-validity short-circuits. `None` (the default; every existing
    caller) preserves the pre-athenaeum#461 unbounded behaviour byte-for-byte.
  - Natural consequence of the reorder: the `EntityIndex` load now happens
    before this run's own C3 merge (re)writes `wiki/auto-*.md` pages, so the
    entity tiers see auto-memory pages as they stood at the end of the
    PREVIOUS run — one cycle stale. Does not affect entity-tier correctness.
  - The run-summary `Token usage:` log line and the `spend.record_spend`
    ledger write are moved from the entity phase to the finalize section so
    they run AFTER both phases. Because the entity phase now runs first, the
    shared run-level `TokenUsage` keeps accruing the auto-memory C4
    detector/resolver spend after the entity loop — recording at the end of
    the entity phase (its pre-athenaeum#461 home, when it ran last) would silently
    undercount every run by the entire C4 cost, defeating the observability
    the athenaeum#460 epic depends on. Both now reflect the WHOLE run's spend, as they
    did pre-athenaeum#461.

- **Librarian: persist the C3 merge output BEFORE C4 detection so a deadline
  trip keeps the compiled pages (athenaeum#462, slice B of athenaeum#460).**
  `merge_clusters_to_wiki` used to build every entry (C3, deterministic), run
  the deadline-checked C4 detector/resolver loop, and only THEN write the
  pages. When C4 tripped the wall-clock deadline — which it did on 10+
  consecutive nights (athenaeum#440) — the exception propagated before the write loop
  and the ENTIRE C3 build was discarded: every night re-paid the full C3 build
  and banked nothing.
  - The merged pages are now written immediately after the C3 build (+ cohesion
    floor + run-global slug resolution, so C3 stays atomic), UNFLAGGED —
    byte-identical to what a deterministic `client=None` compile already writes.
    C4 then re-writes only the entries whose contradiction state actually
    changed (flag added or a stale flag cleared), per entry as detection
    completes, so a later C4 trip leaves every already-detected entry persisted
    with its flag and every unprocessed entry with its durable C3 page.
  - The athenaeum#145 contract holds: no `contradiction-flagged` status is ever written
    without a pending question, because the flag is only rendered after
    detection + escalation. A page flagged by a prior run whose cluster now
    clears is overwritten unflagged. Dry-run still writes nothing. Escalations
    stay a single end-of-pass `tier4_escalate` batch — a C4 trip loses only the
    current run's pending-escalation batch (idempotently re-escalated next run
    via the athenaeum#157 open-block dedup + athenaeum#249 resolved-records cache), never the
    compiled pages. `out_wiki_root` (compile-as-of, athenaeum#359) and `only_cluster_ids`
    (delta, athenaeum#370) semantics are unchanged.
  - README gains a "Building your own adapter" section; `AGENTS.md` references
    the skill by path and name so agent sessions surface it.

## [0.15.0] - 2026-07-20

### Added

- **One unified "human decisions needed" list — `athenaeum decisions` +
  `athenaeum merges` + `list_pending_decisions` MCP tool (athenaeum#401).** Athenaeum
  accumulated two separate human-decision queues — pending **questions**
  (contradiction detector) and pending **merges** (resolver proposals) — but
  merges had **no CLI and appeared in no briefing**, so a real backlog (34
  proposals aged 1–4 weeks, found 2026-07-20) could sit unseen indefinitely.
  - New `athenaeum decisions {list,next,count} --json` returns **both** queues
    in one call, each item tagged `type: "question" | "merge"` with common
    fields (`id`, `created_at`, `summary`, `confidence`) plus a type-specific
    `payload`, sorted oldest-first. `count` prints
    `N decisions pending (Q questions, M merges; oldest Xd)`.
  - New `athenaeum merges {list,next,count} --json` — the merges half, a mirror
    of `athenaeum questions` (the CLI-only briefing path had no way to read
    merges before).
  - New `list_pending_decisions()` MCP tool gives containerized agents the same
    unified list.
  - Every **merge** is rendered as an **answerable question**: each source page
    is named by its human title (frontmatter `name:`, not the uuid-slug) with a
    one-line gist, because cosine topic-similarity alone is not "should-merge"
    and misleads without the pages' own words. A human can decide approve/reject
    from one `decisions list` item without opening the raw wiki files.

- **Kill switch — `athenaeum disable` / `enable` / `status` (athenaeum#379).** One
  discoverable, reversible command stops all athenaeum background work instead
  of hand-editing the hook commands out of `~/.claude/settings.json` and
  `pkill`-ing in-flight detectors. Backed by a state file
  (`$ATHENAEUM_CACHE_DIR/disabled`, default `~/.cache/athenaeum/disabled`) plus
  an `ATHENAEUM_DISABLED` env override that **every entry point honours** — the
  `session-end` compile pass, the MCP write tools (`remember`,
  `resolve_question`, `resolve_merge`), and the shell hooks in
  `examples/claude-code/` (which read the file directly with `grep`, so the
  per-turn recall path adds no Python startup).
  - `athenaeum disable` turns everything off (compile, contradiction detection,
    recall, notifications). `athenaeum disable --compile` is granular — it stops
    only the expensive compile/detect pass and leaves recall on.
  - `athenaeum enable` removes the state file and restores prior behaviour
    exactly. `athenaeum status` now reports the on/off state, scope, and reason.
  - The env override wins over the file; `ATHENAEUM_DISABLED=1` (or `all` /
    `compile`) forces the state without touching the file — handy for a scoped
    one-off — and `athenaeum enable` warns when the env is still forcing it off.
- **Durable LLM-spend ledger + `athenaeum spend` + a spend ceiling (athenaeum#378).**
  Athenaeum runs on two cost models that must never be blended — the
  `claude-cli` **subscription** path (no invoice; consumes subscription quota,
  constrained in TOKENS) and the metered `anthropic` **API** path (real
  dollars: the resolver on the api backend, batch mode, and the per-turn
  `query-topics` recall extractor). The in-memory token summary was logged and
  discarded, so "how much has athenaeum spent, and is any of it real money?"
  was unanswerable from data. Now:
  - **Ledger.** Each pipeline run appends one JSONL record to
    `~/.cache/athenaeum/spend.jsonl` carrying the **provider** (`claude-cli`
    vs `anthropic` — the field that makes "are we spending real money?" an
    empirical question), run type, model ids, the four token counters kept
    **separate**, and a **provider-tagged** USD estimate that is always `$0` on
    the subscription path so subscription rows can never be summed into the
    dollar total. Append-only and crash-safe (single `O_APPEND` write per
    record; the reader tolerates a torn trailing line); records only counts and
    metadata, never content or credentials. On by default; disable with
    `spend.ledger_enabled: false` / `ATHENAEUM_SPEND_LEDGER_ENABLED=0`.
  - **`athenaeum spend --since 7d [--by-model] [--by-provider] [--json]`.**
    Reports **$ for the API path** and **tokens for the subscription path**,
    never blended. `--json` is the shape `/good-morning` consumes.
  - **Spend ceiling.** Configurable per-run and per-day ceilings —
    `spend.max_tokens_per_run` / `spend.max_tokens_per_day` (subscription
    tokens) and `spend.max_usd_per_run` / `spend.max_usd_per_day` (API
    dollars). On breach the librarian pass stops early and loudly and defers
    the remaining intake (like the `max_api_calls` budget) rather than silently
    continuing. Off unless configured.
- **Configurable pre-run pull to sync the knowledge repo (athenaeum#399).** `athenaeum
  run` can now pull the knowledge repo before a run and push intake + outcomes
  after, so a run starts from and lands back to GitHub instead of drifting from
  the remote.
- **Progress heartbeat for the merge + post-compile phases (athenaeum#398).** Both
  phases now emit periodic progress lines, so a long-running (or wedged) run is
  legible instead of logging zero output for hours.

### Fixed

- **Move-then-retire no longer leaves dangling `MEMORY.md` pointers (athenaeum#388).**
  The move-then-retire pass (`retire.py`) `git rm`'d a retired raw member but
  never rewrote the sibling per-scope `MEMORY.md` index that pointed at it, so
  every retirement left a dangling pointer — and unlike the compiled wiki page,
  `MEMORY.md` loads into **every** session's context, so a stale line keeps
  asserting a fact whose file is gone. The retire pass now drops each retired
  member's index pointer in the SAME commit as the deletion (conservative: only
  pointers to members that run retired; a pre-existing dangling pointer is left
  for the backfill), so index and deletion stay atomic. Cross-tree links
  (`../wiki/…`), URLs, anchors, headings and prose are preserved verbatim.
  - **Backfill: `athenaeum auto-memory prune-index` (athenaeum#388).** A one-shot sweep
    for pointers already orphaned by pre-athenaeum#388 runs — dry-run by default (prints
    the dangling-per-scope list, exit 2), `--apply` rewrites the affected
    indexes in one labeled, git-recoverable commit. A pointer is dangling when
    its bare `<file>.md` target no longer exists in the scope directory.
- **Stale pre-athenaeum#330 docstrings in `cli.py` (athenaeum#378 drive-by).** `_cmd_ingest_answers`
  and `_cmd_reresolve_questions` described "builds a live Anthropic client from
  `ANTHROPIC_API_KEY`"; both now build through the provider seam
  (`build_llm_client`), matching the actual post-athenaeum#330 behavior.
- **Resolver over-cluster merges are capped and floored (athenaeum#400).** Degenerate
  merge proposals (thousands of sources at low confidence, re-proposed and
  regenerated every run) are now suppressed by a cohesion floor plus a size cap.
- **`athenaeum run` now has a wall-clock deadline (athenaeum#396).** A hung
  post-checkpoint phase can no longer wedge a run indefinitely while holding the
  run-lock; the run aborts at the deadline instead.
- **Runlock auto-recovers an alive-but-wedged holder (athenaeum#397).** A stuck lock
  holder is now detected and recovered automatically instead of blocking every
  writer until a manual `--force`.
- **`_pending_merges.md` no longer regrows unbounded (athenaeum#394).** Fixes a
  regression of athenaeum#299/#303 that let the file balloon (to ~13MB) and flood each
  run with tens of thousands of malformed-header warnings.
- **Keyword-backend scoring uses term frequency, not presence (athenaeum#384).**
  Corrects the keyword search backend's scoring, fixing a Python 3.13 CI flake.

### Security

- **Workflow hardening for OpenSSF Scorecard (athenaeum#405).** Added a least-privilege
  top-level `permissions: { contents: read }` block to `ci.yml` (closes the
  Token-Permissions / High alert) and SHA-pinned the remaining tag-referenced
  third-party actions — `dependabot/fetch-metadata`, `1password/load-secrets-action`,
  `actions/checkout` + `actions/create-github-app-token` (in `promote-main.yml`),
  and `pypa/gh-action-pypi-publish` — each with a trailing `# vX.Y.Z` comment,
  matching the existing convention. `dependabot.yml`'s `github-actions` ecosystem
  block keeps the pins fresh automatically.

## [0.14.1] - 2026-07-12

Session-end / incremental-compile efficiency (issue athenaeum#370) plus a self-healing
index backstop (athenaeum#373). No public API change; opt-in config knobs only.

> **Upgrade note:** `session-end --dry-run` changed semantics — it is now a
> cheap manifest-diff preview (no compile, no cluster/merge, no chromadb, no
> embedding-model load), not a whole-corpus compile-check. If you relied on
> `--dry-run` to smoke-test a compile, run `session-end --full` (or a real
> `run`) instead. See the behavior note below.

### Changed — behavior

- **`session-end --dry-run` is now a cheap manifest-diff preview (athenaeum#370).** It no
  longer compiles, clusters, or merges, and never opens chromadb or loads the
  embedding model — it reports intended-work counts (`new_or_changed`,
  `reindex.would_change`) from the manifest diff and exits. Previously a dry-run
  paid the full whole-corpus cost.
- **Incremental compile is delta-scoped on the `client=None` path (athenaeum#370).** A
  `session-end`/`ingest` now re-clusters and re-merges only the clusters a change
  actually touches (the changed file's prior cluster plus any above-threshold
  neighbors, closed to a fixpoint over cached embeddings); other `auto-*.md` are
  left untouched. The nightly LLM `run` stays whole-corpus. Correctness falls back
  to a full compile (logged) when a change can't be cleanly bounded.
- **`cluster_id` is now content-addressed** (was a positional sequence index) so
  full and delta runs mint identical ids. **One-time effect:** the first full run
  after upgrade rewrites every `auto-*.md` once as ids re-stamp; harmless (recall
  reads all `auto-*.md`) and self-healing.

### Added

- **Stat (mtime/size) pre-filter on the index and ingest manifests (athenaeum#370).**
  Unchanged files reuse their stored content hash instead of re-reading and
  re-hashing every wiki file on each build. A page whose validity window expires
  still drops with zero reads (the `valid_until` bound is recorded in the
  manifest). `--full` still forces a complete re-hash.
- **Self-healing periodic full re-hash backstop (athenaeum#373).** Config
  `librarian.reindex.full_rehash_max_age_days` (default 7): when the manifest has
  not had a full re-hash in that window, the next incremental reindex re-hashes
  every file (catching a content edit that preserved both mtime and size) while
  still applying only the delta — seconds, not a full re-embed.
- **Delta config knobs:** `librarian.delta.enabled` (default true),
  `librarian.delta.max_affected_clusters` (8), `librarian.delta.max_affected_members`
  (200) (athenaeum#370).

### Fixed

- **`fetch_embeddings` crashed on chromadb's numpy embeddings array** (ambiguous
  truth-value) whenever embeddings were returned, which had effectively broken
  vector-backend clustering; fixed on the read path (athenaeum#370).


## [0.14.0] - 2026-07-11

### Changed — behavior

- **Resolver source-precedence taxonomy expanded 7 → 9 tiers (athenaeum#328).** A new
  `agent-observed:<model>:<session-ref>` tier is inserted at **rank 5** (below
  `wikipedia`, above `claude:`), which re-ranks the lower tiers
  (`claude:` → 6, `script:` → 7, `model-prior:` → 8, `unsourced` → 9). Conflict
  resolutions that compare claims across these tiers can therefore reach a
  **different winner than in 0.13.x**. The change to the taxonomy itself is
  additive (no field removed); the behavior change is in ranking.
- **`repair --backfill-sources` rewrites the `source:` scalar of existing
  DEFAULTED `claude:inferred` memories (athenaeum#328).** With `--apply`, claims whose
  origin transcript shows the user stated them are lifted to `user:<ref>`
  (tier 1) and claims derived from in-session artifacts to
  `agent-observed:<...>` (tier 5). This raises their precedence, so **future
  resolutions over pre-existing data can change outcome.** Dry-run by default;
  only `DEFAULTED` inferred claims are touched; idempotent (a confirmed claim
  gets `inferred_verified: true` and is never re-examined).
- **Opinions no longer lose to precedence (athenaeum#327).** Pairs classified
  `claim_kind: opinion` with different (or unknown) asserters resolve to the
  new `attribute_both` action — both stay active, neither is superseded.
  Same-asserter dated opinions still supersede (newer wins). This changes the
  outcome of opinion-vs-opinion conflicts that previously picked a precedence
  winner.

### Added

- **Incremental indexing for both search backends (athenaeum#348).** Whole-file
  content-hash diffing rebuilds only changed/new/deleted pages; an unchanged
  corpus is a sub-second no-op instead of a full re-embed. Adds `--full` and a
  config seam for the embedding model (default `all-MiniLM-L6-v2` unchanged).
- **On-demand `athenaeum ingest` / `athenaeum reindex` (`--incremental|--full`) (athenaeum#349)**
  — change-gated compile + index with one-line JSON summaries, single-flight.
- **`athenaeum session-end` for cross-agent same-day recall (athenaeum#350).** A
  change-gated compile-then-index entrypoint: a `remember` written in one
  session becomes recallable in another after that session ends, without
  waiting for the nightly librarian. No-op when nothing changed.
- **`claim_kind` classification + `attribute_both` resolver action (athenaeum#327)**,
  including asserter-identity comparison (`same` / `different` / `unknown`)
  with a keep-both fallback when identity is unavailable.
- **`repair --backfill-sources` (athenaeum#328)** — re-classify DEFAULTED
  `claude:inferred` provenance from origin transcripts to `user-stated`,
  `agent-observed`, or confirmed-inferred.
- **Scoped claims: org/locale dimensions + three-way overlap verdict (athenaeum#329)**
  (DISJOINT / OVERRIDE / OVERLAP), disjoint scopes short-circuit to
  not-a-conflict without an LLM call.
- **Temporal `recall --as-of <date>` view and per-claim compiled validity (athenaeum#308).**
  A historical read-time view over validity windows, and per-source validity
  stamped into compiled entries.
- **`athenaeum compile --as-of <date> --out <dir>` — historical recompiled wiki
  view (athenaeum#359).** Distinct from the read-time `recall --as-of` filter: re-runs the
  deterministic C3 blend with `as_of` threaded into the per-member validity
  predicate, writing to a scratch dir (live wiki never touched, no LLM spend), so
  a member expired-as-of-today is re-included when the date precedes its
  `valid_until` close. Valid-time rewind; transaction-time replay is documented as
  deferred (the frontmatter model lacks per-member assertion timestamps).
- **`athenaeum serve` honors `KNOWLEDGE_RAW_PATH` / `KNOWLEDGE_WIKI_PATH` (athenaeum#355)**
  — each env var overrides its root individually; otherwise falls back to
  `<path>/raw|wiki`. Makes athenaeum's MCP server a drop-in for the standalone
  local knowledge server.

### Fixed

- **Resolver aggregate eval floor recalibrated + JSON-repair retry (athenaeum#345).**
  Two mislabeled golden fixtures corrected, the golden set enlarged 5 → 8, the
  floor re-derived with slack, and `propose_resolution` now retries once with a
  strict-JSON reminder when the first response has no parseable JSON object.
- **`TestBatchSyncEquivalence` no longer hangs locally (athenaeum#362).** The batch-mode
  test double now reports an immediately-completing batch at create time, so the
  poll loop's real 30s sleep is skipped under test (suite runs in seconds, not
  ~60s/test); production poll cadence unchanged.


## [0.13.13] - 2026-07-06

### Documentation

- **Surface the `claude-cli` subscription backend in the top-level docs
  (athenaeum#336).** The README environment-variable table now documents
  `ATHENAEUM_LLM_PROVIDER` (`api` | `claude-cli`, default `api`) plus
  `ATHENAEUM_CLAUDE_CLI_BIN` and `ATHENAEUM_CLAUDE_CLI_TIMEOUT`, pointing to
  `docs/configuration.md` → "LLM provider selection" as the source of truth.
  `SECURITY.md`'s scope section now names the `claude-cli` subprocess backend
  — argv-list construction (no shell interpolation), ambient Claude Code auth
  (no credential handling), and neutral-cwd invocation. Docs-only; no code or
  behavior change (the provider seam shipped in athenaeum#330 / v0.13.10).

## [0.13.12] - 2026-07-06

### Changed

- **Librarian: hardened the athenaeum#337 interrupt guard (post-athenaeum#338 review).** The
  writing phase (batch/sync branch through the terminal commit) is now
  wrapped in `try/finally`, so the SIGTERM/SIGINT handlers are restored on
  **every** exit path — normal, interrupt, or an exception from
  `rebuild_index` / the terminal `git_snapshot` — and can never outlive the
  run for an in-process caller. Documented that the partial-commit message's
  file count is tracked only by the synchronous loop: a batch-mode interrupt
  still commits any pages already written (clean tree) but reports `0
  file(s)` (accurate batch-interrupt accounting is athenaeum#236-adjacent and out of
  scope). No behavior change on the normal or synchronous-interrupt paths.

## [0.13.11] - 2026-07-06

### Changed

- **Librarian: a timeout-killed run no longer strands its compile output
  (athenaeum#337).** The pre-dawn sweep bounds the librarian with a wall-clock
  `timeout` (SIGTERM, then KILL after a grace). Previously a timeout landing
  between the start-of-run `pre-processing snapshot` commit and the terminal
  `librarian: processed N file(s)` commit left every wiki page written so far
  as an **uncommitted** working tree — silently absorbed by the *next* run's
  `git add -A` snapshot under a misleading "pre-processing snapshot" message.
  The CLI `athenaeum run` now installs a SIGTERM/SIGINT handler for the
  writing phase that commits the partial progress with a distinct, greppable
  message — `librarian: partial run (interrupted after N file(s), …CUE F)` —
  and exits `124` (matching coreutils `timeout`). **Interrupt-commit
  contract:** an interrupted run leaves the knowledge tree clean and its
  work attributed to a `partial run` commit, not the next run's snapshot.
  A normally-completing run is unchanged (still exactly one `processed N
  file(s)` commit). The handler is opt-in (CLI-only) so in-process callers
  (the MCP server, tests) keep their own signal handling. Newly relevant
  under the `claude-cli` backend (athenaeum#330), whose per-call subprocess latency
  makes timeouts more frequent.

## [0.13.10] - 2026-07-06

### Added

- **LLM provider seam + `claude-cli` subscription backend (athenaeum#330).** A new
  `athenaeum.provider` module centralizes LLM client construction behind
  `build_llm_client(config)` and `resolve_provider(config)` (env
  `ATHENAEUM_LLM_PROVIDER` > yaml `llm.provider` > `api`). Two first-party
  backends:
  - `api` (default): wraps `anthropic.Anthropic(...)` verbatim — params pass
    through **unchanged**, so prompt caching (athenaeum#230), the Messages Batch API
    (athenaeum#236), retries, and cost accounting are byte-for-byte identical to before.
  - `claude-cli`: drives the operator's ambient Claude Code **subscription**
    login via `claude -p --system-prompt <sys> --model <id> --output-format
    json`. No credential handling (same ambient-auth stance as the git-push
    path, athenaeum#284). The adapter mirrors `client.messages.create(**params)` so the
    compile-path call sites (`tiers`, `contradictions`, `resolutions`) are
    unchanged; the recall-time `query_topics` preprocessor stays on the `api`
    path by design (a per-recall subprocess would add seconds to every query).
  Constraints: `cache_control` is stripped on the CLI path (preserved on
  `api`); CLI rate-limit / timeout / transient exits map to
  `_retry.TransientAPIError` — caught downstream as a give-up so the affected
  file is deferred to the next run (not retried in-run); a missing `claude`
  binary fails loudly at startup; batch mode is **API-only** and `claude-cli` +
  `ATHENAEUM_BATCH_MODE` is a loud startup error (no silent fallback); and
  `claude-cli` token COUNTS are still recorded in `TokenUsage` while
  `estimated_cost_usd` reports **$0** (subscription-covered). See
  `docs/configuration.md` → "LLM provider selection (athenaeum#330)".

## [0.13.9] - 2026-07-06

### Added

- **Resolver interval-close on temporal supersession (athenaeum#308 slice 2).** When a
  resolution establishes a TEMPORAL supersession — the loser is
  *valid-then-replaced* history, not a wrong claim —
  `resolutions.enact_resolution` now stamps the loser's `valid_until` in
  ADDITION to the existing `superseded_by` mark (the close augments, never
  replaces, the mark; §8.4 of `docs/provenance-shape.md`). Triggers:
  - `keep_a` / `keep_b` close the loser at the **winner's `valid_from`** when
    known, else the **resolution date** (`date.today()`).
  - Sequential-snapshot `not_a_conflict` closes the **older** member's interval
    at the newer's lower bound (ordering by `valid_from`, else ingestion date;
    no reliable ordering signal ⇒ no stamp). Deliberately NOT added to
    `ENACTING_ACTIONS`, so the merge-pass suppress/drop routing is unchanged.
  - Never closes for `correct_*` / `forget_*` / `deprecate_both` /
    `retain_both_with_context` / `merge` / `propose_merge`.
  **Only-close-never-widen:** an existing earlier `valid_until` is preserved.
  **Boundary reconciliation with athenaeum#324:** `validity_windows_disjoint` uses a
  strict `<` on the inclusive `valid_until`, so `loser.valid_until =
  winner.valid_from` leaves the pair non-disjoint at the shared boundary day by
  design — safe because the superseded loser is also inactive via
  `is_inactive_memory`. No minus-one-day is subtracted. Exact stamped value
  pinned by `tests/test_conflict_resolution.py::TestIntervalCloseSlice2`.
  Follow-up athenaeum#329 generalizes the close to non-time scopes (org/locale).

## [0.13.8] - 2026-07-06

### Added

- **Detector skips disjoint-validity pairs; scope header in member snippets
  (athenaeum#324).** Two claims whose validity windows are DISJOINT (A true through
  March, B true from April) are sequential states of the world and cannot
  conflict — yet the C4 detector kept re-flagging them every compile, wasting a
  Haiku call and re-queuing already-answered pending questions. A shared
  `models.validity_windows_disjoint(meta_a, meta_b)` predicate (windows are
  disjoint iff one side's inclusive `valid_until` ends strictly before the
  other's `valid_from` begins; missing/malformed bounds fail open to "open →
  overlap → detect") now drives four new guards:
  - **Pre-detection short-circuit (merge.py).** When every undeclared pair in a
    cluster is pairwise-disjoint, the detector LLM call is skipped entirely and
    the cluster records `detected=False` with rationale `disjoint-validity` —
    mirroring the declared-relationship short-circuit (athenaeum#167). The
    similarity-sweep path skips disjoint pairs the same way.
  - **Post-detection guard (merge.py).** An otherwise-overlapping cluster can
    still have the detector flag a specific disjoint pair; that verdict is
    downgraded to `detected=False` (rationale `disjoint-validity`) BEFORE the
    escalation / pending-question write.
  - **Resolver synthetic (resolutions.py).** A flagged pair with disjoint
    windows that reaches the resolver returns `not_a_conflict` at confidence 1.0
    with no Opus call, checked before the declared-winner short-circuit.
  - **Scope header in member snippets (contradictions.py).** The detector prompt
    now renders a single TRUSTED `scope:` line per member (`valid: <from> →
    <until> · source: <source_type> · updated: <date>`, each segment omitted
    when absent/default) OUTSIDE the untrusted `<memory>` block, and the system
    prompt marks it as trusted temporal/provenance metadata. This lets the
    detector reason about temporal context for windows that DO overlap.

## [0.13.7] - 2026-07-06

### Added

- **Provenance/context header on `recall` hits (athenaeum#325).** Each recall hit now
  renders a compact metadata header between its `**Tags:**` line and snippet so
  a consuming agent can judge a fact's trust and currency without opening the
  page: a `·`-joined `**Source:**` (`source_type` + the date part of
  `source_ref`/`created`) · `**Updated:**` · `**Valid:**` (`<from> → <until>`,
  `open` for a missing bound) line, plus a `**Status:**` line pointing at
  `_pending_questions.md` when the page is contradiction-flagged. Every field is
  omitted at its default, so an uncontested, unscoped page adds at most one
  extra line and a page with none of the fields renders exactly the prior
  output. Pure render-time formatting from the fresh on-disk frontmatter the
  Layer-C audience re-check already reads — no index schema change, no reindex.

## [0.13.6] - 2026-07-06

> **Versioning note (pre-1.0 convention).** Additive features that do **not**
> add a new top-level CLI subcommand ship as **PATCH** bumps before 1.0; a new
> top-level subcommand is a MINOR bump. So the `### Added` sections in
> 0.13.1–0.13.5 (new flags/knobs, no new subcommand) are patches by policy,
> not oversight. The public `__all__` remains the stability surface.

### Documentation

- **Surface `serve --audience` where operators wire up agents.** The
  audience-scoped read access added in 0.13.4 (athenaeum#312) was documented only in
  `docs/configuration.md` / `docs/security-posture.md`. The README "MCP memory
  server" section now shows `athenaeum serve --audience`, states the default is
  full-wiki-readable, and clarifies it is a single-owner read filter (not a
  multi-user ACL). `SECURITY.md` gains a matching Scope bullet so a vetting
  engineer greps the canonical security file and finds it.

## [0.13.5] - 2026-07-05

### Added

- **Claim-level temporal validity — `valid_from` / `valid_until` foundation
  (athenaeum#308, slice 1).** Supersession was a flat boolean tombstone
  (`superseded_by` / `deprecated`) that cannot say *when* a fact stopped being
  true or answer "what did we believe on date X". This slice adds optional
  ISO-8601 date frontmatter — `valid_from:` / `valid_until:` — declaring the
  real-world window over which a claim is true, beside (not inside) the
  `source:` ingestion-time provenance (the bi-temporal split).

  - **Model + parse.** New `parse_valid_from` / `parse_valid_until` parsers and
    a single shared `valid_until_expired(meta, as_of=None)` helper in
    `models.py`; `AutoMemoryFile` gains `valid_from` / `valid_until` fields that
    round-trip through tier0 discovery byte-for-byte. `superseded_by` /
    `deprecated` are AUGMENTED, not replaced — they keep the winner pointer and
    both-stale flag; `valid_until` is the interval close.
  - **One predicate, two callers in lockstep.** The shared helper is wired into
    BOTH `is_inactive_memory(meta, as_of=None)` (dict path — recall) and
    `AutoMemoryFile.is_inactive(as_of=None)` (dataclass path — C3 compile) as a
    third disjunct, so recall and the compile can never disagree. Every
    live-knowledge read already routes through those predicates, so
    expired-`valid_until` claims are filtered **by default, everywhere**, with no
    call-site changes.
  - **Default-open interval (non-breaking).** Absent `valid_until` ⇒ open upper
    bound ⇒ still valid, so every existing page stays active — no migration, no
    backfill. A malformed/unparseable date **fails open** (logged, treated as
    active) rather than silently hiding a page.
  - **`as_of` designed in, `--as-of` not built.** The predicate takes an `as_of`
    parameter (default `date.today()`) so a later `--as-of DATE` view (slice 3)
    is plumbing, not a rewrite. Deferred: resolver auto-stamping `valid_until` on
    supersession (slice 2), the `--as-of` CLI (slice 3), per-claim validity
    (slice 4). Documented in `docs/provenance-shape.md` §8.

## [0.13.4] - 2026-07-05

### Added

- **Audience-scoped recall — fail-closed read access for secondary agents
  (athenaeum#312).** The MCP `recall` tool and the FTS5/vector/keyword recall index
  previously exposed the WHOLE wiki to any caller, with no read scoping. A
  scheduled routine (e.g. a Voltaire-style email-drafting agent) that needs
  operational knowledge could also read the owner's PII / client-confidential /
  financial pages. Recall is now scopeable to a restricted **audience** pinned
  by the operator at `serve` time, NOT chosen by the caller:

  - **Model.** A new `audience:` frontmatter list holds opaque role/group
    identifiers the operator maps onto an external RBAC (an Active Directory
    group, an app role, a routine name). The pre-existing schema-validated
    `access:` field (`open`/`internal`/`confidential`/`personal`) is reused as
    the coarse default: `access: open` is world-readable to every audience;
    `internal`/`confidential`/`personal` are owner-only unless an explicit
    `audience:` grant is present. Effective audience = roles-from-access ∪
    explicit `audience[]`.
  - **Serve-pinned, never caller-chosen.** `athenaeum serve --audience
    <role>[,<role>…]` (also `ATHENAEUM_AUDIENCE` env and `serve.audience` yaml,
    resolved CLI > env > yaml > owner) pins the server process. The `recall`
    tool takes NO audience argument, so a restricted agent cannot widen its own
    scope. No pin = owner = full access (untagged included) — existing
    single-user behavior is byte-for-byte unchanged.
  - **Fail-closed.** For a restricted caller, untagged pages, malformed or
    unparseable `audience:`/`access:`, and frontmatter parse errors all resolve
    to withheld (never "public"). One bad page degrades to withhold, never
    raises, so a scheduled recall can't be crashed by a typo.
  - **Three enforcement layers close every leak class.** (A) each page's
    effective audience is stored in the index at build time — an UNINDEXED FTS5
    column (kept out of the BM25 term space) and chromadb metadata. (B) the
    audience predicate is pushed INSIDE each backend query (FTS5 `WHERE` before
    `ORDER BY rank LIMIT`; chromadb over-fetch-then-filter; keyword authorize
    before scoring) so BM25/kNN top-k is computed over permitted rows only — a
    forbidden page can neither occupy a slot nor starve a permitted page. (C) a
    defense-in-depth re-check at the single render funnel re-reads fresh on-disk
    frontmatter, so a stale index (a page re-classified since the last rebuild)
    cannot leak a forbidden page's title, tags, snippet, OR body.
  - `athenaeum recall --audience` exercises the identical filter path for shell
    harnesses and tests.

  Intake-side secret/PII screening for `remember()` writes is deliberately
  split into its own issue — it is a distinct write-time mechanism with its own
  policy and failure modes.

## [0.13.3] - 2026-07-05

### Added

- **Warn-only wiki page-size guardrails (athenaeum#310).** Nothing bounded or flagged
  wiki page size; long pages blow the tier-3 merge budget (merges reproduce
  the whole body, so token cost scales with size), crowd out other recall
  breadcrumbs, and usually signal poorly-factored knowledge. `athenaeum
  status` now reports entity pages over a soft **warn** threshold
  (`librarian.page_warn_bytes`, default **8192**, env
  `ATHENAEUM_PAGE_WARN_BYTES`) and a louder **flag** threshold
  (`librarian.page_flag_bytes`, default **16384**, env
  `ATHENAEUM_PAGE_FLAG_BYTES`) via new `StatusInfo` keys `pages_warn` /
  `pages_flag` and an "Oversized pages (warn/flag): N/M" summary line.
  `athenaeum run` logs a non-fatal `WARNING` for each flagged page. The scan
  walks the same `wiki/*.md` set entity-counting walks (skips `_`-prefixed
  and non-entity files) and is purely observational — nothing is ever blocked
  or modified, and the tier-3 merge body cap is unchanged. The librarian
  proposing splits through `_pending_merges.md` is explicitly deferred
  (moscow:could) and NOT built here. Guidance on splitting long pages into
  linked sub-entities added to `docs/why-athenaeum.md`; knobs documented in
  `docs/configuration.md`.

## [0.13.2] - 2026-07-05

### Added

- **Single-machine run lock guards overlapping mutating runs (athenaeum#309).** There
  was no concurrency guard on `athenaeum run` (or the other mutating
  subcommands), so a nightly cron overlapping a manual run — or two sessions —
  could race whole-file wiki writes (lost updates), interleave block appends
  to `wiki/_pending_questions.md` / `wiki/_pending_merges.md` (corrupt block
  structure), double-spend the per-run API-call budget, and race the
  move-then-retire git ops. A new `athenaeum.runlock.RunLock` acquires an
  advisory `fcntl.flock` on `<knowledge_root>/.athenaeum.lock` (carrying the
  holder's PID / ISO-8601 timestamp / hostname for diagnostics) at the start
  of every mutating command: `run`, `ingest-answers`, `ingest-merges`,
  `reresolve-questions`, `rebuild-index`, `auto-memory prune --apply`,
  `repair --apply`, and `dedupe persons --apply` / `dedupe wiki-pages`
  (non-`--dry-run`). Read-only commands (`status`, `recall`, `serve`) and all
  dry-runs do NOT take the lock. Default behavior is fail-fast with a message
  naming the holder; `--wait <seconds>` blocks for the lock. `--force` breaks
  the lock even if a process is still holding it (logging the current holder
  first) — for overriding a live-but-hung run; a genuinely crashed run never
  blocks, since the kernel releases the `flock` on process death. **Scope is
  single-machine only** — no multi-machine coordination. Non-POSIX platforms
  without `fcntl` degrade gracefully (logged warning, no lock). A new
  `librarian.lock_timeout` knob (env `ATHENAEUM_LOCK_TIMEOUT`, default `0` =
  fail-fast) sets the default wait window.
- **Atomic sidecar appends (athenaeum#309).** Every append/rewrite of the
  `_pending_questions.md` / `_pending_merges.md` sidecars (and their
  `_archive.md` siblings) now goes through `athenaeum.atomic_io.atomic_write_text`
  — a same-directory temp file plus `os.replace`, so a crash mid-append can
  never leave a half-written file with corrupt `---` block structure.
  Defense-in-depth alongside the run lock.

## [0.13.1] - 2026-07-05

### Fixed

- **Cluster JSONL rotations no longer accumulate forever (athenaeum#311).** Every
  `athenaeum run` writes a timestamped `<stem>-<UTC-iso>.jsonl` rotation
  next to the canonical cluster report; nothing pruned them, so they grew
  unbounded (~365/yr). The cluster pass now prunes old rotations after each
  run, keeping the newest `librarian.rotation_retention` (default **30**,
  env `ATHENAEUM_ROTATION_RETENTION`; `0` disables). Rotations are
  debugging artifacts, not recovery-critical — recovery is git-based.
  Pruning sorts by the embedded UTC timestamp (deterministic, not mtime)
  and is non-fatal: a prune failure logs a warning and the run continues.

## [0.13.0] - 2026-07-05

Additive public surface since 0.12.0: a new `athenaeum ingest-merges` CLI
subcommand, following the 0.12.0 precedent that a new top-level subcommand
is a minor bump. The rest of this release guards against a real production
incident (24 bogus "Member N" wiki entities from internal scratch labels
leaking into classification) plus the near-duplicate reconfirmation bullets
it exposed, and hardens against a self-resolving-document injection surface.

### Added

- **`athenaeum ingest-merges` CLI command (athenaeum#299, athenaeum#303).** Archives
  resolved (`[x]`-checked) blocks out of the live `_pending_merges.md`
  sidecar into `_pending_merges_archive.md`, mirroring `ingest-answers`
  for the pending-questions sidecar. Nothing else drains this file, so
  it must be run periodically (e.g. alongside `ingest-answers` in your
  scheduled sweep) to bound its size — without it, decided merges never
  leave the live file, which is exactly how it grew to 5MB/67K lines in
  production before this command existed.

### Fixed

- **Tier 2 classification hallucinating placeholder-label entities
  (athenaeum#296).** `CLASSIFY_SYSTEM` guardrail plus a post-filter regex in
  `parse_tier2_entities` reject any classified name matching the exact
  "Member N"/"Member a" shape the pipeline's own contradiction/resolution
  prompts use as scratch labels, with a warning log on drop.
- **Tier 3 merge accumulating near-duplicate "confirmed again" bullets
  (athenaeum#297, athenaeum#302).** `MERGE_SYSTEM` now instructs folding a re-confirming
  observation into an existing bullet's footnotes instead of appending a
  duplicate. The `existing_body` input cap was raised from 4,000 to
  20,000 characters so the guard isn't blind on already-bloated pages,
  with the merge call's output budget (`max_tokens`) raised in lockstep
  and a `stop_reason == "max_tokens"` guard that refuses to overwrite a
  page with a truncated response, escalating for human review instead.
- **Self-resolving documents bypassing pipeline judgment (athenaeum#300, athenaeum#304).**
  `CLASSIFY_SYSTEM`/`CREATE_SYSTEM`/`MERGE_SYSTEM` now instruct treating
  an embedded claim of the document's own human confirmation/ratification
  as untrusted, not independent verification. A new deterministic
  `athenaeum.self_resolving` pre-processing pass additionally flags such
  claims in raw intake before Tier 2 classify sees it (both the
  synchronous and Batch API transports), as a code-level backstop
  independent of prompt compliance.

## [0.12.0] - 2026-07-04

Additive public surface since 0.11.0: wiki-page dedup clustering (existing
compiled `wiki/` entity pages are now clustered for merge proposals, not just
raw intake) and an opt-in post-run git push, plus the pending-merges
fence-parsing hardening that the wiki-dedup drafts depend on.

### Added

- **Wiki-page clustering against each other (athenaeum#290).** The merge-detection
  pass now clusters already-compiled `wiki/*.md` entity pages by
  topic/embedding similarity, not just `raw/auto-memory/*` intake, routing
  true duplicates through the existing `_pending_merges.md` /
  `resolve_merge` approval flow. New `athenaeum dedupe wiki-pages` CLI
  subcommand runs the pass standalone; `athenaeum run` also runs it
  automatically whenever `wiki/` exists (append-only proposals, failures
  are logged and non-fatal).
- **Opt-in post-run `git push` (athenaeum#284).** New `librarian.push_after_run`
  config knob (default off) pushes the librarian's commits to the
  configured remote/branch after a run completes, closing a recovery gap
  where processed knowledge stayed local-only. Uses the ambient git
  credential helper — no tokens handled in-process. Failures are
  non-fatal and logged with a greppable `athenaeum-push-failed:` prefix.
  See [`docs/configuration.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/configuration.md).

### Fixed

- **`pending_merges.py` fence-parsing bugs (athenaeum#289, athenaeum#291, athenaeum#292).**
  `_split_blocks()` now tracks fence state so `---`/`## ` lines inside a
  fenced `**Draft**:` body (YAML frontmatter, markdown subheadings) are no
  longer mistaken for block/paragraph delimiters, and a block whose fence
  is left unclosed no longer swallows the next block. `_split_blocks` and
  `_parse_block` now share one `_scan_fence_state()` helper so the two
  can't re-diverge, and a Draft body may nest its own fenced snippet using
  a different backtick-run length than the outer fence without
  prematurely closing it.

## [0.11.0] - 2026-06-29

Additive public surface since 0.10.0 (which was stamped but never released): a
read-only cross-entity claim detector, an auto-memory hygiene pass that stops
operational session-notes from becoming permanent wiki pages, and an opt-in
cluster-cohesion floor — plus an OSS-hygiene purge of operator identity
literals from the shipped package.

### Added

- **Cross-entity recurring-claim detector — `athenaeum claims --find` (athenaeum#272,
  slice 1 of athenaeum#258).** New read-only `recurring_claims` module extracts claim
  occurrences from wiki entities (footnote source claims per athenaeum#262, else a
  body-sentence fallback), groups cross-entity restatements via an injected
  embedding provider and pairwise cosine `>=` threshold, and renders a YAML
  report. Group keys are stable and order-independent (mirroring
  `fingerprint.claim_pair_fingerprint`). The new `athenaeum claims --find`
  CLI subcommand runs the detector over the recall-index embedding provider.
  **READ-ONLY: never mutates `wiki/`.**
- **Ephemeral auto-memory intake classifier (athenaeum#280, part 1 of athenaeum#278).** New
  `ephemeral` module with `classify_ephemeral` (raw intake) and
  `classify_ephemeral_page` (compiled page). Precision order:
  explicit `ephemeral: true` frontmatter flag > ephemeral-scope glob >
  conservative multi-signal operational markers (`>= 2`, default-empty).
  `discover_auto_memory_files` drops classified-ephemeral intake before
  clustering (logging each drop with its reason) and `merge_cluster_row`
  gains a secondary guard so a stale cluster row pointing at an ephemeral
  file can never materialize a page. Two new yaml knobs under `librarian:`
  drive it (config-resolved, no identity literals in `src/`):
  `ephemeral_scopes` (scope glob patterns, default-empty) and
  `operational_markers` (lower-cased content substrings, default-empty;
  needs `>= 2` to fire). See [`docs/configuration.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/configuration.md).
- **`athenaeum auto-memory prune` CLI for existing operational pages (athenaeum#280,
  part 2 of athenaeum#278).** New `auto_memory_prune` module builds a kill-list of
  operational `wiki/auto-*.md` pages using the same classifier (not loose
  keyword matching). `--dry-run` is the **default** (prints kill-list +
  retained-list with reasons, exits `2` when candidates exist); `--apply`
  `git rm`s only the listed files in one labeled commit and rebuilds the
  recall index. Recovery is git-only.
- **Cluster-cohesion floor for cross-scope over-clusters (athenaeum#281, athenaeum#278).** Two
  default-off yaml knobs under `librarian:` suppress the
  `similarity`-clustering path's low-cohesion blend pages (single-linkage
  chaining a coherent doc together with vaguely-similar session-notes from
  many other scopes): `min_cluster_cohesion` (float, default `0.0` = OFF —
  the cutoff is corpus-specific so operators opt in; `0.47` recommended for
  the reference corpus) and `min_cluster_cohesion_scopes` (int, default `4`).
  A cluster is withheld only when its centroid score is strictly below the
  floor AND it spans at least that many distinct origin scopes. Suppressed
  clusters are dropped before contradiction detection and the write loop and
  never reach the retire pass, so their raw members are left in place (not
  retired, not lost) for a coherent cluster to absorb later; they remain in
  the discovered file list so the ancestor-pool + similarity sweep still
  detect cross-scope contradictions involving them. Each suppression logs
  cluster id + centroid + scope count + reason.

### Internal

- **Purged operator identity literals from shipped `src/` (athenaeum#269).** OSS-hygiene
  pass removing personal names, usernames, and machine paths from the
  published package source. No behavior change; the runtime owner remains
  config-driven (athenaeum#263) and inert when no owner is configured.

## [0.10.0] - 2026-06-27

The expiring-intake-queue epic (athenaeum#259) lands as four slices. `raw/auto-memory/`
becomes a queue that drains into the wiki instead of a permanent store.

> **Upgrade impact — `athenaeum run` now MOVES and DELETES raw auto-memory by
> default (slice B, athenaeum#261).** Once the librarian compiles a cluster into its
> `wiki/auto-<topic>.md` entry and the contradiction detector runs clean, the
> new move-then-retire pass moves each non-contradictory raw fact into the wiki
> (as an origin-traced footnote) and **`git rm`s the raw file**. This is
> default-on. Recovery is git-only (a provenance-snapshot commit precedes the
> move+delete commit), so `git gc` / squash / never-pushing can lose retired
> raw. Contradictory, degraded-verdict, and pending-confirmation clusters are
> HELD, never deleted. Preview with `--dry-run`; disable with
> `athenaeum run --no-retire` or `librarian.retire: false` in `athenaeum.yaml`.
> See the README "Data lifecycle & upgrade impact" section.

### Added

- **Move-then-retire lifecycle for raw auto-memory (athenaeum#261, slice B of athenaeum#259).**
  New `retire.py`. After the C3 merge + C4 detection, `athenaeum run` MOVES
  non-contradictory raw facts into their canonical wiki entry (with footnotes
  and a `retired: true` marker) and `git rm`s the raw so it stops re-entering
  the nightly loop. **Default-on and destructive** (see the Upgrade impact
  callout above). Contradictory clusters, degraded detector verdicts
  (offline / API error / unparseable), and clusters whose members are
  referenced by open `_pending_questions.md` / `_pending_merges.md` entries are
  HELD — a delete never races a pending confirmation. The pass refuses to run
  without a git repo (recovery is git-only): a provenance-snapshot commit
  (commit A) precedes the combined wiki-update + raw-deletion commit (commit
  B). `--dry-run` computes the identical plan and writes nothing. The pass is
  opt-out via the `athenaeum run --no-retire` CLI flag or the
  `librarian.retire` yaml toggle (default `true`); when disabled the raw is
  neither moved nor deleted.
- **Origin-traced source footnotes for compiled facts (athenaeum#260, slice A of
  athenaeum#259).** The source schema gains `source_type`
  (`user-stated` | `external` | `document` | `inferred`, default `inferred`)
  and `source_ref`, rendered by `render_source_footnotes`. A new read-only
  `transcript_verify` module verifies user-stated claims against the session
  transcripts under `~/.claude/projects/<scope>/`, upgrading a footnote from
  the honest `inferred` default to `user-stated` / `external` when the
  transcript confirms it (never citing the raw `auto-memory/...` filename as
  the ultimate source). New citation policy at
  `policies/auto-memory-citation.md`.
- **Owner-singleton invariant (athenaeum#263, slice D of athenaeum#259).** A config-driven
  `owner` block (`config.resolve_owner`, never hardcoded in source) keeps the
  knowledge base's owner a single canonical person instead of fragmenting
  across commit-authorship and footnotes. Owner fragments auto-bind in
  `dedupe.py` via uid / full-name-alias / process-context / a new
  `google_contact` dedup join key, and owner-authored operational/exclusion
  memories (e.g. a family-relationships list) route to a standalone
  `reference` page via `owner.py` rather than polluting the owner bio. Entirely
  inert when no owner is configured, so the shipped package carries no personal
  identity.

### Changed

- **Retarget the contradiction engine from raw atoms to wiki footnotes (athenaeum#262,
  slice C of athenaeum#259).** The cross-scope similarity sweep
  (`cross_scope.cross_scope_similarity_pairs`) no longer cross-products
  `wiki/**` against itself. A new `require_raw_side` argument (default `True`)
  drops candidate pairs where both sides are wiki entries, so re-detection only
  compares NEW raw intake against the matching (topically-similar) wiki entry.
  With move-then-retire (athenaeum#261) deleting the raw atom on move, this collapses the
  number of detector/adjudication (Haiku/Opus) calls from O(corpus²) — one per
  topically-similar wiki pair — to **O(new intake + open contradictions)**. The
  sweep also short-circuits before the wiki embedding fetch + N² cosine loop
  when there is no raw intake at all, so an unchanged corpus with zero new
  intake does no corpus-scale work and produces 0 detector calls (instead of
  one per wiki-pair).
- **Persist the granular diff target on the wiki footnote (athenaeum#262).** When a fact
  is moved into a wiki entry, `retire.py` now stamps the atomic `claim` text —
  and a resolved `verdict`/disposition when one exists (a cleared detector
  over-fire or a declared supersession/refinement) — onto the fact's source.
  `merge.render_source_footnotes` renders both, and `merge._parse_one_source`
  round-trips them through frontmatter, so a future memory has a footnote-level
  thing to diff against now that the raw atom is gone. Both fields are optional
  and append-only; wiki entries written before this change parse unchanged.

### Deprecated

- **`contradiction.resolved_similarity_threshold` (athenaeum#211) and
  `contradiction.not_a_conflict_ttl_days` (athenaeum#251) are deprecated (athenaeum#262).** They
  existed only to babysit the permanent-raw design — suppressing re-detection of
  the same raw atoms forever, or for a TTL window. With retire-on-move +
  footnote-targeting the atom never re-enters the sweep, so both knobs are moot.
  They are **tolerated, not removed**: a config that still sets them keeps
  working and now emits a one-time deprecation warning
  (`fingerprint._warn_deprecated_suppression_knob`). Remove them from
  `athenaeum.yaml` to silence the warning; full removal is a follow-up.

### Known limitations

- **Wiki-vs-wiki drift not re-detected (#2, accepted per athenaeum#259).** Two facts
  that live only in the wiki (their raw originals retired) and that never
  attract a new topically-similar raw intake are no longer compared against
  each other, so a contradiction emerging purely between two settled wiki facts
  is not re-detected. Re-detection is intake-driven by design.
- **Page-level retrieval granularity (#6, accepted for this slice).** Candidate
  retrieval embeds the whole wiki PAGE, not each footnote, so a new claim
  contradicting a single footnote buried in a large multi-fact page may not
  clear the page-level cosine threshold and can go undetected. Per-footnote
  embedding is a tracked follow-up ("per-footnote embedding follow-up").

## [0.9.1] - 2026-06-18

### Fixed

- README image and doc links now use absolute `github.com/.../raw/main` and
  `.../blob/main` URLs so they render on the PyPI project page (which serves
  the README out of repo context). No content change beyond link targets.

### Changed

- Added a mechanical regression test asserting
  `fingerprint._SUPPRESS_VERDICT == resolutions.SUPPRESS_ACTION`, so drift in
  the locally re-declared suppress-verdict literal is caught by the suite
  rather than relying on a comment.

## [0.9.0] - 2026-06-18

_Honest per-run LLM cost accounting, and an incremental contradiction pass that stops re-paying to confirm already-settled conflicts._

### Added

- **Read-time decay of stale auto `not_a_conflict` suppressions (athenaeum#251).** A new
  `contradiction.not_a_conflict_ttl_days` knob (env
  `ATHENAEUM_NOT_A_CONFLICT_TTL_DAYS`, code default `0` = disabled, not seeded in
  `_DEFAULTS` per athenaeum#231) decays cached auto `not_a_conflict` verdicts at read
  time. When set `> 0`, an auto suppression whose `resolved_at` is older than the
  TTL is dropped from the confirmation-pass skip set (`merge.py`), so the
  claim-pair re-enters the Opus confirmation instead of being suppressed
  forever. A pure, injected-`now` helper
  `fingerprint.is_stale_auto_suppression(record, ttl_days, now)` decides
  staleness: only `resolved_by == "auto"` + `not_a_conflict` rows with a parseable
  `resolved_at` can decay; human verdicts and enacting auto verdicts
  (`keep_*`/`correct_*`/`forget_*`/`deprecate_both`) never decay, and a missing or
  unparseable `resolved_at` keeps suppressing (fail-safe for legacy/external
  rows). `now` is frozen once at run start (the same instant the re-clear stamps
  the fresh row with, so refresh resets the clock deterministically) and
  `merge_clusters_to_wiki` gains an additive `now=` keyword for test
  determinism. The append-only contract is unchanged — no row mutation,
  deletion, tombstones, or compaction; an expired row stays as history and is
  simply re-interpreted. Re-validation flows through the existing
  `resolve_max_per_run` cap, so a large expired backlog spreads across nights
  rather than spiking one Opus bill.
- **Incremental `not_a_conflict` caching for the nightly contradiction pass (athenaeum#249).** Auto-cleared `not_a_conflict` claim-pairs are now cached (`resolved_by: "auto"` rows in `raw/_resolved_contradictions.jsonl`) so the nightly Opus confirmation pass skips pairs it already settled, cutting a full re-confirmation run from roughly seven hours to minutes. A material edit to either claim changes the fingerprint and re-escalates that pair; a per-run write-dedup set bounds cache-file growth. Read-time decay of these auto verdicts (athenaeum#251) is a later, opt-in follow-up.
- **Usage accounting for the `ingest-answers` free-text path (athenaeum#248).**
  `propose_freetext_source_edits` (the last LLM call invisible to cost
  accounting) gains an optional `usage: TokenUsage | None = None` keyword and
  accumulates its response's token + cache counts via `add_tokens(...,
  model=<resolved model>)` — tokens and cache counters only, never an
  `api_calls` bump (the caller counts attempts, per the athenaeum#239 convention). The
  `ingest-answers` CLI path (`answers.ingest_answers`) now creates a run-level
  `TokenUsage`, threads it through `_writeback_source` into the proposer,
  bumps `api_calls` once per attempted proposer call at the call site, and
  logs one cost summary line (tokens in/out, cache written/read, estimated
  cost) at the end of a run that made >= 1 API call — mirroring the
  librarian's run-summary format. No summary is emitted when zero calls were
  made. The new parameter is keyword-defaulted so external callers are
  unaffected; the existing DEBUG-level per-call cache log is unchanged. No
  budget enforcement and no new config surface on this interactive path.

### Changed

- **Per-model cost attribution in `TokenUsage.estimated_cost_usd` (athenaeum#247).**
  Token-accumulation methods (`TokenUsage.add` / `add_tokens` /
  `add_batch_tokens`) gain an optional `model=` keyword; the call sites that
  know the serving model (tier-2/tier-3 in `tiers.py`, the C4 detector in
  `contradictions.py`, the resolver in `resolutions.py`, and the Batch API
  consumer in `batch.py`) thread it through. The estimate now prices tokens
  tagged with a known model at that model's rates from a module-level table
  (`claude-opus-4` → $5/$25, `claude-sonnet-4` → $3/$15, `claude-haiku-4` →
  $1/$5 per MTok; longest-prefix match so dated ids resolve), composing the
  existing cache-write (1.25x), cache-read (0.1x), and Batch API (50%)
  multipliers per model. Untagged or unknown-model traffic falls back to the
  blended $1.50/$7.50 rate, so Opus-heavy resolver runs — previously
  under-estimated ~3.3x — now bill at Opus rates. The change is additive:
  the scalar counters keep their current totals and existing `TokenUsage`
  constructors stay valid. No new config surface; rates are code constants.

## [0.8.0] - 2026-06-12

### Added

- **Opt-in Batch API mode for the librarian's tier-2/tier-3 calls (athenaeum#236).**
  `athenaeum run --batch-mode` (env `ATHENAEUM_BATCH_MODE`, yaml
  `librarian.batch_mode`; CLI > env > yaml > default off) restructures the
  entity-tier loop into phased fan-out against the
  [Anthropic Messages Batch API](https://platform.claude.com/docs/en/build-with-claude/batch-processing),
  which bills all token usage at a 50% discount: phase 1 batches every
  tier-2 classification, phase 2 batches every tier-3 create plus the
  tier-3 merges whose target page is touched exactly once this run —
  same-page merges stay synchronous, serialized in intake order. The
  run-level API budget (athenaeum#220) is enforced at batch-assembly time (each
  batched request counts as one `api_calls` attempt; the truncated
  remainder lands in the `_deferred_work.md` manifest), per-result
  `errored`/`expired`/`canceled` outcomes map onto the existing per-file
  failure path, and batch token usage — including prompt-cache counters —
  feeds `TokenUsage` with the 50% discount folded into
  `estimated_cost_usd` via new batch-attributed counters. The synchronous
  path is untouched when the flag is off. The C4 detector and resolver
  calls are not batched (tier-2/tier-3 dominate spend).
- **Resolver-phase cache observability (athenaeum#239).** The merge-phase
  contradiction detector (Haiku) and resolver (Opus) calls — including the
  athenaeum#188 reresolve heal pass — now feed their token and prompt-cache counters
  into the run-level `TokenUsage`, so the librarian run summary's
  `(cache: N written, N read)` line reflects resolver traffic instead of
  only the entity tiers. Previously these call sites only bumped
  `api_calls`. New `TokenUsage.add_tokens()` accumulates counters without
  incrementing `api_calls`, for call sites that count attempts separately.
- **Cache-aware `estimated_cost_usd` (athenaeum#239).** The API's `input_tokens`
  excludes cached tokens, so the run-summary cost estimate now folds in
  the cache counters at the documented multipliers — cache writes at
  1.25x the blended input rate, cache reads at ~0.1x — instead of
  silently omitting cached traffic.
- **Prompt caching on the resolver system prompt (athenaeum#230).** The
  contradiction-resolver call now sets a `cache_control: ephemeral`
  breakpoint on its static system prompt, so repeated resolver calls
  within a run hit the Anthropic prompt cache instead of re-billing the
  full prompt as fresh input tokens, and cache usage (cache writes /
  cache reads) is logged per call. Shipped via PR athenaeum#237; the
  resolver-phase cache observability work above (athenaeum#239) builds on these
  counters.
- **Canonical configuration reference at `docs/configuration.md` (athenaeum#233).**
  One page listing every operator-tunable knob — librarian run budgets,
  model selection, contradiction/resolver tuning, recall/search, and the
  hook/sidecar environment — with env var, yaml key, CLI flag, code
  default, and the global precedence convention (CLI > env > yaml > code
  default). Includes the `ANTHROPIC_BASE_URL` escape hatch for serving
  alternative models through a LiteLLM proxy or any Anthropic-compatible
  gateway (multi-provider support tracked in athenaeum#234). The README env table
  gains the previously undocumented rows (`ATHENAEUM_CACHE_DIR`,
  `ATHENAEUM_TIER4_DEDUP`, `ATHENAEUM_CROSS_SCOPE_MODE`,
  `ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD`,
  `ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP`, `ATHENAEUM_PQ_SNOOZE_HOURS`,
  `ATHENAEUM_PYTHON`) and links to the full reference; the duplicated
  config tables in `docs/auto-resolve.md` and
  `docs/contradiction-detection.md` are trimmed to link there instead.
- **`--max-files` gains env and yaml knobs (athenaeum#232).** The per-run intake batch
  size now resolves CLI `--max-files` > `ATHENAEUM_MAX_FILES` env >
  `librarian.max_files` yaml > default 50, mirroring the athenaeum#220
  `--max-api-calls` pattern. The flag also now validates positive integers
  at parse time (zero/negative/non-numeric values are an argparse error,
  matching `--max-api-calls`).
- **New `models:` yaml section for the env-only model knobs (athenaeum#232).**
  `models.classify` (Tier-2 classifier + C4 contradiction detector, env
  `ATHENAEUM_CLASSIFY_MODEL`), `models.write` (Tier-3 writer, env
  `ATHENAEUM_WRITE_MODEL`), and `models.topic` (recall query-topic
  extraction, env `ATHENAEUM_TOPIC_MODEL`). Per knob: env wins over yaml,
  yaml wins over the code default. The contradiction-resolver model is
  unchanged and stays at `resolve.model`. None of the new keys are seeded
  into config defaults, preserving the athenaeum#231 fix.
- **GitHub Release automation in `release.yml` (athenaeum#235).** After the PyPI
  publish job succeeds, a new `github-release` job creates the GitHub
  Release for the pushed tag, with the matching `[X.Y.Z]` section of
  CHANGELOG.md as the release notes. Idempotent: if a release already
  exists for the tag (e.g. cut manually with `gh release create`), the job
  skips cleanly instead of failing or duplicating. Uses the workflow's
  `GITHUB_TOKEN` with job-scoped `contents: write` — no new secrets.
- **Startup warning when the API budget resolves to 0 (athenaeum#235).** Env/yaml
  `0` is a valid defer-everything cap, but it is also the most likely
  accidental misconfiguration. `athenaeum run` now logs a prominent
  warning at run start ("API budget is 0 — all LLM tiers deferred this
  run...") so an unintended zero is diagnosable immediately rather than
  from the DEGRADED summary at the end.

### Changed

- **anthropic SDK floor raised from `>=0.30.0` to `>=0.39.0` (athenaeum#236).** The
  Messages Batch API surface used by the new `--batch-mode` requires SDK
  0.39.0. Environments pinned to anthropic 0.30–0.38 must upgrade the SDK
  before installing this release; the `<1.0` upper bound is unchanged.
- **README polish (athenaeum#235).** Tagline reworded from "production-grade" to
  "production-tested" (consistent with the honest pre-1.0 framing in Known
  Limitations), and the hero image moved below the value-prop line and
  shrunk so the install command stays above the fold on small windows.
- **CONTRIBUTING.md gains a project-continuity note (athenaeum#235).** One paragraph
  stating plainly that the project has a single primary maintainer today,
  and what users can rely on if it goes quiet: Apache-2.0 fork rights, the
  repo and history staying public, and releases reproducible from source.
- **docs/configuration.md records the env-0/CLI-0 asymmetry decision
  (athenaeum#235).** A design-decision note next to the budget table: CLI flags
  reject `0` (typo guard at the interactive surface) while env/yaml accept
  `0` as a deliberate defer-everything cap — intentional, decided
  2026-06-12, refs athenaeum#235 and the athenaeum#240 review.

### Fixed

- **Config `_DEFAULTS` no longer shadow module code defaults (athenaeum#231).**
  `load_config()` seeded concrete values for `contradiction.*` and
  `librarian.cluster_threshold` / `cluster_output` into every loaded
  config, so resolver functions always saw a "user-set" value and their
  own code defaults were unreachable. Most visibly, the athenaeum#187 resolver-cap
  raise (50 -> 250) never took effect through the config path — the
  default resolver cap was silently 50. Those keys are removed from
  `_DEFAULTS`; the module-level defaults (and their env > yaml > default
  precedence) are now live. The merge also passes through user yaml
  sections absent from `_DEFAULTS` (e.g. `resolve:`) instead of dropping
  them. Explicit yaml settings still win unchanged.

## [0.7.3] - 2026-06-11

Contradiction-pipeline reliability release, closing the silent-loss paths
surfaced by the 2026-06-11 nightly run. LLM JSON responses wrapped in
markdown fences or prose no longer drop clusters, and a run that exhausts
its API budget now says so loudly and leaves a manifest of the deferred
work instead of reporting a clean "Done".

### Added

- **`athenaeum run --path` as an alias for `--knowledge-root` (athenaeum#227).** `run`
  now accepts the same `--path` spelling as `init`/`status`/`serve`;
  `--knowledge-root` keeps working unchanged.
- **`athenaeum run --strict-budget` (athenaeum#227).** Opt-in flag that makes a
  budget-tripped (DEGRADED) run exit nonzero for exit-code-based alerting.
  Default behavior is unchanged: exit 0 with the warning summary and the
  `wiki/_deferred_work.md` manifest.

### Changed

- **Run-level API call budget is configurable and counts every call (athenaeum#220).**
  The budget now resolves `ATHENAEUM_MAX_API_CALLS` env var over
  `librarian.max_api_calls` yaml over the default, and the default is raised
  from 200 to 800 — quadrupling the default per-run API cost ceiling — to fit
  the post-athenaeum#187 full-coverage confirmation-pass profile. Operators who want
  the previous ceiling should pin it explicitly via the env var, yaml key, or
  CLI flag. The counter now includes merge-phase detector/resolver and
  nightly re-resolve calls, making it a true run-level ceiling. The CLI
  `--max-api-calls` flag validates positive integers and explicitly wins
  over env and yaml. The additive configuration surface here (env var, yaml
  key, deferred-work manifest) ships in a patch release because it exists to
  fix the silent-loss defect below; the cost-default change is the one
  operator-visible behavior shift worth reviewing before upgrading.

### Fixed

- **Lenient JSON extraction for LLM responses (athenaeum#219).** The contradiction
  detector and resolver parsed model output with a strict first-to-last-brace
  regex and silently dropped clusters when the model wrapped its JSON in
  markdown ```json fences or surrounding prose — 38 silent drops were
  observed in one nightly run. A new shared `extract_json_object()` helper
  (`athenaeum.json_utils`) prefers fenced content, applies an exactly-one
  rule for unfenced text, and returns None on ambiguity so callers keep
  their loud safe fallback. RecursionError is contained and decode
  diagnostics are logged at debug level. (PR athenaeum#221)
- **Fence pairing is robust to inline backticks, and the last legacy parse
  site is migrated (athenaeum#222).** Only line-leading ``` (CommonMark, up to three
  spaces of indentation) delimits a fence, so stray inline backticks can no
  longer shift fence pairing. When fences yield no object, the helper falls
  back to a whole-text exactly-one scan. `propose_freetext_source_edits` is
  migrated off the greedy first-to-last-brace regex. (PR athenaeum#223)
- **Librarian run-level budget exhaustion is no longer silent (athenaeum#220).** When
  the budget trips, the run now writes a `wiki/_deferred_work.md` manifest
  itemizing deferred intake (in-window and beyond-window, with failed files
  listed separately) and logs a warning-level `Done (DEGRADED — budget
  exhausted)` summary with deferred counts instead of a clean "Done". Stale
  manifests are cleared by the next clean run. (PR athenaeum#224)
- **`athenaeum init` now creates the `raw/auto-memory/` intake directory**, so
  first runs no longer warn about a missing extra-intake root.
- **Yaml `resolve_max_per_run: yes` (a bool) is no longer accepted as an
  integer cap of 1** — bools fall through to the default, using the same guard
  as `librarian.max_api_calls`.
- **`athenaeum test-mcp` now declares `sources=` on its own `remember()`
  call**, so the smoke test no longer trips the issue-athenaeum#90 provenance warning.

## [0.7.2] - 2026-06-09

Pending-question recurrence hardening. Free-text human answers now enact a
concrete source-file edit, the resolved-contradiction decision log matches by
member-pair and vector similarity instead of brittle exact-text fingerprints,
and a nightly self-heal pass re-resolves proposal-less escalations. Builds on
the 0.7.0 source write-back.

### Added

- **Vector + member-pair matching for the resolved-contradiction decision log
  (athenaeum#211).** The decision log keyed each record by a SHA-1 of the exact passage
  text the detector quoted; the detector re-quotes a drifting snippet every
  run, so the key never matched and an already-resolved contradiction
  re-escalated indefinitely. Matching now flows fingerprint → member-pair key
  → embedding cosine, with a configurable
  `contradiction.resolved_similarity_threshold` (default 0.83). Member-pair
  matching is deterministic and works without chromadb; the embedding layer is
  the optional `[vector]` extra and degrades gracefully when absent.
- **Nightly re-resolve pass for open proposal-less questions (athenaeum#188).** A
  question first escalated without a proposal (resolver budget exhausted or
  offline) previously stayed raw forever, because the open-pair dedup merged
  re-detections into the existing block instead of re-running the resolver.
  `reresolve_open_questions` (wired into `librarian.run` and exposed as
  `athenaeum reresolve-questions`) now re-resolves such blocks on a later,
  budgeted run: `not_a_conflict` drops/archives the question, a real verdict
  annotates it. Budget-aware, idempotent, and a no-op offline.

### Changed

- **Free-text answers enact a source edit, not just an annotation (athenaeum#210).**
  When a human resolves a contradiction with a free-text ruling (no verdict
  token), the resolver now interprets that ruling into a concrete edit of the
  source memory file(s) via an LLM-backed proposer and applies it through the
  existing write-back path, instead of appending a non-destructive annotation
  that left the contradictory claim in place. Falls back to annotation when no
  client is available or the proposer returns no edit.

### Fixed

- **Write-back resolves the true source files from `Members involved:` (athenaeum#214,
  follow-up to athenaeum#210).** The auto-memory contradiction blocks attribute their
  real source via a `Members involved:` line (refs relative to
  `raw/auto-memory/`) while their `source:` header names a compiled wiki page.
  The write-back only parsed `**Member paths**:` and resolved under `raw/` +
  `wiki/`, so on real blocks it resolved nothing and edited nothing. It now
  parses `Members involved:` and resolves under the configured intake roots.
- **Decision-log records persist a non-empty `member_key` and full `pair_text`
  (athenaeum#216, follow-up to athenaeum#211).** The human-resolution record site derived
  `member_key` from `pq.source` (a wiki page) and `pair_text` from the
  `**`-truncated `pq.description`, recording empty keys that the matcher could
  never hit. Both are now derived from the full raw block.

## [0.7.1] - 2026-06-08

Patch release addressing two follow-up nits from the athenaeum#207 Zenodotus review.
No behavior change under normal execution.

### Fixed

- Hardened the transient-retry exhaustion guard in `_retry.py` so it survives
  `python -O` (replaced an `assert` used for control flow with an explicit
  runtime guard); the exhausted-retries path still re-raises the captured
  transient error. (athenaeum#207)
- Resolved-contradiction cache records now write a single authoritative
  `action` key instead of duplicate `verdict`/`action` keys; the reader still
  tolerates legacy `verdict`-only records. (athenaeum#207)

## [0.7.0] - 2026-06-08

Pending-question recurrence fix. Answering an adjudicated contradiction now
writes the ratified verdict back to the source-of-truth memory files, the
resolved pair is fingerprinted so the detector stops re-flagging it, and a
newly-detected conflict that matches a prior human verdict is auto-applied
instead of re-escalated. Together these stop resolved contradictions from
regenerating on the next wiki build.

> **Upgrade impact — answering a pending question now edits your source
> memory files.** Before 0.7.0, resolving a pending contradiction only wrote
> a sibling `raw/answers/` provenance doc and left the source memory file
> untouched. As of 0.7.0 the ratified verdict is enacted on the
> source-of-truth file(s): `correct_*` / `forget_*` delete the wrong or
> transient member file, `keep_*` / `deprecate_both` write `superseded_by` /
> `deprecated` frontmatter markers, and `retain_both` / `not_a_conflict` add a
> non-destructive annotation. The `raw/answers/` provenance doc is still
> written. This is the intended fix for contradiction recurrence — but it
> means answering a question now authorizes modification (and, for
> `correct_*` / `forget_*`, deletion) of source memory content. Review
> verdicts accordingly.

### Added

- **Source write-back when answering a pending question (athenaeum#197).** Answering a
  pending question now writes the ratified verdict back to the source-of-truth
  memory file(s) via the existing `enact_resolution` machinery — `pq.source`
  plus every involved member — rather than only emitting a sibling
  `raw/answers/` provenance doc. `correct_*`, `forget_*`, `keep_*`, and
  `deprecate_both` enact destructively on the source; `retain_both` and
  `not_a_conflict` annotate non-destructively; the provenance doc is still
  written. This stops adjudicated contradictions from regenerating on the next
  wiki build.
- **Resolved-contradiction fingerprint cache (athenaeum#198).** Adjudicated claim-pairs
  now get a page-independent, order-independent fingerprint persisted to
  `raw/_resolved_contradictions.jsonl` on resolution (human or auto). The
  detector suppresses already-resolved fingerprints and logs the suppression
  count. A material change to a claim changes its fingerprint and re-enables
  escalation.
- **Auto-apply of prior human-ratified verdicts (athenaeum#199).** A newly-detected
  conflict that matches a prior **human** verdict is auto-applied without
  re-escalation and routed through source write-back. Only human-ratified
  verdicts auto-apply — prior auto-resolutions never do. The match is
  orientation-safe: per-side claim anchors are stored so the verdict is
  flipped to the new conflict's a/b orientation rather than deleting the
  correct claim. Unresolvable orientation or a failed enact falls through to
  escalation.

### Fixed

- **Failed auto-apply enact now escalates instead of silently suppressing
  (athenaeum#203).** If applying a prior verdict's enact fails (file-op error or no-op),
  the conflict escalates to a pending question rather than being silently
  suppressed.
- **Keep/deprecate verdicts are now enacted on the source (athenaeum#191).** Resolving a
  contradiction with a keep/deprecate verdict writes supersede/deprecate
  markers to the source memory file rather than only recording the decision.
- **Correct/forget verdicts are now enacted, not just recorded (athenaeum#166).**
  Resolving with a `correct` or `forget` verdict applies the edit to the source
  memory file. Adds a disambiguation mode for pairs that are distinct entities
  rather than a true contradiction, and routes `correct`/`forget` through the
  tier and merge render paths. Pending-question blocks that lost their checkbox
  line are now recovered rather than dropped.
- **Transient Anthropic overload no longer becomes a permanent librarian
  backlog** (athenaeum#193). The per-file classification path (`tiers.py` tier2/tier3
  calls) now retries HTTP 429 (`RateLimitError`), 529 (`OverloadedError`),
  and `APIConnectionError` with bounded exponential backoff + jitter (5
  attempts, capped at 60s, honoring `Retry-After` when present) via the new
  `athenaeum._retry.with_retry` helper. Previously a single overload window
  deferred every affected file to the next run, and because the same files
  landed in the same late position every night the backlog never self-healed.
  On final give-up the loop logs `Gave up after N retries (transient API
  overload)` distinctly from the malformed-file `Failed to process` line, so
  health reporting can tell transient-API from a genuinely broken file.
  Non-transient errors (e.g. 400 `BadRequestError`) still fail fast.

### Changed

- Resolver per-run Opus call cap (`DEFAULT_RESOLVE_MAX_PER_RUN`) raised
  50 → 250 (athenaeum#187). On a full-knowledge-base ingest the detector can flag
  well over 50 contradictions; at the old default the confirmation pass
  ran out of budget partway through and the surplus escalated raw into
  `_pending_questions.md` instead of being suppressed as `not_a_conflict`.
  The cap is a ceiling, not a target — small bases never approach it.
  Override via `contradiction.resolve_max_per_run` (yaml) or
  `ATHENAEUM_RESOLVE_MAX_PER_RUN` (env).
- **Destructive auto-DELETE bar raised to 0.95 confidence (athenaeum#166).** The
  auto-resolver now requires 0.95 confidence before applying a destructive
  DELETE, and the principled-escalation render path is locked so low-confidence
  conflicts escalate to a human rather than being auto-deleted.
- CI: bumped `dependabot/fetch-metadata` v2→v3 (athenaeum#194) and
  `1password/load-secrets-action` v2→v4 (athenaeum#195).

## [0.6.1] - 2026-05-24

Patch bundle: the self-reference lint added in athenaeum#173 now runs on every
`AutoMemoryFile` construction site, not just `discover_auto_memory_files`.

### Changed

- **Self-reference lint applied to all `AutoMemoryFile` construction sites**
  (athenaeum#181, athenaeum#183) — the lint that strips a memory's own name from its
  `refines` / `supersedes` lists (originally added in athenaeum#173) now also runs
  in the similarity-sweep path (`cross_scope.candidate_to_auto_memory_files`)
  and the cluster-shim path (`merge.merge_cluster_row`). Extracted to
  `athenaeum._lint._strip_self_reference` so all three sites share one
  implementation. Tests tightened to assert against rendered log messages.

## [0.6.0] - 2026-05-24

The librarian-reasoner epic (athenaeum#166) lands as a single backward-compatible
minor bump. The 0.5.x auto-apply + dedupe foundation now stands on a
richer reasoning surface: declared refines/supersedes relationships
short-circuit the detector, the resolver sees full-body context and
field-level provenance, the prompt taxonomy adds a `propose_merge`
action with a `_pending_merges.md` sidecar, and the auto-apply gate is
asymmetric per action so cheap suppressions auto-apply while wiki-body
mutations stay behind a higher bar. This release also bundles five
follow-up polish items (athenaeum#172, athenaeum#173, athenaeum#175, athenaeum#177, athenaeum#179).

### Added

- **Declared refines / supersedes in frontmatter** (athenaeum#167) — memories
  can now declare a relationship to a sibling memory via
  `refines: [name]` or `supersedes: [{name, as_of, reason}]` in YAML
  frontmatter. The detector short-circuits any pair (or fully-declared
  chunk) covered by a declaration, and the resolver surfaces both lists
  in the prompt so the LLM has the audit context even when the
  short-circuit did not fire.
- **Full-body resolver context with token budget** (athenaeum#168) — the Opus
  resolver now sees each member's full body (default 1500-token cap
  per side, char-heuristic) plus `created_at` / `updated_at` /
  `originSessionId` frontmatter and one-hop `[[wikilink]]`
  descriptions. Asymmetric truncation is normal; the conflict passage
  is always emitted regardless of body inclusion.
- **`propose_merge` action + `_pending_merges.md` sidecar** (athenaeum#169) —
  the resolver can propose a merged body for human review rather than
  picking a winner. Proposals land in `wiki/_pending_merges.md`;
  `list_pending_merges` / `resolve_merge` MCP tools triage them.
  Approval writes the draft merged body to wiki; rejection writes a
  `refines:` declaration into the first source so the detector's
  declared-refinement short-circuit suppresses the pair on future runs.
- **Asymmetric per-action auto-apply thresholds** (athenaeum#170) —
  `not_a_conflict` defaults to 0.75 (false-suppress is cheap; detector
  re-fires next run), `keep_a` / `keep_b` to 0.90 (mutates wiki bodies;
  higher bar), `propose_merge` NEVER auto-applies (the draft body must
  go through human review regardless of confidence). Configurable via
  `resolve.auto_apply_threshold_per_action.<action>`; the legacy scalar
  still applies to `keep_a` / `keep_b` only.

### Changed

- **`_filter_declared_pairs` prunes declared pairs from multi-member
  chunks** (athenaeum#172) — previously all-or-nothing: one undeclared pair
  sent the whole chunk (including already-declared pairs) to Haiku.
  Now members whose every partner in the chunk is declared are
  dropped before the detector sees the chunk.
- **Self-reference in `refines:` / `supersedes:` is dropped + warned**
  (athenaeum#173) — a post-load lint pass in `discover_auto_memory_files`
  silently strips entries that name the file's own memory and emits a
  `WARNING` log so the YAML authoring mistake surfaces without
  blocking ingest.
- **Per-call sibling-index cache + clarified `field_sources` semantics**
  (athenaeum#175) — the resolver now caches `slug → description` per scope dir
  per `_build_user_message` call instead of re-globbing + re-parsing
  per member per conflict (O(N·M·K) → O(N·M+K)). The prompt ships ALL
  `field_sources` keys (the earlier "filter to passage-substring"
  comment was aspirational, never wired up; full shipping is the right
  call for provenance-aware resolution).
- **Threshold error wording + gate-decision threshold returned to
  caller** (athenaeum#179) — non-numeric threshold values now say "not a numeric
  value" instead of "out of range" (latter implies a numeric typo).
  `_should_auto_apply` returns `(should_apply, threshold)` so the
  caller logs the resolved threshold from the gate decision rather
  than re-resolving via a second lookup.
- **MCP `resolve_merge` emits legacy aliases for symmetry with
  `resolve_question`** (athenaeum#177) — `block` (= `resolved_block`) and
  `error` (= `message` on failure) are now present on `resolve_merge`'s
  return shape. New callers should still prefer `error_code` +
  `message` + `resolved_block`.

### Backward compatibility

All changes are additive. Configs targeting 0.5.x continue to load
unchanged; the legacy scalar `resolve.auto_apply_threshold` still
applies to `keep_a` / `keep_b` so a pre-athenaeum#170 deployment behaves the
same after upgrading. Frontmatter without `refines:` / `supersedes:`
keys round-trips byte-for-byte.

## [0.5.0] - 2026-05-23

Closes the daily-backlog problem in the pending-questions queue. The Opus
resolver shipped in 0.4.x drafted proposals but never applied them; the
detector wrote one block per destination entity even when the underlying
source-memory pair was the same. On a 2026-05-23 sweep the queue carried
323 unanswered questions, 92% of which were duplicate firings of three
source-pair conflicts. This release adds the structural fix: auto-apply
high-confidence resolutions and dedupe escalations by source pair.

### Added

- **Auto-apply lane for high-confidence resolutions** (athenaeum#156, PR athenaeum#158) —
  when `auto_apply` is enabled and a `ResolutionProposal` reaches the
  threshold, `tier4_escalate` writes the question block as already
  answered (`- [x]`) with an `**Answer:** <rationale>` paragraph and an
  `**Auto-resolved**: true` audit tag. The annotation is additive — the
  resolver's `**Proposed resolution**` / `**Confidence**` /
  `**Rationale**` / `**Source precedence**` block is preserved. The
  rewrite is idempotent and round-trips through `ingest-answers` into
  both `raw/answers/` and `_pending_questions_archive.md`.
- **Configurable model and threshold** (athenaeum#156) — all three config
  surfaces are honored with precedence env > yaml > defaults:
  - Env: `ATHENAEUM_RESOLVE_MODEL`, `ATHENAEUM_RESOLVE_AUTO_APPLY`,
    `ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD`.
  - YAML: `resolve.model`, `resolve.auto_apply`,
    `resolve.auto_apply_threshold`.
  - Defaults: `claude-opus-4-7`, `auto_apply: true`,
    `auto_apply_threshold: 0.90`. Out-of-range threshold raises with
    the source named (`env` vs `yaml`).
- **Source-pair dedup at escalation time** (athenaeum#157, PRs athenaeum#159 and athenaeum#160) —
  before appending a new question block, `tier4_escalate` checks whether
  the same source-memory pair already has an open block in the file (or
  another item in the current batch). If so, the destination entity is
  appended to a `**Also affects**: a, b, c` line instead of creating a
  duplicate block. Falls back to a normalized passage-hash key when
  `Members involved:` is unsourced. Auto-resolved (`[x]`) blocks are
  excluded from the open-pair index so a resurrected conflict still
  produces a fresh question.
- **Highest-confidence-wins auto-apply on merge** (PR athenaeum#160) — when items
  collapse into an existing block, auto-apply is evaluated against the
  highest-confidence proposal seen for the source-pair key in the
  current batch, not just the first. Prevents a low-confidence primary
  item from suppressing a high-confidence sibling that would have
  triggered auto-apply on its own. Cross-batch case is also covered:
  an existing open block can be auto-resolved when a fresh batch brings
  a high-confidence proposal for the same pair.
- **Dedup escape hatch** (athenaeum#157) — `ATHENAEUM_TIER4_DEDUP=false` reverts
  to pre-athenaeum#157 always-append behavior. Default is ON.
- **`docs/auto-resolve.md`** — explains the audit trail, how to disable
  auto-apply (env or yaml), how to tune the threshold, and how to
  reverse an auto-resolution.
- **`README.md` Configuration section** — documents the three precedence
  layers and lists all three new keys with defaults.

### Changed

- `tier4_escalate` signature accepts a `config: dict | None = None`
  argument so the auto-apply gate can read the resolved model and
  threshold. Pre-athenaeum#156 callers passing `config=None` retain the prior
  always-append behavior — no auto-apply, no dedup.
- `EscalationItem.proposal` is a new optional attribute carrying the
  resolver's verdict through to `tier4_escalate`. Legacy callers that
  do not populate it are unaffected.
- `PendingQuestion.also_affects: list[str]` exposes the merged-entity
  list to `answers.parse_pending_questions` consumers.

### Internal

- 50 new tests across `tests/test_auto_resolve.py` (25) and
  `tests/test_tier4_dedup.py` (25), including real-`ResolutionProposal`
  integration tests for the highest-confidence-wins rule on both
  in-batch (Path B) and cross-batch (Path A) merge paths. Round-trip
  preservation of `**Also affects**:` through `apply_auto_resolution`
  and `ingest_answers` is asserted explicitly.

## [0.4.1] - 2026-05-22

A patch release hardening the auto-memory contradiction pipeline shipped
in 0.4.0.

### Fixed

- **Contradiction confirmation pass** (athenaeum#145) — the Opus resolver now runs a
  confirmation pass over Haiku-detector hits and suppresses detector false
  positives before they reach the pending-questions queue. Genuine
  contradictions still escalate, now with the resolver's proposed
  resolution attached.
- **Escalation dedup keyed on source-file set** (athenaeum#146) — escalations are
  deduplicated by the set of flagged source files rather than the cluster
  slug, so the same conflict surfaced by different clusters escalates once
  per run.
- **`add-to-project` CI workflow** — pinned to `actions/add-to-project@v1.0.2`.
  The previous `@v1` ref was never published by the action (only exact
  `v1.0.x` tags exist), so the workflow errored on every issue and PR.

### Internal

- Test coverage for the confirmation-pass resolver-verdict and
  malformed-response escalation paths (athenaeum#148), and the contradiction-merge
  `<2`-member fallthrough (athenaeum#146 review).

## [0.4.0] - 2026-05-11

This release ships three coherent streams of work: (a) the auto-memory
intake → cluster → merge → contradiction-detection pipeline (athenaeum#195, athenaeum#196,
athenaeum#197, athenaeum#198), which lets the librarian fold per-scope Claude Code memory
and other auto-captured turns into the wiki without manual triage;
(b) per-claim provenance, per-value `field_sources`, cross-uid dedupe,
legacy-slug repair tooling, and an Opus-backed contradiction resolver
(athenaeum#90, athenaeum#102, athenaeum#103, athenaeum#97, athenaeum#126, athenaeum#128); and (c) the Apollo connector
extraction and `athenaeum people` filter CLI (athenaeum#82, athenaeum#112). Includes **two
BREAKING changes**; see Removed.

### Added

#### Auto-memory pipeline
- **Auto-memory contradiction detection (C4)** (athenaeum#198) — see details below.
- **Auto-memory cluster merge (C3)** (athenaeum#197) — see details below.
- **Auto-memory cluster pass (C2)** (athenaeum#196) — see details below.
- **Auto-memory ingest path** (athenaeum#195) — see details below.
- **Claude Code auto-memory integration guide** (athenaeum#200) — see details below.
- **`raw/auto-memory` indexed as first-class recall source** (athenaeum#192) — the
  FTS5/vector index now ingests `raw/auto-memory/<scope>/*.md` alongside
  wiki pages so recall surfaces auto-captured turns before they’re merged.

#### Provenance, dedupe, and contradiction tooling
- **Per-claim `source:` on every CLAIM** (athenaeum#90) — every emitted claim now
  carries a typed `<type>:<ref>` provenance pointer.
- **Per-value `field_sources` for list fields** (athenaeum#102) — list-valued
  frontmatter (tags, aliases, etc.) carries per-value provenance instead
  of a single field-level source.
- **Tier 3 emits `source`/`field_sources` + `KNOWN_TYPES` allowlist** —
  the Sonnet writer now produces provenance-shaped output natively.
- **Cross-uid reference rewriter for dedupe** (athenaeum#103) — `athenaeum dedupe
  persons --apply` rewrites every cross-uid reference to the survivor uid
  in one pass; idempotent.
- **Opus-backed contradiction resolver with provenance precedence** (athenaeum#126)
  — `athenaeum contradictions resolve` calls Opus on flagged clusters and
  applies a deterministic source-precedence tie-breaker.
- **Cross-scope contradiction-detection mode toggle** (athenaeum#125) — per-scope
  / cross-scope contradiction detection is now configurable.
- **Pending-questions installable sidecar** (athenaeum#128) — `athenaeum questions`
  CLI (list / next / count) replaces ad-hoc grep against
  `_pending_questions.md`; consumed by the example SessionStart hook and
  the `resolve-questions` skill.
- **Legacy bare-slug repair migration** (athenaeum#97) — `athenaeum repair
  --legacy-source-slugs` rewrites pre-athenaeum#90 `source:` slugs to typed
  `script:<slug>` form; the live tree was migrated 2026-05-09 before the
  parser branch was retired (see Removed).
- **`athenaeum repair` CLI** — dry-run-by-default YAML-frontmatter repair
  for tag-indent corruption, missing fields, and legacy slug migration.

#### Tooling and ingest
- **`athenaeum people` CLI** (athenaeum#82) — frontmatter-only `type:person` filter
  (company / tag / tier / score, plus `--title-regex` / `--company-regex`).
  No LLM, no embeddings — deterministic over the wiki tree.
- **`athenaeum recall <query>` CLI** (athenaeum#71) — shell-accessible wrapper
  around the MCP recall tool; see details below.
- **MCP `remember(sources=…)` wrappers** (athenaeum#96) — the MCP `remember` tool
  now accepts an optional list of typed source pointers; the server
  routes them into the same provenance pipeline the librarian uses.
- **Init templates for entity-author markdown** (athenaeum#89) — `athenaeum init`
  scaffolds example entity templates so first-time authors have a working
  shape to copy.
- **`tier0_passthrough` preserves pre-structured raw-intake** — raw files
  that already carry `uid` + `type` + `name` round-trip byte-for-byte
  through the librarian without LLM tier costs.
- **Pydantic models + write-time validation** (athenaeum#88) — wiki frontmatter is
  validated against typed schemas at write time.
- **`extra_intake_root` config warns when missing** — stale config paths
  no longer fail silently at discovery time.
- **p95 search-latency benchmark harness** (athenaeum#69) — see details below.
- **Auto-memory contradiction detection (C4) (athenaeum#198) [details]** — new
  `athenaeum.contradictions` module runs one claim-level Haiku call per
  merged cluster to decide whether member bodies state or prescribe
  contradictory things (factual or prescriptive). Wires into
  `athenaeum.merge`: flagged clusters carry `status: contradiction-flagged`
  in their wiki frontmatter and append a round-trippable block to
  `wiki/_pending_questions.md` via the existing `tier4_escalate` helper.
  The C3 centroid-cohesion heuristic (`CONTRADICTION_COHESION_THRESHOLD`)
  is retired as the contradiction signal but kept exported for
  backwards-compatibility. Deterministic fallback: when
  `ANTHROPIC_API_KEY` is unset, every cluster reports `detected=False`
  with `rationale="llm-unavailable"`. Includes
  `scripts/measure_contradiction_baseline.py` for local corpus baselining.
- **Claude Code auto-memory integration guide (athenaeum#200) [details]** — new
  `docs/integrations/claude-code.md` documents the generic symlink-bridge
  pattern from `~/.claude/projects/<scope>/memory/` into
  `raw/auto-memory/<scope>/`, a citation frontmatter policy, and an
  end-to-end quick start. Adds `examples/claude-code/setup-symlinks.sh`
  (idempotent bridge with `--dry-run`),
  `examples/claude-code/stop-hook-validate.sh` (non-blocking citation
  validator), and `examples/claude-code/auto-memory-frontmatter.example.md`
  (reference memory file). `examples/claude-code/README.md` gains an
  "Auto-memory integration" section linking the three.
- **Auto-memory cluster merge (C3) (athenaeum#197) [details]** — new
  `athenaeum.merge` module consumes the C2 cluster JSONL and emits one
  consolidated wiki entry per cluster at `wiki/auto-<topic-slug>.md` with
  a deduped `sources[]` union (dedupe key: `(session, turn)`), propagated
  `origin_scope` per source, and a `contradictions_detected` heuristic
  flag (`centroid_score < 0.75`) for the C4 review queue. Size-1
  clusters ARE emitted as wiki entries; raw intake files remain
  untouched. New `--merge-only` CLI flag mirrors `--cluster-only` for
  iterating on merge output without re-embedding.
- **p95 search-latency benchmark harness (athenaeum#69) [details]** — new
  `tests/benchmarks/test_search_bench.py` checks in the ad-hoc benchmark
  used for the Session-2 recall budget as a pytest-benchmark fixture.
  One bench per backend (keyword, fts5; vector opt-in via
  `ATHENAEUM_BENCH_VECTOR=1`), asserts p95 stays within 20% of a locally
  pinned baseline. Ignored by the default `pytest` run (so CI stays
  fast); execute with `pytest tests/benchmarks/ --benchmark-only`.
  `pytest-benchmark` is an optional `[bench]` extra, not a runtime dep.
- **Auto-memory cluster pass (C2) (athenaeum#196) [details]** — new
  `athenaeum.clusters` module groups `AutoMemoryFile` records into
  near-duplicate clusters using the existing chromadb `VectorBackend`
  embedder (no parallel embedding pipeline). Single-linkage clustering
  with cosine cutoff configurable via `librarian.cluster_threshold`
  (default 0.55, tuned against the voltaire/nanoclaw regression fixture).
  Writes JSONL cluster report to `raw/_librarian-clusters.jsonl` with
  rotated timestamped siblings. New `--cluster-only` CLI flag skips the
  tier pipeline. C3 merge (athenaeum#197) consumes the JSONL output.
- **Auto-memory ingest path (athenaeum#195) [details]** — librarian now discovers
  files under `raw/auto-memory/<scope>/*.md` as a parallel intake channel
  alongside the entity-schema `discover_raw_files`. New
  `AUTO_MEMORY_FILE_RE`, `discover_auto_memory_files()`, and
  `AutoMemoryFile` record carry `origin_scope`, `origin_session_id`,
  `origin_turn`, `memory_type`, and `sources` through to downstream
  tiers. Discovery uses `resolve_extra_intake_roots()` so config is
  single-sourced with recall; `MEMORY.md` and `_migration-log.jsonl`
  are excluded; `_unscoped/` is ingested as a first-class scope.
  Clustering (athenaeum#196) and wiki merge (athenaeum#197) ship in subsequent lanes.
- **`athenaeum recall <query>` CLI (athenaeum#71) [details]** — shell-accessible
  wrapper around the MCP `recall` tool for validation harnesses and
  operator debugging. Prints one tab-separated hit per line
  (`<score>\t<filename>\t<preview>`). Respects configured `search_backend`
  and extra intake roots; `--top-k`, `--path`, `--cache-dir`, and
  `--backend` flags supported.

### Removed
- **BREAKING: retired `provenance._LEGACY_SCALAR_RE` and the legacy
  bare-slug `source:` parser branch.** Pre-athenaeum#90 wikis stored `source:` as
  a bare slug (`extended-tier-build`, `warm-network-detect`); the live
  tree was migrated on 2026-05-09 via
  `athenaeum repair --legacy-source-slugs --apply` (15,403 wikis rewritten
  to `script:<slug>`). The parser branch and matching schema/test fixtures
  retired in this PR. `provenance.parse_source` now raises `ValueError`
  on bare-slug input with a pointer to the typed `<type>:<ref>` form.
  External callers that still emit bare slugs must switch to the typed
  form. The migration tool (`repair.migrate_legacy_source_slugs`) keeps
  its own internal slug regex and ships unchanged for any future tree
  that needs it. Completes athenaeum#97 acceptance criterion "Remove
  legacy regex branch + its tests" which was deferred from athenaeum#120 pending
  live-tree migration. The field-keyed `field_sources` legacy reader
  (per `docs/provenance-shape.md` §2.3) is a different legacy form and
  remains accepted on read.
- **BREAKING: extracted `enrich` subcommand and `connectors/apollo` module**
  (athenaeum#112) — the Apollo people-match connector and the `athenaeum enrich
  --persons` CLI subcommand have been removed from the OSS package. Both
  were Kromatic-specific (operator-curated wiki + Apollo API key) and now
  live in a separate private toolkit alongside the
  rest of the Apollo enrichment scripts. The conflict-resolution
  lock document (`docs/conflict-resolution.md`) drops its former section 8
  (`enrich_person` + CLI write path); the conflict-resolution audit suite
  drops `TestEnrichPersonResolution` and `TestCliEnrichWriteFieldSourcesMerge`.
  No other resolver or schema is affected.

## [0.3.1] - 2026-04-21

Quine follow-ups from the 0.3.0 review. Two small hardening fixes on
error-path code; no behavior change under normal execution.

### Fixed
- **`answers._parse_block` no longer relies on `assert` for control flow**
  (athenaeum#64). Under `python -O` the assert is stripped and the subsequent
  `cb_match.group(...)` would raise `AttributeError` on malformed blocks
  instead of returning `None`. Replaced with an explicit `if cb_match is
  None: return None` guard so `-O` behaves the same as default execution.
- **CLI error output now includes the exception class name** (athenaeum#65).
  `Error: {msg}` → `Error ({ExceptionClass}): {msg}`. Makes operator
  triage and Sentry correlation meaningfully easier without changing the
  exit-code contract.

## [0.3.0] - 2026-04-20

Headline feature: **the Tier 4 escalation path is no longer write-only.**
`_pending_questions.md` now supports a full answer loop — humans (or
containerized agents via MCP) can resolve a pending question, and the
librarian ingests the answer as raw intake on the next run. This closes
the human-in-the-loop gap that shipped as a known limitation in 0.2.x.

### Added
- **`athenaeum ingest-answers` CLI** — scans `wiki/_pending_questions.md`,
  converts each `[x]`-checked block into a raw intake file under
  `raw/answers/{ISO-TS}-{entity-slug}.md` (with frontmatter linking back
  to the original source), then moves the block into
  `wiki/_pending_questions_archive.md`. Idempotent — re-running with no
  new `[x]` blocks is a no-op. Malformed blocks are preserved in place
  and logged to stderr, so a single corrupt entry cannot poison the
  rest of the file.
- **`athenaeum.answers` module** — new public surface exposing
  `ingest_answers`, `parse_pending_questions`, `list_unanswered`, and
  `resolve_by_id`. The parser tolerates both schema variants (blocks
  split on `## ` headers or `---` dividers) and emits structured
  `PendingQuestion` dataclasses.
- **Two new MCP tools** — `list_pending_questions()` returns unanswered
  blocks as JSON (id, entity, source, question, conflict_type,
  description, created_at), and `resolve_question(id, answer)` flips
  `- [ ]` -> `- [x]` and writes the answer body below the checkbox.
  Archival is intentionally NOT done at resolve-time — it runs on the
  next `ingest-answers` pass so the write path stays small and the
  archive step is atomic.
- **Checkbox render in `tier4_escalate`** — every new escalation now
  renders a leading `- [ ] {question}` line directly under the header.
  The question is derived from the first line of the LLM's description;
  an empty description falls back to
  `"Resolve {conflict_type} conflict for {entity}"`.
- **`tests/test_answers.py`** — new file covering round-trip,
  idempotency, mixed state, malformed-block tolerance, archive
  newest-first ordering, and MCP-helper happy + error paths.

### Changed
- **`src/athenaeum/__init__.py`** `__version__` bumped `0.2.2 -> 0.3.0`
  (was stale; pyproject was 0.2.3 on the prior release).
- **`tier4_escalate` schema** gained the checkbox line. Existing consumers
  that `.count("## [")` on the file for a pending-question total still
  work unchanged (the header format is preserved verbatim).

## [0.2.3] - 2026-04-21

Documentation and ops release accompanying the launch of
["What We Learned Running Our Own Operations on Agentic Memory"](https://kromatic.com/blog/agentic-memory-in-production/).
No library code changes — every existing install keeps working without
modification.

### Added
- **`docs/why-athenaeum.md`** — evergreen design-rationale doc. Covers the
  three problems that motivated Athenaeum, the four questions a production
  memory system has to answer, and a full comparison against Claude's
  built-in memory, Anthropic's memory tool, RAG, Karpathy's wiki gist, and
  the mem0/Letta/Zep/Cognee category. The four design choices (sources as
  first-class objects, the tiered librarian, passive recall, the editable
  observation filter) are mapped to which failure mode each one fixes.
- **`.github/workflows/promote-main.yml`** — fast-forward `develop → main`
  promotion workflow (workflow_dispatch) matching the rest of the Kromatic
  deploy-pipeline repos. Validates that `main` is a strict ancestor of
  `develop` and that required CI checks passed on the `develop` SHA before
  touching `main`. No merge commits are introduced on `main`, so history
  stays linear.

### Changed
- **`README.md` restructured** around the four design choices. Leads with
  "who this is for" and one-line pointers into `docs/why-athenaeum.md`;
  preserves install / quickstart / MCP / hooks / vector / env vars / data
  formats / known limitations below. The standalone Architecture bullet
  list is gone (now covered in `docs/why-athenaeum.md`).
- **`docs/assets/athena.png`** — replaced with the Athena illustration from
  the launch blog post.
- **`CONTRIBUTING.md`** — documents the develop-first branch flow
  explicitly: feature → `develop`, `develop → main` via the promotion
  workflow, no direct PRs to `main`. Explains why there is no staging
  branch (library repo; releases via PyPI/`release.yml`).

### Fixed
- **`SECURITY.md`** supported-branch statement. The supported release lives
  on `main`; fixes land on `develop` first and are promoted. The prior
  wording had these inverted.

## [0.2.2] - 2026-04-17

Pre-blog-post review pass. Non-breaking fixes surfaced by a final
persona-subagent audit (QA / Design / Developer / Operations / Strategy)
before the public write-up. No behaviour changes for existing users; every
item is hardening, doc clarity, or test-quality.

### Fixed
- **`remember_write` path-traversal guard** replaced string-prefix
  comparison (`str.startswith`) with `Path.is_relative_to`. The old form
  accepted `/a/raw` against `/a/raw-sibling` as a "descendant" — a
  traversal the filesystem treats as a sibling directory. The new form
  resolves both paths and uses the containment check.
- **`athenaeum status` pre-init hint** now matches `serve`: prints the
  remediation `Run 'athenaeum init --path ...' first, then retry.` instead
  of bailing with a bare "not found" line.
- **CI matrix installs `[dev,vector]`** so chromadb is in the test env.
  `TestVectorBackend` and the new hybrid rescue-class tests would have
  shown up as `importorskip` skips on GitHub Actions without this change.
- **CI actions pinned by SHA** to match `release.yml` (supply-chain
  hardening against tag retag attacks).

### Added
- **Hook README explains the two-phase pipeline** — new "How the sidecar
  works (read this first)" section makes the raw→wiki compile step
  explicit, plus a step-6 walk-through for scheduling periodic
  `athenaeum run` via cron or launchd, plus a troubleshooting row for
  "`remember` saves but `recall` finds nothing" (which is the expected
  state until compilation runs).
- **Main README "Known limitations (v0.2.x)" section** — no retrieval
  benchmarks yet, FTS5 rebuild non-atomic+unlocked, keyword backend is
  scan-on-query, Tier 4 is a file not a workflow. Sets adopter
  expectations up front instead of surfacing them in issues.
- **Main README vector-search section expanded** with the concrete
  rescue-class evidence (proper-noun collision: `Return Path`; no-overlap
  semantic query: `iterative feedback loops` → Innovation Accounting) and
  a pointer to `docs/recall-architecture.md`.
- **`tests/test_roundtrip.py`** — end-to-end integration pinning
  init → remember → seed wiki → rebuild-index → recall with an explicit
  `--cache-dir` (regression guard for the class of bugs where a path
  default drifts and layers still pass their own unit tests but no
  longer talk to each other). Also pins that the keyword backend works
  without any cache on fresh installs.
- **`TestHybridRescueClasses` in `tests/test_search.py`** — builds a
  synthetic wiki that exposes each backend's blind spot (short proper
  noun for vector; semantic no-overlap query for FTS5) and asserts each
  backend rescues its class while the other misses it. Without this
  pin, a future simplification that drops one backend still passes
  `test_query_finds_match` on obvious lexical queries and quietly
  regresses the rescue path.
- **`TestWarnIfBackendCacheMissing` in `tests/test_cli.py`** — exercises
  all four branches of the startup warning (keyword no-op, fts5 missing,
  vector missing, unknown backend) so the silent-zero-hits failure mode
  can't regress.
- **`tests/test_query_topics.py::test_returns_empty_when_api_raises`**
  extended with a `caplog` assertion pinning the WARNING-level log and
  the exception class name on API failures.

### Changed
- **Test assertions tightened** — several tests used `or` / loose
  containment where `and` / explicit-equality was meant. Most load-bearing
  of these: `test_recall_skips_underscore_files` (was masked by the
  trivial-pass shape `"score:" not in result or "_index" not in result`
  when "No wiki pages matched" was returned); `test_tiers.py:558`
  escalation description now asserts both `fintech` AND `pivot`;
  `test_shell_hooks.py::test_exits_clean_with_no_index` now asserts
  stderr is clean of `Traceback` / `syntax error` / `command not found`
  so "crashed quietly" can't pass as "correctly bailed."
- **`_snippet` offset math now pinned** by two new tests covering the
  match-near-start (trailing ellipsis) and match-near-end (leading
  ellipsis) branches.

## [0.2.1] - 2026-04-17

Final pre-public-announce review pass. No user-visible behaviour changes;
every fix is either first-adopter ergonomics, supply-chain hardening, or
internal abstraction hygiene so a future contributor can't trip on the
same sharp edges the review found.

### Fixed
- **`athenaeum serve` pre-init hint** — the "Run `athenaeum init --path {args.path}`" line printed the literal placeholder because the f-string prefix was missing; pre-init users saw `{args.path}` and lost trust
- `VectorBackend.query` now logs a WARNING with the exception class name when `get_collection` fails, instead of swallowing silently — "vector returns nothing" was the top first-adopter confusion in the v0.2.0 review
- `query_topics` API failures now log at WARNING (was DEBUG) with the exception class name — silent fall-through to the regex extractor hid degraded proper-noun recall

### Changed
- **Release workflow hardened** — all GitHub Actions pinned to full commit SHAs (supply-chain hardening against tag retag attacks); PEP 740 build-provenance attestations enabled
- **Search backend unification** — the in-memory keyword scorer is now a first-class `KeywordBackend` in `athenaeum.search`, registered alongside FTS5 and vector via the same `SearchBackend` Protocol; `mcp_server.recall_search` dispatches all three backends through one code path
- **`athenaeum serve` cache sanity check** — warns on startup when the configured `search_backend` has no index on disk (so recall doesn't silently return zero hits)
- `EntityIndex` now exposes `__iter__`, `__len__`, and `items()`; callers no longer reach into `_by_name` directly
- Public API types tightened: `status()` returns a `StatusInfo` TypedDict; `parse_frontmatter` / `render_frontmatter` use `dict[str, object]`
- Dependency upper bounds added to `anthropic` (<1.0), `fastmcp` (<3.0), `chromadb` (<2.0), `pyyaml` (<7) — surprise majors can't silently break a released wheel
- Wheel build explicitly includes `src/athenaeum/py.typed` and `src/athenaeum/schema/**/*.md` (was implicit via hatchling defaults)
- README links to GitHub-hosted docs instead of relative paths so they render on PyPI
- Claude Code call-out added to the README tagline pointing to the transparent sidecar section
- `examples/claude-code/README.md` leads with `claude mcp add` as the preferred MCP install recipe (matches canonical form)

## [0.2.0] - 2026-04-17

> **Adopter note.** The vector backend and the Claude Code hook flow ship
> working — but the hook flow assumes you follow the hybrid-recall pattern
> described in [`docs/recall-architecture.md`](docs/recall-architecture.md).
> Four load-bearing invariants (`set -a` around config sourcing, hybrid
> FTS5+vector merge, `hookSpecificOutput.hookEventName` wrapper, console
> API key vs `CLAUDE_CODE_OAUTH_TOKEN`) each shipped CI-green the first
> time they broke — every one of them is a silent failure mode. Read
> `docs/recall-architecture.md` before simplifying any of them.

### Added
- **Vector search backend** with chromadb + `all-MiniLM-L6-v2` (athenaeum#31, athenaeum#32)
- `athenaeum query-topics` CLI — Haiku-based query preprocessor that extracts substantive topics from instruction-heavy prompts and ignores meta-instructions (athenaeum#41, athenaeum#42)
- `athenaeum rebuild-index` CLI for on-demand index rebuilds (athenaeum#34)
- `athenaeum test-mcp` CLI subcommand for verifying MCP setup (athenaeum#36)
- Observation filter wired as the tunable capture authority for the sidecar flow (athenaeum#37)
- `athenaeum.yaml` config with `auto_recall` toggle and `search_backend` selection (athenaeum#30)
- Example Claude Code hooks — SessionStart builds the FTS5 index, UserPromptSubmit queries it per turn, PreCompact nudges save-before-compact
- FTS5-based per-turn wiki recall hook
- `docs/recall-architecture.md` describing the hybrid FTS5+vector pipeline, why each backend is load-bearing, and the four invariants a future "simplification" must not remove
- Example hooks (`user-prompt-recall.sh`, `session-start-recall.sh`) updated to match production hardening — `set -a`/`set +a` around config sourcing so child processes inherit `ANTHROPIC_API_KEY`; hybrid FTS5+vector merge; optional 1Password bootstrap of `ANTHROPIC_API_KEY` at SessionStart
- README sections for the `[vector]` extra and the `athenaeum query-topics` subcommand
- `SECURITY.md` with private disclosure process
- `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1)
- Issue templates (bug / feature) and PR template with a "hook changes only" live-run checklist
- Apache 2.0 `SPDX-License-Identifier` header on all Python source files

### Fixed
- **UserPromptSubmit hook JSON shape** — must be `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}`; a flat `{"additionalContext":...}` payload was silently ignored by Claude Code (athenaeum#39, athenaeum#40)
- **Stale chromadb state across rebuilds** — `VectorBackend.build_index` now nukes `vector_dir` and calls `SharedSystemClient.clear_system_cache()` to avoid "Collection already exists" on repeat builds in the same process (athenaeum#33)
- `serve` CLI now forwards `search_backend` + `cache_dir` to the MCP server (athenaeum#38)
- Recall now fires after the first message, not only at session start
- Stale "vector backend is a stub for issue athenaeum#32" comment in `search.py`

## [0.1.0] - 2026-04-16

Initial public release — a genericised extraction of the Kromatic
knowledge librarian.

### Added
- Tiered compilation pipeline (programmatic → Haiku → Sonnet → human escalation)
- Append-only raw intake with frontmatter-driven entity schema
- Configurable schemas and observation filter
- `athenaeum init` CLI to bootstrap a new knowledge directory
- `athenaeum run` pipeline CLI with `--dry-run`, `--max-files`, `--max-api-calls`
- `athenaeum status` — entity counts, pending files, last run
- MCP memory server (`athenaeum serve`) exposing `remember` and `recall` tools
- Token tracking and per-run cost estimates
- Test suite extracted from upstream + CI coverage enforcement (`>=75%`)
- Transactional writes, type-safety hardening, prompt-injection mitigation, API budget caps

[0.9.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.7.3...v0.8.0
[0.7.3]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Kromatic-Innovation/athenaeum/releases/tag/v0.1.0
