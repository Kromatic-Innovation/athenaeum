# Why Athenaeum

Athenaeum is a knowledge-management pipeline for AI agents: append-only intake,
tiered compilation, and passive recall. This document explains the design
choices behind it — what problem it solves, how it compares to existing memory
tools, and why the architecture looks the way it does.

> **Note on timing.** This doc is a snapshot of the rationale at release. The
> code will continue to evolve; follow-up posts and release notes will cover
> later design changes.

> Companion blog post: [What We Learned Running Our Own Operations on Agentic
> Memory](https://kromatic.com/blog/agentic-memory-in-production/).

## The problem

We built Athenaeum because three issues broke every off-the-shelf memory
option we tried:

- **The agent doesn't know what it knows.** It has a memory but never thinks
  to search for it.
- **The agent doesn't know what to remember.** What it chooses to save isn't
  always what we'd choose.
- **Memories don't have a source of truth.** If we don't know where a fact
  came from, we don't know whether to trust it.

In short: the agent wasn't remembering everything it should, didn't remember
who told it what, and didn't always think to check its memories in the first
place.

## The four questions a production memory has to answer

For a team deploying multiple agents, memory isn't loose notes. It's
infrastructure that has to answer recurring questions where the cost of not
finding the answer is re-doing the underlying work. In agentic systems, that
cost is tokens burned and time wasted.

**_Why are we doing this?_** Strategy and goals need to be documented so an
autonomous flow (or a human who can't remember what was decided last month)
can get up to speed. Every action should link to an overall goal and
contribute to it. Actions that don't should be cut.

**_What's our relationship with this person or company?_** A CRM has data on
who we talked to, but it isn't well integrated with internal documentation,
address books, or client lists. There's usually no single source of truth on
what happened, with whom, and what the outcome was.

**_Why did we decide this?_** Agents and humans both make decisions. They
drift and contradict each other fast if there's no record of who decided what
and why. What was the decision, the retro that informed it, and the customer
interview that triggered it?

**_What did we learn last time we tried this?_** Agents (and humans) make
mistakes, often. Retrospectives need to be filed and recallable so the same
mistake doesn't repeat.

## Existing solutions

We didn't set out to build our own. We tried what was already out there, and
each option solved part of the problem without solving the whole thing.

### Claude's built-in memory

Claude Code has two ways to carry things across sessions: a `CLAUDE.md` file
you write by hand for rules you want Claude to follow, and an auto-memory
feature where Claude saves notes to itself about your preferences and
corrections ([Claude Code memory docs](https://code.claude.com/docs/en/memory)).

**Pros**

- Zero setup — just a markdown file
- Human-readable, versioned in git
- Works immediately

**Cons**

- Built for one person on one machine; nothing crosses to a teammate
- No notion of where a fact came from, so nothing to re-check when it turns out to be wrong
- No way for multiple agents to write safely to the same memory
- Claude still has to think to consult it, and sometimes doesn't

### Anthropic's memory tool

Anthropic ships a [memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
aimed at developers building their own agents. It's a set of basic file
operations an agent can call to read and write its own notes. Combined with
their context-editing feature, Anthropic reports a
[39 percent improvement](https://www.claude.com/blog/context-management) on
internal agent evaluations.

**Pros**

- A good low-level building block for people writing their own agents
- Flexible — you decide what to store and how

**Cons**

- It's a primitive, not a system. No sense of what a "page" or an "entity" is, and no trust graph
- Nothing built in for multiple agents writing to the same memory
- Still on-demand — the agent has to remember to look
- No team sharing out of the box

### Document stores and retrieval (RAG)

The most common answer to "where does our knowledge live?" is a pile of Google
Docs, Notion pages, or a shared drive, with [retrieval-augmented generation](https://blogs.nvidia.com/blog/what-is-retrieval-augmented-generation/)
bolted on so an agent can pull relevant chunks at query time.

**Pros**

- Uses documents people already write
- Agents can find relevant content across a large corpus
- Low initial setup

**Cons**

- "Where did this come from?" stops at the document, not the claim — nothing to invalidate when one source turns out to be unreliable
- Retrieval returns fragments, not a single page an agent or human can trust as the current answer
- No clean story for multiple agents updating the same underlying knowledge; documents drift out of sync
- Still on-demand — only fires when an agent or human explicitly asks

### Karpathy's wiki gist

On April 4, 2026, Andrej Karpathy published [a gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
describing "A pattern for building personal knowledge bases using LLMs." The
idea: have an LLM compile what it learns into wiki-style markdown files and
use [Obsidian](https://obsidian.md/) to browse them.

**Pros**

- Elegant and simple — the wiki is the memory, no hidden index
- Human-readable and human-editable
- Obsidian's graph view is a nice match for how the knowledge is shaped

**Cons**

- Explicitly a personal pattern — framed for individuals, not teams
- No concept of sources as separate, trustworthy objects
- No safe way for several agents to write to the same wiki at once
- The agent still has to think to look, so there's no passive recall

We took Karpathy's core idea — _the wiki is the memory_ — seriously. What we
added are the pieces a team needs that he deliberately left out.

### Agent-memory libraries (mem0, Letta, Zep, Cognee)

There's now a category of tools that package "memory for agents" as a library
or service:

- **[mem0](https://github.com/mem0ai/mem0)** — universal memory layer with a [research paper](https://arxiv.org/abs/2504.19413) behind it and strong numbers on long-conversation benchmarks.
- **[Letta](https://github.com/letta-ai/letta)** — commercial continuation of the [MemGPT paper](https://arxiv.org/abs/2310.08560), which treats agent memory like an operating system paging information in and out of context.
- **[Zep](https://www.getzep.com/)** — hosted memory service built on their open-source temporal knowledge graph, [Graphiti](https://github.com/getzep/graphiti).
- **[Cognee](https://github.com/topoteretes/cognee)** — open-source memory engine combining vector search with a graph database.

**Pros**

- Serious retrieval quality; these teams have invested in making lookup fast and accurate
- Strong fit for long-horizon conversations between one user and one agent
- Competitive on published benchmarks

**Cons**

- Designed around a single user or a single agent; multi-agent collaboration is out of scope or a later add-on
- No real concept of a source and a trust graph — a saved fact doesn't carry where it came from
- Memory is a lookup layer an agent queries, not a shared workspace humans and agents both live in
- The agent still has to decide to query; none of these fire passively before the model responds

### The architectural gap

These tools answer _"given this query, find the right chunk."_ They don't
answer _"given a stream of inputs from multiple agents and humans, keep a
single trustworthy wiki that everyone (agents and humans) can read and write
safely."_ That's a different job, and it's the one a team actually has.

## Athenaeum's design choices

To close that gap, Athenaeum makes four specific design choices. Each
corresponds to one of the failure modes above.

### 1. Sources are first-class objects (trust but verify)

> _Addresses: memories don't have a source of truth._

An LLM-written fact and a human-cited source are _not_ the same type of
thing, and a memory system that treats them the same way is unsafe to run a
business on.

A real example: while synthesizing client documentation, Claude once
hallucinated that we'd worked for the Austrian Federal Forestry Service
(Österreichische Bundesforste). This is false. We talked to them once and had
a fun debate about applying lean startup to growing trees, but it was an
exchange of information, not an engagement. If Claude had later surfaced that
claim in marketing, it would have been a problem.

Our fix: promote sources to first-class objects and require citations for any
factual claim. A person page references sources like a Google contact record.
A client engagement cites a contract or SOW. A strategy doc lists the data
that informed the decision. If a source is later marked wrong, we can find
every page that cited it and fix them.

This is a built-in trust graph. Wikipedia works this way, and it's one of the
reasons Wikipedia actually works. The footnote is the unit of trust. An
unfootnoted claim is an assertion.

This matters particularly for agentic work because agents tend to treat
prompts (including memory files) as absolute truth. In Athenaeum, a human
assertion can be cross-checked against memory files with real sources to tell
who's correct. Agents and humans can both be confidently wrong; sourced
memory is how you catch it.

### 2. The librarian — a tiered compilation pipeline

> _Addresses: multi-agent writes break the wiki if agents edit it directly._

Raw writes to memory get messy. Three facts about the same person surfaced
across three sessions shouldn't become three different person pages. An
observation that contradicts an existing page needs a decision: is the old
page wrong, or is this a new context? That decision can't be made cheaply by
the agent that saw the observation, because it doesn't have the whole wiki in
view.

Each entity carries a stable UID and a list of aliases in its frontmatter,
and search weights both well above body text. That's how "Amanda" still finds
Amanda Smith the next time someone drops her first name, and how the
librarian merges onto the existing page instead of quietly creating a
duplicate.

The librarian is a tiered compilation pipeline that runs _outside_ any agent
session. Agents can only _add_ to raw intake. They cannot overwrite or delete
what's already there, and they cannot write to the wiki itself. The librarian
is the only process that edits wiki pages, and it snapshots the wiki to git
before every run — a bad merge is a `git revert` away.

- **Tier 1 — programmatic.** Normalization, dedup, formatting.
- **Tier 2 — fast LLM.** Classification. Is this about a known entity, or a new one? Routes the observation.
- **Tier 3 — capable LLM.** The actual compilation. Merges new information with existing pages, resolves simple contradictions, writes the updated entity.
- **Tier 4 — human escalation.** Anything ambiguous lands in a `_pending_questions.md` file for a human decision later.

This is a safety property that comes from structure, not trust. We don't have
to trust our agents to be careful writers, because they _can't_ be
destructive writers. That's what makes multi-agent, team-scale memory
possible. The alternative — every agent editing the wiki directly — relies
on hope for consistency.

### 3. Passive recall — recall on every turn, automatically

> _Addresses: the agent doesn't know what it knows._

If an agent has to decide, on every turn, whether to call `recall` (and then
decide what to search for, and then read the results, and then decide whether
to use them), you don't have a memory system. You have a knowledge base the
agent _can_ query if it happens to think of it.

Athenaeum's sidecar remembers for the agent and injects the results into
context automatically. Before any user message is processed, the prompt is
classified for topics, a fast hybrid keyword-plus-vector search runs across
the wiki, and the top hits are injected into the model's context as
_knowledge context_. The agent doesn't decide to recall. Recall just happens.

This doesn't clutter context, because the agent is given only a breadcrumb
trail: page names and one-line hooks. It's the conversational equivalent of
knowing there's a reference book on the shelf that might be relevant — the
agent can pull the full page if the breadcrumb looks useful.

It's also cheap. The sidecar doesn't run a heavy model — just lightweight
topic extraction and a vector lookup.

The result: agents start a new conversation with the right context already
loaded. There's no need to spell everything out in a giant prompt.

For the deep dive on the hybrid FTS5 + vector architecture (including why
both backends are load-bearing), see
[`recall-architecture.md`](./recall-architecture.md).

### 4. The notetaker — a configurable, editable observation filter

> _Addresses: the agent doesn't know what to remember._

Remembering on recall is only half the loop. Something also has to decide
what to save in the first place.

Claude's native memory feature makes that decision behind a fixed, opaque
filter. Sometimes it saves trivia — a passing comment about a preference —
and misses the context that actually matters, like the rationale behind a
decision we just spent an hour debating. There's no way to tune it.

Athenaeum's notetaker replaces that with a configurable observation filter: a
prompt the librarian uses to decide what's worth writing to raw intake.
Because the filter is a prompt and not a black box, it's easy to inspect,
edit, and tune. When we notice memory saving things we don't care about or
missing things we do, we revise the prompt and the next pass improves.

Better still, the filter itself is a wiki page the agent can edit during a
session when we push back on a save, and every change is logged to raw intake
as an audit trail. The notetaker learns from feedback in the moment, instead
of waiting for us to remember to tune it later.

Between the notetaker and the librarian, important context lands in memory
even when the agent isn't paying attention, and it lands in a form we can
audit and improve over time.

## Key takeaways

Three things we learned running this in production:

**Writes are harder than reads.** Everyone's first instinct with agent memory
is to build better retrieval. What breaks quietly at scale is what _gets
written_ to the wiki, and under what constraints. Add-only intake plus a
tiered compiler (with the librarian as the only writer) is a solid
architectural choice.

**Provenance belongs in the model, not in a comment.** If sources aren't
first-class entities, they get added as an afterthought, and the data can't
be trusted. This is suspiciously lacking in most memory models — odd given
that hallucinations are a well-known issue.

**Recall must be automatic.** The default has to be recall-on-every-turn or
it isn't functional recall. Recall needs to be automatic and cheap.

None of these are revolutionary. Human brains work much the same way. But
they separate a production-grade agentic memory from a single-user markdown
file.

## FAQ

### What is agentic memory?

Agentic memory is the infrastructure that lets AI agents carry information
across sessions — decisions, sources, people, projects, context — in a form
multiple agents and humans can read and write safely. It's distinct from the
model's context window, from single-user note-taking, and from retrieval over
arbitrary documents. In production it has to handle multiple writers,
provenance, and passive recall — not just one user chatting with one bot.

### How is agentic memory different from RAG?

RAG retrieves relevant text chunks from a document store at query time. It
answers _"given this question, what text is most relevant?"_ Agentic memory
is the store itself, shaped as compiled entities (people, companies,
decisions) with explicit sources and an invalidation path. RAG is a read
mechanism; agentic memory is a read path, a write path, and a trust graph.
Production systems typically use both.

### Can multiple AI agents share the same memory safely?

Yes, but only if the write path is structured. If every agent edits the wiki
directly, they overwrite each other and provenance disappears. The safer
pattern — the one Athenaeum implements — is append-only intake (agents can
only add raw observations) with a separate librarian process compiling those
observations into the wiki. Multiple agents write concurrently; consistency
is enforced by structure, not by trust.

### How is this different from mem0, Letta, or Zep?

Those tools are optimised for one user talking to one agent: a lookup layer
the agent queries during a conversation. Athenaeum is a shared workspace
agents and humans both read and write, with sources as first-class objects,
multi-agent-safe writes, and passive recall by default. Different problem,
different shape. For a single-user chatbot, those tools are a good fit. For a
team deploying multiple agents that need to agree on who said what, pick
something with a trust graph.

## Getting help

If your team needs help rolling this out, please
[open an issue](https://github.com/Kromatic-Innovation/athenaeum/issues) or
get in touch via [kromatic.com](https://kromatic.com/). We talk to teams
working through agent-memory rollouts often and are happy to point at
whatever's useful.
