# Recall and enumeration

**Reference page.** The full design record is
[recall architecture](../design/recall-architecture.md); this page is the operational
summary.

## What it does

`recall` is a **ranked** search over the compiled wiki: it takes a query string, tokenizes
it, and returns pages ordered by relevance. Three interchangeable backends implement the
same `SearchBackend` protocol:

- **FTS5** (the recommended default) — SQLite full-text search with BM25 ranking and porter
  stemming. Tokens under three characters or in the stopword list are dropped before the
  query runs.
- **Vector** — chromadb plus a local embedding model (`pip install 'athenaeum[vector]'`).
- **Keyword** — a zero-dependency scan-on-query fallback that rereads every page on every
  call. Documented as a fallback for small wikis and tests, not a production default.

`recall` is not the only read path. `enumerate_entities` takes **no query text at all** — it
is the unranked counterpart for "every entity of type X matching these criteria," built for
questions a ranked search answers only partially. `read_entity` is a one-call read of a
single entity by uid, for any declared class. `entity_schema` reports which entity classes
this deployment declares and observes, and which fields each carries — call it before
narrowing `recall` or `enumerate_entities` by `type`, since the tool schemas exposed at
server start advertise only the cheap declared set (a full corpus scan at server-start time
measured 28 seconds on a 23,500-page corpus and blew every client's MCP connect budget).

Example: `recall("Jordan Reyes")` ranks Jordan Reyes's wiki page against every other match by
relevance. `enumerate_entities(entity_type="person", predicates=[{"fields": "company",
"kind": "eq", "value": "Acme Corp"}])` instead lists every person page at Acme Corp,
unranked, complete, with pagination — the question `recall` cannot reliably answer because a
ranked top-k can omit a true match that just scores lower.

A bare email address, or a query phrased as a lookup question, bypasses the ranked pipeline
entirely and resolves as an exact reverse lookup against the identity registry instead of a
BM25/vector search.

## What it reads

- The compiled wiki (`wiki/*.md`) and its search index — FTS5's SQLite database or the
  vector store, selected by `search_backend` (yaml) / `--backend` (CLI), default `fts5`.
- `recall.extra_intake_roots` — additional roots folded into the search surface beyond the
  compiled wiki (default `["raw/auto-memory"]`; `[]` restricts recall to the wiki alone).
- The audience pinned at process start, applied as a predicate *inside* the query's `WHERE`
  clause — never as a post-filter — so a forbidden or wrong-typed page can never occupy a
  top-k slot and starve a page the caller is actually permitted to see.
- `wiki/_schema/types.md` (or the built-in known-types fallback) for `entity_schema`'s
  declared-class list.
- A page's frontmatter *keys* (never values) for `entity_schema`'s field union.

## What it writes

- Nothing. Every tool in this module is read-only. Indexing is the one exception: the FTS5
  and vector indexes are built or incrementally updated as a side effect of a query, keyed
  to a schema version that forces a full rebuild on mismatch, and to each file's
  `(mtime, size)` to skip unchanged content between runs. A periodic full re-hash (default
  every 7 days) catches an edit that happened to preserve both.
- `athenaeum reindex` (optionally `--full --backend vector`) rebuilds the index explicitly
  rather than relying on the incidental per-query update.

## What it refuses

- **An empty or all-stopword query returns no results, not an error.** FTS5 returns `[]`
  immediately when no query terms survive tokenization, and again if the index database
  doesn't exist yet or SQLite raises an operational error mid-query.
- **The keyword backend refuses a full-corpus scan a caller didn't explicitly ask for.** On a
  storage surface that doesn't support a cheap local scan, it raises rather than silently
  paying an unbounded round-trip — an explicit "refuse and name what's missing" rule instead
  of a slow, quiet fallback.
- **`enumerate_entities` refuses to touch a `with_pii`-gated field without the flag.**
  Referencing a contact-identifier-prefixed field as a predicate or requested output field
  without `with_pii=True` raises `ValueError` naming exactly which fields require it — a
  caller who forgot the flag gets a loud error, never a silently incomplete row.
- **An unrecognized `entity_type` is not an error** — `enumerate_entities` returns an empty
  hit set plus the sorted list of classes this deployment actually has, so a caller can
  self-correct instead of parsing an exception.
- **A pagination cursor is bound to its exact query shape.** Resuming with a different
  `(entity_type, sort_key, descending)` triple raises `ValueError`; a malformed cursor raises
  `ValueError` naming the bad value. A cursor that has gone stale because the corpus changed
  falls back to resuming from the start rather than raising.
- **`entity_schema` never reports an excluded-surface field name**, even as a bare field
  name with no value attached — so the schema tool itself can never become an oracle for
  "which PII fields exist" by naming what it will not show.
- **`entity_schema`'s page counts are audience-filtered**, and the memoization cache key
  includes the caller's audience — a restricted caller's class list can never leak the
  existence of a page it isn't allowed to read, even through a cached response meant for a
  different caller.
- **`recall(with_pii=True)`'s excluded-record join runs strictly after page authorization**,
  never before or inside the index. Moving it earlier would turn the flag into a timing
  side-channel for probing whether an excluded record exists behind a page the caller cannot
  read at all.
- **A degraded vector index raises rather than returning a plausible-looking but meaningless
  ranking.** A non-ranked or degenerate result set from the vector backend is a hard error,
  not a silent pass-through.
- **A recall kill switch (`ATHENAEUM_DISABLED`) returns an empty envelope of the same shape**
  rather than an error or a partial result, so a caller relying on the shape of the response
  doesn't need a separate code path for "recall is off."
- **Query-topic extraction never raises and never blocks a turn.** A missing key, missing
  SDK, network failure, timeout, or unparseable response all collapse to an empty topic
  list, which the caller treats as "fall back to the built-in regex/stopword extractor" —
  never as a reason to fail the surrounding recall call.

## See also

- Guides — [Claude Code integration](../guides/claude-code.md) · [Vector search](../guides/vector-search.md) · [Daily operation](../guides/daily-operation.md)
- Modules — [mcp](mcp.md) · [sensitivity](sensitivity.md)
- Design — [recall architecture](../design/recall-architecture.md)
- Extending — [authorized reader contract](../extending/authorized-reader-contract.md)
- Reference — [configuration](../reference/configuration.md)
