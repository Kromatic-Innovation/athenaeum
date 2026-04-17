# Athenaeum

[![PyPI version](https://img.shields.io/pypi/v/athenaeum.svg)](https://pypi.org/project/athenaeum/)
[![Python versions](https://img.shields.io/pypi/pyversions/athenaeum.svg)](https://pypi.org/project/athenaeum/)
[![License](https://img.shields.io/pypi/l/athenaeum.svg)](https://github.com/Kromatic-Innovation/athenaeum/blob/main/LICENSE)

Open source knowledge management pipeline for AI agents — append-only intake, tiered compilation, configurable schemas.

> **Using Claude Code?** Athenaeum ships a transparent memory sidecar — a
> SessionStart + UserPromptSubmit hook pair that auto-recalls wiki pages
> relevant to each prompt and lets Claude save observations without
> explicit `/remember` calls. Jump to
> [Transparent sidecar (hooks)](#transparent-sidecar-hooks).

## Architecture

Athenaeum implements a novel approach to persistent AI agent memory:

- **Append-only intake** — safety through write constraints, not trust scores
- **Wikipedia-style footnote trust** — source entities build an emergent trust graph
- **Configurable observation filter** — a self-improving "what to remember" prompt
- **Three types of contradiction** — factual (fix), contextual (keep both), principled (revise axiom)
- **Four-tier compilation** — programmatic → fast LLM → capable LLM → human escalation

## Installation

```bash
pip install athenaeum
```

## Quick start

```bash
# Initialize a knowledge directory
athenaeum init

# Or specify a custom path
athenaeum init --path ~/my-knowledge
```

## Usage

### Running the pipeline

```bash
# Run the librarian pipeline (processes raw files into wiki entities)
athenaeum run

# Dry run — inspect what would happen without writing files
athenaeum run --dry-run

# Custom paths and limits
athenaeum run \
  --raw-root ~/knowledge/raw \
  --wiki-root ~/knowledge/wiki \
  --knowledge-root ~/knowledge \
  --max-files 50 \
  --max-api-calls 200 \
  --verbose
```

### Checking status

```bash
# Show knowledge base status (entity counts, pending files, last run)
athenaeum status
athenaeum status --path ~/my-knowledge
```

### MCP memory server

Athenaeum includes an MCP-compatible server that gives AI agents `remember` and
`recall` tools for persistent knowledge management.

```bash
# Install with MCP support
pip install athenaeum[mcp]

# Start the server
athenaeum serve --path ~/knowledge
```

Smoke-test the round-trip without a live session:

```bash
athenaeum test-mcp
#   PASS  remember_write
#   PASS  recall_search (keyword)
#   PASS  create_server (FastMCP)
#
# 3 passed, 0 failed
```

When wired to Claude Code, the agent can save facts mid-conversation:

> **User:** Tristan's partner is Amanda; they met at Stanford GSB.
>
> *(Claude calls `remember(content="Tristan's partner is Amanda; they met at Stanford GSB.", source="claude-session")`)*
>
> A raw observation lands in `raw/claude-session/20260417T…-…md`. On the
> next `athenaeum run`, the pipeline compiles it into Tristan's wiki
> entity (under "Key Contacts") and Amanda's own entity if she doesn't
> exist yet. Later sessions can ask *"who is Amanda?"* and `recall`
> returns the compiled page.

### Vector search (optional)

Athenaeum supports a vector search backend (chromadb + `all-MiniLM-L6-v2`)
for semantic recall alongside the default FTS5 keyword backend.

```bash
pip install athenaeum[vector]
```

Enable it in `athenaeum.yaml`:

```yaml
search_backend: vector
```

The recall hook runs a **hybrid FTS5 + vector merge** when vector is
configured. Each backend rescues a failure class the other one has:

- **FTS5 rescues proper-noun collisions in embedding space.** Short queries
  like `Return Path` embed closer to generic pages containing the word
  "path" than to a sparse entity page about the company. FTS5 phrase
  matching surfaces the entity. Vector-only recall misses it.
- **Vector rescues semantic queries with no lexical overlap.** A query like
  *"iterative feedback loops"* has no literal token overlap with
  `Innovation Accounting`, but the vector index places them as neighbours.
  FTS5-only recall misses it.

Removing either backend collapses recall for its rescue class. See
[`docs/recall-architecture.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/recall-architecture.md)
for the full walkthrough and the four invariants a future simplification
must not remove.

### Query-topic extraction (optional)

`athenaeum query-topics "your prompt"` runs a Haiku classifier that
returns substantive topics and ignores meta-instructions:

```bash
$ athenaeum query-topics "Without calling any tools, quote the block about Return Path verbatim"
Return Path
```

Compare to the naive regex+stopword fallback, which returns
`block,calling,quote,return,tools,verbatim,without` — burying "Return
Path" behind meta-instruction tokens and dropping the phrase boundary
entirely. The example recall hook uses `query-topics` to rescue
named-entity recall on instruction-heavy prompts; it falls back
silently to the regex extractor if the API key or CLI is unavailable.

**Claude Code integration** — add to your MCP config and it auto-starts with every session:

```bash
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
```

The server exposes two tools:
- **`remember`** — save observations to raw intake (append-only, never overwrites)
- **`recall`** — search the compiled wiki by keyword (frontmatter-weighted scoring)

Raw files written by `remember` are compiled into wiki entities on the next
`athenaeum run`.

#### Transparent sidecar (hooks)

For a fully transparent experience where Claude automatically recalls context and
saves observations without explicit commands, configure Claude Code hooks:

1. **Copy the example hooks** from `examples/claude-code/` to your scripts directory
2. **Add hook entries** to `~/.claude/settings.json` (see `examples/claude-code/settings-snippet.json`)
3. **Add CLAUDE.md instructions** for proactive memory (see `examples/claude-code/CLAUDE.md.example`)

This gives you:
- **Auto-recall** — a SQLite FTS5 index is built at session start (~300ms); each user message triggers a <50ms search that injects relevant wiki pages into context
- **Auto-remember** — Claude proactively saves important facts without being asked
- **Context checkpointing** — observations are saved before context window compaction

See [`examples/claude-code/README.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/examples/claude-code/README.md) for
complete setup instructions, a smoke test, and the full environment-variable
reference.

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes (unless `--dry-run`) | API key for Tier 2/3 LLM calls |
| `ATHENAEUM_CLASSIFY_MODEL` | No | Override Tier 2 model (default: `claude-haiku-4-5-20251001`) |
| `ATHENAEUM_WRITE_MODEL` | No | Override Tier 3 model (default: `claude-sonnet-4-6`) |
| `ATHENAEUM_TOPIC_MODEL` | No | Override query-topic model (default: `claude-haiku-4-5-20251001`) |
| `ATHENAEUM_OP_KEY_PATH` | No | 1Password path for the session-start ANTHROPIC_API_KEY bootstrap (default: `op://Agent Tools/Anthropic API Key/credential`) |
| `AUTO_RECALL` | No | Per-turn recall on/off (hook shell env; overrides `athenaeum.yaml`'s `auto_recall`). Default: `true` |
| `SEARCH_BACKEND` | No | `fts5` or `vector` (hook shell env; overrides `athenaeum.yaml`'s `search_backend`). Default: `fts5` |
| `ATHENAEUM_HOOK_DEBUG` | No | Set to `1` to log vector-backend errors from `user-prompt-recall.sh` to stderr |

**Note on shell-env overrides.** `AUTO_RECALL` and `SEARCH_BACKEND` are
read from the shell environment after the hook sources
`~/.cache/athenaeum/config.env`, so anything exported in your shell
profile beats the cached config. That's intentional (it lets an adopter
A/B-test a backend without editing `athenaeum.yaml`), but it's also
the first thing to check when the hook "ignores" a config change.

**Note on Claude Code auth.** Claude Code's own `CLAUDE_CODE_OAUTH_TOKEN`
is scoped to its inference endpoint and the general Anthropic Messages
API rejects it with `401 OAuth authentication is currently not supported`.
The pipeline and the example hooks need a separate console API key —
see [`docs/recall-architecture.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/recall-architecture.md#anthropic_api_key-bootstrap-sessionstart)
for the 1Password bootstrap pattern.

### Raw file format

Raw intake files live in `raw/{source}/*.md` and follow the naming convention:

```
{timestamp}-{uuid8}.md
```

Example: `20240406T120000Z-aabb0011.md`

Each file is a plain markdown document containing observations, notes, or session
transcripts. The `{source}` directory name (e.g., `sessions`, `imports`) identifies
the origin of the data.

### Output

The pipeline produces wiki entity pages in `wiki/` with YAML frontmatter:

```yaml
---
uid: a1b2c3d4
type: person
name: Alice Zhang
aliases: [Alice]
access: internal
tags: [active]
created: '2024-04-06'
updated: '2024-04-06'
---
```

Entity pages are indexed in `wiki/_index.md`, grouped by type.
Conflicts requiring human review are appended to `wiki/_pending_questions.md`.

At the end of each run, token usage and estimated costs are logged.

## Known limitations (v0.2.x)

Athenaeum is pre-1.0. The following trade-offs are intentional for this
release and slated for revisit in v0.3:

- **No retrieval benchmarks yet.** The hybrid-search claim rests on the
  concrete failure modes above (proper-noun collision, no-overlap semantic
  queries) and production use — not on a published eval against mem0 /
  Letta / Zep / Cognee. If you need benchmarked recall@k on a closed
  corpus, pick a tool that publishes numbers. If you want a knowledge base
  that survives your tool choices, this is for you. PRs adding an eval
  harness are very welcome.
- **FTS5 index rebuilds are non-atomic and unlocked.** A shell hook and
  the librarian run rebuilding simultaneously can race; the window is
  small and single-user wikis do not hit it in practice, but multi-writer
  safety is v0.3 work. Workaround: don't invoke `athenaeum rebuild-index`
  and `athenaeum run` concurrently on the same `$KNOWLEDGE_ROOT`.
- **The `keyword` search backend is a scan-on-query fallback.** It reads
  every wiki page on every query; fine under ~1,000 entities, painful
  past that. Use `search_backend: fts5` (default in the CLI and hooks)
  for any non-trivial wiki. The keyword backend exists as a
  zero-dependency baseline for tests and bootstrap.
- **Tier 4 (human escalation) is a file, not a workflow.** Conflicts land
  in `wiki/_pending_questions.md`; you read it and decide. No PR-opening,
  no Slack integration, no UI — on purpose, for now.

## Development

```bash
# Clone and install in development mode
git clone https://github.com/Kromatic-Innovation/athenaeum.git
cd athenaeum
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/
```

## License

Apache 2.0 — see [LICENSE](https://github.com/Kromatic-Innovation/athenaeum/blob/main/LICENSE) for details.
