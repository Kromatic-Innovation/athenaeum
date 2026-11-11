# The MCP surface

**Reference page.** For the task-shaped version, see
[Passive recall via hooks](../guides/sidecar.md) and
[Claude Code integration](../guides/claude-code.md).

## What it does

`athenaeum serve` runs an MCP server exposing **15 tools** so agents can write to raw
intake, search the compiled wiki, and triage the human-decision queue.
10 read-only, 5 that mutate human-decision state.
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
| `scan_retraction_cascade` | READ | Flags completed merges that relied on a since-retracted source. Never auto-unmerges. |
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

  **Three read tools are not audience-scoped**: `list_axiom_audit`,
  `scan_retraction_cascade` and `calibration_summary` take no audience and apply no filter.
  They return slugs, paths and counts rather than page bodies, so a restricted caller learns
  no page *content* through them — but it can learn that a page **exists**. If that
  distinction matters to your deployment, do not expose those three to a restricted
  audience. Pages carry `access:`
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
