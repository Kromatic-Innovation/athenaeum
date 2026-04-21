# Changelog

All notable changes to Athenaeum are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Auto-memory cluster pass (C2)** (#196) — new `athenaeum.clusters`
  module groups `AutoMemoryFile` records into near-duplicate clusters
  using the existing chromadb `VectorBackend` embedder (no parallel
  embedding pipeline). Single-linkage clustering with cosine cutoff
  configurable via `librarian.cluster_threshold` (default 0.55, tuned
  against the voltaire/nanoclaw regression fixture). Writes JSONL cluster
  report to `raw/_librarian-clusters.jsonl` with rotated timestamped
  siblings. New `--cluster-only` CLI flag skips the tier pipeline. C3
  merge (#197) consumes the JSONL output.
- **Auto-memory ingest path** (#195) — librarian now discovers files
  under `raw/auto-memory/<scope>/*.md` as a parallel intake channel
  alongside the entity-schema `discover_raw_files`. New
  `AUTO_MEMORY_FILE_RE`, `discover_auto_memory_files()`, and
  `AutoMemoryFile` record carry `origin_scope`, `origin_session_id`,
  `origin_turn`, `memory_type`, and `sources` through to downstream
  tiers. Discovery uses `resolve_extra_intake_roots()` so config is
  single-sourced with recall; `MEMORY.md` and `_migration-log.jsonl`
  are excluded; `_unscoped/` is ingested as a first-class scope.
  Clustering (#196) and wiki merge (#197) ship in subsequent lanes.
- **`athenaeum recall <query>` CLI** (#71) — shell-accessible wrapper around
  the MCP `recall` tool for validation harnesses and operator debugging.
  Prints one tab-separated hit per line (`<score>\t<filename>\t<preview>`).
  Respects configured `search_backend` and extra intake roots; `--top-k`,
  `--path`, `--cache-dir`, and `--backend` flags supported.

## [0.3.1] - 2026-04-21

Quine follow-ups from the 0.3.0 review. Two small hardening fixes on
error-path code; no behavior change under normal execution.

### Fixed
- **`answers._parse_block` no longer relies on `assert` for control flow**
  (#64). Under `python -O` the assert is stripped and the subsequent
  `cb_match.group(...)` would raise `AttributeError` on malformed blocks
  instead of returning `None`. Replaced with an explicit `if cb_match is
  None: return None` guard so `-O` behaves the same as default execution.
- **CLI error output now includes the exception class name** (#65).
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
- **Vector search backend** with chromadb + `all-MiniLM-L6-v2` (#31, #32)
- `athenaeum query-topics` CLI — Haiku-based query preprocessor that extracts substantive topics from instruction-heavy prompts and ignores meta-instructions (#41, #42)
- `athenaeum rebuild-index` CLI for on-demand index rebuilds (#34)
- `athenaeum test-mcp` CLI subcommand for verifying MCP setup (#36)
- Observation filter wired as the tunable capture authority for the sidecar flow (#37)
- `athenaeum.yaml` config with `auto_recall` toggle and `search_backend` selection (#30)
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
- **UserPromptSubmit hook JSON shape** — must be `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}`; a flat `{"additionalContext":...}` payload was silently ignored by Claude Code (#39, #40)
- **Stale chromadb state across rebuilds** — `VectorBackend.build_index` now nukes `vector_dir` and calls `SharedSystemClient.clear_system_cache()` to avoid "Collection already exists" on repeat builds in the same process (#33)
- `serve` CLI now forwards `search_backend` + `cache_dir` to the MCP server (#38)
- Recall now fires after the first message, not only at session start
- Stale "vector backend is a stub for issue #32" comment in `search.py`

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

[Unreleased]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.3...v0.3.0
[0.2.3]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Kromatic-Innovation/athenaeum/releases/tag/v0.1.0
