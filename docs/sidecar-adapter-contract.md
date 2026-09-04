# The sidecar adapter contract

`athenaeum context` (`athenaeum.context.build_context`, issue athenaeum#1358) is
the agent-neutral core of the recall sidecar: one process that takes a prompt
plus a session id and returns ranked candidates plus rendered text. This
document is what makes "agent-agnostic" a checkable claim rather than an
assertion (issue athenaeum#1359) — it pins the envelope the core returns as a
versioned schema, and states what an adapter (the host-specific layer that
turns that envelope into something a specific agent runtime consumes) may
and may not do with it.

Two separate artifacts implement this contract today, and diverged before it
was written down: the live host hook
(`$WORKSPACE_CONFIG_DIR/scripts/hooks/knowledge-recall-on-turn.sh`) and
athenaeum's own reference implementation
(`examples/claude-code/user-prompt-recall.sh`). Converging them onto the
core this document describes is issue athenaeum#1347's whole point; the
cutover itself is athenaeum#1361, and de-forking is athenaeum#1363. **This
document does not change either script** — it defines the interface the
cutover will build against.

---

## 1. The envelope

`build_context()` returns one plain dict, the **envelope**. Its schema is
versioned and pinned in code at
[`src/athenaeum/context_schema.py`](../src/athenaeum/context_schema.py)
(`SCHEMA_VERSION`, `REQUIRED_FIELDS`, `validate_envelope()`) — this section
is the human-readable mirror of that module; the module is the source of
truth a test enforces against.

### Top-level fields (v1)

| Field | Type | Contract | Notes |
|---|---|---|---|
| `v` | `int` | required | Schema version. `1` today. |
| `query` | `str` | required | **The raw prompt text.** See §1.1 below — this is a deliberate departure from this codebase's usual `query_hash`-only convention, flagged rather than silently shipped. |
| `session_id` | `str` | required | Opaque session identifier, as passed to `build_context()`. |
| `candidates` | `list[object]` | required | Ranked, deduped, budget-filtered hits. See below. Empty on a no-hit or kill-switched turn — never absent. |
| `budget` | `object` | required | `{"tokens": int, "used": int}` — the token budget applied and how much of it this turn's candidates consumed. |
| `render` | `object` | required | `{"text": str, "preamble": str}` — host-neutral rendered text. `text` is empty (not absent) when `candidates` is empty. |
| `backend` | `str` | required | `"fts5"` or `"vector"` — which backend produced this turn's candidates. |
| `elapsed_ms` | `float` | **diagnostic, not versioned** | Wall-clock cost of this call. See §1.2 below. |

### `candidates[]` fields

| Field | Type | Notes |
|---|---|---|
| `filename` | `str` | The index's filename key — an opaque id from an adapter's perspective. |
| `name` | `str` | Page display name. |
| `description` | `str` | Clamped to 200 characters, tab/newline/CR-sanitised (issue athenaeum#1344's `PM_DESC_EXPR`, carried forward by athenaeum#1358). Empty string, never absent, when the index predates the `description` column. |
| `backend` | `str` | `"fts5"` or `"vector"` — which backend surfaced THIS candidate (a merged turn can mix both). |
| `relevance` | `float \| null` | BM25 rank for an `fts5` candidate; **always `null` for a `vector` candidate** — a vector similarity score is a different scale and must never be read as comparable to a BM25 rank. |
| `memory_tier` | `str` | **Metadata only — see §2.3.** Empty string, never absent, when the index predates the `memory_tier` column. |
| `audience` | `str` | The index's delimiter-anchored audience string (`"|__access_open__|"`, `"|role|role|"`, `"|"`). See §2.4 below for what an adapter may do with it. |
| `token_cost` | `int` | This candidate's estimated token cost (`athenaeum.context.estimate_tokens`), as counted against `budget`. |

### 1.1 A named finding: `query` is not a hash

Elsewhere in this codebase, a field named `query` on a durable or
inter-process record means a **hash**, never raw text —
`athenaeum.push_metrics.PushRecord`'s own docstring: *"a `query` HASH (never
the raw query text, which can carry PII)"*; `_query_hash`'s docstring:
*"never the raw text"*. The envelope's `query` field breaks that
convention: it is the actual prompt string, because `build_context()`'s
caller (a per-turn hook) already has the prompt in hand and the envelope is
an in-process return value, not (by itself) a durable record.

**This becomes a real hazard the moment an envelope is persisted** — which a
Tier-2 adapter does by design (§3). A Tier-2 adapter that writes an envelope
verbatim to a context file puts the raw prompt on disk, in a codebase whose
telemetry layer (`push_metrics`) goes out of its way never to do that.

Per athenaeum#1359's own scope note, this document **pins the interface as it
exists, and reports the finding rather than changing the core to fix it**
(changing `build_context()`'s behaviour is out of scope here — see
athenaeum#1358). The rule for now:

> **An adapter MUST NOT persist an envelope's `query` field to durable
> storage.** A Tier-1 (push) adapter typically doesn't persist the envelope
> at all and is unaffected. A Tier-2 (periodic/static push) adapter that
> caches envelopes on disk MUST strip or hash `query` before writing, using
> the same construction as `athenaeum.push_metrics._query_hash` (SHA-256,
> truncated to 16 hex characters) if a correlation handle is needed.

Recommended follow-up (not this issue): rename `query` to `prompt` in a v2
schema bump, so the field name itself signals "raw text, handle
accordingly" rather than colliding with this codebase's hash convention.

### 1.2 `elapsed_ms` is diagnostic, not contract

`elapsed_ms` is real output — `build_context()` genuinely measures and
returns its own wall-clock cost — but it is **not part of the versioned
schema**. A wall-clock value is non-reproducible by construction, so it
cannot sit in a golden fixture's exact-value comparison the way every other
field can (see `tests/test_sidecar_envelope_schema.py`'s golden-envelope
test). An adapter may read it for local debugging/logging; **no adapter may
depend on its presence, type, or meaning staying stable across a call**, and
a schema-conformance check does not — and structurally cannot — pin it.

---

## 2. The adapter contract — may / may not

An **adapter** is the host-specific layer that turns one envelope into
whatever a specific agent runtime needs: `hookSpecificOutput` wrapping for
Claude Code, a different wrapper for a different host, a written file for a
periodic-refresh host. This is the boundary athenaeum#1358's core
deliberately stops at ("No `hookSpecificOutput` anywhere in this module" —
wrapping is the adapter's job) and this section is what an adapter is
allowed to do on its side of that boundary.

### 2.1 An adapter MAY

- **Re-render from `candidates`.** Build its own text/markup from the
  `candidates[]` array — a different bullet format, a different preamble, a
  host-native structure (e.g. Claude Code's `hookSpecificOutput` envelope).
- **Wrap the envelope** in whatever transport shape its host requires.
- **Read `elapsed_ms`, `budget`, and `backend`** for local logging/telemetry
  purposes, subject to §1.2's stability caveat on `elapsed_ms`.
- **Cache / persist the envelope**, subject to §1.1's rule on `query`
  (strip or hash it first) and to whatever access-control handling §2.4
  requires for `audience`.
- **Call `athenaeum.context.record_context_push()`** *(planned — issue
  athenaeum#1362, not yet landed as of this document)* after rendering, to
  route push telemetry through the same durable ledger the MCP `recall`
  path uses. Listed here so an adapter author designs for its existence
  rather than rediscovering the need for it independently — the exact
  signature is athenaeum#1362's to decide, not pinned by this document.

### 2.2 An adapter MAY NOT

- **Re-rank.** The core's `candidates[]` order is final — relevance-alone,
  per §2.3. An adapter that resorts, re-scores, or otherwise changes
  candidate order has silently forked the ranking logic this whole
  convergence effort exists to unfork.
- **Re-filter.** An adapter may choose not to RENDER a candidate (e.g. an
  access-control check that the core doesn't perform), but it may not query
  the index itself to find MORE or DIFFERENT candidates than what
  `build_context()` returned. **Every adapter that does its own retrieval
  is a fork in waiting** — this is, verbatim, how this epic came to exist
  (two independently-evolved recall hooks, diverged 2026-04-18).
- **Re-query.** No adapter may call `athenaeum.search`/FTS5/vector query
  functions directly to supplement what the core returned. If the core's
  result set is wrong or incomplete, that is a core defect to fix in
  `athenaeum.context`, not something an adapter routes around.
- **Emit a Claude-specific (or any other host-specific) field back into a
  shared/durable structure** that a DIFFERENT host's adapter also reads —
  e.g. writing `hookSpecificOutput` into a cache file another adapter
  parses. Host-specific shaping happens at the OUTERMOST layer only.

### 2.3 `memory_tier` is metadata, never a filter or a ranking input

This is issue athenaeum#1345's invariant, and it binds adapters too: an
adapter may DISPLAY, LOG, or otherwise use `memory_tier` as metadata (e.g.
"this candidate is tier X" in a debug view), but **may not filter candidates
by tier, and may not use tier to re-order candidates**. `build_context()`
already selects and orders by relevance alone; an adapter that adds a tier
gate on top has reintroduced the exact `AND memory_tier = 'hot'` behaviour
the converged core deliberately removed (see athenaeum#1358's own scope
note and the `test_memory_tier_swap_does_not_change_selection_or_order`
test).

#### 2.3.1 Rollback path for this invariant

If a future change reintroduces a tier predicate on the push path (the
`AND memory_tier = 'hot'` shape §2.3 forbids, on either the FTS5 or the
vector surface — see `tests/test_context_core.py::test_no_tier_predicate_in_source`),
two independent stops apply:

- **No-deploy stop-gap: the athenaeum#379 kill switch.** `athenaeum disable`
  (or `ATHENAEUM_DISABLED=all` in the environment, or a hand-written
  `{cache_dir}/disabled` state file — see `athenaeum.killswitch` and
  `athenaeum.context._recall_disabled`, which reimplements the same check
  inline for this module's own import-weight reasons) short-circuits
  `build_context()` to the empty envelope on every call, immediately, with
  no code deploy. This stops a regressed gate from reaching any live
  session while the code fix lands.
- **Revert-is-sufficient.** Reverting whatever commit reintroduced the
  predicate is sufficient on its own — **no migration and no reindex**.
  Every criterion above leaves `memory_tier` untouched: it stays populated
  in the FTS5 index (`_probe_schema`/`_query_fts5`/`_query_vector` only ever
  SELECT it, never write it) and in page frontmatter (`athenaeum.context`
  contains no write path at all — confirmed by grep: no `write_text`,
  `open(..., "w")`, or `atomic_write` call anywhere in the module). A
  revert therefore restores relevance-alone ranking with the index already
  in the correct shape; there is no drifted or half-migrated state to
  reconcile.

### 2.4 `audience` is an access-control token, not a display field

`audience` carries the index's raw, delimiter-anchored access-control string
(`athenaeum.models.audience_index_string`'s output shape). It is included in
the envelope for exactly one reason: so a Tier-2 adapter that persists
context to disk (§3) can make an authorization decision about who is allowed
to read that cached file, the same way `athenaeum.push_metrics.build_push_record`
derives a push record's `scope` field from it.

- An adapter MAY use `audience` to decide whether to render/persist a
  candidate for a given caller.
- An adapter MAY NOT render `audience`'s raw delimited form directly into
  user-facing text — it is an internal representation
  (`"|__access_open__|"`, `"|ops|ops-admin|"`), not a display string.
- **No inverse-parsing helper for `audience`'s delimited-string form exists
  in `athenaeum.models` today** (confirmed while drafting this contract —
  only the forward direction, `audience_index_string(meta) -> str`, is
  public). An adapter or downstream consumer that needs the parsed role
  list back out should add that inverse function to `athenaeum.models`
  rather than hand-rolling a second parser — this is exactly the drift
  §2.2's "no adapter does its own retrieval/parsing" rule exists to
  prevent, applied to a smaller surface. Issue athenaeum#1362 (push
  telemetry) needs this and should add it there rather than duplicating the
  pre-convergence shell hook's own ad hoc `_pm_scope_from_audience`.

---

## 3. The three-tier host taxonomy

This taxonomy is the part of the contract that carries the generalization
weight — it is what makes "not locked into Claude" a checkable design
property rather than a promise (see the epic's operator decision, quoted in
athenaeum#1359: *"We don't need to implement codex right now... But I want it
working on claude."*).

| Tier | Injection mechanism | Staleness accepted | Example |
|---|---|---|---|
| **1 — Per-turn push** | The adapter runs once per user turn and pushes fresh context into that turn. | None — every turn sees a fresh `build_context()` call against the current prompt. | Claude Code's `UserPromptSubmit` hook. |
| **2 — Periodic / static push** | The adapter runs on a schedule (or once, at session/process start) and refreshes a **context file** the host reads from — not a per-turn hook. | Bounded by the refresh interval — a caller reading the file between refreshes sees context that is up to one interval stale. | A host with no per-turn injection point (see the worked design below). |
| **3 — Pull only** | The agent explicitly CHOOSES to call a retrieval surface. **Not the sidecar — named here to be excluded, not designed for.** | N/A — pull is always as fresh as the moment it's called, by definition; staleness isn't the axis that matters for a pull surface. | The MCP `recall` tool. |

**Why Tier 3 has to be named out of scope, not just omitted.** MCP is a pull
surface: the agent decides to call it, on its own initiative. A sidecar
pushes context the agent did NOT ask for — that's the entire feature. If a
future "simplification" replaces the sidecar with "just expose retrieval
over MCP," that swap has quietly deleted the unprompted-push feature while
LOOKING like it generalized the sidecar (a Tier-1/2 adapter becomes
unnecessary once there's nothing left for it to push). Writing this down is
what keeps that swap from being mistaken, later, for having converged the
epic.

### 3.1 Worked Tier-2 adapter design (paper design, not code)

A host with no per-turn injection point — for example, a runtime that only
reads a fixed context file at session start, or on some coarser cadence, and
has no equivalent of `UserPromptSubmit`. Design, per athenaeum#1359's
acceptance criterion, expressed **without adding any field or flag to the
core** (a design that needs one is proof the contract is Claude-shaped —
this design deliberately doesn't).

**Which envelope fields it reads.** All of `render.text` (what it writes
into the context file) and `budget`/`backend` (for its own refresh-cycle
logging). It does NOT read `query` at all — see below — and treats
`candidates[]` as read-only input to `render.text`, never re-derived.

**How it refreshes.** A separate, out-of-band process (a cron job, a
launchd/systemd timer, or a loop the adapter itself owns) calls
`build_context()` on some fixed cadence (e.g. every 5 minutes, or once per
session at process start) with:

- `prompt`: the adapter's own choice of "what's the current context worth
  refreshing for" — could be a static string (the last N minutes of
  activity, a project name, whatever the host's session-scoping concept
  is), NOT necessarily a live user prompt, since by definition this tier has
  no per-turn prompt to hand `build_context()`.
- `session_id`: the host's own session/process identifier — for
  IDENTIFICATION purposes only. **Unlike the Tier-1 CLI wiring, a Tier-2
  adapter must NOT accumulate a seen-file / pass a non-empty `exclude` set
  across refreshes.** `_cmd_context.py`'s session-dedup bookkeeping
  (issue athenaeum#1358) is designed for an append-only PER-TURN stream,
  where excluding what was already pushed earlier in the same session is
  the correct behaviour. A Tier-2 refresh instead REPLACES the file
  wholesale each cycle (see below) — if it also excluded everything the
  previous refresh pushed, the snapshot would progressively lose its
  best-ranked content and converge to an empty file after a couple of
  cycles. So each refresh calls `build_context()` with `exclude=frozenset()`
  (the default) and keeps no seen-file of its own. This is a genuine,
  load-bearing difference between how Tier-1 and Tier-2 adapters use the
  same `exclude` seam, not an oversight.

The adapter then writes `render.text` (never the raw envelope — see below)
to the context file the host reads, replacing the previous refresh's
content wholesale (not appending — a Tier-2 file is a snapshot, not a log).

**What staleness it accepts.** Bounded by the refresh interval. A caller
reading the context file between two refreshes sees whatever the last
refresh wrote — up to one interval old. This is a real, accepted trade for
a host with no per-turn hook: freshness is capped at the refresh cadence,
not at zero, and the design should log its own refresh timestamp alongside
the written text so a reader can see how stale the file currently is (this
is state the ADAPTER owns and writes — not a core envelope field, per the
"no field added to the core" constraint above).

**On `query`/`audience`/persistence.** Because this adapter WRITES a file to
disk, §1.1's rule applies directly: it must never write the envelope's raw
`query` field to that file. In practice this design doesn't need to —
`render.text` is already just the rendered bullets, with no raw prompt text
embedded in it, so the natural implementation (write `render.text`, not
`json.dumps(envelope)`) satisfies the rule by construction rather than
requiring an explicit strip step. If a future version of this design DOES
need to persist the full envelope (e.g. for its own audit trail), it must
strip or hash `query` first, per §1.1.

---

## 4. Out of scope

- **Writing any adapter.** This document specifies the contract; building a
  Claude Code adapter and a non-Claude proof adapter are separate issues
  (athenaeum#1361, athenaeum#1364 respectively — the operator decision
  recorded in athenaeum#1359 sets the second at `moscow:could`, deferred).
- **Changing the core's behaviour.** This document pins and documents an
  interface. Where writing it down revealed the interface has a rough edge
  (§1.1's `query`-naming finding), that is reported here as a finding, not
  fixed here as a change — see athenaeum#1358 and athenaeum#1359's own
  "Out of scope" sections.
- **Deleting either forked hook.** Both
  `examples/claude-code/user-prompt-recall.sh` and
  `$WORKSPACE_CONFIG_DIR/scripts/hooks/knowledge-recall-on-turn.sh` stay in
  place until the athenaeum#1361 cutover proves out.
