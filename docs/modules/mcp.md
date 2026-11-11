# The MCP surface

**Reference page.** For the task-shaped version, see
[Passive recall via hooks](../guides/sidecar.md) and
[Claude Code integration](../guides/claude-code.md).

## What it does

`athenaeum serve` runs an MCP server exposing **15 tools** so agents can write to raw
intake, search the compiled wiki, and triage the human-decision queue.
9 read-only, 6 that mutate human-decision state.
No tool writes a wiki page directly — the librarian is the only writer.

```bash
pip install 'athenaeum[mcp]'
athenaeum serve --path ~/knowledge
athenaeum test-mcp          # smoke-test the round-trip without a live session
```

| Tool | R/W | What it does |
|---|---|---|
| `recall` | READ | Searches the compiled wiki for pages relevant to a query (keyword/FTS5/vector, depending on the configured backend). |
| `entity_schema` | READ | Reports the entity classes this deployment declares and observes, the fields each carries, and which fields `recall`/`enumerate_entities` can filter on. Call this before narrowing by `type`. |
| `enumerate_entities` | READ | Unranked, criteria-based listing of every entity of a declared type — the counterpart to `recall` for "every entity matching X", where a ranked search returns an incomplete answer. |
| `read_entity` | READ | One-call entity read by uid, for any entity class. One of the two sanctioned paths to a page's excluded fields. |
| `list_pending_questions` | READ | Unanswered contradiction-detector questions. |
| `list_pending_merges` | READ | Unresolved resolver-proposed page merges. |
| `list_pending_decisions` | READ | Unified queue — questions and merge proposals in one call, oldest first. |
| `list_axiom_audit` | READ | Per-slug history of axiom promotions and demotions, so axiom status is auditable without a write tool. |
| `scan_retraction_cascade` | WRITE | Flags completed merges that relied on a since-retracted source by appending a review item to the decision queue. Never auto-unmerges. |
| `calibration_summary` | READ | Per-tier sampled/reviewed/overturned counts for the tiered-reasoning calibration loop. Reports "not enabled" when the opt-in is off. |
| `remember` | WRITE | Appends a piece of knowledge to raw intake. Append-only; compiled into the wiki on the next run. |
| `raise_decision` | WRITE | Files a new agent-raised question or confirmation into the pending-decisions queue, so a mid-session flag has somewhere durable to live. |
| `resolve_question` | WRITE | Flips a pending question to answered and records the answer body. |
| `resolve_merge` | WRITE | Approves or rejects a pending merge proposal. Approval folds or creates the merged wiki page. |
| `review_audit_item` | WRITE | Records a human's confirm/overturn verdict on a sampled tier-audit item. Calibration signal only; never re-executes a merge. |

## Transport

By default `athenaeum serve` speaks MCP over **stdio**: the client launches the process and
talks to it over that process's own stdin and stdout. Nothing is bound and nothing is
reachable from a network. This is the shipped behaviour and it is unchanged.

`--transport http` opts into FastMCP's **streamable HTTP** transport instead, for hosts that
cannot launch a local process:

```bash
athenaeum serve --path ~/knowledge --transport http --port 8000
# [transport] streamable HTTP on http://127.0.0.1:8000/mcp
```

| Flag | Default | Applies to | What it sets |
|---|---|---|---|
| `--transport` | `stdio` | both | `stdio` or `http`. |
| `--host` | `127.0.0.1` | http | The bind address. Loopback only — see below. |
| `--port` | `8000` | http | The TCP port. |
| `--path-prefix` | `/mcp` | http | The URL path the endpoint is mounted at. |

`--transport`, `--host` and `--port` each resolve **CLI flag > environment variable >
`athenaeum.yaml`**, the same order every other knob uses: `ATHENAEUM_SERVE_TRANSPORT`,
`ATHENAEUM_SERVE_HOST` and `ATHENAEUM_SERVE_PORT`, then `serve.transport`, `serve.host` and
`serve.port`. `--path-prefix` is CLI-only. Under `stdio` the other three are inert, because
nothing binds. See [configuration](../reference/configuration.md) and
[the CLI reference](../reference/cli.md).

**TLS is not athenaeum's concern.** Terminate HTTPS at a reverse proxy in front of the
process; the server itself speaks plain HTTP to that proxy over loopback.

## Tool annotations

Every tool carries an MCP `ToolAnnotations` hint so a remote host can decide
whether to call it unattended and when to ask the user for confirmation
(`docs/design/host-chatgpt.md` §3.2). Annotations are hints only — they
change no server-side behavior, which still enforces its own authorization
regardless of what a client infers from them. `openWorldHint` is `false` for
every tool: this deployment only ever reads/writes its own local knowledge
store, never an external service. A blank cell means the hint is unset — per
the MCP spec, `destructiveHint`/`idempotentHint` are meaningful only when
`readOnlyHint` is `false`, so the nine read-only tools never set either.

Tool name cells below are plain text, not code-spans — deliberately, so this
table is never mistaken by tooling for the R/W table above (whose rows are
matched by name/value pairs elsewhere in this repo).

| Tool name | readOnlyHint | destructiveHint | idempotentHint |
|---|---|---|---|
| recall | true | | |
| entity_schema | true | | |
| enumerate_entities | true | | |
| read_entity | true | | |
| list_pending_questions | true | | |
| list_pending_merges | true | | |
| list_pending_decisions | true | | |
| list_axiom_audit | true | | |
| calibration_summary | true | | |
| remember | false | false | |
| raise_decision | false | false | |
| scan_retraction_cascade | false | false | true |
| resolve_question | false | true | true |
| resolve_merge | false | true | true |
| review_audit_item | false | true | true |

