# Athenaeum

Open source knowledge management pipeline for AI agents — append-only intake, tiered compilation, configurable schemas.

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
configured — FTS5 rescues short proper-noun queries that collide in
vector space, vector discovers semantic neighbours with no lexical
overlap. See [`docs/recall-architecture.md`](docs/recall-architecture.md)
for why the hybrid is load-bearing and what invariants must not be
removed.

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

See [`examples/claude-code/README.md`](examples/claude-code/README.md) for
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

**Note on Claude Code auth.** Claude Code's own `CLAUDE_CODE_OAUTH_TOKEN`
is scoped to its inference endpoint and the general Anthropic Messages
API rejects it with `401 OAuth authentication is currently not supported`.
The pipeline and the example hooks need a separate console API key —
see [`docs/recall-architecture.md`](docs/recall-architecture.md#anthropic_api_key-bootstrap-sessionstart)
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

Apache 2.0 — see [LICENSE](LICENSE) for details.
