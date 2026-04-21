# Athenaeum

<p align="center">
  <img src="docs/assets/athena.png" alt="Athena with her owl companion, holding an open book showing a knowledge graph" width="480">
</p>

[![PyPI version](https://img.shields.io/pypi/v/athenaeum.svg)](https://pypi.org/project/athenaeum/)
[![Python versions](https://img.shields.io/pypi/pyversions/athenaeum.svg)](https://pypi.org/project/athenaeum/)
[![License](https://img.shields.io/pypi/l/athenaeum.svg)](https://github.com/Kromatic-Innovation/athenaeum/blob/main/LICENSE)

**Production-grade agentic memory for teams deploying multiple AI agents.**
Append-only intake, a tiered librarian that compiles raw observations into a
trustworthy wiki, and a sidecar that makes recall happen passively on every
turn.

> **Is this for me?** If you're running more than one agent on shared
> knowledge — or if you want agents and humans reading and writing the same
> institutional memory — yes. If you're building a single-user chatbot,
> [mem0](https://github.com/mem0ai/mem0) or
> [Letta](https://github.com/letta-ai/letta) may be a better fit.

## Why Athenaeum

Four design choices separate a production memory system from a single-user
markdown file. Each one fixes something that quietly breaks when a team scales
past one agent:

1. **[Sources as first-class objects](docs/why-athenaeum.md#1-sources-are-first-class-objects-trust-but-verify)** — every claim carries provenance, the way Wikipedia does. An unfootnoted fact is an assertion.
2. **[The librarian — a tiered compilation pipeline](docs/why-athenaeum.md#2-the-librarian--a-tiered-compilation-pipeline)** — agents can only _append_ to raw intake. A separate compiler is the only writer to the wiki. Safety from structure, not trust.
3. **[Passive recall](docs/why-athenaeum.md#3-passive-recall--recall-on-every-turn-automatically)** — a hybrid FTS5+vector search fires on every turn and injects breadcrumbs into context. The agent doesn't have to remember to look.
4. **[An editable observation filter](docs/why-athenaeum.md#4-the-notetaker--a-configurable-editable-observation-filter)** — what the agent saves is governed by a prompt you can read, edit, and audit. Not a black box.

Full rationale, comparison with alternatives (Claude memory, Anthropic's
memory tool, RAG, Karpathy's gist, mem0/Letta/Zep/Cognee), and the lessons
from running it on our own operations live in
[**docs/why-athenaeum.md**](docs/why-athenaeum.md). For the companion blog
post: [What We Learned Running Our Own Operations on Agentic
Memory](https://kromatic.com/blog/agentic-memory-in-production/).

## Installation

```bash
pip install athenaeum
```

## Quick start

```bash
# Initialize a knowledge directory
athenaeum init                  # default: ~/knowledge
athenaeum init --path ~/my-knowledge

# Run the librarian (compile raw intake → wiki entities)
athenaeum run
athenaeum run --dry-run         # inspect without writing

# Check status
athenaeum status
```

Full run with custom paths and budgets:

```bash
athenaeum run \
  --raw-root ~/knowledge/raw \
  --wiki-root ~/knowledge/wiki \
  --knowledge-root ~/knowledge \
  --max-files 50 \
  --max-api-calls 200 \
  --verbose
```

## MCP memory server

Athenaeum ships an MCP server exposing `remember` and `recall` tools so AI
agents can write to raw intake and search the compiled wiki:

```bash
pip install athenaeum[mcp]
athenaeum serve --path ~/knowledge

# Smoke test the round-trip without a live session
athenaeum test-mcp
```

**Claude Code integration.** Add to your MCP config and it auto-starts with
every session:

```bash
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
```

Example round-trip:

> **User:** Tristan's partner is Amanda; they met at Stanford GSB.
>
> *(Claude calls `remember(content="Tristan's partner is Amanda; they met at Stanford GSB.", source="claude-session")`)*
>
> A raw observation lands in `raw/claude-session/20260417T…-…md`. On the
> next `athenaeum run`, the pipeline compiles it into Tristan's wiki
> entity (under "Key Contacts") and Amanda's own entity if she doesn't
> exist yet. Later sessions can ask _"who is Amanda?"_ and `recall`
> returns the compiled page.

## Answering pending questions

When Tier 3 can't resolve an ambiguity or a principled contradiction, the
librarian escalates to `wiki/_pending_questions.md`. Each escalation lands
as a block like:

```markdown
## [2026-04-20] Entity: "Acme Corp" (from sessions/20240406T120000Z-aabb0011.md)
- [ ] Is Acme still Series A after the 2026 recapitalisation?
**Conflict type**: principled
**Description**: Prior wiki says Series A; the 2026-04 raw file implies Series B.
```

You resolve a question one of two ways — pick whichever fits your workflow:

### Option 1 — Edit the file directly

Flip `[ ]` to `[x]` on the checkbox line and type your answer below the
checkbox (above or below the conflict-type / description lines — either
works; the parser strips those metadata lines when extracting the answer):

```markdown
## [2026-04-20] Entity: "Acme Corp" (from sessions/20240406T120000Z-aabb0011.md)
- [x] Is Acme still Series A after the 2026 recapitalisation?

They closed Series B on 2026-03-12, led by Acme Growth Partners.
The 2026-04 raw file is correct; the prior wiki entry is stale.

**Conflict type**: principled
**Description**: Prior wiki says Series A; the 2026-04 raw file implies Series B.
```

### Option 2 — Use the MCP tool

For containerized agents that can't touch the filesystem, `athenaeum serve`
exposes two tools:

- `list_pending_questions()` returns unanswered blocks as JSON — each item
  carries a stable `id` derived from the header + question text.
- `resolve_question(id, answer)` flips the checkbox and writes the answer
  body under it. It does **not** archive on its own — archival runs on the
  next `ingest-answers` pass.

### Step 2 — ingest the answers

Either way, run:

```bash
athenaeum ingest-answers --path ~/knowledge
```

Each `[x]` block is rewritten as a raw intake file under
`raw/answers/{timestamp}-{entity-slug}.md` with frontmatter linking back
to the original source, then moved into
`wiki/_pending_questions_archive.md` (newest-first, append-only — answered
blocks are never deleted, only moved). The next `athenaeum run` picks the
raw file up like any other intake and folds the answer into the wiki
entity.

Re-running with no new `[x]` blocks is a no-op. Malformed blocks are
preserved in place and logged to stderr, so a corrupt single entry cannot
poison the rest of the file.

## Transparent sidecar (Claude Code hooks)

For a fully passive experience where Claude auto-recalls relevant context on
every prompt and saves observations without explicit commands, configure
Claude Code hooks:

1. Copy the example hooks from `examples/claude-code/` to your scripts directory.
2. Add hook entries to `~/.claude/settings.json` (see `examples/claude-code/settings-snippet.json`).
3. Add CLAUDE.md instructions for proactive memory (see `examples/claude-code/CLAUDE.md.example`).

This gives you:

- **Auto-recall** — an FTS5 index is built at session start (~300ms); each user message triggers a <50ms search that injects relevant wiki pages into context.
- **Auto-remember** — Claude proactively saves important facts without being asked.
- **Context checkpointing** — observations are saved before context-window compaction.

Full setup guide, smoke test, and environment-variable reference:
[`examples/claude-code/README.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/examples/claude-code/README.md).

## Vector search (optional)

Athenaeum supports a vector search backend (chromadb + `all-MiniLM-L6-v2`)
for semantic recall alongside the default FTS5 keyword backend. The recall
hook runs a **hybrid FTS5 + vector merge** when vector is configured —
each backend rescues a failure class the other has (short-query proper-noun
collisions for vector; no-lexical-overlap semantic queries for FTS5).

```bash
pip install athenaeum[vector]
```

Enable it in `athenaeum.yaml`:

```yaml
search_backend: vector
```

Full walkthrough and the four invariants a future simplification must not
remove: [`docs/recall-architecture.md`](docs/recall-architecture.md).

## Query-topic extraction (optional)

`athenaeum query-topics "your prompt"` runs a Haiku classifier that returns
substantive topics and ignores meta-instructions:

```bash
$ athenaeum query-topics "Without calling any tools, quote the block about Return Path verbatim"
Return Path
```

The naive regex+stopword fallback returns
`block,calling,quote,return,tools,verbatim,without` — burying "Return Path"
behind meta-instruction tokens. The example recall hook uses `query-topics`
to rescue named-entity recall on instruction-heavy prompts and falls back
silently to the regex extractor if the API key or CLI is unavailable.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes (unless `--dry-run`) | API key for Tier 2/3 LLM calls |
| `ATHENAEUM_CLASSIFY_MODEL` | No | Override Tier 2 model (default: `claude-haiku-4-5-20251001`) |
| `ATHENAEUM_WRITE_MODEL` | No | Override Tier 3 model (default: `claude-sonnet-4-6`) |
| `ATHENAEUM_TOPIC_MODEL` | No | Override query-topic model (default: `claude-haiku-4-5-20251001`) |
| `ATHENAEUM_OP_KEY_PATH` | No | 1Password path for the session-start `ANTHROPIC_API_KEY` bootstrap (default: `op://Agent Tools/Anthropic API Key/credential`) |
| `AUTO_RECALL` | No | Per-turn recall on/off (hook shell env; overrides `athenaeum.yaml`'s `auto_recall`). Default: `true` |
| `SEARCH_BACKEND` | No | `fts5` or `vector` (hook shell env; overrides `athenaeum.yaml`'s `search_backend`). Default: `fts5` |
| `ATHENAEUM_HOOK_DEBUG` | No | Set to `1` to log vector-backend errors from `user-prompt-recall.sh` to stderr |

**Shell-env overrides.** `AUTO_RECALL` and `SEARCH_BACKEND` are read from the
shell environment after the hook sources `~/.cache/athenaeum/config.env`, so
exports in your shell profile beat the cached config. Intentional (lets you
A/B-test a backend without editing `athenaeum.yaml`), but it's the first
thing to check when the hook "ignores" a config change.

**Claude Code auth caveat.** Claude Code's own `CLAUDE_CODE_OAUTH_TOKEN` is
scoped to its inference endpoint, and the Anthropic Messages API rejects it
with `401 OAuth authentication is currently not supported`. The pipeline and
example hooks need a separate console API key — see
[`docs/recall-architecture.md`](docs/recall-architecture.md#anthropic_api_key-bootstrap-sessionstart)
for the 1Password bootstrap pattern.

## Data formats

**Raw intake** lives in `raw/{source}/*.md` with the naming convention
`{timestamp}-{uuid8}.md` (e.g., `20240406T120000Z-aabb0011.md`). Each file is
a plain markdown document containing observations, notes, or session
transcripts. The `{source}` directory identifies the origin (e.g.,
`sessions`, `imports`).

**Wiki entity pages** live in `wiki/` with YAML frontmatter:

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

Entities are indexed in `wiki/_index.md` grouped by type. Conflicts requiring
human review are appended to `wiki/_pending_questions.md`. Each run logs
token usage and estimated costs at the end.

## Known limitations (v0.2.x)

Athenaeum is pre-1.0. These trade-offs are intentional for this release and
slated for revisit in v0.3:

- **No retrieval benchmarks yet.** The hybrid-search claim rests on concrete
  failure modes (proper-noun collision, no-overlap semantic queries) and
  production use — not a published eval against mem0 / Letta / Zep /
  Cognee. If you need benchmarked recall@k on a closed corpus, pick a tool
  that publishes numbers. If you want a knowledge base that survives your
  tool choices, this is for you. PRs adding an eval harness are very
  welcome.
- **FTS5 index rebuilds are non-atomic and unlocked.** A shell hook and the
  librarian run rebuilding simultaneously can race; the window is small and
  single-user wikis do not hit it in practice, but multi-writer safety is
  v0.3 work. Workaround: don't invoke `athenaeum rebuild-index` and
  `athenaeum run` concurrently on the same `$KNOWLEDGE_ROOT`.
- **The `keyword` search backend is a scan-on-query fallback.** It reads
  every wiki page on every query; fine under ~1,000 entities, painful past
  that. Use `search_backend: fts5` (default in the CLI and hooks) for any
  non-trivial wiki. The keyword backend exists as a zero-dependency baseline
  for tests and bootstrap.
- **Tier 4 (human escalation) is a file, not a workflow.** Conflicts land in
  `wiki/_pending_questions.md`; you read it and decide. No PR-opening, no
  Slack integration, no UI — on purpose, for now.

## Development

```bash
git clone https://github.com/Kromatic-Innovation/athenaeum.git
cd athenaeum
pip install -e ".[dev]"

pytest tests/ -v
ruff check src/ tests/
```

## Getting help

Rolling this out on a team? Open an
[issue](https://github.com/Kromatic-Innovation/athenaeum/issues) or reach out
via [kromatic.com](https://kromatic.com/). We talk to teams working through
agent-memory rollouts often and are happy to point at whatever's useful.

## License

Apache 2.0 — see [LICENSE](https://github.com/Kromatic-Innovation/athenaeum/blob/main/LICENSE).
