# Athenaeum

[![PyPI version](https://img.shields.io/pypi/v/athenaeum.svg)](https://pypi.org/project/athenaeum/)
[![Python versions](https://img.shields.io/pypi/pyversions/athenaeum.svg)](https://pypi.org/project/athenaeum/)
[![License](https://img.shields.io/pypi/l/athenaeum.svg)](https://github.com/Kromatic-Innovation/athenaeum/blob/main/LICENSE)

**Compiled, provenance-carrying memory for AI agents whose knowledge has outgrown a flat memory file.**

## Quick start

```bash
uv tool install --python 3.13 'athenaeum[mcp]'
athenaeum --version
athenaeum test-mcp    # smoke-tests remember/recall against a scratch dir
```

The later steps (`athenaeum run`, which compiles notes) need a logged-in `claude` CLI or an `ANTHROPIC_API_KEY`; see [Supplying an API key](docs/guides/install.md#i-want-to-supply-an-api-key-without-1password).

**That block needs no API key.** `test-mcp` runs against a scratch directory,
so you can confirm the install works before wiring anything up or spending API
budget. Python 3.13+ is required (`--python 3.13` lets `uv` fetch it if needed)
and `git` must be installed.

See [Install](docs/guides/install.md) for the `PATH` setup `uv tool install`
needs, and the wrong-interpreter warning to check against if `athenaeum --version`
ever looks stale. No git identity is needed (`init` sets a repo-local one if you have none).
Only the current release is supported; see the [support policy](docs/guides/install.md#support-policy) and
[what counts as stable](docs/guides/stability.md).
Upgrading an existing install? See [Upgrading](docs/guides/upgrading.md).

The `[mcp]` extra in the install command above is required by `athenaeum serve` and
`athenaeum test-mcp`. Now set up your knowledge directory:

```bash
# Create a knowledge directory (default: ~/knowledge)
athenaeum init

# Wire it into Claude Code — it auto-starts with every session
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
```

> **Extras:** `[mcp]` is all a fresh install needs. `[vector]` adds semantic search;
> without it search falls back to `fts5` with a one-time warning, never a failure.
> See [Install](docs/guides/install.md) for both extras.

> **Troubleshooting `init`:** `athenaeum init` makes its seed commit with git
> hooks disabled, so a global `core.hooksPath` commit hook (for example a
> protected-branch guard) does not block it.

Then check the install (read-only):

```bash
athenaeum doctor
```

On a fresh install, steps you have not done yet show as `WARN` with
`setup pending` and the command that finishes each one; `doctor` still exits
`0`. Only something present but broken (an unparseable `settings.json`, a hook
whose script is missing, an unreadable index) is a `FAIL` and exits non-zero.
Abridged output right after `athenaeum init`, before `claude mcp add` (paths
shortened):

```text
Athenaeum Doctor
========================================
[OK] package_version: running X.Y.Z; latest release is X.Y.Z
[OK] interpreter: Python 3.13.1
[WARN] mcp: setup pending: no 'athenaeum' entry under mcpServers in ~/.claude.json
    fix: claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
[WARN] hooks: setup pending: no Claude Code settings file found at ~/.claude/settings.json
    fix: merge examples/claude-code/settings-snippet.json into ~/.claude/settings.json ...
[WARN] key_mode: setup pending: llm.provider=claude-cli but Claude Code is not logged in ...
    fix: run `claude` and `/login`, or set ANTHROPIC_API_KEY
[WARN] search_index: setup pending: no vector index found under ~/.cache/athenaeum/wiki-vectors
    fix: run `athenaeum reindex --path <knowledge-root>` to build it, ...
...
All checks passed, with 4 warning(s).
```

Run it again once you have finished the quick start (through
`athenaeum session-end`): `mcp` and `search_index` are then OK. `hooks` stays
a `WARN` because the per-turn recall hook is optional and the quick start does
not wire it (see [Passive recall via hooks](docs/guides/sidecar.md)).

Once wired up, your agent has two tools: `remember` to write an observation, `recall` to search
the compiled wiki. A round-trip looks like this:

> **You:** Jordan's partner is Priya; they met at Stanford GSB.
>
> *Claude calls `remember(...)`. A raw observation lands in
> `~/knowledge/raw/claude-session/…md`.*

Nothing is in the wiki yet — `remember` only appends. Compile it:

```bash
athenaeum run                    # add --dry-run to inspect without writing
athenaeum status
```

`athenaeum init` already chose the compile provider and printed it. If it
found a logged-in `claude` CLI it selected `claude-cli`, and compiles use that
login with no key (they draw on your Claude Code plan's usage limits; about
3,800 to 4,400 tokens and 30 s per note, see [Install](docs/guides/install.md)).
An Anthropic API key is the alternative: `export ANTHROPIC_API_KEY=...` before
`athenaeum run` if `init` told you no login was found.

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

> **Is this for me?** Three cases, in order of how well they are established:
>
> - **Your corpus no longer fits your agent's native memory.** Claude Code's auto memory
>   loads the first 200 lines or 25KB of its index and nothing more; past a few hundred
>   pages the agent is grepping a directory. If you have thousands of pages of people,
>   projects, decisions and retros, you are already past that line. This is the case
>   Athenaeum is being measured against, head to head, and the result decides whether the
>   project continues — see [use cases](docs/use-cases.md) §1.
> - **You need to know where a fact came from before you act on it.** Relationship and
>   client memory where a stale scraped value must never overwrite what a person told
>   you, and where contact data has to stay off the searchable corpus.
> - **Several agents, or agents and humans, write the same knowledge.** This is what the
>   write path is built for. It is the design goal, not yet a validated market: we run it
>   this way ourselves, but we have not confirmed that teams want it.
>
> If you are building a single-user chatbot that needs conversational continuity and
> your memory fits in a file, [mem0](https://github.com/mem0ai/mem0),
> [Letta](https://github.com/letta-ai/letta), or the agent's own memory is a better fit.

The full argument, including how this differs from Claude's built-in memory, Anthropic's
memory tool, RAG, and the existing agent-memory libraries, is in
[**Why Athenaeum**](docs/why-athenaeum.md).

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
answer, and audit — `athenaeum decisions` is the one queue for every pending item (questions,
merge proposals, and more). → [guides/decisions](docs/guides/decisions.md)

**Recall is scoped, and fails closed.** A scheduled agent that needs operational context
must not be able to reach PII or client-confidential pages — and "visible unless labeled
private" means one missed label is a leak. A restricted reader sees only what it is
explicitly authorized for; untagged pages stay hidden.
→ [modules/sensitivity](docs/modules/sensitivity.md)

**The background compiler is bounded.** An unattended LLM batch job with no ceiling turns a
bad day into an unbounded bill, and two overlapping runs writing the same store is a
correctness bug. Every run takes a single-writer lock and enforces a hard per-run API
budget. → [modules/librarian](docs/modules/librarian.md)

**Page recompile restructures oversized pages automatically, on by default.** It costs
model calls every night; the cost, bounds and off switch are in
[guides/nightly-cost](docs/guides/nightly-cost.md).

**Recall happens without being asked.** Memory an agent has to remember to look things up
in is memory that goes unused. A sidecar injects the relevant slice of the wiki into
context on every turn, no tool call required. → [guides/sidecar](docs/guides/sidecar.md)

**Runs on an API key or your Claude subscription.** Metering a separate API key for a
nightly batch job is a barrier when you already pay for Claude Code. One provider seam, two
transports, identical prompts either way. → [modules/provider](docs/modules/provider.md)

**Can be served remotely (experimental).** A host that cannot launch a local process,
such as ChatGPT or the Grok apps, can't reach a stdio memory server at all.
`athenaeum serve --transport http` serves the store over HTTPS behind your reverse proxy,
with OIDC bearer-token auth (`serve.auth.*`) and a `remote` serve profile that narrows
what leaves your machine, and each remote tool call is written to an audit log
(digests and page ids only, never page text). Experimental, and not covered by the `athenaeum.__all__`
compatibility promise: flags and keys may change in a minor release.
→ [guides/remote](docs/guides/remote.md)

**Transcript authorship can be attested (experimental).** `promptwitness` is a separate
package and console script that keeps a hash-chained ledger recording that a human typed
each prompt, with no prompt text stored, so a transcript can later be checked against it.
Experimental, and not part of `athenaeum.__all__`: its CLI and formats may change in a
minor release. → [design/witness-ledger](docs/design/witness-ledger.md)

## Stability and versioning

- **Pre-1.0.** Athenaeum follows semantic versioning, but until 1.0 a minor
  version (0.x) may make breaking changes. Read the [CHANGELOG](CHANGELOG.md)
  before upgrading across a minor.
- **Public API.** Only the names in `athenaeum.__all__` are public API and carry the
  compatibility promise. Everything else, including every other importable module
  and each CLI flag, config key and file format not documented as stable, is
  internal and may change.
- **Experimental surfaces** are marked as such where they are documented (remote
  serving with OIDC auth, `promptwitness`, and the source trust graph). They sit
  outside `athenaeum.__all__` and may change in any minor release without a
  deprecation cycle.
- **Removed surfaces.** 0.24.0 removed `athenaeum merges` and `athenaeum questions`
  (use `athenaeum decisions`), `athenaeum rebuild-index` (use `athenaeum reindex`),
  `athenaeum split-oversize` (use `athenaeum recompile`), seven one-shot repair
  commands, the legacy `block` / `error` / `resolved_block` keys in MCP responses,
  and a set of config keys and switches that had no effect or only turned
  always-on behaviour off. See [Upgrading](docs/guides/upgrading.md) for the
  command-by-command mapping and the [CHANGELOG](CHANGELOG.md) for the full list.
- **Security fixes** go to the latest release only; see [SECURITY.md](SECURITY.md)
  for supported versions and how to report a vulnerability.

## Project status

Athenaeum is maintained by Kromatic. Security reports go to the mailbox named
in [SECURITY.md](SECURITY.md). If the project is ever discontinued, the
repository will be archived and this README will say so at the top.

## Where to go next

| | |
|---|---|
| **Why this exists** | [Why Athenaeum](docs/why-athenaeum.md) · [North star](docs/north-star.md) |
| **Get it running** | [Install](docs/guides/install.md) · [What runs unattended, and what happens when a prerequisite is missing](docs/guides/install.md#what-runs-unattended-after-install) · [Other clients and platforms](docs/guides/clients.md) · [Reading a shared store from a second install](docs/guides/shared-store-reader.md) · [Daily operation](docs/guides/daily-operation.md) · [Troubleshooting](docs/guides/troubleshooting.md) |
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
