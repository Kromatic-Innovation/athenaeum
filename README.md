# Athenaeum

[![PyPI version](https://img.shields.io/pypi/v/athenaeum.svg)](https://pypi.org/project/athenaeum/)
[![Python versions](https://img.shields.io/pypi/pyversions/athenaeum.svg)](https://pypi.org/project/athenaeum/)
[![License](https://img.shields.io/pypi/l/athenaeum.svg)](https://github.com/Kromatic-Innovation/athenaeum/blob/main/LICENSE)

**Production-tested agentic memory for teams deploying multiple AI agents.**

<p align="center">
  <img src="https://github.com/Kromatic-Innovation/athenaeum/raw/main/docs/assets/athena.png" alt="Athena with her owl companion, holding an open book showing a knowledge graph" width="360">
</p>

## What is this?

A context window forgets everything the moment a session ends, and even within a session
it can't tell a durable fact ("Acme is a client") from a passing remark not worth
remembering ("the weather is nice"). And when two sessions disagree, most memory systems
resolve the contradiction silently rather than escalating it to a source of truth — such as
a human authority.

Athenaeum is a memory layer that sits outside any one agent session. Agents **write**
observations to an append-only intake log. A separate compiler — **the librarian** — turns
that raw stream into a structured, deduplicated **wiki** of entities. A **sidecar** injects
the relevant slice of that wiki back into context automatically, on every turn.

The result is memory that survives across sessions, across agents, and across a team — not
just across turns of one conversation.

> **Is this for me?** If you're running more than one agent on shared knowledge — or you
> want agents and humans reading and writing the same institutional memory — yes. If you're
> building a single-user chatbot, [mem0](https://github.com/mem0ai/mem0) or
> [Letta](https://github.com/letta-ai/letta) may be a better fit.

The full argument, including how this differs from Claude's built-in memory, Anthropic's
memory tool, RAG, and the existing agent-memory libraries, is in
[**Why Athenaeum**](docs/why-athenaeum.md).

## Quick start

Requires Python 3.13+.

```bash
pip install 'athenaeum[mcp]'

# Create a knowledge directory (default: ~/knowledge)
athenaeum init

# Wire it into Claude Code — it auto-starts with every session
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
```

That gives your agent two tools: `remember` to write an observation, `recall` to search
the compiled wiki. A round-trip looks like this:

> **You:** Jordan's partner is Priya; they met at Stanford GSB.
>
> *Claude calls `remember(...)`. A raw observation lands in
> `~/knowledge/raw/claude-session/…md`.*

Nothing is in the wiki yet — `remember` only appends. Compile it:

```bash
export ANTHROPIC_API_KEY=...     # or configure the claude-cli backend
athenaeum run                    # add --dry-run to inspect without writing
athenaeum status
```

The librarian creates or updates Jordan's entity page, creates Priya's, and links them.
A later session asking *"who is Priya?"* gets the compiled page back from `recall`.

Two more commands you will use daily:

```bash
athenaeum session-end   # make this session's memories recallable now, not tonight
athenaeum decisions     # anything the pipeline refused to resolve on its own
```

→ [Daily operation](docs/guides/daily-operation.md) ·
[Passive recall via hooks](docs/guides/sidecar.md) ·
[MCP tool reference](docs/modules/mcp.md)

## Key features

**A single librarian writes the wiki.** Multiple agents writing the same store produce
duplicate, drifting, and contradictory pages that nobody can arbitrate. One compiler is the
only writer, so the schema stays consistent, duplicates merge, contradictions get caught —
and every run is git-snapshotted, so a bad merge is one `revert` away.
→ [modules/librarian](docs/modules/librarian.md)

**Agents append; they never edit.** Once a bad write is in the store it is
indistinguishable from a good one. Intake is append-only, so the raw record is always
recoverable and safety is a property of the structure rather than of trusting every agent
to be a careful writer. → [modules/intake](docs/modules/intake.md)

**Sources rank; they don't race.** Last-write-wins lets a stale scraped fact quietly
overwrite something the user said directly. Every claim carries a source and conflicts
resolve through a fixed nine-tier precedence order, so low-authority data can never clobber
high-authority data. → [modules/conflicts](docs/modules/conflicts.md)

**Unresolvable conflicts go to a human, not a coin flip.** A memory system that silently
picks a side on every ambiguity will eventually pick the wrong one, and nobody will know to
check. Conflicts the pipeline can't settle land in a durable decisions queue you can list,
answer, and audit. → [guides/decisions](docs/guides/decisions.md)

**Recall is scoped, and fails closed.** A scheduled agent that needs operational context
must not be able to reach PII or client-confidential pages — and "visible unless labeled
private" means one missed label is a leak. A restricted reader sees only what it is
explicitly authorized for; untagged pages stay hidden.
→ [modules/sensitivity](docs/modules/sensitivity.md)

**The background compiler is bounded.** An unattended LLM batch job with no ceiling turns a
bad day into an unbounded bill, and two overlapping runs writing the same store is a
correctness bug. Every run takes a single-writer lock and enforces a hard per-run API
budget. → [modules/librarian](docs/modules/librarian.md)

**Recall happens without being asked.** Memory an agent has to remember to look things up
in is memory that goes unused. A sidecar injects the relevant slice of the wiki into
context on every turn, no tool call required. → [guides/sidecar](docs/guides/sidecar.md)

**Runs on an API key or your Claude subscription.** Metering a separate API key for a
nightly batch job is a barrier when you already pay for Claude Code. One provider seam, two
transports, identical prompts either way. → [modules/provider](docs/modules/provider.md)

## Where to go next

| | |
|---|---|
| **Why this exists** | [Why Athenaeum](docs/why-athenaeum.md) · [North star](docs/north-star.md) |
| **Get it running** | [Install](docs/guides/install.md) · [Daily operation](docs/guides/daily-operation.md) · [Troubleshooting](docs/guides/troubleshooting.md) |
| **How it works** | [Module reference](docs/index.md#modules) — what each part does, reads, writes, and refuses |
| **Every knob** | [Configuration](docs/reference/configuration.md) · [Environment](docs/reference/environment.md) · [Data formats](docs/reference/data-formats.md) |
| **Extend it** | [Adapter contracts](docs/extending/) · [Storage surfaces](docs/extending/storage-adapter-contract.md) |
| **Everything** | [**docs/index.md**](docs/index.md) — the complete map |

## Known limitations

Stated plainly, because you will hit these:

- **Anthropic only.** The provider seam has two backends — the Anthropic API and the
  `claude` CLI. There is no OpenAI, local-model, or Ollama backend. But it's extensible and other providers can be added.
- **Single-owner, not multi-tenant.** Audience scoping is a read filter the operator pins
  at serve time. It is not a multi-user ACL and there is no per-user memory isolation **yet**.
- **The librarian is a batch job.** Compilation is nightly by default; `session-end` closes
  the same-day gap, but there is no streaming write path to the wiki.
- **Storage is markdown on disk.** The storage-adapter seam exists, but no database-backed
  wiki ships in-tree today.
- **Reasoning-tier screening is off by default.** The T2 tier can auto-apply a merge with
  no human review, so it stays opt-in.

Full list: [docs/index.md#limitations](docs/index.md#limitations).

## Contributing

Development setup, branch flow, and the review gate are in
[CONTRIBUTING.md](CONTRIBUTING.md). Security policy: [SECURITY.md](SECURITY.md).

## Getting help

Open an issue at
[Kromatic-Innovation/athenaeum](https://github.com/Kromatic-Innovation/athenaeum/issues).

## License

Apache 2.0. See [LICENSE](LICENSE).
