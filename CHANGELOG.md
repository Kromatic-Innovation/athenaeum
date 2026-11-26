# Changelog

All notable changes to Athenaeum are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

From 0.25.0 on, each release opens with a short `**Summary.**`
paragraph and its `**Upgrading from ...**` steps, followed by sections headed with the Keep a Changelog names, in
this order and only the ones that apply: `### Removed`, `### Changed`,
`### Added`, `### Fixed`, `### Security`. Each heading appears once per
release, so release-notes tooling can parse them. Release sections before
0.25.0 are in [CHANGELOG-archive.md](CHANGELOG-archive.md), kept as they were
written.

From the release after 0.29.0 on, a change the summary calls
security-relevant also gets an entry under `### Security`, so a reader who
scans the headings finds it. Likewise, a release that changes how
`sync.mode: integrate` behaves names that change in its
`**Upgrading from ...**` block for `sync.mode: integrate` users, even while
the mode is experimental.

Entries under `[Unreleased]` follow seven conventions, so a release section
can be read in a minute:

- Within each heading, behaviour changes come first. Docs and measurement
  entries open with `Docs:` and follow them as a trailing group, so the
  upgrade-relevant lines are found first.
- An internal slice name (such as `S3a`) is never used without a gloss
  saying what that slice does.
- Bullets are not separated by blank lines; a blank line comes only before
  and after a heading.
- When a release carries two themes, its summary opens with a one-sentence
  headline for each.
- A change the summary lists as a behaviour change goes at the head of
  `### Changed`, even when it also fixes a bug, so a reader who scans
  `### Changed` for what to check after upgrading finds it there.
  `### Fixed` holds fixes that only restore the behaviour already documented.
- A measurement entry is one or two sentences plus a link to its report; the
  numbers, intervals and follow-up issues live in the report.
- Measurement reports and eval-harness changes are not a theme. When a
  release ships any, its summary ends with one sentence naming them, for
  example "It also ships two measurement reports and an eval-harness change;
  neither changes behaviour." Each eval-harness change gets its own entry
  opening with `Eval harness:`, after the `Docs:` entries, rather than
  being folded into a report's entry.

The 0.30.0 section was revised after its cut to follow the conventions on
behaviour changes and measurement entries, and its Upgrading block gained the cost of the index refresh
`athenaeum run` now does; earlier release sections are kept as cut.

