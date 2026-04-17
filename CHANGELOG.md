# Changelog

All notable changes to Athenaeum are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Kromatic-Innovation/athenaeum/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Kromatic-Innovation/athenaeum/releases/tag/v0.1.0
