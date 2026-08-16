# Athenaeum

[![PyPI version](https://img.shields.io/pypi/v/athenaeum.svg)](https://pypi.org/project/athenaeum/)
[![Python versions](https://img.shields.io/pypi/pyversions/athenaeum.svg)](https://pypi.org/project/athenaeum/)
[![License](https://img.shields.io/pypi/l/athenaeum.svg)](https://github.com/Kromatic-Innovation/athenaeum/blob/main/LICENSE)

**Production-tested agentic memory for teams deploying multiple AI agents.**
Append-only intake, a tiered librarian that compiles raw observations into a
trustworthy wiki, and a sidecar that makes recall happen passively on every
turn.

<p align="center">
  <img src="https://github.com/Kromatic-Innovation/athenaeum/raw/main/docs/assets/athena.png" alt="Athena with her owl companion, holding an open book showing a knowledge graph" width="360">
</p>

> **Is this for me?** If you're running more than one agent on shared
> knowledge — or if you want agents and humans reading and writing the same
> institutional memory — yes. If you're building a single-user chatbot,
> [mem0](https://github.com/mem0ai/mem0) or
> [Letta](https://github.com/letta-ai/letta) may be a better fit.

## What is this?

A context window forgets everything the moment a session ends, and even
within a session it can't tell a durable fact ("Acme is a client") from a
passing remark. Athenaeum is a memory layer that sits outside any one agent
session: agents **write** observations to an append-only intake log, a
separate compiler (**the librarian**) turns that raw stream into a
structured, deduplicated **wiki** of entities, and a **sidecar** injects the
relevant slice of that wiki back into context automatically on every turn.
The result is memory that survives across sessions, across agents, and
across a team — not just across turns of one conversation.

Full rationale, comparison with alternatives (Claude memory, Anthropic's
memory tool, RAG, Karpathy's gist, mem0/Letta/Zep/Cognee), and the lessons
from running it on our own operations live in
[**docs/why-athenaeum.md**](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/why-athenaeum.md). For the companion blog
post: [What We Learned Running Our Own Operations on Agentic
Memory](https://kromatic.com/blog/agentic-memory-in-production/).

The system's purpose and operating principles — the north star every design
decision is checked against — are stated canonically in
[**docs/north-star.md**](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/north-star.md).

## Key features and why they're built this way

### The librarian compiles raw intake into a wiki — it doesn't store verbatim

**What:** Agents can only *append* to `raw/`. A separate process, the
librarian, is the only writer to `wiki/`, and it runs the observation through
a tiered pipeline — programmatic normalization, a fast classifier that routes
the observation to a known or new entity, and a capable model that merges it
into the entity page and resolves simple contradictions.

**Why:** If every agent could edit the wiki directly, three facts about the
same person surfaced across three sessions would become three drifting
person pages, and a bad write from one agent would be indistinguishable from
a good one. Routing everything through one compiler makes safety a property
of *structure*, not of trusting every agent to be a careful writer — the
librarian snapshots the wiki to git before every run, so a bad merge is a
`git revert` away.

### Contradiction detection escalates to a human decision queue

**What:** When the pipeline can't confidently resolve a conflict between an
incoming observation and the existing wiki, it doesn't guess — it appends a
block to `wiki/_pending_questions.md` for a human to answer. The
`list_pending_decisions` MCP tool (and `athenaeum decisions` on the CLI)
surfaces this queue, unified with pending merge proposals, as one
"decisions needed" list.

**Why:** An automated memory system that silently picks a side on every
ambiguous conflict will eventually silently pick the *wrong* side, and
nobody will know to check. Some conflicts are cheap to auto-resolve; the
ones that aren't need a human in the loop, and that only works if there's a
durable, listable surface for them rather than a conflict resolved by
whichever write happened last.

### Source precedence is a 9-tier taxonomy, not last-write-wins

**What:** Every claim carries a source, and when two claims conflict, the
resolver picks a winner using a fixed 9-tier precedence order (from
`docs/why-athenaeum.md` and `resolutions.py`): direct user statement, curated
public profile, third-party authoritative API, consensus public source,
agent-observed-from-artifact, LLM-generated, pipeline script, model prior,
and unsourced last.

**Why:** "Whichever fact was written most recently wins" is how memory
systems quietly go wrong — a stale scraped fact can overwrite something the
user said directly. A precedence order means a low-authority source can
never silently clobber a high-authority one; genuine ties still fall through
to a human question rather than a coin flip.

### Recall is scoped by audience, fail-closed

**What:** `athenaeum serve --audience <role>` pins a restricted read scope
for the life of that server process. Every page-content-bearing tool —
`recall` and every list tool — applies the same predicate: a restricted
caller sees only pages it's explicitly authorized for. Untagged pages fail
**closed** for a restricted audience (the owner, with no audience pinned,
still sees everything).

**Why:** A secondary or scheduled agent (an email-drafting routine, say)
often needs *some* of the wiki but must never reach PII or client-confidential
pages. Defaulting to "visible unless labeled private" means a missed label
leaks; defaulting to "hidden unless labeled visible for this audience" means
a missed label is merely annoying, not a leak.

### Spend ceilings and a run lock bound the background compiler

**What:** `athenaeum run` enforces a per-run API-call budget
(`ATHENAEUM_MAX_API_CALLS`, default 800) and takes a shared run lock
(`RunLock`, issue athenaeum#309) so `run`, `ingest`, `reindex`, and `session-end` are
single-flight against each other on the same knowledge root.

**Why:** The librarian is an LLM-driven background process with no human
watching each call — without a hard ceiling, a bad day (a big backlog, a
runaway retry loop) turns into an unbounded bill. And because the librarian
mutates the wiki with git commits, two overlapping runs writing at once is a
correctness bug, not just a cost one — the lock guarantees a single writer.

### A provider abstraction lets the pipeline run on the API or a Claude Code subscription

**What:** All LLM calls go through one factory (`athenaeum.provider`) with two
backends: `api` (the Anthropic SDK, metered) and `claude-cli` (drives the
operator's own `claude` binary against their Code subscription, no API key).
The `api` backend wraps `anthropic.Anthropic` with parameters passed through
unchanged, so behavior is byte-for-byte identical to the pre-abstraction
code; the `claude-cli` backend mirrors the same call surface so callers need
no branching logic.

**Why:** Not every operator wants to meter a separate API key for a nightly
batch job when they already pay for a Claude Code subscription — but the
tiers, prompts, and call sites shouldn't need two implementations to support
both. One seam, two transports, identical prompts either way.

### Storage adapters are an extension point (not yet used in-tree)

**What:** `storage.py` resolves each wiki entity class to a **storage
surface** — a backing store plus a corpus policy — through a small registry
(`register_adapter` / `storage.adapters` in config). Two adapters ship
built-in: the default `wiki-markdown-embedded` surface, and an `excluded`
surface (all-false corpus policy) that PII/archival content is routed
through so it's excluded from embed/recall/merge by construction.

**Why (honestly scoped):** This is presented as an extension point, not a
finished feature: only one corpus-policy bit (`is_merge_eligible`) currently
has a real call site in `src/` (`wiki_dedupe.py`); the `is_embedded` and
`is_recallable` bits exist on `StorageAdapter` but nothing in `src/` reads
them yet, and there's no third-party adapter registered in-tree today. The
seam exists so a future storage surface (e.g. a database-backed wiki) is a
config change, not a core rewrite — but as of this writing it has one real
built-in consumer (the PII-exclusion surface), not a general policy engine.

> **Note on reasoning tiers.** `reasoning_tiers.py` implements a tiered
> reasoning pipeline that screens merge proposals before the human queue
> (`DEFAULT_TIER_CHAIN`, the pipeline's own default chain, is the empty tuple),
> but it is not part of any default run path. It has two production call
> sites in `merge.py` — `t1_screen_rejects_merge_proposal` (reject/pass-up
> only) and `t2_screen_merge_proposal` (which can auto-apply a safe-class
> merge with **no human review**) — both gated behind the same
> `resolve_reasoning_tier_auditing_enabled` flag, an explicit opt-in that
> defaults to **off**. Until an operator turns that setting on, production
> merge behavior is unaffected. Full writeup, including when it's worth
> turning on and what it costs:
> [Reasoning-tier screening (T1/T2)](docs/configuration.md#reasoning-tier-screening-t1t2--off-by-default).

## The MCP surface

Athenaeum ships an MCP server (`athenaeum serve`) exposing **11 tools** so AI
agents can write to raw intake, search the compiled wiki, and triage the
human-decision queue — 7 read-only, 4 that mutate human-decision state.

| Tool | R/W | What it does |
|---|---|---|
| `recall` | READ | Searches the compiled wiki for pages relevant to a query (keyword/FTS5/vector depending on configured backend). |
| `list_pending_questions` | READ | Lists unanswered contradiction-detector questions from `wiki/_pending_questions.md`. |
| `list_pending_merges` | READ | Lists unresolved resolver-proposed page merges from `wiki/_pending_merges.md`. |
| `list_pending_decisions` | READ | Unified queue — pending questions **and** merge proposals in one call, oldest first. |
| `list_axiom_audit` | READ | Per-slug history of `memory_class: axiom` promotions/demotions, so axiom status is auditable without a write tool. |
| `scan_retraction_cascade` | READ | Flags completed merges that relied on a since-retracted source; never auto-unmerges. |
| `calibration_summary` | READ | Per-tier sampled/reviewed/overturned counts for the tiered-reasoning calibration loop (reports "not enabled" when the opt-in above is off). |
| `remember` | WRITE | Appends a piece of knowledge to raw intake (append-only; compiled into the wiki on the next run). |
| `resolve_question` | WRITE | Flips a pending question to answered and records the answer body. |
| `resolve_merge` | WRITE | Approves or rejects a pending merge proposal; approval folds/creates the merged wiki page. |
| `review_audit_item` | WRITE | Records a human's confirm/overturn verdict on a sampled tier-audit item (calibration signal only, never re-executes a merge). |

The three decision-queue mutators (`resolve_question`, `resolve_merge`,
`review_audit_item`) plus every page-content-bearing read tool are subject to
audience scoping: a restricted (`--audience`) server process fails those
mutators closed entirely, since adjudicating the operator's decision queue is
owner-only. See [`docs/security-posture.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/security-posture.md).

```bash
pip install 'athenaeum[mcp]'
athenaeum serve --path ~/knowledge

# Smoke test the round-trip without a live session
athenaeum test-mcp
```

**Claude Code integration.** Add to your MCP config and it auto-starts with
every session:

```bash
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
```

**Custom raw/wiki locations.** The raw and wiki roots default to
`<path>/raw` and `<path>/wiki`. To point them at independent locations —
for example a read-only mounted wiki, or an existing config that predates
this command — set `KNOWLEDGE_RAW_PATH` and/or `KNOWLEDGE_WIKI_PATH`; each
overrides its root individually while `--path` remains where `athenaeum.yaml`
and extra intake roots resolve:

```bash
KNOWLEDGE_RAW_PATH=/data/knowledge/raw \
KNOWLEDGE_WIKI_PATH=/data/knowledge/wiki \
  athenaeum serve --path ~/knowledge
```

**Scoped read access (secondary agents).** By default `athenaeum serve`
exposes the **whole wiki** to `recall` — the owner sees everything. If you
wire a secondary/scheduled agent (e.g. an email-drafting routine) to this
server, pin it to a restricted audience so it can reach operational pages but
never your PII / client-confidential / financial ones:

```bash
# This server process may only recall pages an "ops" audience is allowed to read.
athenaeum serve --path ~/knowledge --audience ops
```

The audience is pinned by the operator at serve time and cannot be widened by
the caller. Pages carry `access:` (`open`/`internal`/`confidential`/`personal`)
and/or an `audience:` role list; untagged pages fail **closed** for a restricted
audience (owner still sees them). This is a single-owner read filter, not a
multi-user ACL. See
[`docs/security-posture.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/security-posture.md)
and [`docs/configuration.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/configuration.md)
for the frontmatter model and the fail-closed enforcement.

**Write-time PII screening (opt-in, off by default).** Complementing the
read-time audience filter above, an intake screener can classify each
`remember()` payload *before* it is stored and auto-label matched **medical**
content `access: personal` so a restricted `recall` never surfaces it (it
labels, never drops). It is **off by default** — a deliberate choice, not a
gap: unscreened intake already defaults to the non-world-readable
`access: internal`, so nothing is world-readable unless a page is explicitly
labeled `open`. Turn it on in `athenaeum.yaml`:

```yaml
screening:
  medical:
    action: label_restrict   # default: off
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

## Installation

Requires Python 3.13+.

```bash
pip install athenaeum
```

> Athenaeum ships an optional reasoning-tier screen (T1/T2) that can
> pre-screen merge proposals before they reach your human review queue — off
> by default, since the T2 tier can auto-apply a merge with no human review.
> Worth turning on once your merge queue outgrows manual triage; see
> [Reasoning-tier screening (T1/T2)](docs/configuration.md#reasoning-tier-screening-t1t2--off-by-default)
> for when and how.

## Quick start

```bash
# Initialize a knowledge directory
athenaeum init                  # default: ~/knowledge
athenaeum init --path ~/my-knowledge

# Run the librarian (compile raw intake → wiki entities).
# `athenaeum run` needs ANTHROPIC_API_KEY — use --dry-run to explore keyless.
# `run` and `status` operate on ~/knowledge by default (point elsewhere with
# `--path`; `run` also accepts `--knowledge-root` as the original spelling).
athenaeum run
athenaeum run --dry-run         # inspect without writing

# Check status
athenaeum status
```

Full run with custom paths and budgets (`--max-api-calls 200` here
deliberately lowers the per-run API budget below the default of 800 —
omit the flag to accept the default):

```bash
athenaeum run \
  --raw-root ~/knowledge/raw \
  --wiki-root ~/knowledge/wiki \
  --path ~/knowledge \
  --max-files 50 \
  --max-api-calls 200 \
  --verbose
```

### On-demand ingest & reindex (issue athenaeum#349)

The nightly `athenaeum run` is the batch path. When an agent (or you) needs a
just-`remember`ed fact to become recallable **now** — decoupled from the
nightly cadence — use the two on-demand commands. Both are single-flight (they
share the same run lock as `run`, issue athenaeum#309) and print a one-line JSON summary
with counts and duration; both exit non-zero on failure.

```bash
# Compile only raw intake that is new/changed since the last ingest, then
# refresh the search index — the round-trip that makes a memory recallable.
athenaeum ingest              # --incremental is the DEFAULT (fast no-op if none)
athenaeum reindex             # --incremental hash-diff delta (depends on athenaeum#348)

athenaeum ingest --full       # recompile all pending raw intake
athenaeum reindex --full      # rebuild the index from scratch
athenaeum ingest --session <id>   # scope new/changed detection to one session
```

`ingest --incremental` tracks a content-hash stamp
(`~/.cache/athenaeum/ingest-manifest.json`, mirroring the athenaeum#348 index manifest),
so it is a fast no-op when nothing has changed. `tier0_passthrough`
pre-structured intake compiles with **no LLM cost**. `reindex` is the canonical
name; `rebuild-index` remains as a back-compat alias for the exact same
command. The reusable engine lives at `athenaeum.librarian.ingest` (the
SessionEnd path, issue athenaeum#350, calls it directly).

### Cross-agent same-day recall — `session-end` (issue athenaeum#350)

`remember` writes only to `raw/`; `recall` reads only the compiled `wiki/`
index; the librarian that compiles `raw/`→`wiki/` runs **nightly**. Without
intervention a memory written by one agent at 10:00 is invisible to every other
agent until the next nightly run — a ~24h gap. `athenaeum session-end` closes it
by composing the two on-demand steps above into **one change-gated command** the
SessionEnd hook (and the nightly-after-librarian path) invokes:

```bash
athenaeum session-end                     # incremental ingest + reindex (DEFAULT)
athenaeum session-end --session <id>      # scope new/changed detection to one session
athenaeum session-end --full              # force a full recompile + full index rebuild
athenaeum session-end --dry-run           # cheap manifest-diff preview — no compile, no reindex, no model load
```

Both steps are change-gated so an idle SessionEnd is cheap:

1. **Incremental `ingest`** of this session's new/changed raw intake — a fast
   no-op (zero LLM) when nothing is new; `tier0` structured entries compile with
   no model cost.
2. **Then `reindex`**, but *only when the compile actually ran* and succeeded.
   An **idle SessionEnd (no new raw) does no LLM work and no reindex**; a failed
   compile never indexes a half-built wiki; a `--dry-run` touches nothing.

The result is a memory `remember`ed in session A becoming recallable — as a
fully-resolved wiki entry — in session B the moment A ends, no waiting for the
nightly librarian. It is single-flight (shares the `run` lock, athenaeum#309) and prints
a one-line JSON summary nesting the ingest counts plus the reindex page count.
The reusable engine is `athenaeum.librarian.session_end`; the CLI is a thin
wrapper. The hook that fires this at SessionEnd lives in your Claude Code
workspace config — this repo ships the command it calls.

## Maintenance & inspection commands

Two subcommands operate over the compiled wiki outside the nightly `run`
loop — one read-only, one with an opt-in destructive `--apply`:

```bash
# Read-only: find claims restated across distinct wiki entities.
athenaeum claims --find
athenaeum claims --find --threshold 0.9 --path ~/knowledge
```

`claims --find` is a cross-entity recurring-claim detector. It scans the
wiki, embeds each claim via the recall-index embedding provider, and prints
a YAML report grouping claims that recur across two or more distinct
entities (default cosine cutoff `0.85`, override with `--threshold`). It
**never mutates `wiki/`** — it only reports. With no embedding backend
available it degrades to an empty report rather than failing.

```bash
# Cluster compiled wiki pages against each other and propose merges.
athenaeum dedupe wiki-pages
athenaeum dedupe wiki-pages --dry-run --threshold 0.6
```

`dedupe wiki-pages` clusters already-compiled concept/reference/principle
`wiki/*.md` entity pages by topic/embedding similarity (issue athenaeum#290) —
complementing the raw-intake clustering that runs during `athenaeum run`.
True duplicates are routed through the existing `wiki/_pending_merges.md` /
`resolve_merge` approval flow (never auto-applied). Writing a proposal is
idempotent — rerunning for a source set already proposed is a no-op.
`--dry-run` previews without writing; `--threshold` overrides
`librarian.cluster_threshold` (default `0.55`, the same cutoff the raw
auto-memory cluster pass uses). `athenaeum run` also runs this pass
automatically whenever `wiki/` exists — there is currently no toggle to
opt out of the run-embedded pass; failures are logged (`wiki-page dedup
pass failed; continuing run`) and non-fatal to the run.

```bash
# Archive resolved (approved/rejected) blocks out of the live sidecar.
athenaeum ingest-merges --path ~/knowledge
```

`resolve_merge` does **not** archive on its own — like `resolve_question`,
it only flips the checkbox in place. Run `ingest-merges` (issue athenaeum#299) to
move every resolved block out of `wiki/_pending_merges.md` into
`wiki/_pending_merges_archive.md` (newest-first, append-only), keeping the
live sidecar limited to genuinely open proposals. Idempotent — this must
be scheduled (or run periodically); nothing else archives resolved merges,
which is exactly how the live file grew to 5MB/67K lines in production
before this command existed (athenaeum#299).

```bash
# Dry-run (default): print the kill-list + retained-list, change nothing.
athenaeum auto-memory prune
# Apply: git rm the kill-list in one commit and rebuild the recall index.
athenaeum auto-memory prune --apply
```

`auto-memory prune` retires operational/ephemeral `wiki/auto-*.md` pages
(throwaway scratch scopes, install-token boilerplate, and pages flagged
`ephemeral: true`) using the same classifier the intake gate applies.
**Dry-run is the default**: it prints the kill-list and retained-list with
a reason per page and exits `2` when candidates exist (a CI / sign-off
signal), `0` when there is nothing to prune. `--apply` `git rm`s the
kill-list in a single labeled commit and then rebuilds the recall index;
the commit pathspec is scoped to the kill-list, so unrelated staged work is
never swept in. Like move-then-retire, recovery is **git-only** — the
command refuses to run outside a git repo and never hard-`unlink`s, so a
pruned page is recoverable from history.

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

### Option 2 — Use the MCP tools

For containerized agents that can't touch the filesystem, `athenaeum serve`
exposes the full 11-tool surface documented above, including:

- `list_pending_questions()` returns unanswered blocks as JSON — each item
  carries a stable `id` derived from the header + question text.
- `resolve_question(id, answer)` flips the checkbox and writes the answer
  body under it. It does **not** archive on its own — archival runs on the
  next `ingest-answers` pass.
- `list_pending_decisions()` returns the **unified** queue — pending
  questions AND resolver merge proposals in one call, each tagged
  `type: "question" | "merge"` (issue athenaeum#401). Merges name their source pages
  by human title with a one-line gist so the item reads as an answerable
  question.

### One unified "decisions needed" list

Athenaeum accumulates two human-decision queues: **questions** (contradiction
detector) and **merges** (resolver merge proposals). `athenaeum decisions`
unifies both so there is one place to look:

```bash
athenaeum decisions count            # "7 decisions pending (3 questions, 4 merges; oldest 30d)"
athenaeum decisions list --json      # both queues, each tagged type, oldest first
athenaeum decisions next             # the single oldest decision
```

Each merge item is rendered as an answerable question — the source pages are
named by their frontmatter `name:` (not the uuid-slug) with a one-line gist
each — because cosine topic-similarity alone is not "should-merge" and misleads
without the pages' own words. The merges half is also available on its own via
`athenaeum merges {list,next,count,revalidate,provenance}` (the mirror of
`athenaeum questions`, plus two merge-only modes):

```bash
athenaeum merges list  [--limit N] [--json]        # all unresolved proposals
athenaeum merges next  [--json]                     # the oldest unresolved proposal
athenaeum merges count [--json]                     # "N unresolved (oldest: <iso-date>)"
athenaeum merges revalidate [--apply] [--json]      # re-check the queue against the CURRENT gate
athenaeum merges provenance [--canonical-slug S] [--merge-id ID] [--json]
```

**`revalidate` is the first move for "is the merge queue healthy?"** It
re-runs the current suppression gate (size cap + confidence floor) against
every unresolved proposal and reports each one's `n_sources` and the
suppression reason for anything that would now be rejected. Proposals queued
before the gate tightened (issue athenaeum#400/#421) don't get re-checked on their
own — this command is how you find them. It is **dry-run by default**; pass
`--apply` to archive the stale ones to `wiki/_pending_merges_archive.md`
(non-destructive — moved, never deleted). `provenance` is the read side for
merges that already executed: which source pages a completed merge relied
on, from `wiki/_merge_provenance.jsonl` (issue athenaeum#425).

**Never hand-parse `wiki/_pending_merges.md`.** It is a hand-rolled markdown
sidecar with nested code fences and multi-line fields — grep/awk against it
is fragile and has burned real investigation time. `parse_pending_merges()`
(`src/athenaeum/pending_merges.py`) is the only sanctioned reader; the
`athenaeum merges` subcommands above are the sanctioned CLI surface built on
top of it. Also note: the MCP `list_pending_decisions` / `resolve_merge`
view adds **derived fields that do not exist in the file**, most visibly
`sources_omitted` (a rendered-vs-total source count computed in
`decisions.py`) — don't assume a field you saw in that view is present in
the markdown or in `athenaeum merges` JSON output.

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

## Building your own adapter

Any external source — an API, an export file, a message feed, a scraper — can
feed Athenaeum by writing **raw-intake files** that the librarian compiles into
the wiki. That seam is a small, stable contract: a source only _appends_ a raw
file; the librarian is the only writer to the wiki.

- **The contract** — [`docs/adapter-contract.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/adapter-contract.md): file location, frontmatter shape, provenance, idempotency, and how compilation reconciles duplicates/updates.
- **Guided walkthrough** — the bundled [`adapter-authoring`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/skills/adapter-authoring/SKILL.md) skill (ships in the package under `skills/`) teaches an agent or a human how to build a custom adapter step by step.
- **Worked example** — [`examples/adapters/minimal_adapter.py`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/examples/adapters/minimal_adapter.py): a synthetic, runnable adapter you can copy as a starting point.

## Integrations

- **Claude Code auto-memory** — bridge `~/.claude/projects/<scope>/memory/` into Athenaeum's `raw/` intake so the librarian can cluster, merge, and contradiction-check Claude Code's durable memory alongside other sources. A complete worked adapter for the auto-memory intake lane. See [`docs/integrations/claude-code.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/integrations/claude-code.md).
- **Contradiction detection** — pipeline overview, cross-scope modes, source-precedence taxonomy, configuration reference, and cost model for the auto-memory contradiction path. See [`docs/contradiction-detection.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/contradiction-detection.md).

## Vector search (optional)

Athenaeum supports a vector search backend (chromadb + `all-MiniLM-L6-v2`)
for semantic recall alongside the default FTS5 keyword backend. The recall
hook runs a **hybrid FTS5 + vector merge** when vector is configured —
each backend rescues a failure class the other has (short-query proper-noun
collisions for vector; no-lexical-overlap semantic queries for FTS5).

```bash
pip install 'athenaeum[vector]'
```

Enable it in `athenaeum.yaml`:

```yaml
search_backend: vector
```

Full walkthrough and the four invariants a future simplification must not
remove: [`docs/recall-architecture.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/recall-architecture.md).

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

The table below covers the common knobs. The exhaustive list — every env var,
yaml key, and CLI flag with its code default and precedence chain — lives in
[`docs/configuration.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/configuration.md).

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes (unless `--dry-run`) | API key for Tier 2/3 LLM calls |
| `ATHENAEUM_CLASSIFY_MODEL` | No | Override Tier 2 model. Precedence: env > `models.classify` in `athenaeum.yaml` > default `claude-haiku-4-5-20251001` |
| `ATHENAEUM_WRITE_MODEL` | No | Override Tier 3 model. Precedence: env > `models.write` in `athenaeum.yaml` > default `claude-sonnet-4-6` |
| `ATHENAEUM_LLM_PROVIDER` | No | LLM backend for the compile path: `api` (default, metered Anthropic API) or `claude-cli` (run the librarian on a Claude Code **subscription** via the `claude` binary, no API key). Precedence: env > `llm.provider` in `athenaeum.yaml` > `api`. Batch mode is API-only. See [`docs/configuration.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/configuration.md) → "LLM provider selection" |
| `ATHENAEUM_CLAUDE_CLI_BIN` | No | Path or name of the `claude` binary for the `claude-cli` provider (default: `claude`, resolved on `PATH`) |
| `ATHENAEUM_CLAUDE_CLI_TIMEOUT` | No | Per-call timeout in seconds for the `claude-cli` subprocess (default: `300`) |
| `ATHENAEUM_RESOLVE_MODEL` | No | Override the contradiction-resolver model (default: `claude-opus-4-7`) |
| `ATHENAEUM_RESOLVE_MAX_PER_RUN` | No | Cap resolver calls per ingest run (default: `250`, raised from 50 in athenaeum#187) |
| `ATHENAEUM_MAX_API_CALLS` | No | Run-level API call budget for `athenaeum run`. Precedence: `--max-api-calls` CLI flag > env > `librarian.max_api_calls` in `athenaeum.yaml` > default `800`. Env `0` is valid and defers the entire intake (writes `wiki/_deferred_work.md` and logs the DEGRADED summary); the CLI flag rejects `0` |
| `ATHENAEUM_MAX_FILES` | No | Per-run intake batch size for `athenaeum run`. Precedence: `--max-files` CLI flag > env > `librarian.max_files` in `athenaeum.yaml` > default `50`. Env `0` is valid (defer-everything window); the CLI flag rejects `0` |
| `ATHENAEUM_BATCH_MODE` | No | Opt-in [Batch API](https://platform.claude.com/docs/en/build-with-claude/batch-processing) mode for `athenaeum run` (athenaeum#236): tier-2/tier-3 calls are submitted as batches at a 50% token discount. Latency-tolerant — most batches finish within an hour, 24h worst case — intended for the nightly run. Precedence: `--batch-mode` / `--no-batch-mode` CLI flags > env > `librarian.batch_mode` in `athenaeum.yaml` > default off (`--no-batch-mode` forces the synchronous path even when env/yaml turn batch mode on) |
| `ATHENAEUM_RESOLVE_AUTO_APPLY` | No | Auto-apply high-confidence resolutions (default: `true`). See [`docs/auto-resolve.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/auto-resolve.md) |
| `ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD` | No | Confidence floor for auto-apply, in `[0.0, 1.0]` (default: `0.90`) |
| `ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP` | No | Per-side body cap for the resolver's full-body context, ~4 chars/token (default: `1500`; must be positive) |
| `ATHENAEUM_CROSS_SCOPE_MODE` | No | Cross-scope contradiction detection: `off` / `ancestor` / `similarity` / `both` (default: `ancestor`). See [`docs/contradiction-detection.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/contradiction-detection.md) |
| `ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD` | No | Cosine threshold for matching new detections against the resolved-decision log (default: `0.83`) |
| `ATHENAEUM_TIER4_DEDUP` | No | Dedupe pending-question escalations by source-memory pair (default: `true`; set `false`/`0`/`no`/`off` for legacy always-append) |
| `ATHENAEUM_CACHE_DIR` | No | Cache root for the librarian's embedding/cluster pass (default: `~/.cache/athenaeum`) |
| `ATHENAEUM_TOPIC_MODEL` | No | Override query-topic model. Precedence: env > `models.topic` in `athenaeum.yaml` > default `claude-haiku-4-5-20251001` |
| `ATHENAEUM_OP_KEY_PATH` | No | 1Password path for the session-start `ANTHROPIC_API_KEY` bootstrap (default: `op://Agent Tools/Anthropic API Key/credential`) |
| `ATHENAEUM_PQ_SNOOZE_HOURS` | No | Snooze TTL in hours for pending-questions surfacing (default: `24`; consumed by the `resolve-questions` skill) |
| `ATHENAEUM_PYTHON` | No | Python interpreter used by the example hooks (default: `python3`) |
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
[`docs/recall-architecture.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/recall-architecture.md#anthropic_api_key-bootstrap-sessionstart)
for the 1Password bootstrap pattern.

## Configuration

Settings are resolved in the order **CLI flag > env var > `<knowledge_root>/athenaeum.yaml` > built-in default**, so a one-off shell export beats the yaml without requiring an edit. The canonical reference for every knob — librarian budgets, model selection, contradiction/resolver tuning, recall/search, and hook environment — is [`docs/configuration.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/configuration.md). As one example, the resolver model lives under the top-level `models:` block, and the rest of the resolver's behavior knobs live under `resolve:`:

```yaml
models:
  resolve: claude-opus-4-7        # ATHENAEUM_RESOLVE_MODEL

resolve:
  auto_apply: true                # ATHENAEUM_RESOLVE_AUTO_APPLY (default: true)
  auto_apply_threshold: 0.90      # ATHENAEUM_RESOLVE_AUTO_APPLY_THRESHOLD, [0.0, 1.0]
  full_body_token_cap: 1500       # ATHENAEUM_RESOLVE_FULL_BODY_TOKEN_CAP, per-side body cap (~4 chars/token)
```

When `auto_apply` is on and a proposal's confidence meets or exceeds `auto_apply_threshold`, the pending-question block is auto-flipped to answered with an `Auto-resolved: true` audit-trail tag. See [`docs/auto-resolve.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/auto-resolve.md) for the full lane, including how to disable, lower the threshold, or reverse an auto-resolution.

**Alternative model gateways.** All model calls go through the Anthropic SDK, which honors `ANTHROPIC_BASE_URL` — so a LiteLLM proxy or any Anthropic-compatible gateway can serve alternative models with zero code change. Only Claude models are first-party tested; see [`docs/configuration.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/configuration.md#alternative-model-gateways-anthropic_base_url) for the details and [#234](https://github.com/Kromatic-Innovation/athenaeum/issues/234) for multi-provider tracking.

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

**Degraded runs.** When a run exhausts its API call budget (see
`ATHENAEUM_MAX_API_CALLS` above), it writes a `wiki/_deferred_work.md`
manifest itemizing the raw files it did not process and ends with a
warning-level `Done (DEGRADED — budget exhausted)` summary line — the
machine-greppable signal that intake was deferred rather than completed.
The deferred files stay on disk and are picked up automatically by the
next run. The manifest is overwritten on every budget-tripped run and
cleared by the next clean run (full, merge-only, or cluster-only).
The cap is enforced at the entity-tier loop; merge-phase and re-resolve
calls count toward the budget but do not themselves stop the run, so a
merge-heavy run can overshoot the cap before enforcement kicks in.
A degraded run still exits `0` by default; pass `athenaeum run
--strict-budget` to make a budget-tripped run exit nonzero instead —
opt-in, for exit-code-based alerting.

## Data lifecycle & upgrade impact

> **Upgrade impact (0.10.0) — `athenaeum run` now MOVES and DELETES raw
> auto-memory by default.** `raw/auto-memory/` is an *expiring intake queue*,
> not a permanent store. As of 0.10.0, once the librarian has compiled a
> cluster into its canonical `wiki/auto-<topic>.md` entry and the
> contradiction detector has run clean, the move-then-retire pass (issue athenaeum#261)
> **moves** each non-contradictory raw fact into the wiki entry (as an
> origin-traced footnote) and **`git rm`s the raw file** so it no longer
> re-enters the nightly loop. This is on by default. If you upgrade and run
> without reading this, your raw auto-memory files will start disappearing
> from the working tree — recoverable, but only from git history.

**What is moved vs. held.** Only non-contradictory clusters are retired.
A cluster is **held** in the queue (never deleted) when the detector flags a
contradiction, when the detection degraded (offline / API error / unparseable
response), or when a member is referenced by an open entry in
`_pending_questions.md` / `_pending_merges.md`. When in doubt, the pass keeps
the raw.

**Recovery is git-only.** The pass refuses to run when `knowledge_root` is not
a git repo, and it never hard-`unlink`s. Each retirement lands as two commits
in your knowledge repo:

- **Commit A — provenance snapshot.** The raw intake about to be retired is
  committed first (scoped `git add` of exactly those files) so every deleted
  file is recoverable from history.
- **Commit B — move + delete together.** The wiki updates (new footnotes,
  `retired: true` marker) and the raw `git rm`s land in a single commit, so the
  fact is never simultaneously absent from both the raw file and the wiki.

To recover a retired raw file, find commit B (or A) in your knowledge repo and
`git show`/`git checkout` the path. **Warning:** because recovery depends
entirely on git history, anything that rewrites or discards that history can
lose retired raw permanently — `git gc` pruning unreachable objects, a
squash/rebase that collapses the snapshot commits, or simply never committing
(running on a dirty repo) / never pushing to a backup remote. If you rely on
retired-raw recovery, keep the knowledge repo's history intact and pushed.

**Pushing after every run (opt-in, issue athenaeum#284).** A scheduled nightly run
commits locally, but does not push by default — so origin silently drifts and
the git-only recovery story only holds on the machine that ran the librarian.
Two ways to turn on a post-run push so origin stays current:

```bash
athenaeum run --push               # one run: push after this run
```

```yaml
# athenaeum.yaml — persistent opt-in
librarian:
  push_after_run: true
  # Optional; defaults are origin + the current branch's upstream.
  # push_remote: origin
  # push_branch: develop
```

The `--push` CLI flag overrides the yaml toggle. When enabled, athenaeum
invokes `git push` (using the operator's ambient git auth — credential helper
/ SSH; no tokens or secrets are handled by athenaeum) after a successful run
that produced at least one commit. `--dry-run` never pushes; a run with no
new commits never pushes. A push failure is reported as a non-fatal warning
(distinct log line `athenaeum-push-failed:`) — commits remain local and the
next run retries (`git push` is idempotent).

**`--dry-run`** computes the exact same plan and logs a structured report
without moving, deleting, or committing anything — use it to preview what a
run would retire.

**Disabling it.** Move-then-retire stays on by default, but you can turn it
off two ways:

```bash
athenaeum run --no-retire          # one run: skip the retire pass entirely
```

```yaml
# athenaeum.yaml — persistent opt-out
librarian:
  retire: false
```

The `--no-retire` CLI flag overrides the yaml toggle. When disabled, raw
auto-memory is neither moved into the wiki nor `git rm`'d; it stays in the
intake queue and is re-examined on every run.

> **Related destructive operation.** `athenaeum auto-memory prune --apply` is a
> second opt-in command that `git rm`s pages (operational `wiki/auto-*.md`),
> with the same git-only recovery story as move-then-retire. It is dry-run by
> default — see [Maintenance & inspection commands](#maintenance--inspection-commands)
> above.

## Known limitations

Athenaeum is pre-1.0. These trade-offs are intentional for the current
release line:

- **No retrieval benchmarks yet.** The hybrid-search claim rests on concrete
  failure modes (proper-noun collision, no-overlap semantic queries) and
  production use — not a published eval against mem0 / Letta / Zep /
  Cognee. If you need benchmarked recall@k on a closed corpus, pick a tool
  that publishes numbers. If you want a knowledge base that survives your
  tool choices, this is for you. PRs adding an eval harness are very
  welcome.
- **FTS5 index rebuilds are non-atomic and unlocked.** A shell hook and the
  librarian run rebuilding simultaneously can race; the window is small and
  single-user wikis do not hit it in practice, but hardened multi-writer
  safety remains future work. The `athenaeum run` / `ingest` / `reindex`
  (`rebuild-index`) commands are single-flight against each other via the run
  lock (issue athenaeum#309); the residual race is only with the example shell hook,
  which does not take that lock.
- **The `keyword` search backend is a scan-on-query fallback.** It reads
  every wiki page on every query; fine under ~1,000 entities, painful past
  that. Use `search_backend: fts5` (default in the CLI and hooks) for any
  non-trivial wiki. The keyword backend exists as a zero-dependency baseline
  for tests and bootstrap.
- **Tier 4 (human escalation) is a file, not a workflow.** Conflicts land in
  `wiki/_pending_questions.md`; you read it and decide. No PR-opening, no
  Slack integration, no UI — on purpose, for now.
- **Reasoning tiers exist but aren't wired into any default run path.** See
  the note under "Key features" above — the tiered-reasoning pipeline is
  reachable only through one opt-in, default-off audit hook.
- **Storage adapters are an extension point with one real in-tree
  consumer.** See "Key features" above — `is_embedded`/`is_recallable` have
  no callers in `src/` yet; only `is_merge_eligible` is wired.

## Development

```bash
git clone https://github.com/Kromatic-Innovation/athenaeum.git
cd athenaeum
pip install -e ".[dev,vector]"   # matches CI; [dev] alone works if you won't touch search/clustering

pytest tests/ -v
ruff check src/ tests/
```

## Branch flow

Athenaeum follows trunk-style development, with `develop` as the active
branch and `main` as the released-revision pointer:

- **`develop`** is the active development branch and the GitHub default. All
  pull requests target `develop`.
- **`main`** carries the most recent released revision. Release tags
  (`vX.Y.Z`) live on `main` and trigger the PyPI release workflow.

Most users should install via `pip install athenaeum` (above). To work from
source against the latest released revision instead of the active branch,
clone and check out the latest tag:

```bash
git clone https://github.com/Kromatic-Innovation/athenaeum.git
cd athenaeum
git checkout "$(git describe --tags --abbrev=0)"
```

See [CONTRIBUTING.md](https://github.com/Kromatic-Innovation/athenaeum/blob/main/CONTRIBUTING.md) for the full promotion flow.

## Where to go next

This README covers the shape of the system and how to run it. For depth on a
specific piece:

- [`docs/why-athenaeum.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/why-athenaeum.md) — full design rationale, comparison with mem0/Letta/Zep/Cognee/RAG/Claude memory, and production lessons.
- [`docs/recall-architecture.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/recall-architecture.md) — the hybrid FTS5 + vector recall path and its invariants.
- [`docs/contradiction-detection.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/contradiction-detection.md) — contradiction pipeline, cross-scope modes, cost model.
- [`docs/conflict-resolution.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/conflict-resolution.md) and [`docs/auto-resolve.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/auto-resolve.md) — the resolver's action taxonomy and auto-apply lane.
- [`docs/authority-manifest.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/authority-manifest.md) and [`docs/source-handles.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/source-handles.md) — source precedence and provenance handles.
- [`docs/security-posture.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/security-posture.md) — audience scoping, fail-closed enforcement, PII screening.
- [`docs/storage-adapter-contract.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/storage-adapter-contract.md) — the storage extension point.
- [`docs/whole-store-adapter-design.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/whole-store-adapter-design.md) — design lock for generalising that extension point to the whole store: the seam inventory, the index-build latency constraints, and how git-backed recoverability becomes an adapter capability.
- [`docs/adapter-contract.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/adapter-contract.md) — writing a custom intake adapter.
- [`docs/configuration.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/configuration.md) — every env var, yaml key, and CLI flag, with defaults and precedence.
- [`docs/exit-codes.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/exit-codes.md) — `athenaeum run`'s exit-code contract (`0` / `1` / `75` graceful-partial / `124` external-kill).

## Getting help

Rolling this out on a team? Open an
[issue](https://github.com/Kromatic-Innovation/athenaeum/issues) or reach out
via [kromatic.com](https://kromatic.com/). We talk to teams working through
agent-memory rollouts often and are happy to point at whatever's useful.

## License

Apache 2.0 — see [LICENSE](https://github.com/Kromatic-Innovation/athenaeum/blob/main/LICENSE).