Release cadence and support: releases ship when their changes are ready, on
no fixed schedule, so sometimes several land close together (two minors can
share a date). Only the latest release is supported, and breaking changes
ship without a deprecation period. See the
[support policy](docs/guides/install.md#support-policy), and
[Running athenaeum in production](docs/guides/stability.md#running-athenaeum-in-production)
for how to pin and upgrade under it.

The bundled `promptwitness` package is experimental: it is not part of the
athenaeum public API, is not covered by semver, and is subject to change
without notice. From the release after 0.27.2 on, changes to it get their own
`promptwitness` line in the release section, separate from `athenaeum` entries.

## [Unreleased]

### Removed

- **BREAKING:** `librarian.corrections.sensitive_fields` and
  `config.resolve_corrections_sensitive_fields` are removed (athenaeum#3162).
  A correction now goes where the storage settings put the field, as for every
  other writer. A configuration that still sets the key to a non-empty value is
  refused by any run that processes corrections (the correction phase of
  `athenaeum run`, `athenaeum corrections-trust-dryrun`, and ratifying an
  escalated correction), before any batch is read and with no correction
  applied. The message prints the complete replacement
  `storage.excluded_fields.<class>` list, including the built-in contact fields
  (`emails`, `phones`, `former_emails`, `alt_emails`) for `pii`, because an
  explicit list replaces the built-in one. Commands that do not process
  corrections keep running and print the removed-key notice. To upgrade:
  delete the key and paste the printed `storage:` block into `athenaeum.yaml`.

### Added

- `storage.routes`: a list of rules that each send a page type, or chosen fields of one, or a sensitivity class, to one named location (athenaeum#3166). A rule is `page_type` (or `sensitivity_class`), an optional `fields` list and `to`, a list of exactly one location name. A field rule's target must be `sensitive: true`; a rule without `fields` is the long form of a `storage.mapping` entry, which stays; a `sensitivity_class` rule replaces the per-class lookup in value routing. Duplicate rules, a page type named in both `storage.routes` and `storage.mapping`, an unknown target and a non-sensitive field target raise `StorageConfigError`. Nothing is routed unless a rule says so, records already on disk are read through a rule without being moved, and `athenaeum init` writes a commented example and no active rule.

- `storage.history: git | none` (default `git`) declares whether a knowledge
  directory keeps history (athenaeum#3160). `athenaeum init --no-history`
  writes `none` into the configuration it creates and creates no repository:
  no git command runs. `StoreCapabilities` gains a declared `history` value;
  `versioned` keeps meaning "the location is in fact a git working tree".
  `storage.adapters.<name>.history` overrides the setting for one location.
  Under `none`, `Store.snapshot` returns `None` without calling git, and
  `athenaeum doctor` reports the declared value (and, under `git` with no
  repository, that destructive operations will be refused). Under `none` there
  is no restore point, no undo, and no history-based commands.

### Changed

- A field correction is written to the location `storage.route_for(page_type,
  field)` returns, and in place on the page when it returns none (athenaeum#3162).
  Default behaviour change: where contact fields are kept on a separate
  location (`storage.mapping` maps `pii` outside the corpus), a correction to
  `emails` on a person is now written to that location's record and not to the
  page, with no corrections-specific setting; previously the empty
  corrections table left it on the page. Installations that keep contact
  fields on the page are unchanged.
- With `storage.history: none`, raw retirement (`athenaeum retire`), batch
  retirement of processed correction batches and raw retirement by shape rules
  remove the file and report success instead of refusing for lack of a restore
  point (athenaeum#3164). Each logs once per run that no restore point exists.
  It follows the declared value, not whether a `.git` exists. Under `git`
  nothing changes, including the refusal when no repository is found.
  `athenaeum storage move-type --undo`, `athenaeum recover-parked restore` and
  `athenaeum pii-restore` print "no history" and exit 0 under `none`.
- The four `access:` levels are defined once (`models.ACCESS_LEVELS`) and the
  built-in contact field names once (`pii.BUILTIN_CONTACT_FIELDS`); the other
  copies are computed from them, with no behaviour change (athenaeum#3159).
- `backing_store` now names a registered store implementation (athenaeum#3165).
  `markdown` and `wiki-markdown` resolve to the filesystem implementation; any
  other value, which was previously accepted and built the same filesystem
  store, now raises `StorageConfigError` naming the known values. If you wrote
  a free-text value such as `files` or `sqlite`, change it to `markdown`.
  `storage.register_store_backend(name, factory)` registers another
  implementation, and `open_store` builds the store through that registry.
- One resolver now answers where a field or a page type is stored:
  `storage.route_for(page_type, field=None, *, config)` returns a frozen
  `RouteDecision` (`targets`, `rule`, `sensitive`) computed from the existing
  `storage.mapping`, `storage.excluded_fields`, `storage.excluded_read_mapping`
  and `librarian.corrections.sensitive_fields` keys. The excluded-field read
  path, `process_correction_record` and the free-text value-routing vault
  location read those tables through it, and a test fails the build if another
  module reads them. No behaviour change and no new configuration key
  (athenaeum#3158).
- The four `access:` levels are defined once (`models.ACCESS_LEVELS`) and the
  built-in contact field names once (`pii.BUILTIN_CONTACT_FIELDS`); the other
  copies are computed from them, with no behaviour change (athenaeum#3159).
- `athenaeum retire-pages` is now `athenaeum retire` (athenaeum#3151).
  `retire-pages` still works for this release as a deprecated alias that
  prints a note, and is removed in the next one. The override for the
  command's own guards is `--force-retire`, because `--force` is the run-lock
  flag shared by every locking command.
- A decision answer that does not name the fingerprint of the item it
  answers is no longer applied on an authority-routed item (`confirmation`,
  `proposed-rule`, `auto-apply-threshold`, `dimension-proposal`,
  `relevance-policy-edit`, `page-recompile`). It is held with error code
  `held_unconfirmed` and the item stays in the queue; answering again with
  the item's current `fingerprint` (from `athenaeum decisions list --json` or
  `list_pending_decisions`) applies it. On a competence-routed item
  (`question`, `merge`, `audit`, `coordinate`) such an answer still applies,
  marked `unverified`. Two more refusals apply to every answer: one naming a
  fingerprint the item no longer has ends `stale` and is not applied, and
  one whose answerer is an agent is refused on an authority-routed item
  (`agent_on_authority`). The checks run when the answer is applied, so they
  cover the immediate apply, the `ingest-answers` tick and answer files
  written by hand. An answer file already waiting on disk for an authority
  item is held the same way on the next tick (athenaeum#2530).
- The per-turn recall hook (`athenaeum-claude-hook`) now gives the topic
  extractor the previous turn as well as the new message: the previous user
  message and the end of the assistant's last reply, each cut to 1,500
  characters, read from the session transcript the hook payload names. It is
  still one extractor call per turn, with about 450 more input tokens on
  average. A message under 8 characters is now searched when a previous turn
  exists; with none it still gets no push. A missing or unreadable transcript
  falls back to the message alone. On by default; set
  `ATHENAEUM_PUSH_CONTEXT_CARRYOVER=0` to turn it off. This builds the arm the
  athenaeum#3104 measurement recommended (pooled strict push coverage +.043,
  95% CI [.016, .074], over 202 tasks). athenaeum#3105
- Every compile write path that creates a page now decides its file name
  style where it resolves the page's destination, through one helper,
  `destination_for_new_entity`. A store's `opaque_filenames` choice can no
  longer be skipped by a path that resolves the destination and forgets to
  apply it. A new test fails if any module resolves a destination or sets the
  flag any other way. File names are unchanged (athenaeum#3126).
- Docs: the README `## Status` section (formerly `## Project status`) now says
  Athenaeum is a side project: development continues at an irregular cadence,
  releases ship when changes are ready, and issues are triaged best-effort. It
  is not in maintenance mode and not archived (athenaeum#3184).
- Docs: the CHANGELOG preamble adds two conventions: a change the summary
  lists as a behaviour change goes at the head of `### Changed` even when it
  also fixes a bug, and a measurement entry is one or two sentences plus a
  link to its report. The 0.30.0 section now follows both: its `recall`,
  `athenaeum run` and past-revision-read changes moved from `### Fixed` to
  `### Changed`, and its four measurement entries are shortened
  (athenaeum#3122).
- Docs: the release sections for 0.24.0 and older moved, unchanged, from
  `CHANGELOG.md` to [CHANGELOG-archive.md](CHANGELOG-archive.md), which the
  preamble links. `CHANGELOG.md` now holds only the releases its heading
  contract covers, and `tests/test_changelog_contract.py` checks that the
  split stays that way (athenaeum#3132).
- Docs: the install guide and the 0.30.0 Upgrading block now say that
  `athenaeum run` refreshes the search index after compiling, what that
  costs, and that a refresh failure does not change `run`'s exit code.
  [A new measurement](docs/measurements/run-index-refresh-cost-2026-11-22.md)
  timed it with `scripts/bench_run_index_refresh.py` (athenaeum#3124).
- Docs: the README Quick start now states the Python 3.13 floor before its
  first install command, and says what a `No matching distribution` error
  from plain `pip` on an older Python means. The sample `athenaeum doctor`
  output and the `init` git-hooks note moved to the install guide, so the
  Quick start reaches its first `remember` and `recall` round trip sooner
  (athenaeum#3123).
- Docs: the CHANGELOG preamble now says that measurement reports and
  eval-harness changes are not a release theme: a release that ships any ends
  its summary with one sentence naming them, and each eval-harness change
  gets its own `Eval harness:` entry. `tests/test_changelog_contract.py`
  checks both for releases after 0.30.0. `pyproject.toml` records that
  `docs/measurements/` ships in the sdist, which the test suite reads, and
  not in the wheel (athenaeum#3125).
- Docs: the `read_blob` docstring now states which callers must pass `path`.
  A caller that compares, merges, displays or re-commits a blob a commit holds
  must pass it, to get filtered content; only a blob written by `write_blob`
  may be read raw. Both existing callers are classified at their call sites,
  and `tests/test_read_blob_callers.py` fails on an unclassified new caller.
  The three-way-merge ancestor read in `raise_supersede` is raw, and is wrong
  under a clean/smudge filter when the ancestor was recorded at raise
  acceptance; its comment says so (athenaeum#3127).
- Docs: the README Quick start now says that the Python 3.13 floor is a
  CI-scope choice, not a technical requirement, and that there is no plan to
  test or declare 3.11 or 3.12. `AGENTS.md` now gives the same floor as
  `requires-python`, where it said 3.11 (athenaeum#3131).
- Docs: the security posture (encryption at rest) now says that sync runs
  the clean/smudge filters the store's git config defines: store reads of a
  committed file and integrate-mode writes of a merged file execute the
  selected filter command during `athenaeum run`. It also says what that means
  for a store you cloned or copied from someone else, and how to check before
  the first sync (athenaeum#3128).
- Docs: the install guide's supported dependency ranges now say which
  versions CI tests and how much of the suite each one runs: the locked
  version (full suite), the newest release on the day CI runs (smoke subset)
  and the floor (SDK subset, advisory). Every other version in a range is best
  effort, which for `anthropic` includes whole majors; the guide says how to
  find the locked `anthropic` for a release. The `anthropic` range is
  unchanged, and the `pyproject.toml` comment no longer calls the SDK pre-1.0
  (athenaeum#3130).
- Eval harness: the nightly overflow-breadcrumb eval
  (`tests/evals/test_adapter_overflow_breadcrumb_1905.py`) reads the overflow
  notice again. The push now carries a top-hit excerpt and a linked-neighbour
  block after the candidates, and the eval took the last line of either for
  the notice, which failed the nightly tier. It now stops at the first line
  after the candidates (athenaeum#3135).

### Added

- A storage location can be marked `sensitive: true` (athenaeum#3161): a vault,
  where reading a value needs the explicit read flag. The built-in excluded
  location is sensitive; the wiki location cannot be. A sensitive location
  whose `corpus_policy` has any capability true is refused with
  `StorageConfigError`. `storage.is_sensitive(location, config)` is the one
  predicate. A custom location outside the corpus keeps its read behaviour
  whether or not it is marked, and `athenaeum doctor` warns for each such
  location that holds routed fields but is unmarked.

- `athenaeum retire` retires any information by uid, in whichever store holds
  it: a wiki page, a contact record or an off-corpus record (athenaeum#3151).
  A bare uid resolves to its store; `page:`, `contact:` and `off-corpus:`
  prefixes pin one. Only what the caller can access is retired: a uid on a
  store whose `_store/access.yaml` does not grant the owner `read` reads as
  unknown, and one without `compile` (or under an invalid declaration) is
  refused by name, refusing the whole run. Contact records are archived like
  pages (a provenance snapshot commit, then `git rm`) and dropped from the
  persisted contact-record index; off-corpus records are erased with both
  index shards and cannot be recovered. Retiring the only contact records of
  a live page needs `--force-retire`. `--pageless` selects every record
  `contact-surface-lint` reports as page-less, printing per uid the record
  count, each value masked, whether it also lives on a live record and the
  commit that added it. No output or commit message carries an unmasked
  address. Purging the erasure key stays out of scope.

- Decision answers now record who answered, where, and what they were shown
  (`answer_version: 2`). `athenaeum decisions answer` takes
  `--shown-fingerprint`, `--answerer-id`, `--answerer-kind`
  (`human`/`agent`/`unknown`), `--channel-host`, `--channel-surface`,
  `--evidence-strength` and `--evidence-ref`; the MCP tools
  `resolve_question`, `resolve_merge` and `review_audit_item` take
  `shown_fingerprint`, `answerer`, `channel` and `evidence`. Each surface
  records its own channel by default (`cli`/`tty` for the CLI, `mcp` with
  host `unknown` for the MCP tools), and agent triage records itself as an
  agent answerer with the fingerprint of the item it read. A field that does
  not fit is refused before anything is written (`invalid_answer_record`). A
  `merge` answer with a named answerer stamps `decided_by` as `human:<id>` or
  `agent:<id>` instead of `human:unattributed`. Each budget-ledger answer
  event now carries `channel.host` and `evidence.strength` (event version 2;
  version 1 events read as before) (athenaeum#2530).
- Spend records can now say who paid. Set `spend.account` in
  `athenaeum.yaml` (or `ATHENAEUM_SPEND_ACCOUNT`, which wins) to an opaque
  label such as `team-api-key`, and every new `spend.jsonl` record carries it
  as `account`. With no label set, records have no `account` key at all. A
  value that is not 1 to 64 letters, digits, `.`, `_` or `-` is dropped with
  a warning and the run still records its spend. The ledger schema moves to
  version 5; version 4 records read exactly as before (athenaeum#3146).
- A compile started by `athenaeum ingest --if-triggered` when a trigger fires
  now records its spend with `run_type: "librarian-triggered"`, so triggered
  spend is no longer counted as `librarian` (interactive and session-end
  runs). `ATHENAEUM_RUN_TYPE` still overrides it, and the drain advisor
  counts these rows like any other librarian run. A plain `athenaeum ingest`
  is unchanged (athenaeum#3146).
- Docs: the data-formats reference now documents the `spend.jsonl` fields an
  external reader may rely on, and the rules for reading them: only
  `billing_mode: "api"` rows carry money, `notional_usd` is never money, and
  one record is one run (athenaeum#3146).

## [0.30.0] - 2026-11-22

**Summary.** 0.30.0 has two themes. First, it acts on the 0.29.0 release
review: the README, install guide, support policy and CHANGELOG are clearer,
and the one uncapped runtime dependency is now capped. Second, it keeps
encryption at rest possible later: a store can opt in to file names that do
not reveal whose records it holds, and reads at a past revision now go
through the repository's smudge filters. It is a MINOR release because it
adds a per-store option (0.29.1 to 0.29.5 were never published).
`athenaeum.__all__` is unchanged.

For most users:

- Breaking changes: none.
- Behaviour changes: `recall` (CLI and MCP tool) drops a hit whose file no
  longer exists, and `athenaeum run` refreshes the search index for what it
  changed, so `recall` right after `run` returns the compiled page. The
  `witness-anchor` extra now requires `opentimestamps<1`. Reads at a past
  revision return what a checkout would write; with no clean/smudge filter
  configured the bytes are unchanged.
- The search index is not rebuilt by this upgrade.

Experimental: `opaque_filenames` on a `storage.adapters` entry is new and
off by default; it may change without notice. `sync.mode: integrate` stays
experimental. See `docs/guides/stability.md` for which surfaces count as
stable.

Security-relevant: opt-in `opaque_filenames` keeps the names of the people
and organisations a sensitive store holds out of its file listing, and
filtered reads at a past revision let a filter-based encryption scheme work
with `sync.mode: integrate`. athenaeum itself still encrypts nothing. Both
are listed under `### Security`.

**Upgrading from 0.29.0**

1. `pip install -U athenaeum` (or `uv tool upgrade athenaeum`), then
   `athenaeum doctor`.
2. If you install the `witness-anchor` extra, note it now caps
   `opentimestamps` below 1.0.
3. `sync.mode: integrate` users: a page edited on both sides is now merged
   on its plaintext when a clean/smudge filter (encryption, Git LFS) is
   configured, instead of being held. With no filter configured, integrate
   runs behave as before.
4. Scheduled `athenaeum run` users: `run` now refreshes the search index
   after compiling. It embeds only the pages the run changed, on the CPU,
   with no model calls. That added about 4 s for 5 changed pages on the
   `vector` backend and under 0.2 s on `fts5`
   ([measurement](docs/measurements/run-index-refresh-cost-2026-11-22.md)).
   A refresh failure prints a warning and does not change `run`'s exit
   code. No other steps.

### Changed

- `recall` (CLI and MCP tool) now drops a hit whose file no longer exists, and `athenaeum run` refreshes the search index for what it changed, so `recall` right after `run` returns the compiled page instead of an empty `[uncompiled]` hit for the raw note `run` deleted (athenaeum#3090).
- Reads of a file at a past revision (`read_file_at_revision`, and `read_blob` when given the file's path) now return what a checkout would write, by way of `git cat-file --filters`, instead of the committed bytes. With a clean/smudge filter configured (encryption, Git LFS pointers) `sync.mode: integrate` used to fail to decode the committed bytes and hold every page edited on both sides; it now merges the plaintext, and writes the merged page back through the clean filter. With no filter configured the bytes are unchanged. Applies to `sync.mode: integrate` (athenaeum#3076).
- The `witness-anchor` extra now caps `opentimestamps` below 1.0 (`opentimestamps>=0.4,<1`), like every other runtime dependency, so a future major release cannot silently break an installed athenaeum. A new test keeps every runtime dependency and runtime extra capped (athenaeum#3080).
- Docs: the CHANGELOG preamble now says that a release which changes `sync.mode: integrate` behaviour names it in that release's Upgrading block. A new end-to-end test, `tests/test_sync_integrate_e2e.py`, drives full integrate runs across two clones of a test store and asserts both clones' final state: a page changed on both sides is held, a uid collision is renumbered, and an erasure mark propagates (athenaeum#3085).
- Docs: the CHANGELOG preamble now states four conventions for `[Unreleased]` entries: behaviour changes first and `Docs:` entries as a trailing group under each heading, no internal slice name without a gloss, no blank lines between bullets, and a one-sentence headline per theme when a release carries two. The current `[Unreleased]` section follows them; released sections are unchanged (athenaeum#3082).
- Docs: the CHANGELOG preamble now says that a change a release summary calls security-relevant also gets an entry under `### Security`, and `tests/test_changelog_contract.py` checks it for every release after 0.29.0; the 0.29.0 section is left as cut (athenaeum#3081).
- Docs: `docs/guides/stability.md` has a new "Running athenaeum in production" section: pin an exact version, read each release's Upgrading block, upgrade one minor at a time, and note that security fixes ship only in the latest release. The support policy and the CHANGELOG preamble link to it; the policy itself is unchanged (athenaeum#3083).
- Docs: the install guide now says why Python 3.13 is the floor: nothing in athenaeum or its dependencies needs a 3.13 feature; CI tests one Python version at a time and the package declares only that one (athenaeum#3084).
- Docs: the README Quick start's sample `athenaeum doctor` output now reads `running X.Y.Z; latest release is X.Y.Z` instead of naming a release, so it no longer goes stale at each release (athenaeum#3078).
- Docs: the README Quick start now says, directly after its first code block, that the later steps (`athenaeum run`) need a Claude Code login or an `ANTHROPIC_API_KEY`, and links to where that is explained (athenaeum#3079).
- Docs: [a Linux clean-container install of the Claude Code plugin, re-run after its re-sync to 0.29.0](docs/measurements/plugin-clean-install-linux-resync-0.29.0-2026-11-21.md): the server starts, `remember` and `recall` work, and a configured `knowledgePath` is used; the two install-message gaps it found are fixed under `### Fixed` (athenaeum#3053).
- Docs: [the no-API-key first recall, re-verified on the published 0.29.0](docs/measurements/first-recall-no-api-key-0.29.0-2026-11-21.md): a fact told in one session is recalled in a new one; the stale index hit after `athenaeum run` and the plan-usage figures it found are fixed under `### Changed` and `### Fixed` (athenaeum#2307).
- Docs: [the shipped 0.29.0 push on the native-memory grid](docs/measurements/combined-push-grid-2026-11-21.md): leaving native memory on beside the push costs 34% to 68% more per correct answer at 1,000 and 3,000 pages for no gain in correctness, and the agent never reads it (athenaeum#3073).
- Docs: `docs/design/security-posture.md` has a new "Encryption at rest" subsection with a threat-model table per surface (local disk with and without full-disk encryption, the local index and caches, a hosted git remote, a cloud-synced folder, remote HTTPS mode, the model provider). It states that embeddings count as plaintext, that the cache directory belongs outside synced folders and only on encrypted backups, and that no at-rest option reduces what the model provider sees (athenaeum#3076).
- Docs: [carrying conversation context into the per-turn topic extractor, rounds 1 and 2](docs/measurements/context-carryover-round1-2026-11-21.md): adding the previous turn is the only arm that clearly beats prompt-only extraction (+.043 strict push coverage over 202 tasks), so it is built ungated as athenaeum#3105 (athenaeum#3104).
- Docs: `docs/guides/claude-code.md` has a new section 8 recommending, as an opt-in, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` when the per-turn push is installed. In the combined push grid, native auto-memory on cost 30 to 70% more per correct answer at the same correctness, and the agent opened a native memory file in 0 of 330 sessions. The section names what turning it off stops (new auto-memory files for the `raw/auto-memory/` intake; files already written stay readable), what it leaves alone (the push, `recall` and `CLAUDE.md`), and how to check. No plugin or hook default changes; the plugin and hook READMEs point to it (athenaeum#3094).

### Added

- A store can opt in to opaque page file names: `opaque_filenames: true` on a `storage.adapters` entry makes every NEW page written to that surface a uid-only `<uid>.md` instead of `<uid>-<slug-of-name>.md`, so a listing of the store no longer shows whose records it holds. Default off, so existing stores are unchanged; pages already on disk keep their names, and commit messages and page bodies are untouched. Recall, compile, `read_entity`, link resolution and `uid-collision-lint` find pages by uid and work unchanged (athenaeum#3077).

### Fixed

- The Claude Code per-turn hook now reads linked notes and parent names from the configured `KNOWLEDGE_ROOT` (environment or the cache `config.env`) instead of always `~/knowledge/wiki`, so an install whose store is elsewhere no longer silently loses both (athenaeum#3096).
- The plan-usage figures that `athenaeum init`, the README quick start and `docs/guides/install.md` quote now match what compiles cost: about 2 to 3 model calls, 3,200 to 7,000 tokens (in plus out) and 18 to 50 s per note, measured on 0.28.0 (2026-11-20) and 0.29.0 (2026-11-21), and 3 notes in one run took 7 calls, 9,233 tokens and 50.8 s. The old 0.26.0 figures sat above the 0.29.0 samples and below a 0.28.0 one (athenaeum#3092).
- `athenaeum status` no longer appends the claude CLI's raw JSON output to the not-logged-in compile failure reason; the reason now ends after the `/login` fix text and the raw output goes to the debug log (athenaeum#3091).
- The Claude Code plugin's `knowledgePath` option now carries its default in its title (`Knowledge base path (default ~/knowledge)`), which `claude plugin configure` prints, and its description opens with `Optional: leave unset to use ~/knowledge.`; the plugin README explains that Claude Code's `not yet set` notice at install is expected for this optional option (athenaeum#3098).
- The Claude Code plugin's missing-`uv` message now also says that Claude Code may keep the athenaeum MCP server skipped for about 15 minutes after a failed first connect, even once `uv` is installed, and that editing the plugin config (`/plugin`, or `claude plugin configure athenaeum@athenaeum-claude-plugin`) retries at once. The plugin README gains a troubleshooting note on the wait and on what the `still preparing` line means in that case (athenaeum#3097).

### Security

- Opt-in `opaque_filenames` on a `storage.adapters` entry keeps the names of the people and organisations a sensitive store holds out of its file listing (a host's web view, notification emails, CI logs). It covers new pages only: existing file names, commit messages and page bodies still carry names, and it is not encryption (athenaeum#3077).
- Store reads at a past revision now go through the repository's smudge filters, so a filter-based encryption-at-rest scheme cannot silently make `sync.mode: integrate` hold every concurrent edit. Nothing is encrypted by athenaeum itself; confidentiality on a librarian's machine still rests on full-disk encryption plus host access control (athenaeum#3076).

## [0.29.0] - 2026-11-21

**Summary.** 0.29.0 makes the per-turn push carry more of the answer: it now
ends with an excerpt of the top match and names the pages that match links
to, so a turn that needs one passage, or a page one link away, often needs no
`recall` call. It also adds more experimental groundwork for sharing a store
between several installs: per-store intake routing, raising pages between
stores and retracting them, declared store rights, a search index separable
by store, and a safer `sync.mode: integrate`. It is a MINOR release because it
adds features (0.28.1 to 0.28.13 were never published). `athenaeum.__all__`
and the declared dependencies are unchanged.

For most users:

- Breaking changes: none.
- Behaviour changes: each per-turn push is longer. The excerpt (default 600
  characters) and up to six linked-page lines are added after the bullets,
  inside the existing push-token budget and Claude Code's 10,000-character
  hook output cap. Set `ATHENAEUM_PUSH_EXCERPT_CHARS=0` (or
  `recall.push_excerpt_chars: 0`) to turn the excerpt off. `athenaeum recall`
  now prints a line to stderr on an empty result and adds a line when the
  query names something no returned page mentions.
- The search index is rebuilt automatically on the first run after upgrading
  (FTS5 schema version 7, vector metadata schema version 4). On a large
  corpus that run takes longer than usual.

Experimental and not called by a single-store install: intake routing,
the raise ledger and retract, `athenaeum.store_intake_dispatch`,
`athenaeum.store_reviews`, the erasure mark and the integrate-mode sync
changes build on the 0.28.0 federated-store groundwork. They may change
without notice. See `docs/guides/stability.md` for which surfaces count as
stable.

Security-relevant: an install now refuses to mount a declared store whose
`_store/access.yaml` does not grant it read, and a store's declared rights
can only narrow the write guard, never widen local config. Both apply only
to a surface that carries a declaration; a surface without one behaves as
before.

**Upgrading from 0.28.0**

1. `pip install -U athenaeum` (or `uv tool upgrade athenaeum`), then
   `athenaeum doctor`.
2. Expect the first run to rebuild the search index. No other steps.

### Changed

- Docs and eval harness: native memory against the push at 1,000 and 3,000 pages (the native `MEMORY.md` index truncated at Claude Code's 200-line cap, verified live), and a push carrying the linked neighbours of every hit. Native memory's advantage on `unprompted_push` tasks does not survive at that size; the neighbour push answers them at every size tested (athenaeum#3066)
- Docs and eval harness: a native-memory baseline across Haiku, Sonnet and Opus (built-in auto memory against athenaeum's push and pull), and one-factor tests on Sonnet of push shape, a recall instruction line, a coding plugin and a takeover arm, with an Opus spot-check. Native memory does better than the push on tasks that never mention the stored fact; an excerpt in the push cuts cost without losing answers (athenaeum#3061)
- Docs and eval harness: a host-config measurement of the `push_breadcrumb_pull` recall rate with tools upfront, deferred behind `ToolSearch`, and with a coding plugin. Deferral does not lower the pull rate on task-shaped probes (about 0.99 in all three); on `unprompted_push` tasks the pull rate is low everywhere and lowest with the plugin. Probe sessions for the pull-family arms now run in a fresh empty working directory instead of the repository (athenaeum#3057)
- `athenaeum push-metrics pull-diagnosis` now counts a native auto-memory topic read made through any tool, not only the built-in `Read`, `Grep` and `Glob`: a call whose input names a path inside an auto-memory directory (other than the `MEMORY.md` index), in a string field or a list such as a glob list, counts too. A write-capable tool (an edit or write tool) is never counted, and a field that mentions a memory path in prose is not a read. The JSON adds `native_topic_read_builtin` and `native_topic_read_other`, which split `native_topic_read` (athenaeum#3056)
- Docs: re-ran `pull-diagnosis` on the deployment with the athenaeum#3056 reader and corrected the native topic column in `docs/measurements/pull-diagnosis-2026-11-21.md` section 5. Interactive native topic reads are 14 of 679 pushed windows (2.1%), all through a non-built-in tool, against 42 (6.2%) Athenaeum pulls, so the conclusion is unchanged (athenaeum#3059)

- Docs: `docs/measurements/pull-diagnosis-2026-11-21.md` section 5 now holds the deployment counts (athenaeum#3038). In interactive pushed windows the agent pulled from Athenaeum in 6.2% of windows and read a native auto-memory topic file in none. `recall` was deferred behind tool search in every interactive session. By the note's rule, the lever for athenaeum#3032 is to make the push carry the answer.
- `athenaeum recall` no longer ends silently on an empty result: it prints one line to stderr saying nothing matched, and points to `athenaeum doctor` when the search index is missing or empty. A raw-intake hit now carries the `[uncompiled]` marker (at the start of the preview column, so the tab-separated columns are unchanged), using the same predicate as the MCP `recall` tool. Stdout for an empty result and the exit code are unchanged (athenaeum#3043)
- The `claude` CLI not-logged-in error now leads with the fix (run `claude` then `/login`, or set `ANTHROPIC_API_KEY` and use `llm.provider: api`) and puts the CLI's own output after it as detail, cut at a word boundary with `...` when long instead of mid-token (athenaeum#3044)
- Docs: the north star states shipped behaviour for raw intake (recallable, labelled `[uncompiled]`, ranked below compiled pages, so a note is available the same day) instead of forbidding it. Three method rejections (per-source parsers, an LLM as the shape detector, a separate short-term store) move to `docs/design/method-rejections.md`. Section 2.1 says the read interface includes uncompiled hits, and section 2.12 allows the filing checks to run right after publish for a value another checked writer already filed, with a failure opening a correction and never holding the publish. `docs/use-cases.md` section 3 now says federation is being built and names the four design notes and epic athenaeum#2631 (athenaeum#2809)

- Docs: `docs/design/host-cursor.md` is corrected against the Cursor probe (athenaeum#2395). The interactive CLI does deliver a Claude-format `UserPromptSubmit` hook's `additionalContext` to the model every turn, `beforeSubmitPrompt` carries `session_id`, `type: "stdio"` is not required, and `sessionStart` injection lands for hooks that finish within about 60 seconds. Claude-format hook groups need a `matcher` key, and `--print` mode does not run the prompt hook
- Clean-machine check of the Claude Code plugin on Linux, in a throwaway container with no mounts (athenaeum#2308). The plugin installs from its private self-hosted marketplace, but its MCP server cannot start as shipped: the launcher omits the `[mcp]` extra and nothing creates the knowledge directory. With three workarounds a fact was remembered, compiled on the Claude Code login and recalled in a new session. The first-session "still preparing" message and the uninstall check pass; the `uv`-missing message is written but discarded by `2>/dev/null`. Report: `docs/measurements/plugin-clean-install-linux-2026-11-20.md`.

- First recall without an API key, measured live on 0.28.0 (athenaeum#2307): with no `ANTHROPIC_API_KEY` and a Claude Code login, a fact told through `remember` was compiled by the default `claude-cli` provider and returned by `recall` in a new session, and the no-login counter-example now names what is missing in `init`, `doctor` and the failed run. A single-note compile took 3 calls, 3,625 to 7,025 tokens and 24 to 50 s; the CLI gives no share of the plan's limit. The counts are in `docs/measurements/first-recall-no-api-key-2026-11-20.md`.
- `recall` now says so when the query names something no returned page mentions. A capitalised query word that appears on none of the pages shown (`Yusuf Adeyemi` when no page names him) adds one line after the results, telling the agent that the store most likely has no page on that name, so searching again for part of the name or another spelling will not help. It targets the abstention cost on the native-memory grid, where the verdict arm spent four `recall` calls per unknown name before saying it did not know. Over every `recall` call stored from run 44783114473, the line would have appeared on 49 of 71 abstention calls and on none of 451 others. The per-row counts and projection are in `docs/measurements/abstention-cost-2026-11-21.md` (athenaeum#3033).

- North-star grid: the oracle control now tolerates one delivered-page omission. When an enrolled class and scale has failing `oracle` rows that are all marker misses with delivery, and at most one probe is involved, that (probe, scale) pair is excluded from every arm, native arms included, for conditions 1, 2 and 3, and the report names it in an "Excluded by the oracle tolerance" table; a second failing probe, or a miss without delivery, still voids. `spend_approver_named` gains the alternative marker `requester explain` on its second page. `grader_revision` moves to `athenaeum#3029`; `corpus_digest` does not (pages are untouched). Run 44783114473's stored rows are re-read offline in `docs/measurements/native-memory-baseline-2026-11-20-regrade.md` (athenaeum#3029).

### Added

- The per-turn push now carries the linked neighbours of its hits: the pages the matched pages' `related` edges point to, pooled across every hit, deduplicated, never a page the bullets already name, the neighbours of higher-ranked hits first, at most six lines. Each line is `name — description`, under a `Linked from the matches above:` line after the excerpt, so the page that answers a task one link away from what matched is named without a `recall` call. A dangling, retired, tombstoned or superseded target is left out, and a hit with no `related` edges adds nothing. The block takes only what the push-token budget and Claude Code's 10,000-character hook output cap leave after the bullets and the excerpt; a line that does not fit is skipped, never cut. Its tokens count toward the top page's `token_cost`, and the push ledger row records how many lines were pushed (`neighbour_lines`, shown by `athenaeum push-metrics tail`). `build_context` takes `neighbour_lines` (default `0`, no block) and needs `wiki_root` to read the edges; `build_context_for_turn` passes six, so the Claude Code hook pushes it. Neighbours are not added to the session's seen list (athenaeum#3070)
- The per-turn push now ends with an excerpt of its top match: the passage of that page which matched the message, under a `Top match, excerpt of "<name>":` line after the bullets, so a turn that needs only that passage gets it without a `recall` call. Length comes from `ATHENAEUM_PUSH_EXCERPT_CHARS` or `recall.push_excerpt_chars` (default 600, at most 2000; `0` turns it off), and the excerpt is cut further to fit the push-token budget and Claude Code's 10,000-character hook output cap, or left out when too little room remains; bullets are never trimmed for it. Its tokens count toward the top page's `token_cost` in the envelope and the push ledger. An index without a `body` column pushes no excerpt. `build_context` takes `excerpt_chars` (default `0`, no excerpt) and `build_context_for_turn` resolves it, so the Claude Code adapter, `athenaeum context` and the Codex adapter push the same excerpt (athenaeum#3032)
- `examples/claude-code/CLAUDE.md.example` has a "Per-turn push" section ending with a one-line instruction: when a pushed item looks relevant, call `recall` yourself before answering. The Claude Code guide and the hook setup steps document it, and the setup steps give a command that copies that line from the shipped example into `~/.claude/CLAUDE.md` once (athenaeum#3032)
- `athenaeum push-metrics pull-diagnosis` counts, per turn window that got a proactive push, how often the agent pulled: `recall`/`read_entity` calls, reads of a native auto-memory topic file (the `MEMORY.md` index and `Bash` reads counted apart), and `ToolSearch` calls for `recall`. It also counts, per session, whether `recall` was deferred behind tool search, whether its server failed, and whether it was called. Counts and rates only; read-only, no model calls. The diagnosis note `docs/measurements/pull-diagnosis-2026-11-21.md` sets out what the published counts already settle, and leaves the deciding counts for a run of this reader on the deployment (athenaeum#3032)
- Sync: the erasure mark. `erasure.erase_pages_and_commit` deletes pages and commits the deletion with one `Athenaeum-Erasure: <uid>` line per page (a uid and nothing else, no name, path or reason). Integrate reads the history between the common ancestor and each side through the store seam (`store.deletion_commit_messages`); a page deleted on either side by a commit marked for its uid is deleted in the merge even when the other side edited it, and the merge commit records `Athenaeum-Erasure: <uid>` for it with no page line. A deletion without the mark is still an ordinary retire, contested against an edit (athenaeum#2998)
- Intake routing S3a (athenaeum#2999): the compile write path (sync and batch transports, page recompile) takes a new page's store from the intake decision (`intake_destination`), with `storage.mapping` as the default store, so every page lands where it did before. A decision naming a store this install does not hold or cannot write fails closed to the personal store and is reported.
- A retract now applies in the target (athenaeum#3003, raise S11). `raise_retract.apply_retract` finds the copy by its derived uid, follows a tombstone, and removes an untouched copy (projected payload hash equal to `raised_from.content_hash`) through `apply_retirement`, with `Athenaeum-Raise: <raise_id> seq=<n> retract` on the removal commit. An edited copy raises a remove-or-keep decision (`answer_retract`; kept sets `watch: ended`), a still-waiting raise is withdrawn, and a retract that did not come from the raise's proposer and location becomes a decision. The store raw-intake run registers it for the `retract` kind.
- A store's raw intake on its single compiling install now lives at `raw/_stores/<store_id>/` (athenaeum#3000, intake routing S15). The discovery that feeds classify skips it, so nothing there reaches the capture gates, `decide` or the member's personal classify path; the intake run hands each file to a per-store dispatcher (`athenaeum.store_intake_dispatch`, a handler registry keyed by message kind, `raise` acceptance first). A message naming another store than its directory is not applied and is recorded; a non-conforming file is raised as a decision in that store's queue; a directory that is not a valid `store_id` is reported and not read. The recall-index config check now refuses any indexed root, including a `recall.extra_intake_roots` entry, that is or contains `raw/_stores/`.
- The raise ledger (raise S4): `athenaeum.raise_ledger` defines the raiser's row (`raise_id`, `seq`, origin and target `store_id`, `source_uid`, `route`, `content_hash`, `sent_at`, `state`; ids and a hash, no page text), a parser that reports a malformed row without raising, an append-only writer, and readers that return the latest row per `raise_id` by `seq`. It is registered in the artifact registry as operational, store-durable state in the cache dir (`_raise_ledger.jsonl`). Nothing writes it yet (athenaeum#3001)
- Raise S7b: a `raise` or `raise_held` decision can be answered reject with a reason code from a fixed list (`athenaeum.raise_acceptance.reject_raise`, `answer_decision`); the ledger records the `raise_id` as rejected and the message is removed from the store's raw intake once every message in its file has a final disposition. A later message of any kind for a rejected `raise_id` is dropped by rule and recorded, with no decision. A taken uid or a failed rule check is held as a `raise_held` decision naming the uid, the reason and the waiting message, answered reject (or accept once a taken uid is free); a duplicate is recorded only (athenaeum#3002)
- An install no longer mounts a declared store it cannot read. A `storage.adapters` surface whose `_store/access.yaml` does not grant `owner.asserter` read (propose alone does not count), or whose declared `store_id` differs from the entry's new optional `store_id`, is left out of the index and of recall; the refusal is reported per store in the log and the run summary, and nothing is deleted. A store with no declaration mounts as before; an invalid declaration, or a directory that cannot answer, mounts it as a reader and says so (athenaeum#2989)
- A store's declared rights now narrow the write guard. When a surface carries `_store/access.yaml` and the owner's `owner.asserter` does not hold compile there (a reader, a proposer only, not listed, an invalid declaration, or a directory that cannot answer), every write to that surface's pages is refused with `ReadOnlySurfaceError`, whatever the adapter's `read_only` says. The declaration never widens local config, and a surface with no declaration behaves as before. Rights are resolved once at run start (or on the first write when no run started), never per write and never on the recall path (athenaeum#2988)
- The search index is now separable by surface: every FTS5 row and vector embedding records the surface it came from, and `list_surface_derived_data` / `remove_surface_derived_data` list and remove one surface's derived data without reading or rewriting another's. An existing index is rebuilt automatically on the next run (FTS5 schema version 7, vector metadata schema version 4). (athenaeum#2990)
- `athenaeum store access check`: a read-only command that compares each mounted declared store's rights with the local `read_only` setting and with this install's recorded last fetch and last real push per location (no push is made; a never-pushed install reports `push: not yet observed`), and refuses a declaration that breaks the one-writer rule, holds answer without compile on a store with no bench, or has a software-only librarian with no person holding answer. Exits 2 on any difference or refusal; `--json` gives the same report. The last fetch and push outcomes are kept in a small machine-local file in the cache dir. A principal block may now carry an optional `type` (athenaeum#2991)
- The `reviews` block of a store declaration now has meaning: `athenaeum.store_reviews` finds each review's source among the mounted stores, runs a review only when the source's own declaration grants the reviewing principal read, computes when it is due from `every` and the last run, and keeps the cursor and a run record (counts only, no source text) in the install's own state. No model is called and nothing is written to the source. (athenaeum#2992)
- `athenaeum uid-collision-lint` is a read-only check that reports each uid carried by more than one page inside a single surface, with the page paths and which pages are tombstones. It exits 2 when it finds one and offers `--json`. The same uid on two different surfaces is not reported. Both 8-hex and 16-hex uids are handled. (athenaeum#2993)
- Sync: each repository in `sync.mode: integrate` now takes a lease on a lock file inside its own git directory (`athenaeum-sync.lock`, never tracked or committed) for the integrate and push steps, inside the run lock, through the existing single-machine `flock` lease. A second process on the same clone skips the repository and reports `lease-held`; separate clones and `fast_forward` repositories are unaffected. (athenaeum#2997)
- Sync: a repository in `sync.mode: integrate` is now checked, before each integrate, for tracked machine-local files (the artifact registry's `operational` / `machine-local` entries). A finding is a `machine-local-files` row in the run summary and a log warning; the sync goes on and nothing is untracked. `athenaeum machine-local-check` runs the same check read-only and exits 2 on a finding. (athenaeum#2994)
- Integrate mode no longer blocks the whole sync when two installs minted the same page uid. A uid added on both sides with a different `type` or normalised `name` is a collision: the local, unpublished page gets a new 16-hex uid, its file is renamed, references to the old uid made in the same unpublished local commits are rewritten (a reference in anything already published is left alone), and the merge commit records `Athenaeum-Uid-Reminted: <old uid> <new uid>`; the run summary counts re-mints as `uid_reminted`. The same uid with the same `type` and name is one page merged against an empty ancestor. A uid already in the common ancestor on two pages still blocks (athenaeum#2996)
- A page that integrate mode holds because two installs changed it and the librarian could not settle it is now filed as a `held-page` decision in the owning store's own decision queue, naming the uid, the contested units, the reason codes and the base, local and remote blob ids (no page text from another store). The `Athenaeum-Page` line of the merge commit carries `decision=<id>`, the decision shows in the one decision listing, and `answer_held_decision` applies "keep the published version", "take the local version" or a supplied merged text as one ordinary new commit and closes the decision. The published version stays on the page until then; a `deferred` page is not filed and a page settled by rule files nothing; filing the same held page twice makes one decision (athenaeum#2995)

### Fixed

- Claude Code plugin (staged tree): the MCP server now starts on a clean machine. The launcher installs `athenaeum[mcp]` at the pinned version, the first `serve` creates a missing knowledge directory through `athenaeum init`, `userConfig.knowledgePath` reaches `athenaeum serve --path`, and the "install uv" message goes out as SessionStart hook JSON (`systemMessage` plus `additionalContext`) instead of being discarded by `2>/dev/null`. The install runs inside the MCP start, so a first-session race cannot become the roughly 15-minute cached failed connect. Re-syncing the plugin repo and repeating the Linux container run happen at the next release (athenaeum#3046)
- A note saved with `remember` now reaches `recall` as `[uncompiled]` before any compile, with no credential. On an install without chromadb, `remember` refreshed the vector index (which cannot build there) while `recall` reads the fts5 one, so the note stayed invisible until a manual reindex. And `session-end` now reindexes raw intake even when the compile fails, for a missing credential too, so the index is not left older than raw intake; its exit code and message are unchanged (athenaeum#3042).
- Every Claude-format hook group we ship now has a `matcher` key (`""` where no filter is wanted): `examples/claude-code/settings-snippet.json`, the staged plugin `hooks/hooks.json`, and the hook examples in `docs/guides/sidecar.md` and `docs/design/contradiction-detection.md`. Cursor's CLI silently skips a group without one, so a Cursor user got no per-turn push and no error; Claude Code accepts the key and fires every hook as before. A new test sweeps every JSON example and fenced `json` block and fails if a group lacks the key (athenaeum#3048)
- Removing one surface's derived data no longer misbehaves in two edge cases: a vector index that cannot be opened now raises instead of pruning the manifest and orphaning embeddings, and an index built before the `surface` column existed is handled by deriving the surface from the filename instead of raising. (athenaeum#2990)

- `athenaeum doctor`'s `hooks` check no longer reports a flag as a missing hook script. A hook command with an env assignment holding a command substitution (`SID=$(jq -r '.session_id // empty'); nohup bash <script>`) used to fail with `-> -r`; each simple command of a `;`, `&&`, `||`, `|` or `&`-joined command is now resolved separately, `bash <script>` resolves to the script rather than to `bash`, a word starting with `-` is never taken as the target, and a compound command whose target cannot be determined is a warning instead of a failure. (athenaeum#3021)

## [0.28.0] - 2026-11-20

**Summary.** 0.28.0 makes a fresh install pass its own health check, and gives
the per-turn push ways to stay quiet when it has nothing relevant to add. It
also adds the experimental foundations for sharing a store between several
installs. It is a MINOR release because it adds features (0.27.3 to 0.27.8
were never published). `athenaeum.__all__` is unchanged, but **the declared
dependency floors rise**, which can break an install pinned below them:
`anthropic>=0.50.0` (was `>=0.39.0`) and `fastmcp>=4.0.0` (was `>=3.4.3`).

For most users:

- A fresh `athenaeum init` now passes `athenaeum doctor`. Setup that is not
  finished yet reports `WARN` with the command that finishes it, instead of
  `FAIL`, and `athenaeum init` works under a global git commit hook.
- The per-turn push no longer fires on text the harness injected into the
  prompt. A scheduled or scripted run can switch it off for one process with
  `AUTO_RECALL=false`, which is recorded as a skip. An optional relevance floor
  for pushed pages is available and is off by default.
- The `remember` tool says so when it drops or replaces something the caller
  supplied.

Experimental and not called by a single-store install: the new
`athenaeum.store_*`, `athenaeum.sync_*`, `athenaeum.raise_*`,
`athenaeum.page_merge`, `athenaeum.record_merge` and
`athenaeum.intake_decision` modules are the groundwork for federated stores:
a store declaration, per-store decision queues, a multi-writer sync seam and
raising pages between stores. They may change without notice. See
`docs/guides/stability.md` for which surfaces count as stable.

**Upgrading from 0.27.2**

1. If you pin `anthropic` below 0.50.0 or `fastmcp` below 4.0.0, raise those
   pins first. The installer will refuse the older versions.
2. `pip install -U athenaeum` (or `uv tool upgrade athenaeum`), then
   `athenaeum doctor`.

### Changed

- The declared dependency floors are now ones CI actually tests: `anthropic` is `>=0.50.0` (was `>=0.39.0`; releases before 0.50.0 lack the `OverloadedError` class the retry layer classifies, so overload errors would not have been retried) and `fastmcp` is `>=4.0.0` (was `>=3.4.3`, which declares `mcp<2.0` and could not be installed alongside the required `mcp>=2.0`). A new advisory `floors` CI job installs those floors and runs the SDK- and MCP-touching tests; `docs/guides/install.md` lists which dependency versions CI tests (athenaeum#2909).
- A remote `remember` now says so when the server replaces or drops frontmatter the caller wrote: a note's own `audience:` that differs from the pinned audience, an `audience:` grant on a restricted note, or `access: open`. The reply gains a `Note:` line after `Saved to ...` and the server logs it at info level; before, the change was silent. `docs/guides/remote.md` also now states that notes saved before 0.27.2 stay untagged, that no command retags them, and that saving the note again through the remote `remember` is the one way to make it recallable by the restricted audience (athenaeum#2922).
- The `remember` MCP tool now says so when it drops a caller-supplied `source`: the reply gains a second line (`Note: the supplied \`source\` was ignored; ...`) and the server logs a warning, where before the value was discarded with nothing at warning level. Omitting `source`, or naming the transport default itself, changes nothing (athenaeum#2900).
- The README states the prerequisites (`git`, an API key or a logged-in `claude` CLI, Python 3.13+) before a single `uv tool install` line instead of repeating the install command and the Python requirement (athenaeum#2907).
- The owner-only `access:` levels (`internal`, `confidential`, `personal`) are now defined once, in `athenaeum.models`, and the remote `remember` audience stamp reads that same set, so the two can no longer drift apart (athenaeum#2924).
- `athenaeum doctor` now reports the first-run setup checks (`mcp`, `hooks`,
  `key_mode`, `search_index`) as `WARN` with "setup pending" and the command
  that finishes setup, instead of `FAIL`, so a fresh `athenaeum init` exits
  `0`. Present-but-broken state (unparseable `settings.json` or
  `.claude.json`, a hook whose script is missing, an unreadable fts5 index, an
  unrecognised `llm.provider`) is still `FAIL`. The README Quick start now runs
  `doctor` after `test-mcp` with sample output. Topic extraction also logs a
  one-time-per-process warning when it falls back to the regex extractor
  because no LLM credential is available, naming how to enable LLM extraction
  (athenaeum#2906).

- The per-turn push no longer fires on text the harness injected into the prompt
  channel (background-task notices, messages from other agents, system
  reminders). A wholly injected submission is skipped and recorded as one push
  ledger row with `skip_reason: harness_injected`; a submission mixing injected
  and user text is queried on the user text only. The Claude Code adapter
  supplies the patterns and the engine supplies the skip
  (athenaeum#2914).

- North-star grid: the oracle positive control now gates the report. When any
  condition-2-enrolled class scores below 1.000 at oracle, the decision block
  prints as void (no conditions, cutoff scale or verdict table), the marker
  matcher strips markdown emphasis (`**bold**`, `_italic_`) before matching
  (paraphrase is still not accepted), and four probe markers that named a fact
  the question did not ask for are repaired. `grader_revision` moves to
  `athenaeum#2918`; `corpus_digest` does not (pages are untouched). The 30
  failing oracle cells of run 44438673846 are classified and re-graded offline
  in `docs/measurements/north-star-oracle-control-regrade-2026-11-19.md`
  (athenaeum#2918).
- The README Quick start now opens with a three-command block (install, `athenaeum --version`, `athenaeum test-mcp`) and says it needs no API key; the nightly-cost and `page_recompile` prose moved from the README to a new guide, `docs/guides/nightly-cost.md`, linked from the README and `docs/index.md` (athenaeum#2923).

### Added

- The sidecar guide and the environment reference describe how a scheduled or
  scripted run whose prompt carries all its input skips the per-turn push:
  its launcher sets `AUTO_RECALL=false` for that run, the explicit `recall`
  tool still works, and the launcher value beats the cached config
  (athenaeum#2977).
- The per-turn push writes one skip row with reason `declared` when the
  caller turns it off with `AUTO_RECALL=false`, so a declared-off turn is
  counted as skipped rather than read as a turn the hook never saw. The row
  holds only the reason, session id and timestamp. `athenaeum push-metrics
  baseline` reports skipped turns (`skipped_turns`, `skipped_by_reason`,
  `skip_only_sessions`) separately from pushes, and a session whose only rows
  are skips gets the reference reason `push-skipped` instead of
  `no-push-records` (athenaeum#2976).
- The per-turn push applies the push-path relevance floor
  (`recall.relevance_floor.push.<backend>`, or
  `ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5`) before the count cap. FTS5 and
  vector candidates are each judged in their own direction. A dropped
  candidate is not counted in the overflow line. The floor stays off by
  default, and with no floor set the envelope is unchanged. Direct
  `build_context` callers, such as the static-refresh adapter, are not
  affected (athenaeum#2978).
- Filing checks after a raise is accepted (`athenaeum.raise_filing_checks`, athenaeum#2927, slice S21 of the federation raise and retract design). Once `accept_raise` has created a raised copy, or `apply_supersede` has replaced one, the target store runs the same three filing checks as the sync (a contact value already on another page of the store, an address whose local part names a different person, a person-shaped address on an organisation's page) on the copy, reading the target's pages at the commit that published it, and opens a `filing-check` item in the target's decision queue naming the copy for each failure. A created copy is checked whole and a replaced one on the units that changed. The checks run only after the copy is committed and its acceptance recorded: they never reject, hold or ask about a raise, never edit the copy, and a crash in them leaves the acceptance standing. The result is the new `filing` field of the returned `Outcome`. The checks themselves move to `athenaeum.filing_checks`, shared with `athenaeum.sync_filing_checks`, whose behaviour is unchanged.
- Apply a supersede in the target (`athenaeum.raise_supersede`, athenaeum#2940, slice S13 of the federation raise and retract design): `apply_supersede` finds the copy by its derived uid and decides whether it is untouched by projecting it as it stands and comparing that payload hash with `raised_from.content_hash`, never a hash of the whole page. An untouched copy is replaced by the new payload with the recorded `type` and `tags` adjustment applied again, and `content_hash` and `seq` are updated (commit line `Athenaeum-Raise: <raise_id> seq=<n> supersede`); an edited copy is merged three-way (`page_merge`) with the last accepted version, read by the blob id in the acceptance ledger, as the ancestor, and any contested unit holds the page, keeps the published version and raises a decision. A payload that changes `access` or `audience` is held for a person even when the copy is untouched, and a message with a `seq` no higher than the one applied is ignored. A successor-form supersede stamps `superseded_by` with the successor copy's name slug; when the successor's raise was rejected it is a retract with reason `superseded_at_source`, handed to a caller-supplied applier (raise S11 is not built yet) or held. The ancestor is the payload as the raiser last sent it, filed as a fresh copy would be and pinned under `refs/athenaeum/raise-accepted/` so `git gc` keeps it, so a merged copy never becomes the next ancestor and reverts the target's edits. `store` gains `read_blob` and `write_blob`, and the acceptance ledger gains the reason codes this slice records.
- Accept a raised page, asking every time (`athenaeum.raise_acceptance`, athenaeum#2939, slice S7a of the federation raise and retract design): the rule checks run before anyone is asked and a message that fails is recorded in the acceptance ledger with its reason code and raised as a decision (a duplicate is only recorded); a raise that passes becomes a `raise` decision in the target store's queue showing the payload, where the person may accept it or adjust its `type` and `tags` to something the store's `intake` rules accept. Accepting creates the copy at the uid derived from the target `store_id` and `raise_id` with the body unchanged, a `raised_from` block and an `Athenaeum-Raise: <raise_id> seq=<n> accept` commit line, and never overwrites a page already at that uid (the raise is held and a decision is raised). The compiling member's own relevance policy is never applied. `store_decisions` gains `resolve_decision` and an optional `detail` on a filed decision.
- Filing checks after a merge is published (`athenaeum.sync_filing_checks`, athenaeum#2928, slice S17 of the multi-writer sync design). Once an integrate-mode push that carried a merge has succeeded, the install runs three of the section 2.12 checks (a contact value already on another page of the store, an address whose local part names a different person, a person-shaped address on an organisation's page) on the units whose value differs from the remote's value just before the push, and opens a `filing-check` item in that store's decision queue naming the page and the unit for each failure. The pages are read at the pushed commit through the store seam and no index is consulted. The push never waits on the checks, no unit is held and nothing is reverted; a crash in the checks is a `filing-checks` row in the run summary and does not fail the sync. `PushOutcome` gains `remote_before` and `pushed`, `IntegrateResult` gains `remote_tip`, and `page_merge.page_units` names a page's units.
- Integrate mode for a store's pages (`athenaeum.sync_integrate`). A store whose
  `_store/access.yaml` declares `sync.mode: integrate` is synced by snapshot,
  fetch, one two-parent merge commit (never a rebase), compile and a push that,
  when rejected, integrates and pushes again up to three times before reporting
  `push: failed, reason: contended`. Pages merge by unit, a contested unit keeps
  the remote value, and every `_store/` file or unregistered path changed on both
  sides keeps the remote version. A merged page that has no `uid`, `type` or
  `name`, carries a conflict marker, or links from a shared surface to a private
  one is held. The merge commit records `Athenaeum-Sync: 1` and one
  `Athenaeum-Page:` line per page changed on both sides, with blob ids only. A
  store with no declaration stays fast-forward; `sync_mode: fast_forward` under
  `storage.adapters.<name>` narrows an install, and a local `integrate` on a store
  that does not declare it is ignored and reported (athenaeum#2935).
- A two-clone sync test harness (`tests/fixtures/two_clone_harness.py`): one bare
  remote and two installs, each with its own clone and configuration, that
  diverge and drive fetch, merge commit and push through the store seam over the
  local file transport, with no network and no model call (athenaeum#2932).
- A decision queue per store (`athenaeum.store_decisions`): a decision is filed
  with the one store whose page it would change, in that store's own local queue
  file. It quotes only that store's pages and records any other store's page by
  `store_id` and uid alone, filled in on display from the viewer's own mounts
  (an unreadable page is shown only as unreadable). `list_pending_decisions`
  stays one listing: every item names its `store_id` (`personal` for the existing
  queues), and a store on which the owner does not hold answer contributes
  nothing (athenaeum#2934).
- The raise acceptance ledger (athenaeum#2938, slice S5 of the federation raise and retract design): `athenaeum.raise_acceptance_ledger` defines the row shape and parser for one disposition per raise message (accepted, rejected with a fixed reason code, or held), carrying ids, the proposer's identity, the arrival location, the time, the payload hash, any type and tags adjustment, the accepted blob id and, for a fact, the page uids written. Lookups answer whether a `raise_id` is rejected and which adjustment applies to later payloads. It is registered in `ARTIFACT_REGISTRY` as `raise-acceptance-ledger`, kept in the install's cache dir, holds no page text or source page identifier, and never enters the recall index.
- `athenaeum.store` gains the write side of the multi-writer sync seam:
  `write_merge_commit` builds the merged tree and a two-parent merge commit first
  and moves the local branch last (all or nothing), and `push_branch` pushes
  without force and reports a rejection because the remote moved apart from any
  other failure. `StoreCapabilities` gains `multi_writer`, which only a store
  versioned by git with a remote can declare. Nothing rebases or rewrites a
  commit (athenaeum#2931).
- `athenaeum.record_merge.merge_records(kind, local, remote)` is a pure merge of
  a record file changed on both sides: records are read through the file's own
  parser (for the pending merges list, `parse_pending_merges_text`), keyed by
  id, unioned in a stable order and written back through its renderer. A record
  resolved on one side and open on the other takes the resolved state; one
  resolved differently on the two sides is returned as held for a person.
  `merge_derived` takes one side of a regenerated file and says to regenerate
  it. No file access, git call or model call, and nothing calls it yet
  (athenaeum#2929).

- `athenaeum.store` gains the read side of the multi-writer sync seam: fetch a
  remote branch, find the common ancestor of two revisions, read a file at a
  revision, list paths changed between two revisions, and detect rewritten
  remote history. Read-only; pull and push are unchanged (athenaeum#2930).
- The `intake` block of `_store/access.yaml` (declaration `version: 2`): a
  `default` of `accept` or `refuse` and ordered `type`/`tag` rules, last match
  wins, a tag covering its `/` children as the relevance policy matches them.
  A `subject` key, a `route` or `always` verdict, or the block in a version-1
  file is refused. `athenaeum.store_intake.intake_accepts` is the pure accept
  check; nothing applies it yet (athenaeum#2936).
- `athenaeum.store_rights`: the `DirectoryAdapter` protocol, the built-in `static`
  adapter (reads the declaration's `groups:` block) and `resolve_rights`, which
  returns the subset of read, propose, compile and answer an owner holds on a
  store, keyed by `asserter_identity_key` and never a role name. A directory that
  cannot answer resolves as a reader only. A librarian run resolves the owner's
  rights on its surfaces at start and logs one INFO line each; nothing acts on
  the result and a failure is swallowed, so no run behaves differently
  (athenaeum#2933).

- `athenaeum.raise_provenance` parses and validates the `raised_from` block of a
  raised copy, failing closed (any key outside the nine of the design note is
  refused), pins `raise:<raise_id>` as a typed source, and backs a new
  read-only command, `athenaeum raised-from-lint`, that reports a page whose
  block is malformed. `raised_from` is now left out of `content_hash`. No page
  is written or changed; the precedence rank of the `raise` source type stays
  unset (athenaeum#2626, athenaeum#2799).
- `athenaeum.raise_projection`, a pure module with no I/O: the raise
  projection of a page (`type`, `name`, `description`, `aliases`, `tags`,
  `access`, `audience` and the body with links reduced to their labels), the
  payload hash over that projection, `raise_id` / `message_id` minting and
  validators, and the derived uid of a raised copy. Nothing calls it yet, so no
  install behaves differently (athenaeum#2626, athenaeum#2798).
- `athenaeum.raise_messages`, a pure codec for the six raise protocol records
  (`raise`, `propose-fact`, `supersede`, `retract`, `lapse`, `attest`) inside the
  existing envelope-first JSONL batch, with the fixed retract reason codes and a
  refusal of any field that names the source page. A file or record that does not
  conform comes back as a fallthrough and never raises. Nothing calls it yet, so no
  install behaves differently (athenaeum#2626, athenaeum#2937).
- `athenaeum.store_access` parses and validates a store's declaration,
  `_store/access.yaml`, failing closed: a present-but-invalid file yields a result
  naming each offending key, and nothing reads it yet so no install behaves
  differently. `_store/` files are registered as `config` artifacts, and a
  `_`-prefixed directory of any surface is no longer yielded as a page by
  `PageLocator.iter_surface_pages` or the index build (athenaeum#2772).
- `athenaeum.page_merge.merge_page(base, local, remote, spec)` is a pure
  three-way merge of one page by unit (frontmatter key, H2 section, lines in
  a section) using the multi-writer sync design's rule table. It returns the
  merged text, the contested units, the held units with a reason code, and the
  rule that settled each unit. `access` and `audience` are never merged: a
  difference in either is held, and contested or held units carry the remote
  value so the result always parses. It does no file access, runs no git
  command and calls no model, and nothing calls it yet. The section and list
  helpers it shares with the structured upsert moved to `athenaeum.page_text`
  (athenaeum#2773).
- The relevance policy table gains an optional `Store` column and a `route`
  verdict. A `route` row names a `store_id` or `personal`; `RelevancePolicy.match_route`
  answers the destination question, and `match` / `resolve` still consider only
  `always` and `never` rows, so `never` beats `route` and capture is unchanged.
  The hash, read-back, `relevance_policy` tool and `set_capture_rule` (new
  `store` argument) carry the store. Nothing acts on a `route` row yet
  (athenaeum#2625).
- `athenaeum.intake_decision.decide`, one pure function giving the intake decision for a
  fact (capture, store, type, reason code), now called by the three capture gates in both
  pipeline transports. Only its capture answer is used and it is the one the gates computed
  before, so no install captures, drops or places anything differently (athenaeum#2625).
- New guide `docs/guides/stability.md` says which surfaces count as stable beyond `athenaeum.__all__` (CLI, config keys, MCP tools, file formats), which are experimental and which are internal; the README links it beside the support policy (athenaeum#2908).
- A push ledger row now records the query terms it searched on and which
  extractor produced them (`model`, `regex_fallback` or `regex_only`), so a push
  that missed can be attributed to extraction or to ranking. On by default;
  `push_metrics.record_query_terms: false` (or
  `ATHENAEUM_PUSH_RECORD_QUERY_TERMS=0`) turns it off. Older ledgers read
  unchanged, `push-metrics tail` projects the fields and `athenaeum demo` shows
  them (athenaeum#2916).
- Design record for how the per-turn push should treat self-contained programmatic prompts: a caller-declared skip, then a measured relevance floor, with build slices (athenaeum#2915).

### Fixed

- `athenaeum init` makes its seed commit with git hooks disabled, so a global `core.hooksPath` commit hook (for example a protected-branch guard) no longer aborts it with `git commit` failing on a fresh install (athenaeum#2901).

- Six vector-backend tests (in `test_doctor.py`, `test_cli.py::TestWarnIfBackendCacheMissing` and `test_indexed_surfaces.py`) now skip instead of failing under a `[dev]`-only install without chromadb (athenaeum#2905).
- `athenaeum.store_access` no longer raises on a `_store/access.yaml` whose YAML
  parses but fails to construct a value (an impossible date such as `2026-02-30`,
  a bad `!!int`, deep nesting): it returns the invalid result naming the parse
  failure. A `version` written as `1.0` is now invalid too (athenaeum#2772).

### Security

- `SECURITY.md` now states Athenaeum's exposure to the four open chromadb server advisories (it runs chromadb embedded and never starts a server) and warns against running `chroma run` over the vector index directory; a new test keeps `src/athenaeum` free of server-mode chromadb use (athenaeum#2913).

## [0.27.2] - 2026-11-19

**Summary.** 0.27.2 is a PATCH release on 0.27.0 (0.27.1 was never
published). It changes one behaviour: a note saved by a remote `remember`
(`--transport http`) under a restricted `--audience` is now stamped with the
server's pinned audience, so the writer and anyone else holding that role can
recall it. Before, every restricted audience withheld it, including the
writer's own (athenaeum#2779). It also refreshes the pinned versions in the
CI constraints file, `requirements-ci.txt`, to clear open security advisories
against those pins (athenaeum#2812). That file only constrains CI and
`-c requirements-ci.txt` installs; the dependency ranges the published package
declares are unchanged. `athenaeum.__all__` is unchanged.

**Upgrading from 0.27.0**

1. `pip install -U athenaeum` (or `uv tool upgrade athenaeum`), then
   `athenaeum doctor`.
2. If you run the server remotely with a restricted `--audience`, notes saved
   through it from now on are visible to that whole audience without review.
   Notes saved before the upgrade stay untagged.

### Changed

- A remote `remember` (`--transport http`) under a restricted `--audience` now stamps the saved note with the server's pinned audience (`audience: [<roles>]`), so the caller that wrote it, and anyone else holding that role, can recall it. Before, the note was untagged and every restricted audience withheld it, including the writer's own. The stamp comes only from the server-side pin: it replaces any `audience:` in the note's own frontmatter, drops a note-written `access: open`, and no `remember` argument changes it. A note whose `access:` is `internal`, `confidential` or `personal` (set by intake screening or by the note) is not stamped and stays owner-only. A different restricted audience still cannot recall the note, `--audience owner` and stdio stamp nothing, and `docs/guides/remote.md` now says remote saves are visible to the caller's whole audience without review (athenaeum#2779).

### Added

- Three measurement reports under `docs/measurements/`, linked from `docs/index.md`: why pushed pages are irrelevant, the 2026-11-19 native-memory baseline re-run, and the rendered north-star grid report for that run. Documentation only; no code changes (athenaeum#2921).

### Security

- The CI constraints file `requirements-ci.txt` takes the grouped update of 13 Dependabot bumps that clears open security advisories, among them several against `aiohttp`, plus the transitive pins those updates drag along: counting those, 20 existing pins change and one new pin (`opentelemetry-exporter-otlp-common` 0.66b2) is added. Two of the moves cross a major version, `oauthlib` 3.3.1 to 4.0.1 and `filelock` 3.32.2 to 4.2.1; `fastmcp` and `fastmcp-slim` move from 4.0.11 to 4.1.2, and `aiohttp` from 3.14.3 to 3.14.4. Only CI and installs run with `-c requirements-ci.txt` see the change; the package's declared dependency ranges are unchanged (athenaeum#2812).

## [0.27.0] - 2026-11-19

**Summary.** 0.27.0 is a MINOR release on 0.26.0. It fixes the first-run path:
a note saved with `remember` is now recallable before the first compile,
`athenaeum doctor` and `athenaeum init` report a logged-out or missing `claude`
CLI instead of claiming a login, `init` exits cleanly when `git` is missing or
fails, and `athenaeum import-memory` checks the provider before reading
anything, prices a run per file and lists the files it will read. It adds the
decision-queue digest and an opt-in push notifier, and routes first-person
auto-memory notes to the owner page. The documentation and packaging fixes
asked for by the 0.25.0/0.26.0 release review are in: README quick start
order, project status and release cadence, supported dependency ranges, and
the remote-mode security notes. `athenaeum.__all__` is unchanged. One change is
breaking and data-mutating for scripted callers: answering a decision
(`athenaeum decisions answer` and the `resolve_question`, `resolve_merge` and
`review_audit_item` MCP tools) now applies the answer at once by default, where
it used to only record it (athenaeum#2531). Scripts that relied on the old
record-only behaviour must pass `--apply deferred` (MCP: `apply="deferred"`);
see upgrade step 3. The behaviour changes are listed below.

Behaviour changes an existing install will notice:

- Answering a decision (`athenaeum decisions answer`, and the
  `resolve_question`, `resolve_merge` and `review_audit_item` MCP tools) now
  applies it at once by default. Pass `--apply deferred` (MCP:
  `apply="deferred"`) for the previous record-only behaviour (athenaeum#2531).
- With an owner configured, `user`-type auto-memory notes are now bridged to
  the owner page. The entity-tier calls this adds run inside `max_api_calls`.
  Set `owner.bridge_memory_types: []` to turn it off (athenaeum#2690).
- The `remember` tool ignores a caller-supplied `source` and files the note
  under the transport default (`claude-session` under stdio, `remote-session`
  under HTTP), so `recall` sees it before compile (athenaeum#2764).
- New pages get a 16-hex page uid. Existing 8-hex uids stay valid and are never
  rewritten (athenaeum#2771).
- The `fastmcp` floor is now 3.4.3 (athenaeum#2728).
- The CI constraints file is renamed from `requirements-ci.lock` to
  `requirements-ci.txt`; contents are unchanged (athenaeum#2784).

Experimental surface (not in `athenaeum.__all__`; flags, keys, formats and
import paths may change in any minor release without a deprecation cycle): the
decision digest (`athenaeum decisions digest`, the `queue_digest` MCP tool),
decision push (`notify.yaml`) (athenaeum#2489), the `apply` option on decision answers, the
`owner.bridge_memory_types` key, the `witness_paste` receipt field, the new
`athenaeum.type_guidance` module (athenaeum#2731), and everything 0.26.0 listed as experimental.

**Upgrading from 0.26.0**

1. `pip install -U athenaeum` (or `uv tool upgrade athenaeum`), then
   `athenaeum doctor`. The Python floor (3.13+) is unchanged. If you pin
   `fastmcp` yourself, it must be 3.4.3 or later.
2. If you install with `-c requirements-ci.lock`, switch to
   `-c requirements-ci.txt`.
3. If you script `athenaeum decisions answer`, or call the `resolve_question`,
   `resolve_merge` or `review_audit_item` MCP tools, and rely on answers staying
   unapplied until the next `ingest-answers` tick, add `--apply deferred` (MCP:
   `apply="deferred"`) to keep the previous record-only behaviour. Without it,
   each answer mutates the knowledge base at once.
4. If you have an owner configured and do not want auto-memory notes bridged to
   the owner page, set `owner.bridge_memory_types: []`.
5. Optional: re-run `athenaeum registry` to build the
   `registry-body-emails.json` sidecar, so an email-shaped `recall` can find a
   person whose address is only in a page body. Without it, recall behaves as
   before.
6. The manual systemd stamp deletion in the 0.26.0 upgrade notes (step 2) is
   no longer needed: `schedule install` now clears a leftover stamp itself.
7. If you call the `remember` MCP tool with a `source` argument, stop: since
   0.27.0 the tool ignores it and files the note under the transport default
   (athenaeum#2764). From the release after 0.27.2 the reply also says so.
   Pass `sources` for provenance.

```bash
athenaeum --version            # expect 0.27.0
athenaeum doctor               # read-only health check; names a fix per FAIL or WARN
athenaeum test-mcp             # smoke-test remember/recall, no API key needed
```

The per-merge Changed / Added / Fixed / Security entries are in
[docs/changelog/0.27.0-build-notes.md](https://github.com/Kromatic-Innovation/athenaeum/blob/v0.27.0/docs/changelog/0.27.0-build-notes.md).
They are reference material; this section is the release.

### Changed

- Release review documentation and packaging: README quick start now comes
  first and names the first-compile prerequisite; README gains "Project
  status"; the CHANGELOG preamble and CONTRIBUTING state the release cadence;
  the install guide gains "Supported dependency ranges" and "Support policy";
  ruff targets `py313` (athenaeum#2722, athenaeum#2727, athenaeum#2728).
- `athenaeum import-memory` prices a run per file (about 2,800 tokens a file,
  measured), names the API call count, and lists the files it will read before
  asking to proceed (athenaeum#2733, athenaeum#2737).
- `athenaeum init` and the install guide state what a compile costs, measured
  on 0.26.0 (athenaeum#2766).
- Decision answers apply immediately by default, new pages get 16-hex uids,
  `remember` ignores a supplied `source`, and the CI constraints file is
  renamed, as listed above (athenaeum#2531, athenaeum#2771, athenaeum#2764,
  athenaeum#2784).

### Added

- Decision-queue digest: `athenaeum decisions digest [--json]` and the
  `queue_digest` MCP tool return what needs a decision now, with per-type caps.
  Building a digest makes no model call (athenaeum#2529).
- Decision push, off by default: a librarian run can pipe the digest to a
  user-configured command set in the per-user `notify.yaml` (athenaeum#2533).
- Owner-page routing of first-person auto-memory notes, controlled by
  `owner.bridge_memory_types` (athenaeum#2690).

### Fixed

- A note saved with `remember` is returned by `recall` before the first
  compile, labelled `[uncompiled]` (athenaeum#2764).
- `athenaeum doctor` and `athenaeum init` check `claude auth status` instead of
  treating a present `claude` binary as a login; a failing `claude` call
  reports its real cause, and `athenaeum status` shows the last failed compile
  (athenaeum#2762, athenaeum#2763).
- `athenaeum doctor` `search_index` accepts an FTS5 index on an install without
  the `[vector]` extra, and `athenaeum serve` no longer warns about a missing
  vector index there (athenaeum#2765, athenaeum#2780).
- `athenaeum init` exits with one line, and removes what it created, when `git`
  is missing or a git command fails (athenaeum#2778, athenaeum#2730).
- `athenaeum import-memory` stops before reading when the provider cannot run,
  exits non-zero when every classify call fails, finishes re-runs that a held
  cluster used to repeat, and no longer prints false-alarm warnings on a
  healthy run (athenaeum#2735, athenaeum#2734, athenaeum#2736).
- Full-echo Tier-3 merge replies with trailing prose after `ADDS_NEW_CLAIM:`
  no longer drop the observation (athenaeum#2739).
- `athenaeum schedule install` clears a leftover systemd timer stamp, so a
  fresh install no longer fires a stale compile (athenaeum#2721).
- An email-shaped `recall` falls back to registry handles and page-body
  addresses (athenaeum#2714).
- The Claude Code witness hook classifies interactive prompts, slash commands
  and launch arguments correctly, fills `harness_version`, and no longer waits
  indefinitely for the ledger lock; intake attests slash-command arguments and
  marks detected pastes (athenaeum#2161, athenaeum#2744, athenaeum#2745,
  athenaeum#2759, athenaeum#2760, athenaeum#2769, athenaeum#2770).
- The remote guide names the one outbound request `serve --transport http`
  makes at startup and how to disable it (athenaeum#2782).
- The test suite isolates git config itself and leaves the working tree clean
  (athenaeum#2729).

### Security

- Under `--transport http`, the `remember` reply names the saved file relative
  to the corpus root instead of the server's absolute path (athenaeum#2781).
- Documented that on an authenticated server bearer-token validation is the
  only DNS-rebinding defence, and listed the remote-mode checks as in scope in
  `SECURITY.md`. No behaviour change (athenaeum#2726).

## [0.26.0] - 2026-11-16

**Summary.** 0.26.0 is a MINOR release on 0.25.0. It adds per-type librarian
guidance, so an operator can tell the librarian how to write and merge one page
type, and it fixes `athenaeum schedule remove` on systemd, which left a timer
stamp behind that made a later `schedule install` fire a stale run straight
away. Nothing changes for an install that adds no guidance file. The
remote-mode hardening planned with this release already shipped in 0.25.0.
There are no breaking changes.

**Upgrading from 0.25.0**

1. `pip install -U athenaeum` (or `uv tool upgrade athenaeum`), then
   `athenaeum doctor`. The Python floor (3.13+) is unchanged. No configuration
   change is needed.
2. If you removed a systemd schedule with 0.25.0 or earlier and will install it
   again, first delete the stamp that removal left behind,
   `~/.local/share/systemd/timers/stamp-athenaeum-run.timer` (under
   `$XDG_DATA_HOME` if you set it). The new `schedule remove` only cleans up a
   schedule that is still installed.
3. Optional: add `wiki/_schema/_type-guidance-<type>.md` or
   `wiki/_schema/_entity-template-<type>.md` for a page type you want written
   differently. The format is in `docs/reference/data-formats.md`.

```bash
athenaeum --version            # expect 0.26.0
athenaeum doctor               # read-only health check; names a fix per FAIL or WARN
athenaeum test-mcp             # smoke-test remember/recall, no API key needed
```

The per-merge Added / Fixed entries are in
[docs/changelog/0.26.0-build-notes.md](https://github.com/Kromatic-Innovation/athenaeum/blob/v0.26.0/docs/changelog/0.26.0-build-notes.md).
They are reference material; this section is the release.

### Added

- Per-type librarian guidance, opt-in: it does nothing until configured
  (athenaeum#2590). Two optional files in
  `wiki/_schema/` steer the Tier-3 create and merge prompts for one page type:
  `_entity-template-<type>.md` replaces `_entity-template.md` for creates of
  that type, and `_type-guidance-<type>.md` adds guidance to the create and
  merge prompts, on both the synchronous and Batch API transports. Only types
  declared in `types.md` with a plain lowercase key are read. The run summary
  gains a `type_guidance=` token.
- Experimental surface (not in `athenaeum.__all__`; flags, keys, formats and
  import paths may change in any minor release without a deprecation cycle):
  - The per-type guidance file names and format, and the `type_guidance=` run
    summary token.
  - Everything 0.25.0 listed as experimental stays experimental, remote serving
    included.
  - `athenaeum.__all__` is unchanged from 0.25.0.

### Fixed

- On the systemd backend, `athenaeum schedule remove` now also clears the
  `athenaeum-run.timer` stamp: it runs `systemctl --user clean --what=state`
  (systemd 247 or later) and then deletes the stamp file if it is still there
  (athenaeum#2707). A later `athenaeum schedule install` no longer runs a
  "missed" compile at once. An install that changes no configuration will
  notice this.

### Security

- No security-relevant changes. The new guidance files are operator-authored
  prompt input: they are size-capped and defanged like the other
  `wiki/_schema/` fragments, and a fixed precedence clause stops guidance from
  overriding the librarian's rules or authorising paraphrase (athenaeum#2590).

## [0.25.0] - 2026-11-16

**Summary.** 0.25.0 is a MINOR release on 0.24.0. It enforces the relevance
policy: `always` and `never` capture rules in `wiki/_schema/relevance-policy.md`,
which 0.24.0 could only read, now decide what every intake path compiles. It
adds an owner page, so first-person facts and preferences have a page to live
on, and per-type stores, so one type's pages can live in their own git
repository or be read from a shared one. It also hardens remote mode. An
install with no policy file, no owner and no `storage:` config compiles exactly
as before. Read "Upgrading from 0.24.0" if you run `athenaeum serve --transport http`.

**Upgrading from 0.24.0**

1. `pip install -U athenaeum` (or `uv tool upgrade athenaeum`), then
   `athenaeum doctor`. The Python floor (3.13+) is unchanged.
2. If you run `athenaeum serve --transport http --profile remote` without an
   audience, set `serve.audience` (a role, or `owner` deliberately) before
   restarting. See `docs/guides/upgrading.md`.
3. If your remote server's issuer mints access tokens without `exp`, change the
   issuer first: those tokens are now refused.
4. If you reach an unauthenticated HTTP server by a name other than
   `localhost`, `127.0.0.1` or `::1`, switch to one of those.
5. Optional: `athenaeum owner set --name "Your Name"` to give first-person facts
   a page, and a `wiki/_schema/relevance-policy.md` to declare what is always
   or never captured (format in `docs/reference/data-formats.md`).

```bash
athenaeum --version            # expect 0.25.0
athenaeum doctor               # read-only health check; names a fix per FAIL or WARN
athenaeum test-mcp             # smoke-test remember/recall, no API key needed
athenaeum relevance-policy     # show the active capture rules, if any
```

The per-merge Added / Changed / Fixed entries are in
[docs/changelog/0.25.0-build-notes.md](https://github.com/Kromatic-Innovation/athenaeum/blob/v0.25.0/docs/changelog/0.25.0-build-notes.md).
They are reference material; this section is the release.

### Changed

- Breaking (remote mode only; stdio serving is unchanged):
  - `athenaeum serve --transport http --profile remote` refuses to start when no
    audience is configured (athenaeum#2604). Pin a role with `--audience <role>`,
    `serve.audience` or `ATHENAEUM_AUDIENCE`, or set `owner` deliberately.
  - Remote mode refuses an access token with no `exp`, a future `nbf` or `iat`
    (beyond `serve.auth.clock_leeway`, default 60 seconds), or, when the new
    `serve.auth.max_token_lifetime` is set, a lifetime over that cap
    (athenaeum#2599). An issuer that mints tokens without `exp` no longer works.
  - An unauthenticated HTTP server answers a request whose `Host` is not
    `localhost`, `127.0.0.1` or `::1` with `421`, and a foreign `Origin` with
    `403` (athenaeum#2605). Reach it through one of those names.
- Behaviour changes (an install that changes no configuration will notice these):
  - `athenaeum ingest-answers` now applies an answered `classification_failed`
    question: it writes the answer as a new raw note under `raw/question-answers/`
    for the next compile, and archives a question with nothing left to apply as
    `unrecoverable` (athenaeum#2705). Answers held under 0.24.0 are applied on the
    first run.
  - `athenaeum run` prints a `WARNING:` line for each removed config key still
    set, and `athenaeum doctor` reports them at a new `WARN` level that does not
    change its exit code (athenaeum#2686).
  - `athenaeum --help` groups its subcommands; every invocation parses as before
    (athenaeum#2686).
  - Protected resource metadata advertises `serve.auth.issuer` byte for byte,
    with no added trailing slash (athenaeum#2600).

### Added

All of these are opt-in: each does nothing until configured.

- Relevance policy enforcement (athenaeum#2644 to athenaeum#2650). A `never`
  rule drops a covered fact before any model tier runs, on the synchronous,
  batch and auto-memory paths alike, and records it in the drop ledger. Facts
  with no declared category are classified, escalated to the reasoning tier when
  the classifier is unsure, and dropped if still unsure. A covered raw note is
  kept out of the recall index. `always` rules reach the classify prompt as a
  hint. `remember(explicit_request=true)` overrides a `never` rule, with an
  audit record. The new `set_capture_rule` MCP tool records a rule from
  conversation (a proposal, not an edit, over HTTP), and
  `athenaeum relevance-policy report` lists existing pages a `never` rule covers.
- Owner page (athenaeum#2652 to athenaeum#2655). `athenaeum owner set --name NAME`
  (or `athenaeum init --owner-name NAME`) creates the owner's person page and
  records `owner.uid`. With an owner set, first-person facts and preferences
  ("I prefer...", "my team...") are filed on that page, and batch runs get
  person hints too.
- Per-type stores (athenaeum#2617 to athenaeum#2622). Compile writes a page to
  the surface its type maps to in `storage.mapping`; `read_entity`, recall and
  structured upsert find a page on any indexed surface. `storage.adapters`
  entries gain `visibility` (no merges across stores, no link from a shared page
  to a more private one, checked by `athenaeum store-link-lint`) and `read_only`
  (a shared git repository that is pulled and indexed but never written).
  `athenaeum storage move-type <type> --to <surface>` moves a type's existing
  pages, dry run by default, with `--undo`. Guide:
  `docs/guides/shared-store-reader.md`.
- Never-merge rulings survive page splits (athenaeum#2651).
- Experimental surface (not in `athenaeum.__all__`; flags, keys, formats and
  import paths may change in any minor release without a deprecation cycle):
  - Everything listed above in this section: the relevance policy file format,
    `set_capture_rule`, `explicit_request`, `athenaeum relevance-policy`,
    `athenaeum owner`, the `storage.adapters` keys `visibility` and `read_only`,
    `athenaeum storage move-type` and `athenaeum store-link-lint`.
  - Remote serving stays experimental, including the new `serve.auth.jwks_uri`,
    `serve.auth.jwks_refetch_interval`, `serve.auth.allowed_clients`,
    `serve.auth.max_token_lifetime` and `serve.auth.clock_leeway` keys.
  - `athenaeum.__all__` is unchanged from 0.24.0.

### Security

All in experimental remote mode.

- Token checks: `exp` required, `nbf` and `iat` enforced, optional lifetime cap
  (athenaeum#2599). Refusals are logged with the `sub` and a reason, never the
  token.
- A remote server can no longer serve the whole corpus by omission
  (athenaeum#2604).
- DNS-rebinding protection for unauthenticated HTTP servers (athenaeum#2605).
- `serve.auth.allowed_clients` pins which client apps may use a server and can
  make any of them read-only (athenaeum#2602).
- Signing keys are found from `serve.auth.jwks_uri` or issuer discovery, and an
  unknown `kid` refetches the key set at most once per
  `serve.auth.jwks_refetch_interval` seconds (athenaeum#2601).
- A `remember` that overrides a `never` rule is refused unless its audit record
  is written first (athenaeum#2646); a remote client cannot edit the relevance
  policy directly (athenaeum#2649).

<!-- There is no [0.27.1] link: 0.27.1 was never published (no tag, no release), so [0.27.2] compares against v0.27.0. -->
[Unreleased]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.30.0...HEAD
[0.30.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.29.0...v0.30.0
[0.29.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.28.0...v0.29.0
[0.28.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.27.2...v0.28.0
[0.27.2]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.27.0...v0.27.2
[0.27.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.26.0...v0.27.0
[0.26.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.25.0...v0.26.0
[0.25.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.24.0...v0.25.0
