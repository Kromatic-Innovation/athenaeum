# CLI Reference

This page is GENERATED from `athenaeum.cli.build_parser()`'s real `argparse` tree by `scripts/gen_cli_reference.py` — do not hand-edit it. CI (`tests/test_generated_docs_parity.py`) regenerates it and fails the build on any diff. To change an entry, change the subcommand's own `add_argument(...)` call in its owning `_cmd_*.py` module and regenerate:

```
python scripts/gen_cli_reference.py
```

Every subcommand is registered top-level on one `parser.add_subparsers()` in `cli.py` — there is no `query` group. A handful of commands (`dedupe`, `auto-memory`, `questions`, `merges`, `decisions`, `authority`, `axiom`, `calibration`, `storage`, `push-metrics`, `measure`, `memory-class`, `description`, `verdicts`, `dimensions`, `subject`) have their own nested sub-subcommands, listed under their own section below.

## Command index

- [`athenaeum authority`](#athenaeum-authority) (group) — Authority manifest: detect + convert memories that duplicate a live source (skill file, code path, config) into pointer stubs.
- [`athenaeum authority convert`](#athenaeum-authority-convert) (command) — Convert ONE page into a one-line pointer stub for a given manifest source. Default is dry-run; --apply writes the file. Scoped to a single --page; never walks the corpus.
- [`athenaeum authority lint`](#athenaeum-authority-lint) (command) — List wiki pages that duplicate a manifest-listed authoritative source. READ-ONLY — never mutates wiki/.
- [`athenaeum auto-memory`](#athenaeum-auto-memory) (group) — Operate on compiled wiki/auto-*.md pages.
- [`athenaeum auto-memory prune`](#athenaeum-auto-memory-prune) (command) — Prune operational/ephemeral wiki/auto-*.md pages. Default is dry-run (prints kill-list + retained-list with reasons); --apply git rm's the kill-list in one commit and rebuilds the recall index.
- [`athenaeum auto-memory prune-code-entities`](#athenaeum-auto-memory-prune-code-entities) (command) — Retire wiki entity pages minted from filenames/paths — a page whose entity name is a code artifact (has a source/config extension or a path separator). Default is dry-run; --apply git rm's the kill-list in one commit.
- [`athenaeum auto-memory prune-index`](#athenaeum-auto-memory-prune-index) (command) — Prune dangling pointers from <scope>/MEMORY.md indexes. A pointer is dangling when its target.md no longer exists on disk. Default is dry-run; --apply rewrites the indexes in one commit.
- [`athenaeum axiom`](#athenaeum-axiom) (group) — Axiom governance: explicit human-approved promotion/demotion of memory_class: axiom pages, plus the assignment audit.
- [`athenaeum axiom demote`](#athenaeum-axiom-demote) (command) — Record a human-approved axiom demotion for a wiki page slug.
- [`athenaeum axiom list`](#athenaeum-axiom-list) (command) — Assignment audit: every slug's current status + full promote/demote history (when/why/by-whom).
- [`athenaeum axiom promote`](#athenaeum-axiom-promote) (command) — Record a human-approved axiom promotion for a wiki page slug.
- [`athenaeum bounce-contract`](#athenaeum-bounce-contract) (command) — Check whether a candidate raw-intake note would be recognized by the Tier-0 hard-bounce gate, before submitting it. Read-only, offline, deterministic.
- [`athenaeum calibration`](#athenaeum-calibration) (group) — Tier-audit calibration: per-tier sampled/reviewed/overturned summary, and record a human confirm/overturn of an audit item.
- [`athenaeum calibration review`](#athenaeum-calibration-review) (command) — Record a human confirm/overturn of a sampled audit item.
- [`athenaeum calibration summary`](#athenaeum-calibration-summary) (command) — Per-tier calibration counts (sampled / reviewed / overturned).
- [`athenaeum claims`](#athenaeum-claims) (command) — Detect claims restated across distinct wiki entities (read-only). Default --find prints a YAML report.
- [`athenaeum compile`](#athenaeum-compile) (command) — : recompile a historical wiki snapshot as-of a past date into a scratch --out dir (compile-as-of). Distinct from the read-time `recall/reindex --as-of` filter — this re-runs the C3 blend so members expired now but valid then are re-included. Deterministic (no LLM); never mutates the live wiki or raw tree.
- [`athenaeum context`](#athenaeum-context) (command) — Build one sidecar context envelope (ranked candidates + rendered text) for a prompt — the agent-neutral core,
- [`athenaeum decay-sweep`](#athenaeum-decay-sweep) (command) — Archive expired bucket:daily wiki pages. Default is dry-run (prints kill-list + retained-list); --apply git-archives the kill-list in a two-commit pair and rebuilds the recall index.
- [`athenaeum decisions`](#athenaeum-decisions) (group) — One unified 'human decisions needed' list — pending questions AND merges, each tagged by type. Three modes: list, next, count.
- [`athenaeum decisions count`](#athenaeum-decisions-count) (command) — Print `N decisions pending (Q questions, M merges; oldest Xd)`.
- [`athenaeum decisions list`](#athenaeum-decisions-list) (command) — List all pending decisions, oldest first.
- [`athenaeum decisions next`](#athenaeum-decisions-next) (command) — Show the oldest pending decision (single block).
- [`athenaeum decisions raise-confirmation`](#athenaeum-decisions-raise-confirmation) (command) — File a NEW agent-raised 'implemented X without Y, confirm?' item into the pending-decisions queue — the CLI counterpart of the MCP raise_decision tool's kind="confirmation" path.
- [`athenaeum decisions scan-retractions`](#athenaeum-decisions-scan-retractions) (command) — Flag any completed merge that relied on a now-retracted source for human review. Idempotent; never unmerges.
- [`athenaeum dedup-oversize-escalations`](#athenaeum-dedup-oversize-escalations) (command) — Collapse duplicate oversize-page-family escalations in _pending_questions.md to one unanswered block per entity
- [`athenaeum dedupe`](#athenaeum-dedupe) (group) — Find or merge duplicate wiki entries.
- [`athenaeum dedupe persons`](#athenaeum-dedupe-persons) (command) — Person-wiki dedupe (HIGH-confidence apollo_id / linkedin / exact-name match). Default --find prints a YAML report; --apply consumes the report and merges.
- [`athenaeum dedupe wiki-pages`](#athenaeum-dedupe-wiki-pages) (command) — Cluster concept/reference/principle wiki pages and propose merges for near-duplicate topics. Writes idempotent proposals to wiki/_pending_merges.md; --dry-run previews without writing.
- [`athenaeum demo`](#athenaeum-demo) (command) — Open the recall viewer for the current Claude session — resolves the session id, picks a free port, and opens a browser.
- [`athenaeum description`](#athenaeum-description) (group) — Page-summary maintenance: backfill the one-line description: frontmatter the recall hook injects.
- [`athenaeum description backfill`](#athenaeum-description-backfill) (command) — Write description: onto pages that lack it — batched LLM summaries through the 'classify' knob, or --mechanical for a zero-LLM opening-paragraph derivation. Dry-run unless --apply. Never overwrites an existing value. Resumable: re-run to continue.
- [`athenaeum dimensions`](#athenaeum-dimensions) (group) — Dimension registry: show a claim's coordinates or compare two claims' coordinates axis-by-axis.
- [`athenaeum dimensions compare`](#athenaeum-dimensions-compare) (command) — Compare two wiki pages' coordinates axis-by-axis, one relation per dimension.
- [`athenaeum dimensions show`](#athenaeum-dimensions-show) (command) — Show one wiki page's coordinates across every registered dimension.
- [`athenaeum disable`](#athenaeum-disable) (command) — Turn athenaeum's background work off (compile, detectors, recall, notifications). Reversible with 'athenaeum enable'.
- [`athenaeum drain`](#athenaeum-drain) (command) — Supervised API+batch drain of the raw-intake backlog (cost-guarded)
- [`athenaeum enable`](#athenaeum-enable) (command) — Undo 'athenaeum disable' — restore all background work.
- [`athenaeum entity`](#athenaeum-entity) (command) — One-call read of a SINGLE entity's page by uid, for any entity class, with an explicit --include-excluded flag (default off). The generic form of `person`; prints the same JSON object shape.
- [`athenaeum enumerate`](#athenaeum-enumerate) (command) — Enumerate every entity of a declared type matching field predicates — no query text. The generalized form of the former `athenaeum people` (removed) — see docs/design/recall-architecture.md's capability-parity table.
- [`athenaeum explain-routing`](#athenaeum-explain-routing) (command) — Read-only preview: resolved provider/model/batch/price per model knob. Prints what 'athenaeum run' would actually use for this athenaeum.yaml + environment -- no LLM call, no file processed, no routing behavior changed.
- [`athenaeum ingest`](#athenaeum-ingest) (command) — Compile new/changed raw intake into the wiki on demand. --incremental (default) compiles only files new/changed since the last ingest; --full recompiles.
- [`athenaeum ingest-answers`](#athenaeum-ingest-answers) (command) — Ingest answered pending questions from _pending_questions.md
- [`athenaeum ingest-merges`](#athenaeum-ingest-merges) (command) — Archive resolved pending merges from wiki/_pending_merges.md
- [`athenaeum init`](#athenaeum-init) (command) — Initialize a new knowledge directory
- [`athenaeum measure`](#athenaeum-measure) (group) — v6 memory-model measurement pack: shadow-mode complete-linkage population, backlog price sheet, ordinary-night steady-state table, C4-vs-comparator shadow parity.
- [`athenaeum measure backlog-price`](#athenaeum-measure-backlog-price) (command) — Backlog price sheet with a decision-inflow sensitivity table.
- [`athenaeum measure ordinary-night`](#athenaeum-measure-ordinary-night) (command) — Ordinary-night steady-state table: measured load + amortized comparator-regime assumptions vs the nightly call/wall-clock budgets.
- [`athenaeum measure shadow-linkage`](#athenaeum-measure-shadow-linkage) (command) — Shadow-mode complete-linkage cluster population over the live wiki store: embeddings only, zero LLM calls, read-only.
- [`athenaeum measure shadow-parity`](#athenaeum-measure-shadow-parity) (command) — Run the C4 detector and the cluster comparator over the SAME corpus and report their verdict agreement matrix + call multiplier (; the live-corpus route is).
- [`athenaeum memory-class`](#athenaeum-memory-class) (group) — Memory-taxonomy class maintenance: backfill the memory_class: frontmatter axis across the wiki.
- [`athenaeum memory-class backfill`](#athenaeum-memory-class-backfill) (command) — Assign memory_class: to pages that lack it — deterministic type-rule map, plus an optional classifier pass over the residual. Dry-run unless --apply. Never overwrites an existing value and never mints 'axiom'.
- [`athenaeum merges`](#athenaeum-merges) (group) — Inspect unresolved resolver merge proposals in `wiki/_pending_merges.md`. Three modes: list, next, count. The merges half of `athenaeum decisions`.
- [`athenaeum merges count`](#athenaeum-merges-count) (command) — Print `N unresolved (oldest: <iso-date>)`.
- [`athenaeum merges list`](#athenaeum-merges-list) (command) — List all unresolved merge proposals.
- [`athenaeum merges next`](#athenaeum-merges-next) (command) — Show the oldest unresolved merge (single block).
- [`athenaeum merges propose-fold`](#athenaeum-merges-propose-fold) (command) — Propose folding one or more source pages INTO a named canonical page. Derives merge_target_name from the canonical page's `name:` and write_kind from the corpus — no hand-built proposal. Dry-run by default; --apply to queue.
- [`athenaeum merges provenance`](#athenaeum-merges-provenance) (command) — List EXECUTED merges from `wiki/_merge_provenance.jsonl` — which source pages each merge relied on.
- [`athenaeum merges recompare`](#athenaeum-merges-recompare) (command) — Re-run the five-verdict comparator over every unresolved merge proposal and record a verdict per source pair in the verdict ledger. Dry-run by default; --apply writes to the LEDGER only — this command never approves, rejects, or archives a proposal, and PII-hazard proposals always route to a human regardless of verdict.
- [`athenaeum merges revalidate`](#athenaeum-merges-revalidate) (command) — Re-validate existing unresolved merge proposals against the CURRENT suppression gate and archive stale ones. Dry-run by default; pass --apply to write.
- [`athenaeum merges scrub-pii`](#athenaeum-merges-scrub-pii) (command) — Redact contact data out of merge-proposal bodies in place. The zero-LLM purge path for a stale `draft_merged_body` left behind by `storage migrate-pii`: it clears the values without approving, rejecting or withdrawing the merge. Dry-run by default; --apply writes.
- [`athenaeum outbound-lint`](#athenaeum-outbound-lint) (command) — Scan outbound-destined text for PII (emails/phones) before it ships; flag findings (default) or --redact them. Offline, deterministic.
- [`athenaeum pii-restore`](#athenaeum-pii-restore) (command) — Anchored PII-restore: recover non-PII tokens a [contact redacted -> excluded surface] marker replaced, via rename-following and retro-filename history lookup. Default is dry-run; pass --apply to write fixes.
- [`athenaeum push-metrics`](#athenaeum-push-metrics) (group) — Push-precision + coverage baseline: compute/record the precision snapshot, sample sessions for a human-reviewed coverage-audit worksheet, record a single hook-path push, and stream the documented NDJSON tail contract over the ledgers.
- [`athenaeum push-metrics baseline`](#athenaeum-push-metrics-baseline) (command) — Compute precision + coverage over a window; write the dated snapshot to docs/measurements/memory-model-measurements.md. Refuses to write (exit 1) when the window has zero reference-determination records. See --dry-run to inspect without writing.
- [`athenaeum push-metrics coverage-audit`](#athenaeum-push-metrics-coverage-audit) (command) — Sample N sessions' push records into a worksheet of the structural facts hash-only records support (candidate-pool size, tier/scope concentration, filter removal, policy-set bounds) — never a per-candidate marking or a measured miss rate.
- [`athenaeum push-metrics liveness`](#athenaeum-push-metrics-liveness) (command) — Read-only assertion: PASS if any of the most recent --window rows is sidecar-tagged, FAIL (exit 1) if --window rows exist and none is, INCONCLUSIVE (exit 0) if fewer than --window rows are recorded (including an absent ledger).
- [`athenaeum push-metrics record`](#athenaeum-push-metrics-record) (command) — Record one hook-path push: the fire-and-forget entry point the per-turn UserPromptSubmit recall hook calls with the session id and the ids it actually injected. Writes a push record tagged source=hook, distinct from an explicit MCP `recall` push (no source key) and the `athenaeum context` sidecar adapter (source=sidecar). Always exits 0 — see this subcommand's own docstring.
- [`athenaeum push-metrics tail`](#athenaeum-push-metrics-tail) (command) — Stream NDJSON — one object per push record and per reference-determination record, newest-last. Read-only; never mutates the ledgers. See docs/reference/configuration.md ('push-metrics tail — the public NDJSON contract') for the documented --json record shape, schema version, and compatibility note.
- [`athenaeum query-topics`](#athenaeum-query-topics) (command) — Extract substantive search topics from a prompt (Haiku). Used by the UserPromptSubmit hook to rewrite queries before FTS5/vector search. Prints one topic per line to stdout; empty output means fall back to the caller's built-in extractor.
- [`athenaeum questions`](#athenaeum-questions) (group) — Inspect unresolved entries in `_pending_questions.md`. Three modes: list, next, count. Used by the example SessionStart hook and the resolve-questions skill.
- [`athenaeum questions count`](#athenaeum-questions-count) (command) — Print `N unresolved (oldest: <iso-date>)`.
- [`athenaeum questions list`](#athenaeum-questions-list) (command) — List all unresolved questions.
- [`athenaeum questions next`](#athenaeum-questions-next) (command) — Show the oldest unresolved question (single block).
- [`athenaeum rebuild-index`](#athenaeum-rebuild-index) (command)
- [`athenaeum recall`](#athenaeum-recall) (command) — Search the wiki from the shell (one tab-separated hit per line)
- [`athenaeum reconcile`](#athenaeum-reconcile) (command) — Retire pending raw-intake files whose content is already materialized in the wiki (dual-write cleanup). Default is dry-run; pass --apply to remove.
- [`athenaeum recovery-yield`](#athenaeum-recovery-yield) (command) — Read-only readout of the auto-memory origin-recovery yield signal: recovered/uncited counters, basis split, resolved threshold, and (AC4) the corpus share of type:auto-memory pages with sources:[]. One JSON object on stdout, exit 0 always, no side effects.
- [`athenaeum registry`](#athenaeum-registry) (command) — : compile the source-handle registry.json (entity uid → handle set) from wiki entity frontmatter. Deterministic, no LLM; emits a well-formed registry even when no handles are populated yet.
- [`athenaeum reindex`](#athenaeum-reindex) (command) — Rebuild the search index (FTS5 or vector, per config). --incremental (default) applies only the hash-diff delta; --full rebuilds from scratch.
- [`athenaeum repair`](#athenaeum-repair) (command) — Repair YAML-frontmatter corruption in wiki files. Default is dry-run; pass --apply to write fixes.
- [`athenaeum reresolve-questions`](#athenaeum-reresolve-questions) (command) — Re-resolve open proposal-less pending questions (self-heal transient cap/offline escalations)
- [`athenaeum run`](#athenaeum-run) (command) — Run the librarian pipeline
- [`athenaeum serve`](#athenaeum-serve) (command) — Start the MCP memory server
- [`athenaeum session-end`](#athenaeum-session-end) (command) — Change-gated ingest + reindex for SessionEnd: compile this session's new raw intake, then refresh the index — a fast no-op (no LLM, no reindex) when nothing changed.
- [`athenaeum spend`](#athenaeum-spend) (command) — Report LLM spend from the durable ledger ($ for API, tokens for subscription — never blended)
- [`athenaeum status`](#athenaeum-status) (command) — Show knowledge base status
- [`athenaeum stopwords`](#athenaeum-stopwords) (command) — Print the stopword list (one word per line). Used by the example UserPromptSubmit hook's regex fallback to stay in sync with the FTS5 query filter.
- [`athenaeum storage`](#athenaeum-storage) (group) — Storage-surface operator tasks (migrate a page's PII off-corpus).
- [`athenaeum storage audit-h1-redaction`](#athenaeum-storage-audit-h1-redaction) (command) — Read-only audit: report pages whose H1 heading line carries the inline-redaction marker, classified into a defect population (marker consumed the whole heading subject) and a non-defect population (marker replaced one inline token inside an otherwise-intact title). Never writes.
- [`athenaeum storage lint-mapping`](#athenaeum-storage-lint-mapping) (command) — storage.mapping completeness lint + the deferred (read_policy, adapter) pair check: every sensitivity class the scanned corpus carries must have a live storage.mapping entry naming a real adapter; exit non-zero on a gap. Advisory-only D4 policy-mismatch findings are also reported but never fail the gate on their own.
- [`athenaeum storage lint-pii`](#athenaeum-storage-lint-pii) (command) — Corpus-wide PII gate: scan EVERY file under wiki/ (queue/index/archive/_-prefixed and.bak files included) for an inline email/phone; exit non-zero on any finding. Also reports raw/ retention as a separate, non-gating count.
- [`athenaeum storage migrate-pii`](#athenaeum-storage-migrate-pii) (command) — Move archival contact data (emails/phones) off entity pages to the excluded surface, leaving durable identifiers only. Single page (--page) or bulk (--all / --glob).
- [`athenaeum storage prune-dispositions`](#athenaeum-storage-prune-dispositions) (command) — One-time prune of wiki/_shape_rule_dispositions.jsonl to its positive-disposition records only (AC3/AC4). Dry-run by default: reports the disposition histogram and projected size. --apply writes.
- [`athenaeum subject`](#athenaeum-subject) (group) — Subject-coordinate maintenance: backfill the subject: frontmatter axis.
- [`athenaeum subject backfill`](#athenaeum-subject-backfill) (command) — Write subject: (= uid) onto comparator-relevant pages that lack it. Zero-LLM, deterministic. Dry-run unless --apply. Never overwrites an existing value. Read the module docstring before using --apply on a live store.
- [`athenaeum surface-divergence`](#athenaeum-surface-divergence) (command) — Report the two-surface divergence for a REGISTERED field (wiki frontmatter vs. the contacts/excluded surface) and, by default, exit non-zero when it exceeds the field's declared allowance. Generalizes bounce-divergence / do-not-email-divergence into one per-field guard. Read-only; output is safe to paste publicly.
- [`athenaeum test-mcp`](#athenaeum-test-mcp) (command) — Smoke-test MCP remember/recall against a synthetic knowledge dir
- [`athenaeum usage-report`](#athenaeum-usage-report) (command) — Per-claim usage report (pushed / referenced / last-referenced) computed from the push-metrics ledgers — ids-only, no content.
- [`athenaeum verdicts`](#athenaeum-verdicts) (group) — Inspect the verdict ledger (`wiki/_verdicts/`) — pairwise comparison verdicts with their justification basis. Four modes: count, list-by-verdict, show-one-pair, show-stale.
- [`athenaeum verdicts count`](#athenaeum-verdicts-count) (command) — Print the live verdict count.
- [`athenaeum verdicts list-by-verdict`](#athenaeum-verdicts-list-by-verdict) (command) — List all live verdicts, optionally filtered by --verdict.
- [`athenaeum verdicts show-one-pair`](#athenaeum-verdicts-show-one-pair) (command) — Show the current live verdict for one pair.
- [`athenaeum verdicts show-stale`](#athenaeum-verdicts-show-stale) (command) — List every live verdict currently flagged stale.
- [`athenaeum viewer`](#athenaeum-viewer) (command) — Serve a localhost-only, read-only page showing pushed-unbidden vs. pulled-deliberately vs. overlap recall for one session.

## `athenaeum authority`

Authority manifest: detect + convert memories that duplicate a live source (skill file, code path, config) into pointer stubs.

Subcommands:

- `athenaeum authority convert` — Convert ONE page into a one-line pointer stub for a given manifest source. Default is dry-run; --apply writes the file. Scoped to a single --page; never walks the corpus.
- `athenaeum authority lint` — List wiki pages that duplicate a manifest-listed authoritative source. READ-ONLY — never mutates wiki/.

## `athenaeum authority convert`

Convert ONE page into a one-line pointer stub for a given manifest source. Default is dry-run; --apply writes the file. Scoped to a single --page; never walks the corpus.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Write the converted stub to --page. Without this flag, the command is a dry-run that prints the result to stdout. |
| `--page` | — | — | Path to the wiki page to convert. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge), used to resolve the authority manifest. |
| `--source-slug` | — | — | The manifest source slug this page duplicates. |
| `--title` | — | — | Override the stub's title (default: the page's frontmatter name). |

## `athenaeum authority lint`

List wiki pages that duplicate a manifest-listed authoritative source. READ-ONLY — never mutates wiki/.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum auto-memory`

Operate on compiled wiki/auto-*.md pages.

Subcommands:

- `athenaeum auto-memory prune` — Prune operational/ephemeral wiki/auto-*.md pages. Default is dry-run (prints kill-list + retained-list with reasons); --apply git rm's the kill-list in one commit and rebuilds the recall index.
- `athenaeum auto-memory prune-code-entities` — Retire wiki entity pages minted from filenames/paths — a page whose entity name is a code artifact (has a source/config extension or a path separator). Default is dry-run; --apply git rm's the kill-list in one commit.
- `athenaeum auto-memory prune-index` — Prune dangling pointers from <scope>/MEMORY.md indexes. A pointer is dangling when its target.md no longer exists on disk. Default is dry-run; --apply rewrites the indexes in one commit.

## `athenaeum auto-memory prune`

Prune operational/ephemeral wiki/auto-*.md pages. Default is dry-run (prints kill-list + retained-list with reasons); --apply git rm's the kill-list in one commit and rebuilds the recall index.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | git rm the kill-list in one labeled commit and rebuild the recall index. Without this flag the command is a dry-run. |
| `--backend` | — | fts5, vector | Override the recall index backend for the rebuild (default: read from athenaeum.yaml). --apply only. |
| `--cache-dir` | — | — | Cache directory for the recall index rebuild (default: ~/.cache/athenaeum). --apply only. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum auto-memory prune-code-entities`

Retire wiki entity pages minted from filenames/paths — a page whose entity name is a code artifact (has a source/config extension or a path separator). Default is dry-run; --apply git rm's the kill-list in one commit.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | git rm the kill-list in one labeled commit. Without this flag the command is a dry-run. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum auto-memory prune-index`

Prune dangling pointers from <scope>/MEMORY.md indexes. A pointer is dangling when its target.md no longer exists on disk. Default is dry-run; --apply rewrites the indexes in one commit.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Rewrite the affected MEMORY.md indexes in one labeled commit. Without this flag the command is a dry-run. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum axiom`

Axiom governance: explicit human-approved promotion/demotion of memory_class: axiom pages, plus the assignment audit.

Subcommands:

- `athenaeum axiom demote` — Record a human-approved axiom demotion for a wiki page slug.
- `athenaeum axiom list` — Assignment audit: every slug's current status + full promote/demote history (when/why/by-whom).
- `athenaeum axiom promote` — Record a human-approved axiom promotion for a wiki page slug.

## `athenaeum axiom demote`

Record a human-approved axiom demotion for a wiki page slug.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--by` | — | — | Who is authorizing the demotion. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--reason` | — | — | Why this axiom is being demoted. |
| `--slug` | — | — | The wiki page slug being demoted. |

## `athenaeum axiom list`

Assignment audit: every slug's current status + full promote/demote history (when/why/by-whom).

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum axiom promote`

Record a human-approved axiom promotion for a wiki page slug.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--by` | — | — | Who is authorizing the promotion. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--reason` | — | — | Why this page is being promoted to axiom. |
| `--scope` | — | — | Optional context scope (e.g. "applies to resume work"). Stored + surfaced; enforcement is a consumer's concern (out of scope for). |
| `--slug` | — | — | The wiki page slug being promoted. |

## `athenaeum bounce-contract`

Check whether a candidate raw-intake note would be recognized by the Tier-0 hard-bounce gate, before submitting it. Read-only, offline, deterministic.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--file` | — | — | Path to a file holding the candidate note. Mutually exclusive with --text. |
| `--json` | `False` | — | Emit a machine-readable JSON verdict instead of plain text. |
| `--text` | — | — | The candidate note (frontmatter + body), given inline. Mutually exclusive with --file; if neither is given, the note is read from stdin. |

## `athenaeum calibration`

Tier-audit calibration: per-tier sampled/reviewed/overturned summary, and record a human confirm/overturn of an audit item.

Subcommands:

- `athenaeum calibration review` — Record a human confirm/overturn of a sampled audit item.
- `athenaeum calibration summary` — Per-tier calibration counts (sampled / reviewed / overturned).

## `athenaeum calibration review`

Record a human confirm/overturn of a sampled audit item.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--id` | — | — | The audit item id (from `decisions list`). |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--note` | `` | — | Optional free-text note on the review. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--verdict` | — | — | The human's verdict. Equal to the tier's original verdict = confirm; different = overturn (a calibration signal only). |

## `athenaeum calibration summary`

Per-tier calibration counts (sampled / reviewed / overturned).

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum claims`

Detect claims restated across distinct wiki entities (read-only). Default --find prints a YAML report.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--find` | `False` | — | Discover recurring claims and print a YAML report. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--threshold` | — | — | Cosine similarity cutoff (default: 0.85) |

## `athenaeum compile`

: recompile a historical wiki snapshot as-of a past date into a scratch --out dir (compile-as-of). Distinct from the read-time `recall/reindex --as-of` filter — this re-runs the C3 blend so members expired now but valid then are re-included. Deterministic (no LLM); never mutates the live wiki or raw tree.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--as-of` | — | — | Historical date to recompile as of (inclusive). Members whose validity window had closed on this date (or that carry a tombstone) are excluded; members expired now but valid then are re-included. Rewind is valid-time, not transaction-time (see docs §8.7). |
| `--out` | — | — | Scratch directory to write the recompiled wiki into. MUST NOT be the live wiki/ directory. |
| `--path`, `--knowledge-root` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge). |

## `athenaeum context`

Build one sidecar context envelope (ranked candidates + rendered text) for a prompt — the agent-neutral core,

**Positional arguments:**

- `prompt` — The prompt text (omit to read JSON from stdin)

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--backend` | `fts5` | fts5, vector | Search backend (default: fts5) |
| `--budget` | — | — | Push token budget (default: resolved/1200) |
| `--cache-dir` | — | — | Cache dir holding wiki-index.db (default: ~/.cache/athenaeum) |
| `--llm-timeout` | `3.0` | — | LLM extraction timeout in seconds |
| `--n` | `3` | — | Max candidates (default: 3) |
| `--no-llm` | `False` | — | Skip LLM term extraction, use the regex fallback |
| `--session-id` | — | — | Session id, for dedup bookkeeping by the caller |
| `--stdin-json` | `False` | — | Read {"prompt":..., "session_id":...} from stdin (hook-input shape) |

## `athenaeum decay-sweep`

Archive expired bucket:daily wiki pages. Default is dry-run (prints kill-list + retained-list); --apply git-archives the kill-list in a two-commit pair and rebuilds the recall index.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Git-archive the kill-list (two-commit: provenance snapshot, then git rm) and rebuild the recall index. Without this flag the command is a dry-run. |
| `--as-of` | — | — | Rewind the expiry check to this date (YYYY-MM-DD) instead of today. Dry-run only in practice, but accepted by --apply too. |
| `--backend` | — | fts5, vector | Override the recall index backend for the rebuild (default: read from athenaeum.yaml). --apply only. |
| `--cache-dir` | — | — | Cache directory for the recall index rebuild (default: ~/.cache/athenaeum). --apply only. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum decisions`

One unified 'human decisions needed' list — pending questions AND merges, each tagged by type. Three modes: list, next, count.

Subcommands:

- `athenaeum decisions count` — Print `N decisions pending (Q questions, M merges; oldest Xd)`.
- `athenaeum decisions list` — List all pending decisions, oldest first.
- `athenaeum decisions next` — Show the oldest pending decision (single block).
- `athenaeum decisions raise-confirmation` — File a NEW agent-raised 'implemented X without Y, confirm?' item into the pending-decisions queue — the CLI counterpart of the MCP raise_decision tool's kind="confirmation" path.
- `athenaeum decisions scan-retractions` — Flag any completed merge that relied on a now-retracted source for human review. Idempotent; never unmerges.

## `athenaeum decisions count`

Print `N decisions pending (Q questions, M merges; oldest Xd)`.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum decisions list`

List all pending decisions, oldest first.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--limit` | `0` | — | Truncate to first N (default: 0 = unlimited). |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--with-proposal` | `False` | — | Include the (optional) `**Proposed resolution**` block on question items. |

## `athenaeum decisions next`

Show the oldest pending decision (single block).

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--with-proposal` | `False` | — | Include the (optional) `**Proposed resolution**` block on question items. |

## `athenaeum decisions raise-confirmation`

File a NEW agent-raised 'implemented X without Y, confirm?' item into the pending-decisions queue — the CLI counterpart of the MCP raise_decision tool's kind="confirmation" path.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--alternative` | — | — | The road not taken — what a human might have wanted instead. |
| `--context` | `` | — | Optional standalone context. Auto-phrased from the structured fields above when omitted. |
| `--entity` | `` | — | Optional short human-readable header label. Cosmetic only. |
| `--implemented-behavior` | — | — | What was actually built instead. |
| `--issue-ref` | — | — | The issue or PR the narrowing relates to. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--narrowed-scope` | — | — | What was narrowed — the scope NOT covered. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--question` | `` | — | Optional checkbox question text. Auto-phrased from the structured fields above when omitted. |
| `--raiser` | — | — | Who/what narrowed scope (agent name, lane id,...). |
| `--repo` | — | — | The owner/repo narrowed in. |

## `athenaeum decisions scan-retractions`

Flag any completed merge that relied on a now-retracted source for human review. Idempotent; never unmerges.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum dedup-oversize-escalations`

Collapse duplicate oversize-page-family escalations in _pending_questions.md to one unanswered block per entity

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum dedupe`

Find or merge duplicate wiki entries.

Subcommands:

- `athenaeum dedupe persons` — Person-wiki dedupe (HIGH-confidence apollo_id / linkedin / exact-name match). Default --find prints a YAML report; --apply consumes the report and merges.
- `athenaeum dedupe wiki-pages` — Cluster concept/reference/principle wiki pages and propose merges for near-duplicate topics. Writes idempotent proposals to wiki/_pending_merges.md; --dry-run previews without writing.

## `athenaeum dedupe persons`

Person-wiki dedupe (HIGH-confidence apollo_id / linkedin / exact-name match). Default --find prints a YAML report; --apply consumes the report and merges.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Read a report and perform the merge (idempotent). |
| `--find` | `False` | — | Discover duplicate pairs and write a YAML report. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--from` | — | — | Path to the YAML report to apply (default: stdin). --apply only. |
| `--out` | — | — | Path to write the YAML report (default: stdout). --find only. |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |
| `--wiki-root` | — | — | Wiki directory (default: ~/knowledge/wiki). |

## `athenaeum dedupe wiki-pages`

Cluster concept/reference/principle wiki pages and propose merges for near-duplicate topics. Writes idempotent proposals to wiki/_pending_merges.md; --dry-run previews without writing.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--dry-run` | `False` | — | Print what would be proposed without writing to wiki/_pending_merges.md. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--threshold` | — | — | Cosine similarity cutoff (default: librarian.cluster_threshold / 0.55 — same threshold the raw auto-memory cluster pass uses). |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum demo`

Open the recall viewer for the current Claude session — resolves the session id, picks a free port, and opens a browser.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory holding the push-metrics ledgers (default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum) |
| `--no-browser` | `False` | — | Serve without opening a browser (headless/CI). |
| `--path` | — | — | Knowledge directory (default: ~/knowledge) |
| `--port` | `8756` | — | Preferred TCP port on localhost (default: 8756). Falls back to an OS-assigned free port if this one is busy. |
| `--projects-root` | — | — | Claude Code transcript root (default: ~/.claude/projects) |
| `--session` | — | — | Session id to scope to. Default: $CLAUDE_CODE_SESSION_ID, then $CLAUDE_SESSION_ID, then the newest Claude Code transcript. |

## `athenaeum description`

Page-summary maintenance: backfill the one-line description: frontmatter the recall hook injects.

Subcommands:

- `athenaeum description backfill` — Write description: onto pages that lack it — batched LLM summaries through the 'classify' knob, or --mechanical for a zero-LLM opening-paragraph derivation. Dry-run unless --apply. Never overwrites an existing value. Resumable: re-run to continue.

## `athenaeum description backfill`

Write description: onto pages that lack it — batched LLM summaries through the 'classify' knob, or --mechanical for a zero-LLM opening-paragraph derivation. Dry-run unless --apply. Never overwrites an existing value. Resumable: re-run to continue.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Write the descriptions. Without this flag the command reports and writes nothing. |
| `--batch-size` | `20` | — | Pages per LLM call (default: 20). |
| `--dry-run` | `False` | — | Report without writing. Already the default; OVERRIDES --apply when both are given (safe mode wins). |
| `--include-retired` | `False` | — | Do not skip pages carrying 'retired: true'. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--limit` | — | — | Decide at most N pages this pass (the rest are reported 'undecided'). Re-run to continue — already-described pages are skipped, so successive runs drain the backlog. |
| `--mechanical` | `False` | — | Derive each description from the page's opening paragraph instead of an LLM call. Free and instant; lower quality on pages whose first paragraph is not a summary. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--sample` | `0` | — | Print the first N decided descriptions (dry-run review aid). |

## `athenaeum dimensions`

Dimension registry: show a claim's coordinates or compare two claims' coordinates axis-by-axis.

Subcommands:

- `athenaeum dimensions compare` — Compare two wiki pages' coordinates axis-by-axis, one relation per dimension.
- `athenaeum dimensions show` — Show one wiki page's coordinates across every registered dimension.

## `athenaeum dimensions compare`

Compare two wiki pages' coordinates axis-by-axis, one relation per dimension.

**Positional arguments:**

- `file_a` — Path to the first wiki page (.md).
- `file_b` — Path to the second wiki page (.md).

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON. |
| `--path` | `~/knowledge` | — | Knowledge directory, for loading athenaeum.yaml (default: ~/knowledge). |

## `athenaeum dimensions show`

Show one wiki page's coordinates across every registered dimension.

**Positional arguments:**

- `file` — Path to a wiki page (.md).

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON. |
| `--path` | `~/knowledge` | — | Knowledge directory, for loading athenaeum.yaml (default: ~/knowledge). |

## `athenaeum disable`

Turn athenaeum's background work off (compile, detectors, recall, notifications). Reversible with 'athenaeum enable'.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory for the state file (default: ~/.cache/athenaeum). |
| `--compile` | `False` | — | Granular: stop only the expensive compile/detect pass (session-end contradiction detection); leave recall on. |
| `--reason` | — | — | Optional note recorded in the state file and shown by 'athenaeum status'. |

## `athenaeum drain`

Supervised API+batch drain of the raw-intake backlog (cost-guarded)

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--knowledge-root`, `--path` | — | — | Knowledge directory (default: ~/knowledge). |
| `--max-files` | — | — | Intake window size — files compiled per window (default: librarian.max_files / 50). The drain loops windows until the backlog empties or the cost ceiling trips. |
| `--max-usd` | — | — | Mandatory cost ceiling in USD applied CUMULATIVELY across the whole drain (not per window). Maps onto the spend.max_usd_per_run ceiling for each window as the remaining budget. |
| `--raw-root` | — | — | Raw intake directory (default: <knowledge-root>/raw). |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |
| `--wiki-root` | — | — | Wiki directory (default: <knowledge-root>/wiki). |
| `--yes` | `False` | — | Proceed without the interactive cost confirmation (required to run non-interactively — the drain incurs real API spend). |

## `athenaeum enable`

Undo 'athenaeum disable' — restore all background work.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory for the state file (default: ~/.cache/athenaeum). |

## `athenaeum entity`

One-call read of a SINGLE entity's page by uid, for any entity class, with an explicit --include-excluded flag (default off). The generic form of `person`; prints the same JSON object shape.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--class` | `person` | — | The page's `type:` (person, vendor, …). Selects which excluded SURFACE is read — a `person` page's record lives on the `pii` surface — while the page itself is resolved by uid whatever its type. Default: person. |
| `--include-excluded` | `False` | — | Include the actual excluded values (default: off — withheld fields carry a redaction marker instead). |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--uid` | — | — | The entity's durable uid. |
| `--usage-class` | `[]` | observed, provider, unclassified | Return only values of this usage class (repeatable; one of observed, provider, unclassified). Default: every value, each carrying its class. |

## `athenaeum enumerate`

Enumerate every entity of a declared type matching field predicates — no query text. The generalized form of the former `athenaeum people` (removed) — see docs/design/recall-architecture.md's capability-parity table.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--ascending` | `False` | — | Sort ascending instead of the default descending. |
| `--audience` | — | — | Run under a restricted read scope, matching `recall --audience`. Unset = owner = full access. |
| `--cache-dir` | — | — | Cache directory (default: ~/.cache/athenaeum) |
| `--cursor` | — | — | Opaque continuation token from a prior call's `next_cursor`. |
| `--field` | `[]` | — | Additional declared field to include per hit (repeatable), beyond the always-present uid/type/name. |
| `--limit` | `50` | — | Max rows to return (default: 50; 0 = unlimited). |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--sort` | `name` | — | Frontmatter field to sort by (default: name). |
| `--type` | — | — | Declared entity type (a page's `type:`). Call `athenaeum query entity-schema`-equivalent (the MCP `entity_schema` tool) to discover this deployment's classes. An unrecognized value does not error — the response's `known_classes` names what this deployment DOES have. |
| `--where` | `[]` | — | Field predicate, AND-combined, repeatable. KIND is one of eq, ne, substring, regex (eq/substring/regex all compare case-insensitively). FIELD may be a comma-separated ORDERED fallback list, OR-combined (e.g. current_company,linkedin_company_at_connect:substring:Acme). |
| `--with-pii` | `False` | — | Required to predicate or select `google_contact_*` fields (AC amendment 1). Same flag contract `recall --with-pii` already uses. NOT required for `do_not_email` (ungated by). |

## `athenaeum explain-routing`

Read-only preview: resolved provider/model/batch/price per model knob. Prints what 'athenaeum run' would actually use for this athenaeum.yaml + environment -- no LLM call, no file processed, no routing behavior changed.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of a formatted table. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum ingest`

Compile new/changed raw intake into the wiki on demand. --incremental (default) compiles only files new/changed since the last ingest; --full recompiles.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory holding the ingest stamp manifest (default: ~/.cache/athenaeum) |
| `--dry-run` | `False` | — | Run the compile without writing files, committing, or updating the ingest stamp. |
| `--evaluate-only` | `False` | — | Lock-free public trigger-evaluation mode: evaluate the configured reasoning-tier triggers against LIVE state and print the verdict, then exit — NEVER takes.athenaeum.lock and NEVER compiles, even when a trigger fired (unlike --if-triggered, which compiles on a fire). Reads the exact same reasoning-trigger stamp --if-triggered completion writes. Exit codes: 2 = a trigger fired, 0 = none fired, 1 = an error occurred evaluating (mirrors this repo's dry-run-found-something ternary, e.g. `athenaeum decay`/`athenaeum repair`). Cannot be combined with --if-triggered. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--full` | `True` | — | Recompile all pending raw intake, ignoring the ingest stamp. |
| `--if-triggered` | `False` | — | Additive control signal: before compiling, evaluate the configured reasoning-tier triggers (backlog file count / bytes, elapsed interval, nightly backstop — librarian.reasoning_triggers.*) against LIVE state. When none fired, does NOT compile — prints the same one-line JSON summary carrying trigger="none" and exits 0 (cheap, side-effect-free: no lock taken). When one fired, runs the normal incremental ingest exactly as without this flag, with the firing trigger's name in the summary, and — on a clean non-dry-run completion — advances the reasoning-trigger last-run stamp used by the elapsed-interval and nightly-backstop checks. This adds a control signal to the existing on-demand poke; it is not a second way for data to enter, and it never forces a full recompile (always --incremental's budgeted, resumable path). |
| `--incremental` | — | — | Compile only raw files new/changed since the last successful ingest (tracked via a content-hash stamp). This is the DEFAULT. |
| `--path`, `--knowledge-root` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge). --knowledge-root is an alias, matching `run`. |
| `--session` | — | — | Scope the new/changed detection to one originSessionId (the SessionEnd use-case). |
| `--verbose`, `-v` | `False` | — | Enable debug logging |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum ingest-answers`

Ingest answered pending questions from _pending_questions.md

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--quiet`, `-q` | `False` | — | Suppress per-block malformed-block warnings; print only the final summary line(s). |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum ingest-merges`

Archive resolved pending merges from wiki/_pending_merges.md

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum init`

Initialize a new knowledge directory

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--force` | `False` | — | Overwrite existing template files at the destination; no backup is created (only applies with --with-templates and/or --with-rules). |
| `--path` | `~/knowledge` | — | Target directory (default: ~/knowledge) |
| `--rules-dest` | — | — | Override the shape-rules destination directory (default: <path>/rules). |
| `--templates-dest` | — | — | Override the templates destination directory (default: <path>/templates). |
| `--with-rules` | `False` | — | Also copy bundled EXAMPLE shape rules into <path>/rules/. Every example ships 'mode: observe' -- installing them changes nothing until you review wiki/_shape_rule_dispositions.jsonl and edit a copy to 'mode: live'. See docs/design/shape-rules.md. |
| `--with-templates` | `False` | — | Also copy bundled entity-author templates (person/company/project/concept/source) into <path>/templates/. |

## `athenaeum measure`

v6 memory-model measurement pack: shadow-mode complete-linkage population, backlog price sheet, ordinary-night steady-state table, C4-vs-comparator shadow parity.

Subcommands:

- `athenaeum measure backlog-price` — Backlog price sheet with a decision-inflow sensitivity table.
- `athenaeum measure ordinary-night` — Ordinary-night steady-state table: measured load + amortized comparator-regime assumptions vs the nightly call/wall-clock budgets.
- `athenaeum measure shadow-linkage` — Shadow-mode complete-linkage cluster population over the live wiki store: embeddings only, zero LLM calls, read-only.
- `athenaeum measure shadow-parity` — Run the C4 detector and the cluster comparator over the SAME corpus and report their verdict agreement matrix + call multiplier (; the live-corpus route is).

## `athenaeum measure backlog-price`

Backlog price sheet with a decision-inflow sensitivity table.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--backlog-count` | — | — | Operator-supplied override for the backlog file count (AC3(a)). Omit to re-derive it from the live raw/ tree (default). When supplied, the snapshot records backlog_count_source=operator-supplied. |
| `--cache-dir` | — | — | Cache directory holding the spend ledger (default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum) |
| `--calls-per-file` | — | — | Operator-supplied override for calls/file (AC3(b)). Omit to re-derive it from the spend ledger (default). When supplied, the snapshot records calls_per_file_source=operator-supplied. |
| `--docs-path` | `docs/measurements/memory-model-measurements.md` | — | Where the snapshot section is written/appended (default: docs/measurements/memory-model-measurements.md). |
| `--dry-run` | `False` | — | Compute and display the measurement without writing to --docs-path. |
| `--human-daily-budget` | `20` | — | Human decisions/day the sensitivity table paces against (default: 20, per the issue's stated budget). |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--prefilter-excluded-fraction` | — | — | Fraction of the backlog a write-refusal/retention-pack pre-filter would exclude (that classifier does not exist yet in this codebase — omit to report the 'with prefilter' column as n/a). |
| `--six-month-days` | `182` | — | Day count marking the 6-month horizon a sensitivity row can breach (default: 182). |
| `--summary-log` | — | — | Path to a nightly log file containing 'librarian-run-summary' lines, used to derive wall-clock/file. Omit to report wall-clock figures as not-yet-measurable (no fabricated figure). |
| `--wall-clock-per-file-seconds` | — | — | Operator-supplied override for wall-clock/file (AC3(c)). Omit to re-derive it from --summary-log (default). When supplied, the snapshot records wall_clock_source=operator-supplied. |

## `athenaeum measure ordinary-night`

Ordinary-night steady-state table: measured load + amortized comparator-regime assumptions vs the nightly call/wall-clock budgets.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--audit-sampling-calls-per-night` | `0.0` | — | — |
| `--audit-sampling-seconds-per-night` | `0.0` | — | — |
| `--cache-dir` | — | — | Cache directory holding the spend ledger (default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum) |
| `--calls-per-file` | — | — | Operator-supplied override for calls/file (AC5). Omit to re-derive it from the spend ledger (default). When supplied, the snapshot records calls_per_file_source=operator-supplied. |
| `--comparator-amortization-nights` | `7` | — | — |
| `--comparator-calls-per-pair` | `1.0` | — | — |
| `--comparator-pair-count` | — | — | Artifact 1's measured comparator_pair_count (complete-linkage), amortized over --comparator-amortization-nights to derive comparator-pairs-per-night. Overridden by --comparator-pairs-per-night if both are given. |
| `--comparator-pairs-per-night` | — | — | — |
| `--comparator-seconds-per-pair` | `0.0` | — | — |
| `--docs-path` | `docs/measurements/memory-model-measurements.md` | — | Where the snapshot section is written/appended (default: docs/measurements/memory-model-measurements.md). |
| `--dry-run` | `False` | — | Compute and display the measurement without writing to --docs-path. |
| `--files-per-day` | — | — | Operator-supplied override for files/day of ordinary intake (AC5). Omit to re-derive it from the trailing --intake-window-days scan of raw/ (default). When supplied, the snapshot records files_per_day_source=operator-supplied. |
| `--intake-window-days` | `14` | — | Trailing window (days) files/day-of-intake is measured over (default: 14). |
| `--invalidation-wave-calls-per-night` | `0.0` | — | — |
| `--invalidation-wave-seconds-per-night` | `0.0` | — | — |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--nights-in-wave` | — | — | — |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--summary-log` | — | — | Path to a nightly log file containing 'librarian-run-summary' lines, used to derive wall-clock/file. Omit to report wall-clock figures as not-yet-measurable (no fabricated figure). |
| `--total-nights` | — | — | — |
| `--ttl-recheck-calls-per-night` | `0.0` | — | — |
| `--ttl-recheck-seconds-per-night` | `0.0` | — | — |
| `--wall-clock-per-file-seconds` | — | — | Operator-supplied override for wall-clock/file (AC5). Omit to re-derive it from --summary-log (default). When supplied, the snapshot records wall_clock_source=operator-supplied. |

## `athenaeum measure shadow-linkage`

Shadow-mode complete-linkage cluster population over the live wiki store: embeddings only, zero LLM calls, read-only.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--docs-path` | `docs/measurements/memory-model-measurements.md` | — | Where the snapshot section is written/appended (default: docs/measurements/memory-model-measurements.md). |
| `--dry-run` | `False` | — | Compute and display the measurement without writing to --docs-path. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum measure shadow-parity`

Run the C4 detector and the cluster comparator over the SAME corpus and report their verdict agreement matrix + call multiplier (; the live-corpus route is).

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cases` | — | — | A corpus YAML in the eval case shape (repeatable). Required — the live-corpus route (--path) is and is not wired here. |
| `--dry-run` | `False` | — | Projection only: zero paid calls, nothing written. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--max-usd` | — | — | Hard cost ceiling in USD — aborts before the run starts if the projected lower-bound cost already exceeds it, or mid-run as soon as observed spend crosses it (partial report is still written). |
| `--out` | `measurements` | — | Output directory the dated shadow-parity report is written to (default: measurements). |
| `--path` | `~/knowledge` | — | Reserved for the live-corpus run; unused until then. |

## `athenaeum memory-class`

Memory-taxonomy class maintenance: backfill the memory_class: frontmatter axis across the wiki.

Subcommands:

- `athenaeum memory-class backfill` — Assign memory_class: to pages that lack it — deterministic type-rule map, plus an optional classifier pass over the residual. Dry-run unless --apply. Never overwrites an existing value and never mints 'axiom'.

## `athenaeum memory-class backfill`

Assign memory_class: to pages that lack it — deterministic type-rule map, plus an optional classifier pass over the residual. Dry-run unless --apply. Never overwrites an existing value and never mints 'axiom'.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Write the assignments. Without this flag the command reports and writes nothing. |
| `--batch-size` | `20` | — | Pages per classifier call (default: 20). |
| `--classifier` | `False` | — | Also class the residual (auto-memory/preference/feedback/incident/issue and untyped-but-frontmattered pages) with batched LLM calls routed through the 'classify' knob. Off by default: the deterministic rule map needs no model and covers ~97%% of pages. |
| `--dry-run` | `False` | — | Report without writing. This is already the default; the flag exists so a caller can state it, and it OVERRIDES --apply when both are given (safe mode wins). |
| `--include-retired` | `False` | — | Do not skip pages carrying 'retired: true'. Off by default — a retired page is on its way out and does not merit a classifier call. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum merges`

Inspect unresolved resolver merge proposals in `wiki/_pending_merges.md`. Three modes: list, next, count. The merges half of `athenaeum decisions`.

Subcommands:

- `athenaeum merges count` — Print `N unresolved (oldest: <iso-date>)`.
- `athenaeum merges list` — List all unresolved merge proposals.
- `athenaeum merges next` — Show the oldest unresolved merge (single block).
- `athenaeum merges propose-fold` — Propose folding one or more source pages INTO a named canonical page. Derives merge_target_name from the canonical page's `name:` and write_kind from the corpus — no hand-built proposal. Dry-run by default; --apply to queue.
- `athenaeum merges provenance` — List EXECUTED merges from `wiki/_merge_provenance.jsonl` — which source pages each merge relied on.
- `athenaeum merges recompare` — Re-run the five-verdict comparator over every unresolved merge proposal and record a verdict per source pair in the verdict ledger. Dry-run by default; --apply writes to the LEDGER only — this command never approves, rejects, or archives a proposal, and PII-hazard proposals always route to a human regardless of verdict.
- `athenaeum merges revalidate` — Re-validate existing unresolved merge proposals against the CURRENT suppression gate and archive stale ones. Dry-run by default; pass --apply to write.
- `athenaeum merges scrub-pii` — Redact contact data out of merge-proposal bodies in place. The zero-LLM purge path for a stale `draft_merged_body` left behind by `storage migrate-pii`: it clears the values without approving, rejecting or withdrawing the merge. Dry-run by default; --apply writes.

## `athenaeum merges count`

Print `N unresolved (oldest: <iso-date>)`.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum merges list`

List all unresolved merge proposals.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--limit` | `0` | — | Truncate to first N (default: 0 = unlimited). |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum merges next`

Show the oldest unresolved merge (single block).

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum merges propose-fold`

Propose folding one or more source pages INTO a named canonical page. Derives merge_target_name from the canonical page's `name:` and write_kind from the corpus — no hand-built proposal. Dry-run by default; --apply to queue.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Queue the proposal. Default: dry-run — print the plan, write nothing. |
| `--draft-file` | — | — | Override the merged draft body with this file's contents (for a genuine content merge). Default: the canonical page's current text VERBATIM. |
| `--into` | — | — | The canonical page to fold sources into (a slug, a `<slug>.md` filename, or a path). Must be an existing wiki page whose filename matches its `name:` slug. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--rationale` | — | — | Optional human rationale recorded on the proposal. |
| `--source` | `[]` | — | A source page to fold away (repeatable). Each must be an existing wiki page and must not equal --into. |

## `athenaeum merges provenance`

List EXECUTED merges from `wiki/_merge_provenance.jsonl` — which source pages each merge relied on.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--canonical-slug` | — | — | Filter to records for this canonical target slug. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--merge-id` | — | — | Filter to the record for this merge id. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum merges recompare`

Re-run the five-verdict comparator over every unresolved merge proposal and record a verdict per source pair in the verdict ledger. Dry-run by default; --apply writes to the LEDGER only — this command never approves, rejects, or archives a proposal, and PII-hazard proposals always route to a human regardless of verdict.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Append the computed verdicts to the verdict ledger. Default: dry-run — compare and report, write nothing. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--limit` | `0` | — | Only re-run the first N unresolved proposals (default: 0 = all). |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum merges revalidate`

Re-validate existing unresolved merge proposals against the CURRENT suppression gate and archive stale ones. Dry-run by default; pass --apply to write.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Archive proposals the current gate would suppress. Default: dry-run — report only, write nothing. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum merges scrub-pii`

Redact contact data out of merge-proposal bodies in place. The zero-LLM purge path for a stale `draft_merged_body` left behind by `storage migrate-pii`: it clears the values without approving, rejecting or withdrawing the merge. Dry-run by default; --apply writes.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--allowlist` | — | — | Adjudicated PII allowlist (default: `wiki/_pii-allowlist.yml`). A value with a reasoned entry there is not PII and is left untouched. |
| `--apply` | `False` | — | Redact the detected values in place. Default: dry-run — report only, write nothing. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum outbound-lint`

Scan outbound-destined text for PII (emails/phones) before it ships; flag findings (default) or --redact them. Offline, deterministic.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--allow` | — | — | An email or phone number already known to the recipient, which is dropped from the report. Repeatable. |
| `--allowlist-file` | — | — | Path to a file with one allowlisted address per line. |
| `--file` | — | — | Path to a file whose contents are scanned. Mutually exclusive with --text. |
| `--json` | `False` | — | Emit machine-readable JSON findings instead of plain text (ignored in --redact mode). |
| `--redact` | `False` | — | Strip mode: print the text with each finding replaced by a redaction placeholder (to stdout) instead of reporting findings. |
| `--text` | — | — | Text to scan, given inline. Mutually exclusive with --file; if neither is given, text is read from stdin. |

## `athenaeum pii-restore`

Anchored PII-restore: recover non-PII tokens a [contact redacted -> excluded surface] marker replaced, via rename-following and retro-filename history lookup. Default is dry-run; pass --apply to write fixes.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Write restorations. Without this flag the command is a dry-run. |
| `--contacts-root` | — | — | Excluded 'pii' surface root, resolved from config by default. Never scanned for markers and never written to. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--knowledge-root` | — | — | Knowledge root / git repo root (default: ~/knowledge). |
| `--limit` | — | — | Cap the number of marker sites scanned (debugging/bounded runs). |
| `--reindex` | `False` | — | After a successful --apply, rebuild the search index so restored text replaces corrupted text in the vector/fts5 index. Rewriting a page changes its content hash -- WITHOUT this, --apply leaves the corrupted prose live in the index. Ignored on a dry-run. |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |
| `--wiki-root` | — | — | Wiki directory to scan for markers (default: <knowledge-root>/wiki). |

## `athenaeum push-metrics`

Push-precision + coverage baseline: compute/record the precision snapshot, sample sessions for a human-reviewed coverage-audit worksheet, record a single hook-path push, and stream the documented NDJSON tail contract over the ledgers.

Subcommands:

- `athenaeum push-metrics baseline` — Compute precision + coverage over a window; write the dated snapshot to docs/measurements/memory-model-measurements.md. Refuses to write (exit 1) when the window has zero reference-determination records. See --dry-run to inspect without writing.
- `athenaeum push-metrics coverage-audit` — Sample N sessions' push records into a worksheet of the structural facts hash-only records support (candidate-pool size, tier/scope concentration, filter removal, policy-set bounds) — never a per-candidate marking or a measured miss rate.
- `athenaeum push-metrics liveness` — Read-only assertion: PASS if any of the most recent --window rows is sidecar-tagged, FAIL (exit 1) if --window rows exist and none is, INCONCLUSIVE (exit 0) if fewer than --window rows are recorded (including an absent ledger).
- `athenaeum push-metrics record` — Record one hook-path push: the fire-and-forget entry point the per-turn UserPromptSubmit recall hook calls with the session id and the ids it actually injected. Writes a push record tagged source=hook, distinct from an explicit MCP `recall` push (no source key) and the `athenaeum context` sidecar adapter (source=sidecar). Always exits 0 — see this subcommand's own docstring.
- `athenaeum push-metrics tail` — Stream NDJSON — one object per push record and per reference-determination record, newest-last. Read-only; never mutates the ledgers. See docs/reference/configuration.md ('push-metrics tail — the public NDJSON contract') for the documented --json record shape, schema version, and compatibility note.

## `athenaeum push-metrics baseline`

Compute precision + coverage over a window; write the dated snapshot to docs/measurements/memory-model-measurements.md. Refuses to write (exit 1) when the window has zero reference-determination records. See --dry-run to inspect without writing.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory holding the push-metrics ledgers (default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum) |
| `--docs-path` | `docs/measurements/memory-model-measurements.md` | — | Where the snapshot section is written/appended (default: docs/measurements/memory-model-measurements.md). |
| `--dry-run` | `False` | — | Compute and display the baseline without writing to --docs-path. This is the read-only way to check whether a baseline is computable — combine with --json for a read-only machine-readable inspection. Note: --json alone does NOT suppress the write; use --dry-run for that. |
| `--exclude-session` | — | — | Exclude a KNOWN-synthetic session id (e.g. one that ran the test suite and leaked fixture pushes into the ledger) from the precision/session counts. Repeatable. Excluded sessions and their record counts are always reported, never silently dropped. Accepts the full session id or an unambiguous prefix of exactly one known session id; a value matching zero or multiple known session ids is a hard error (exit 1), never a silent zero-effect success. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--since` | — | — | Window lower bound: relative (7d/24h/30m/2w) or absolute ISO-8601. Default: the whole ledger (instrument-enabled to now). |

## `athenaeum push-metrics coverage-audit`

Sample N sessions' push records into a worksheet of the structural facts hash-only records support (candidate-pool size, tier/scope concentration, filter removal, policy-set bounds) — never a per-candidate marking or a measured miss rate.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory holding the push-metrics ledgers (default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum) |
| `--exclude-session` | — | — | Exclude a KNOWN-synthetic session id (same semantics as `baseline --exclude-session`) from being sampled and from other sessions' candidate lists. Repeatable. Excluded sessions and their record counts are always reported, never silently dropped. Accepts the full session id or an unambiguous prefix of exactly one known session id; a value matching zero or multiple known session ids is a hard error (exit 1), never a silent zero-effect success. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--n` | `10` | — | Number of sessions to sample (default: 10). |
| `--output` | `coverage-audit-worksheet.json` | — | Worksheet output file (default:./coverage-audit-worksheet.json). |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--seed` | — | — | Optional deterministic sample seed (test/repro seam). |

## `athenaeum push-metrics liveness`

Read-only assertion: PASS if any of the most recent --window rows is sidecar-tagged, FAIL (exit 1) if --window rows exist and none is, INCONCLUSIVE (exit 0) if fewer than --window rows are recorded (including an absent ledger).

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory holding the push-metrics ledgers (default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum) |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--window` | — | — | Row-count window to check (default: athenaeum.push_metrics.LIVENESS_WINDOW). |

## `athenaeum push-metrics record`

Record one hook-path push: the fire-and-forget entry point the per-turn UserPromptSubmit recall hook calls with the session id and the ids it actually injected. Writes a push record tagged source=hook, distinct from an explicit MCP `recall` push (no source key) and the `athenaeum context` sidecar adapter (source=sidecar). Always exits 0 — see this subcommand's own docstring.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--backend` | — | — | Optional retrieval-backend label, recorded as-is. |
| `--cache-dir` | — | — | Cache directory holding the push-metrics ledgers (default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum) |
| `--id` | — | — | One id actually injected into the turn. Repeatable. Replaced entirely (not merged) when --stdin-json supplies an `ids` array. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--query` | — | — | Optional raw query text for this push — only its hash is ever retained. |
| `--session-id` | — | — | Consuming session id. Falls back to CLAUDE_CODE_SESSION_ID / CLAUDE_SESSION_ID (push_metrics.resolve_session_id) when omitted and --stdin-json was not passed, or its payload carried none. |
| `--stdin-json` | `False` | — | Read {"session_id":..., "ids": [...], "query":..., "backend":...} from stdin (hook-input shape, mirrors `athenaeum context --stdin-json`). |

## `athenaeum push-metrics tail`

Stream NDJSON — one object per push record and per reference-determination record, newest-last. Read-only; never mutates the ledgers. See docs/reference/configuration.md ('push-metrics tail — the public NDJSON contract') for the documented --json record shape, schema version, and compatibility note.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory holding the push-metrics ledgers (default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum) |
| `--follow` | `False` | — | After draining the ledgers, keep polling and emit records appended afterward, like `tail -f`. Without it, drain and exit. Runs until interrupted (e.g. Ctrl-C). |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--session` | — | — | Only emit records for this consuming session id (exact match). Without this, a viewer process that itself calls recall observes its own pushes mixed into the stream. |
| `--since` | — | — | Only emit records timestamped at/after this bound: relative (7d/24h/30m/2w) or absolute ISO-8601. Default: the whole ledger. |

## `athenaeum query-topics`

Extract substantive search topics from a prompt (Haiku). Used by the UserPromptSubmit hook to rewrite queries before FTS5/vector search. Prints one topic per line to stdout; empty output means fall back to the caller's built-in extractor.

**Positional arguments:**

- `prompt` — The user's raw message.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--knowledge-root`, `--path` | — | — | Knowledge directory whose athenaeum.yaml supplies models.topic (default: ~/knowledge). --path is an alias, matching init/status/serve. |
| `--timeout` | `3.0` | — | Seconds to wait for the LLM before giving up (default: 3.0) |

## `athenaeum questions`

Inspect unresolved entries in `_pending_questions.md`. Three modes: list, next, count. Used by the example SessionStart hook and the resolve-questions skill.

Subcommands:

- `athenaeum questions count` — Print `N unresolved (oldest: <iso-date>)`.
- `athenaeum questions list` — List all unresolved questions.
- `athenaeum questions next` — Show the oldest unresolved question (single block).

## `athenaeum questions count`

Print `N unresolved (oldest: <iso-date>)`.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum questions list`

List all unresolved questions.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--limit` | `0` | — | Truncate to first N (default: 0 = unlimited). |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--with-proposal` | `False` | — | Include the (optional) `**Proposed resolution**` block from the resolver. |

## `athenaeum questions next`

Show the oldest unresolved question (single block).

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--with-proposal` | `False` | — | Include the (optional) `**Proposed resolution**` block from the resolver. |

## `athenaeum rebuild-index`

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--as-of` | — | — | : build an as-of index reflecting the wiki as it stood on this date (pages outside their [valid_from, valid_until] window then are excluded). Always a full build; point --cache-dir at a scratch directory so the live index is not overwritten, then `recall --cache-dir <that>`. Unset = today (the normal live index). |
| `--backend` | — | fts5, vector | Override configured backend (default: read from athenaeum.yaml) |
| `--cache-dir` | — | — | Cache directory (default: ~/.cache/athenaeum) |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--full` | `False` | — | Wipe and fully rebuild instead of applying only the changed/added/deleted delta. Use for seeding or after an embedding-model change; default is incremental. |
| `--incremental` | `False` | — | Apply only the changed/added/deleted hash-diff delta. This is the DEFAULT; the flag makes it explicit. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum recall`

Search the wiki from the shell (one tab-separated hit per line)

**Positional arguments:**

- `query` — Search query string

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--as-of` | — | — | : temporal 'as-of' view. Return the wiki as it stood on this date — pages outside their [valid_from, valid_until] window then are excluded; a fact valid then but expired now is included. Builds a throwaway as-of index in a scratch cache dir (indexed backends) or filters at query time (keyword); the live index is untouched. Unset = today. |
| `--audience` | — | — | : run recall under a restricted read scope. Comma-separated role/group ids; only pages tagged for one of these roles (or 'access: open') are returned. Unset = owner = full access. Exercises the identical filter path as `serve --audience`. |
| `--backend` | — | keyword, fts5, vector | Override configured backend (default: read from athenaeum.yaml) |
| `--cache-dir` | — | — | Cache directory (default: ~/.cache/athenaeum) |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--top-k` | `5` | — | Maximum results to return (default: 5) |
| `--type` | `[]` | — | : narrow to one or more entity classes (a page's `type:`), repeatable (OR semantics). Opaque, NOT validated against wiki/_schema/types.md — see the MCP `entity_schema` tool for the declared/observed registry. Unset = every class (default, unchanged behavior). An unrecognized value prints the deployment's known classes instead of a bare 'no results'. |
| `--usage-class` | `[]` | observed, provider, unclassified | With --with-pii, return only excluded values of this usage class (repeatable; one of observed, provider, unclassified). Matches `entity --usage-class`. Default: every value. |
| `--with-pii` | `False` | — | : also resolve each matching entity's EXCLUDED fields — contact data for a person, and whatever else the operator routes off-corpus for any other class. Appended to each hit line as tab-separated `field=value` pairs; a withheld field appears as `field=[redacted:N]` so withheld never looks like absent. Default off, and free when off: no excluded surface is scanned at all. The join runs strictly AFTER the audience and recallable filters, so it can never widen what this command returns. |

## `athenaeum reconcile`

Retire pending raw-intake files whose content is already materialized in the wiki (dual-write cleanup). Default is dry-run; pass --apply to remove.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Remove reconciled files via `git rm` + commit. Without this flag, the command is a dry-run: nothing is written. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--import-commit` | — | — | Git commit (in --knowledge-root) at which the source dual-wrote raw intake and wiki pages together. No default — pass the exact SHA for the dual-write event you are reconciling. |
| `--knowledge-root` | — | — | Knowledge root (default: ~/knowledge). Must be a git repo containing --import-commit. |
| `--source` | `drive` | — | raw/<source>/ tree to reconcile (default: drive). |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum recovery-yield`

Read-only readout of the auto-memory origin-recovery yield signal: recovered/uncited counters, basis split, resolved threshold, and (AC4) the corpus share of type:auto-memory pages with sources:[]. One JSON object on stdout, exit 0 always, no side effects.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory the signal sidecar lives under (default: ~/.cache/athenaeum) |
| `--path` | `~/knowledge` | — | Knowledge directory for the AC4 corpus scan (default: ~/knowledge). The signal fields are reported regardless of whether this exists. |

## `athenaeum registry`

: compile the source-handle registry.json (entity uid → handle set) from wiki entity frontmatter. Deterministic, no LLM; emits a well-formed registry even when no handles are populated yet.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--out` | — | — | Where to write registry.json (default: <knowledge-root>/registry.json). |
| `--path`, `--knowledge-root` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge). |
| `--stdout` | `False` | — | Print the registry JSON to stdout instead of writing a file. |

## `athenaeum reindex`

Rebuild the search index (FTS5 or vector, per config). --incremental (default) applies only the hash-diff delta; --full rebuilds from scratch.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--as-of` | — | — | : build an as-of index reflecting the wiki as it stood on this date (pages outside their [valid_from, valid_until] window then are excluded). Always a full build; point --cache-dir at a scratch directory so the live index is not overwritten, then `recall --cache-dir <that>`. Unset = today (the normal live index). |
| `--backend` | — | fts5, vector | Override configured backend (default: read from athenaeum.yaml) |
| `--cache-dir` | — | — | Cache directory (default: ~/.cache/athenaeum) |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--full` | `False` | — | Wipe and fully rebuild instead of applying only the changed/added/deleted delta. Use for seeding or after an embedding-model change; default is incremental. |
| `--incremental` | `False` | — | Apply only the changed/added/deleted hash-diff delta. This is the DEFAULT; the flag makes it explicit. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum repair`

Repair YAML-frontmatter corruption in wiki files. Default is dry-run; pass --apply to write fixes.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--all` | `False` | — | Run all repair passes in sequence (tag-indent then value-quoting). |
| `--apply` | `False` | — | Write fixes. Without this flag, the command is a dry-run. |
| `--backfill-sources` | `False` | — | Re-classify memories whose source was DEFAULTED to `claude:inferred` against their origin transcript: user-stated / agent-observed upgrades, else confirm inferred. |
| `--bounce-fold` | `False` | — | Fold slug-keyed bounce marks stranded by the pre- resolve path onto the person record that already lists the same address. Not included in --all — see module docstring. |
| `--contacts-root` | — | — | --bounce-fold: override the contacts surface root (defaults to the configured `pii` entity-class surface under --knowledge-root). |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--knowledge-root` | — | — | Knowledge root for --backfill-sources / --bounce-fold (default: ~/knowledge); auto-memory is read from <root>/raw/auto-memory, and the contacts surface is resolved from it via the configured storage mapping unless --contacts-root overrides that. |
| `--legacy-source-slugs` | `False` | — | Migrate legacy bare-slug `source:` values to typed `script:<slug>` form (/ design-lock §5). |
| `--limit` | — | — | --backfill-sources: cap memories acted on per run (bounded resumable batch). Idempotency makes the resume implicit. |
| `--projects-root` | — | — | Transcript root for --backfill-sources (default: ~/.claude/projects). |
| `--tag-indent` | `False` | — | Normalize block-list indentation under top-level keys (tags:, emails:, aliases:,...). |
| `--value-quoting` | `False` | — | Quote unquoted YAML values that break safe_load (values starting with '-' or '['). |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |
| `--wiki-root` | — | — | Wiki directory (default: ~/knowledge/wiki) |

## `athenaeum reresolve-questions`

Re-resolve open proposal-less pending questions (self-heal transient cap/offline escalations)

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum run`

Run the librarian pipeline

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--allow-degraded` | `False` | — | Exit 0 even when the run stopped early for a resource reason (budget / spend-ceiling / entity-share) AND committed ZERO files -- the DEGRADED REFUSAL, which otherwise exits EXIT_LIBRARIAN_REFUSAL (3) by default so a cron wrapper can tell 'compiled nothing' apart from success by exit code alone. The 'librarian-run-degraded reason=... files=0...' marker line is still logged at ERROR either way -- this flag controls only the exit code, not the log line. Opt-in escape hatch for a deliberate deterministic-phases-only / budget-starved run. --strict-budget takes precedence if both are set (see its help). Full exit-code contract: docs/reference/exit-codes.md. |
| `--batch-mode`, `--no-batch-mode` | — | — | Submit tier-2/tier-3 LLM calls via the Anthropic Messages Batch API at a 50%% token discount. Latency-tolerant: most batches finish within an hour, 24h worst case — intended for the nightly run. --no-batch-mode forces the synchronous path even when the env/yaml default is on. Default: ATHENAEUM_BATCH_MODE env, then athenaeum.yaml librarian.batch_mode, then off. |
| `--cluster-only` | `False` | — | Only run C2 auto-memory discovery + clustering — skip the entity tier pipeline. Writes the cluster JSONL report and exits. Useful for validating the cluster output before C3. |
| `--dry-run` | `False` | — | Run pipeline without writing files or committing |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--full-compile` | `False` | — | Force a whole-corpus auto-memory compile this run, bypassing both the delta gate and the librarian.full_compile_every_days cadence. Use for an immediate full reconciliation (e.g. after suspecting delta drift) without waiting for the periodic backstop. |
| `--full-contradiction-sweep` | `False` | — | Force C4 (contradiction detection) over EVERY cluster this run, regardless of the delta gate or --full-compile's own cadence, and advance the contradiction-sweep-completed stamp. Distinct from --full-compile: this forces only C4, not a full C2 re-cluster. The explicit escape hatch — absent this flag, a full-corpus contradiction sweep never runs implicitly. |
| `--knowledge-root`, `--path` | — | — | Knowledge git repo root (default: ~/knowledge). --path is an alias, matching init/status/serve. |
| `--max-api-calls` | — | — | Maximum estimated API calls per run (default: ATHENAEUM_MAX_API_CALLS env, then athenaeum.yaml librarian.max_api_calls, then 800) |
| `--max-files` | — | — | Stop after processing this many raw files (default: ATHENAEUM_MAX_FILES env, then athenaeum.yaml librarian.max_files, then 50) |
| `--max-runtime` | — | — | Run-level wall-clock deadline in seconds. On trip the run commits partial progress, releases the lock, and exits 75 (EXIT_GRACEFUL_PARTIAL, resumable; — 124 is reserved for an external kill, e.g. coreutils timeout, and is never returned by this internal check) — bounding the WHOLE run incl. the post-compile phases, not just the per-file loop. Default: ATHENAEUM_MAX_RUNTIME env, then athenaeum.yaml librarian.max_runtime, then 3600. Pass 0 (or a negative value) to disable the deadline (unbounded run). Full exit-code contract: docs/reference/exit-codes.md. |
| `--merge-only` | `False` | — | Only run C3 cluster merge — read the canonical cluster JSONL from the last C2 run and emit wiki/auto-*.md entries. Skips discovery, clustering, and the entity tier pipeline. |
| `--no-retire` | — | — | Skip the move-then-retire pass: raw auto-memory is neither moved into the wiki nor git-removed. Overrides the athenaeum.yaml librarian.retire toggle (default on). See the README 'Data lifecycle & upgrade impact' section. |
| `--pull` | — | — | Before the run starts, invoke `git pull --ff-only --autostash` on the knowledge repo using the operator's ambient git credentials, so the run compiles against origin's latest. Overrides the athenaeum.yaml librarian.pull_before_run toggle (default off). No-op on --dry-run. A pull failure (e.g. diverged history that --ff-only rejects) is reported as a non-fatal warning; the run proceeds against the local tree. |
| `--push` | — | — | After a successful run that produced at least one commit, invoke `git push` on the knowledge repo using the operator's ambient git credentials. Overrides the athenaeum.yaml librarian.push_after_run toggle (default off). No-op on --dry-run or when the run produced no commits. A push failure is reported as a non-fatal warning; commits remain local and the next run retries (`git push` is idempotent). |
| `--raw-root` | — | — | Raw intake directory (default: ~/knowledge/raw) |
| `--run-type` | — | — | Declare which caller invoked this run, for spend-ledger attribution: `athenaeum spend --by-provider` groups ledger rows by this value, so an operator can tell a scheduled nightly compile's burn apart from an interactive session's. Default: ATHENAEUM_RUN_TYPE env, then 'librarian' (unchanged pre- behavior). The nightly wrapper — which lives in a different repo, not this one — is expected to set the env var rather than pass this flag, since an env var is easier for an external cron/launchd invocation to set; pass 'librarian-nightly' for that case. |
| `--strict-budget` | `False` | — | Exit nonzero (1) when the run trips the API call budget (the DEGRADED path) instead of the default 0. Opt-in, for exit-code-based alerting; the warning summary and deferred-work manifest are written either way. Broader than a zero-progress refusal (fires on ANY deferral, not just a zero-files one) and wins if both this and --allow-degraded are set. |
| `--verbose`, `-v` | `False` | — | Enable debug logging |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |
| `--wiki-root` | — | — | Wiki output directory (default: ~/knowledge/wiki) |

## `athenaeum serve`

Start the MCP memory server

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--audience` | — | — | : pin this server to a restricted read scope. Comma-separated role/group ids (e.g. 'operations,voltaire'). The recall tool then returns only pages tagged for one of these roles (plus 'access: open' pages); untagged/confidential/personal pages are withheld. Unset = owner = full access. Overrides ATHENAEUM_AUDIENCE and serve.audience in athenaeum.yaml. |
| `--cache-dir` | — | — | Cache directory holding the compiled index (default: ATHENAEUM_CACHE_DIR env, else ~/.cache/athenaeum).: serve previously hardcoded ~/.cache/athenaeum and ignored ATHENAEUM_CACHE_DIR, so recall could serve a stale/empty index when the compiler wrote elsewhere. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge). The raw/wiki roots default to <path>/raw and <path>/wiki; the KNOWLEDGE_RAW_PATH / KNOWLEDGE_WIKI_PATH environment variables override them individually (drop-in parity with the legacy knowledge-mcp server). |

## `athenaeum session-end`

Change-gated ingest + reindex for SessionEnd: compile this session's new raw intake, then refresh the index — a fast no-op (no LLM, no reindex) when nothing changed.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--backend` | — | fts5, vector | Override configured search backend (default: read from athenaeum.yaml) |
| `--cache-dir` | — | — | Cache directory holding the ingest + index manifests (default: ~/.cache/athenaeum) |
| `--dry-run` | `False` | — | Run the compile without writing files, committing, updating the ingest stamp, or reindexing. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--full` | `True` | — | Force a full recompile of all pending raw intake AND a full index rebuild (operator escape hatch). |
| `--incremental` | — | — | Compile only raw new/changed since the last ingest and apply the index delta. This is the DEFAULT. |
| `--path`, `--knowledge-root` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge). --knowledge-root is an alias, matching `run`/`ingest`. |
| `--session` | — | — | Scope the new/changed detection to one originSessionId (the SessionEnd use-case). |
| `--verbose`, `-v` | `False` | — | Enable debug logging |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum spend`

Report LLM spend from the durable ledger ($ for API, tokens for subscription — never blended)

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--by-knob` | `False` | — | Break down per model knob (classify/write/resolve/topic/reasoning_t1/reasoning_t2). |
| `--by-model` | `False` | — | Break down per serving model. |
| `--by-provider` | `False` | — | Break down per run type within each cost path. |
| `--by-surface` | `False` | — | Break down per declared non-batched surface (C4 contradiction detector/resolver, same-page multi-merge, the truncation retry, the tier-3 full-echo fallback) plus an unattributed remainder. |
| `--cache-dir` | — | — | Cache dir holding spend.jsonl (default: ATHENAEUM_CACHE_DIR env, else ~/.cache/athenaeum). |
| `--candidate-max-pct-per-day` | — | — | Ceiling backtest: candidate spend.max_pct_per_day value to replay (only takes effect paired with --candidate-weekly-token-limit). |
| `--candidate-max-tokens-per-day` | — | — | Ceiling backtest: candidate spend.max_tokens_per_day value to replay. |
| `--candidate-max-tokens-per-run` | — | — | Ceiling backtest: candidate spend.max_tokens_per_run value to replay. |
| `--candidate-max-usd-per-day` | — | — | Ceiling backtest: candidate spend.max_usd_per_day value to replay. |
| `--candidate-max-usd-per-run` | — | — | Ceiling backtest: candidate spend.max_usd_per_run value to replay. |
| `--candidate-weekly-token-limit` | — | — | Ceiling backtest: candidate spend.weekly_token_limit value to replay (only takes effect paired with --candidate-max-pct-per-day). |
| `--ceiling-backtest` | `False` | — | Replay candidate spend ceilings (--candidate-* below) against the ledger in the --since window and report per-run/per-day trip rates, using the SAME ceiling_tripped predicate a live run is gated on. Does not choose or arm any ceiling — that is an operator decision — this only reports what a candidate value would have done. READ-ONLY — the ledger file is never modified. |
| `--json` | `False` | — | Emit machine-readable JSON (what /good-morning consumes). |
| `--ledger` | — | — | Explicit ledger file path (overrides --cache-dir and config). |
| `--path` | `~/knowledge` | — | Knowledge directory for config resolution (default: ~/knowledge). |
| `--reprice` | `False` | — | Recompute historical rows from their per-model token attribution at the CURRENT rates and report the delta against the stored figures. READ-ONLY — the ledger file is never modified. |
| `--since` | `7d` | — | Lower bound: a window (7d / 24h / 30m / 2w) or an ISO date (2026-07-01). Default: 7d. |

## `athenaeum status`

Show knowledge base status

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory holding the kill-switch state (default: ~/.cache/athenaeum). Only affects the kill-switch line. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum stopwords`

Print the stopword list (one word per line). Used by the example UserPromptSubmit hook's regex fallback to stay in sync with the FTS5 query filter.

No flags beyond `-h`/`--help`.

## `athenaeum storage`

Storage-surface operator tasks (migrate a page's PII off-corpus).

Subcommands:

- `athenaeum storage audit-h1-redaction` — Read-only audit: report pages whose H1 heading line carries the inline-redaction marker, classified into a defect population (marker consumed the whole heading subject) and a non-defect population (marker replaced one inline token inside an otherwise-intact title). Never writes.
- `athenaeum storage lint-mapping` — storage.mapping completeness lint + the deferred (read_policy, adapter) pair check: every sensitivity class the scanned corpus carries must have a live storage.mapping entry naming a real adapter; exit non-zero on a gap. Advisory-only D4 policy-mismatch findings are also reported but never fail the gate on their own.
- `athenaeum storage lint-pii` — Corpus-wide PII gate: scan EVERY file under wiki/ (queue/index/archive/_-prefixed and.bak files included) for an inline email/phone; exit non-zero on any finding. Also reports raw/ retention as a separate, non-gating count.
- `athenaeum storage migrate-pii` — Move archival contact data (emails/phones) off entity pages to the excluded surface, leaving durable identifiers only. Single page (--page) or bulk (--all / --glob).
- `athenaeum storage prune-dispositions` — One-time prune of wiki/_shape_rule_dispositions.jsonl to its positive-disposition records only (AC3/AC4). Dry-run by default: reports the disposition histogram and projected size. --apply writes.

## `athenaeum storage audit-h1-redaction`

Read-only audit: report pages whose H1 heading line carries the inline-redaction marker, classified into a defect population (marker consumed the whole heading subject) and a non-defect population (marker replaced one inline token inside an otherwise-intact title). Never writes.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--path` | `~/knowledge` | — | Knowledge root (default: ~/knowledge). |
| `--wiki-root` | — | — | Wiki directory to scan (default: <knowledge-root>/wiki). |

## `athenaeum storage lint-mapping`

storage.mapping completeness lint + the deferred (read_policy, adapter) pair check: every sensitivity class the scanned corpus carries must have a live storage.mapping entry naming a real adapter; exit non-zero on a gap. Advisory-only D4 policy-mismatch findings are also reported but never fail the gate on their own.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--corpus` | — | — | Corpus root to scan for sensitivity_class: frontmatter (default: the --path knowledge root). Always caller-supplied — this lint never falls back to a hardcoded or environment-derived path ('s own AC). |
| `--json` | `False` | — | Emit machine-readable JSON findings instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge root (default: ~/knowledge); also the default corpus root. |

## `athenaeum storage lint-pii`

Corpus-wide PII gate: scan EVERY file under wiki/ (queue/index/archive/_-prefixed and.bak files included) for an inline email/phone; exit non-zero on any finding. Also reports raw/ retention as a separate, non-gating count.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--allowlist` | — | — | Adjudicated allowlist of values that are NOT PII (service accounts, tagged test addresses, example-domain placeholders, identifier/timestamp digit runs the phone axis misreads). Each entry needs a non-empty reason. Default: <knowledge-root>/wiki/_pii-allowlist.yml. A missing file means nothing is adjudicated. The allowlist is excluded from its own scan (unblocking). |
| `--json` | `False` | — | Emit machine-readable JSON findings instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge root (default: ~/knowledge). |

## `athenaeum storage migrate-pii`

Move archival contact data (emails/phones) off entity pages to the excluded surface, leaving durable identifiers only. Single page (--page) or bulk (--all / --glob).

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--all` | `False` | — | Bulk: migrate every entity page (top-level wiki/*.md, skipping _-prefixed queue/index/archive files) that carries contact data. Idempotent — re-running skips already-migrated pages, so a run that dies halfway resumes cleanly with no double-writes. |
| `--apply` | `False` | — | Write the changes (rewrite the origin page + create the excluded contact record). Without this flag the command is a dry-run that prints what would change and writes nothing. In bulk mode the dry-run prints a summary (pages affected, records to create), not one diff per page. |
| `--glob` | — | — | Bulk: migrate every file under wiki/ matching PATTERN (supports recursive ** globs; not restricted to *.md), e.g. an archive to redact in place. Same idempotent/resumable semantics as --all. |
| `--list-deferred` | `False` | — | List the pages the rename slice deferred (ambiguous local-part) with their reason, instead of only counting them. This is the operator's manual-naming worklist — deliberately never guesses a display name, so the deferred set is work a human has to do and needs to be enumerable. |
| `--page` | — | — | Path to a single live entity wiki page to migrate. |
| `--path` | `~/knowledge` | — | Knowledge root (default: ~/knowledge). |
| `--reindex` | `False` | — | After a successful --apply, rebuild the search index so the migrated contact data is no longer recallable. Rewriting a page changes its content hash, so an incremental reindex evicts the stale index entry and re-embeds the scrubbed text — WITHOUT this, --apply leaves the pre-migration text live in the index and every migrated address stays reachable via recall. Ignored on a dry-run (nothing changed to reindex). |
| `--rename-name-email` | `False` | — | Also migrate the name-is-an-email population: a page whose name:/preferred_name: IS an email address (the carve-out) is renamed to a display name derived from the local-part (e.g. jane.doe@example.com -> 'Jane Doe'), the address is moved to the excluded contact record, and inbound [[wikilink]]s are rewritten to the new slug. An ambiguous local-part (role address, +tag, initial-blob, numeric/opaque) is left unrenamed and counted as a residual rather than guessed at. Scoped by whichever target selector is in use (--page / --all / --glob); combines with the ordinary contact-data migration in the same run unless --rename-only is given. |
| `--rename-only` | `False` | — | Run ONLY the name-is-an-email rename slice; skip the body-text contact-data migration entirely. Implies --rename-name-email. Use this when the body-migration pass would act on findings you do not want migrated — e.g. while the phone axis still carries detector false positives, where a full --all --apply would redact real prose (; the failure mode spent two restore passes repairing). |
| `--rename-to` | — | — | Operator-supplied display name for a --page rename. refuses to GUESS a name from an ambiguous local-part, but offered no way to supply one — so the deferred population had no route through the tool and could only be hand-edited, which skips the excluded record, the slug rename and the inbound-link rewrite. This is a human asserting the name, so it bypasses the confidence gate by design. Requires --page and --rename-name-email (or --rename-only). |

## `athenaeum storage prune-dispositions`

One-time prune of wiki/_shape_rule_dispositions.jsonl to its positive-disposition records only (AC3/AC4). Dry-run by default: reports the disposition histogram and projected size. --apply writes.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Write the pruned ledger (atomic replace). Without this flag the command is a dry-run that prints the histogram and projected size and writes nothing. Refuses to write (exit 1, nothing written) if a re-parse of the constructed output does not carry exactly the positive-row count the scan pass promised. |
| `--force` | `False` | — | Break the run lock even if a process is still holding it (the current holder is logged first) and proceed. Use ONLY when you are certain the holder is hung or dead; never run two --force invocations concurrently. |
| `--path` | `~/knowledge` | — | Knowledge root (default: ~/knowledge). |
| `--wait` | — | — | Block up to SECONDS for the run lock instead of failing fast. Default: ATHENAEUM_LOCK_TIMEOUT env, then athenaeum.yaml librarian.lock_timeout, then 0 (fail fast). |

## `athenaeum subject`

Subject-coordinate maintenance: backfill the subject: frontmatter axis.

Subcommands:

- `athenaeum subject backfill` — Write subject: (= uid) onto comparator-relevant pages that lack it. Zero-LLM, deterministic. Dry-run unless --apply. Never overwrites an existing value. Read the module docstring before using --apply on a live store.

## `athenaeum subject backfill`

Write subject: (= uid) onto comparator-relevant pages that lack it. Zero-LLM, deterministic. Dry-run unless --apply. Never overwrites an existing value. Read the module docstring before using --apply on a live store.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--apply` | `False` | — | Write the assignments. Without this flag the command reports and writes nothing. |
| `--dry-run` | `False` | — | Report without writing. Already the default; OVERRIDES --apply when both are given (safe mode wins). |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum surface-divergence`

Report the two-surface divergence for a REGISTERED field (wiki frontmatter vs. the contacts/excluded surface) and, by default, exit non-zero when it exceeds the field's declared allowance. Generalizes bounce-divergence / do-not-email-divergence into one per-field guard. Read-only; output is safe to paste publicly.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--contacts-root` | — | — | Override the contacts/excluded surface root (defaults to the configured `pii` entity-class surface). |
| `--field` | — | bounced, do_not_email | Registered field to check. |
| `--json` | `False` | — | Emit the report as JSON instead of plain text. Carries the same opaque handles — no addresses or names in either form. |
| `--path` | `~/knowledge` | — | Knowledge-base root to report on. Both surfaces are resolved from it (the contacts/excluded surface through the configured storage mapping). |
| `--report-only` | `False` | — | Preserve the pre- exit-0-unless-unreadable contract for interactive inspection: never fail on divergence, only on an unreadable surface. Do not pass this from an unattended caller — it is exactly the inert behavior generalizes past. |
| `--wiki-root` | — | — | Override the wiki surface root (defaults to <path>/wiki). |

## `athenaeum test-mcp`

Smoke-test MCP remember/recall against a synthetic knowledge dir

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--keep` | `False` | — | Don't delete the temp knowledge dir on exit (for debugging) |

## `athenaeum usage-report`

Per-claim usage report (pushed / referenced / last-referenced) computed from the push-metrics ledgers — ids-only, no content.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory holding the push-metrics ledgers (default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum) |
| `--claim-id` | — | — | Report usage for a single claim id only. |
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path`, `--knowledge-root` | `~/knowledge` | — | Knowledge directory whose wiki/ AC4-relocated push-records ledger this report reads (default: ~/knowledge).. |
| `--since` | — | — | Window lower bound: relative (7d/24h/30m/2w) or absolute ISO-8601. Default: the whole ledger. |

## `athenaeum verdicts`

Inspect the verdict ledger (`wiki/_verdicts/`) — pairwise comparison verdicts with their justification basis. Four modes: count, list-by-verdict, show-one-pair, show-stale.

Subcommands:

- `athenaeum verdicts count` — Print the live verdict count.
- `athenaeum verdicts list-by-verdict` — List all live verdicts, optionally filtered by --verdict.
- `athenaeum verdicts show-one-pair` — Show the current live verdict for one pair.
- `athenaeum verdicts show-stale` — List every live verdict currently flagged stale.

## `athenaeum verdicts count`

Print the live verdict count.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum verdicts list-by-verdict`

List all live verdicts, optionally filtered by --verdict.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--verdict` | — | duplicate, contradiction, specialization, distinct, underdetermined | Filter to only this verdict value. |

## `athenaeum verdicts show-one-pair`

Show the current live verdict for one pair.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--pair` | — | — | Pair key, e.g. 'id-a+id-b' (order-independent). |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum verdicts show-stale`

List every live verdict currently flagged stale.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--json` | `False` | — | Emit machine-readable JSON instead of plain text. |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |

## `athenaeum viewer`

Serve a localhost-only, read-only page showing pushed-unbidden vs. pulled-deliberately vs. overlap recall for one session.

| Flag | Default | Choices | Help |
|---|---|---|---|
| `--cache-dir` | — | — | Cache directory holding the push-metrics ledgers (default: ATHENAEUM_CACHE_DIR env or ~/.cache/athenaeum) |
| `--path` | `~/knowledge` | — | Knowledge directory (default: ~/knowledge) |
| `--port` | `8756` | — | TCP port to bind on localhost (default: 8756). Pass 0 to let the OS assign a free port. |
| `--session` | — | — | Scope the view to one consuming session id. Strongly recommended: without it, the view includes every session in the ledger, and if the viewer's own process ever triggers a recall call its own activity would appear mixed in. |