Two side effects were found while auditing the nine read-only tools for
hidden state changes, and judged NOT disqualifying:

- `recall` appends a best-effort push-metrics telemetry record on every
  call. It is wrapped so a failure there can never surface as a `recall`
  failure, and it never touches wiki or decision-queue content — a local
  instrumentation side-channel, not a change to what `recall` or any other
  tool reads back.
- `enumerate_entities` and `read_entity` (when given a `cache_dir`) refresh a
  persisted search-index / entity-index cache file under that directory —
  the same derived, rebuildable plumbing `recall`/`rebuild-index` already
  maintain, never user-authored content.

One tool's side effect WAS disqualifying: `scan_retraction_cascade` durably
appends newly-flagged `(merge, retracted source)` pairs to
`wiki/_pending_retractions.jsonl`, which `list_pending_decisions` then
surfaces as `type: "retraction"` items — a real, user-visible change to the
decision queue, not a cache or telemetry side-channel. It is annotated as a
write (`readOnlyHint: false`), grouped with `remember`/`raise_decision` as
append-only and non-destructive (it never edits/removes an existing record
and never touches a completed merge), but — unlike those two — it is also
`idempotentHint: true`: a re-scan of unchanged state emits nothing new (see
the tool's own docstring).

## What it reads

- The compiled wiki at `<path>/wiki` (or `KNOWLEDGE_WIKI_PATH`) and its search index.
- The decision queues, `wiki/_pending_questions.md` and `wiki/_pending_merges.md`.
- The audience pinned at process start by `--audience`.

The raw and wiki roots default to `<path>/raw` and `<path>/wiki`. `KNOWLEDGE_RAW_PATH` and
`KNOWLEDGE_WIKI_PATH` override each root individually, while `--path` remains where
`athenaeum.yaml` and extra intake roots resolve:

```bash
KNOWLEDGE_RAW_PATH=/data/knowledge/raw \
KNOWLEDGE_WIKI_PATH=/data/knowledge/wiki \
  athenaeum serve --path ~/knowledge
```

## What it writes

- `remember` appends one file under `raw/<source>/`. Nothing else touches `raw/`.
- The three decision-queue mutators edit the queue files and, for an approved merge, the
  merged wiki page.
- `scan_retraction_cascade` appends newly-flagged review items to
  `wiki/_pending_retractions.jsonl`, surfaced by `list_pending_decisions`. It never edits
  an existing record and never touches a completed merge.
- **No MCP tool writes an arbitrary wiki page.** An agent that wants a fact in the wiki
  calls `remember` and waits for the librarian.

## What it refuses

- **An HTTP server refuses every non-loopback bind until an auth issuer is configured.**
  This is a security property rather than a cautious default. The HTTP transport carries no
  authentication of its own, so binding a routable address would publish the entire store to
  anyone who can reach the port. The check is a POSITIVE allowlist of loopback address
  **literals** — any `127.0.0.0/8` address, the IPv6 loopback (bracketed or bare), and
  IPv4-mapped IPv6 loopback forms. Every other value exits non-zero **before any socket is
  bound**, and the message names the missing `serve.auth.issuer` setting rather than only
  reporting that the address is disallowed. Host *names* are never resolved, `localhost`
  included: resolving one would move the loopback decision into `/etc/hosts`, DNS and search
  domains, so a name that is loopback today could be routable tomorrow with nothing in
  athenaeum's own configuration having changed.

- **A restricted audience fails closed on reads.** `athenaeum serve --audience ops` pins a
  read scope for the life of the process. `recall`, `read_entity`, `enumerate_entities` and
  the three pending-decision list tools all apply the same predicate.

  **Three tools are not audience-scoped**: `list_axiom_audit`,
  `scan_retraction_cascade` and `calibration_summary` take no audience and apply no filter.
  They return slugs, paths and counts rather than page bodies, so a restricted caller learns
  no page *content* through them — but it can learn that a page **exists**. If that
  distinction matters to your deployment, do not expose those three to a restricted
  audience. Note that `scan_retraction_cascade` is not a read tool at all: it appends
  review items to the owner's decision queue (which is why it is annotated
  `readOnlyHint: false`, above), and unlike the three decision-queue mutators it is
  **not** refused for a pinned audience. Its annotation records that behaviour; it does
  not change it. Pages carry `access:`
  (`open`/`internal`/`confidential`/`personal`) and/or an `audience:` role list; **untagged
  pages are invisible** to a restricted caller. The owner, with no audience pinned, sees
  everything.
- **The audience cannot be widened by the caller.** It is an operator decision made at
  serve time.
- **A restricted process refuses the decision-queue mutators entirely.**
  `resolve_question`, `resolve_merge` and `review_audit_item` fail closed for any pinned
  audience — adjudicating the operator's decision queue is owner-only.
- **`calibration_summary` reports "not enabled"** rather than fabricating counts when
  reasoning-tier screening is off.
- **`scan_retraction_cascade` never unmerges.** It reports; a human decides.

This is a single-owner read filter, not a multi-user ACL.

## See also

- Guides — [Claude Code integration](../guides/claude-code.md) · [Sidecar](../guides/sidecar.md) · [Answering decisions](../guides/decisions.md)
- Modules — [recall](recall.md) · [intake](intake.md) · [sensitivity](sensitivity.md)
- Design — [security posture](../design/security-posture.md) · [authorized reader contract](../extending/authorized-reader-contract.md)
- Reference — [configuration](../reference/configuration.md) · [exit codes](../reference/exit-codes.md)
