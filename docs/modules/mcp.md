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

- **A restricted audience fails closed on reads.** `athenaeum serve --audience ops` pins a
  read scope for the life of the process. Every page-content-bearing tool — `recall` and
  every list tool — applies the same predicate. Pages carry `access:`
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
