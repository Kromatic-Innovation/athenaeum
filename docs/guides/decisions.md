**Reference:** [conflicts](../modules/conflicts.md) · [corrections](../modules/corrections.md)

# Answering pending decisions

When the pipeline can't confidently resolve a conflict between an incoming
observation and the existing wiki, it doesn't guess — it escalates to a
human decision queue. This guide is about clearing that queue.

## I want to see what's waiting for a decision

```bash
athenaeum decisions count            # "7 decisions pending (3 questions, 4 merges; oldest 30d)"
athenaeum decisions list --json      # both queues, each tagged type, oldest first
athenaeum decisions next             # the single oldest decision
```

`athenaeum decisions` unifies the two human-decision queues Athenaeum
accumulates — **questions** (contradiction detector) and **merges**
(resolver merge proposals) — so there's one place to look instead of two.
Each merge item is rendered as an answerable question: the source pages
are named by their frontmatter `name:` (not the uuid-slug) with a
one-line gist each, because cosine topic-similarity alone doesn't tell you
whether two pages *should* merge.

## I want to answer a contradiction question

When the resolver can't settle an ambiguity or a principled contradiction,
it escalates to `wiki/_pending_questions.md`. Each escalation lands as a
block like:

```markdown
## [2026-04-20] Entity: "Acme Corp" (from sessions/20240406T120000Z-aabb0011.md)
- [ ] Is Acme still Series A after the 2026 recapitalisation?
**Conflict type**: principled
**Description**: Prior wiki says Series A; the 2026-04 raw file implies Series B.
```

Resolve it one of two ways.

**Edit the file directly.** Flip `[ ]` to `[x]` and type your answer below
the checkbox (above or below the conflict-type / description lines —
either works; the parser strips those metadata lines when extracting the
answer):

```markdown
## [2026-04-20] Entity: "Acme Corp" (from sessions/20240406T120000Z-aabb0011.md)
- [x] Is Acme still Series A after the 2026 recapitalisation?

They closed Series B on 2026-03-12, led by Acme Growth Partners.
The 2026-04 raw file is correct; the prior wiki entry is stale.

**Conflict type**: principled
**Description**: Prior wiki says Series A; the 2026-04 raw file implies Series B.
```

**Or use the MCP tools**, for a containerized agent that can't touch the
filesystem:

- `list_pending_questions()` returns unanswered blocks as JSON — each item
  carries a stable `id` derived from the header and question text.
- `resolve_question(id, answer)` flips the checkbox and writes the answer
  body under it. It does not archive on its own.
- `list_pending_decisions()` returns the unified queue described above.

## I want to work through the merge-proposal queue

`athenaeum merges` mirrors `athenaeum questions`, plus four merge-only
modes:

```bash
athenaeum merges list  [--limit N] [--json]        # all unresolved proposals
athenaeum merges next  [--json]                     # the oldest unresolved proposal
athenaeum merges count [--json]                     # "N unresolved (oldest: <iso-date>)"
athenaeum merges revalidate [--apply] [--json]      # re-check the queue against the CURRENT gate
athenaeum merges recompare [--apply] [--limit N] [--json]
                                                    # re-run the five-verdict comparator over every
                                                    # unresolved proposal and ledger a verdict per
                                                    # source pair. Dry-run by default; --apply writes
                                                    # to the ledger only — it never approves, rejects,
                                                    # or archives a proposal, and PII-hazard proposals
                                                    # always route to a human.
athenaeum merges scrub-pii [--apply] [--allowlist F] [--json]
                                                    # redact contact data out of proposal BODIES in
                                                    # place. Zero LLM cost, and it does NOT force the
                                                    # merge decision — the proposal stays unresolved.
                                                    # Dry-run by default.
athenaeum merges provenance [--canonical-slug S] [--merge-id ID] [--json]
```

**`revalidate` is the first move for "is the merge queue healthy?"** It
re-runs the current suppression gate (size cap + confidence floor) against
every unresolved proposal and reports each one's `n_sources` and the
suppression reason for anything that would now be rejected. Proposals
queued before the gate tightened don't get re-checked on their own — this
command is how you find them. It's dry-run by default; pass `--apply` to
archive the stale ones to `wiki/_pending_merges_archive.md`
(non-destructive — moved, never deleted). `provenance` is the read side
for merges that already executed: which source pages a completed merge
relied on.

**Never hand-parse `wiki/_pending_merges.md`.** It's a hand-rolled
markdown sidecar with nested code fences and multi-line fields —
grep/awk against it is fragile. `parse_pending_merges()` is the only
sanctioned reader; the `athenaeum merges` subcommands above are the
sanctioned CLI surface built on top of it. The MCP `list_pending_decisions`
/ `resolve_merge` view also adds derived fields that don't exist in the
file itself, most visibly `sources_omitted` — don't assume a field you
saw in that view is present in the markdown or in `athenaeum merges` JSON
output.

## I want a resolved answer to actually reach the wiki

Answering a question (either way, above) only flips the checkbox. Run:

```bash
athenaeum ingest-answers --path ~/knowledge
```

Each `[x]` block is rewritten as a raw intake file under
`raw/answers/{timestamp}-{entity-slug}.md`, with frontmatter linking back
to the original source, then moved into
`wiki/_pending_questions_archive.md` (newest-first, append-only —
answered blocks are never deleted, only moved). The next `athenaeum run`
picks the raw file up like any other intake and folds the answer into the
wiki entity.

Re-running with no new `[x]` blocks is a no-op. Malformed blocks are
preserved in place and logged to stderr, so a corrupt entry can't poison
the rest of the file.

## See also

- Guides — [Daily operation](daily-operation.md) · [Sidecar](sidecar.md)
- Modules — [conflicts](../modules/conflicts.md) · [corrections](../modules/corrections.md) · [mcp](../modules/mcp.md)
- Design — [conflict resolution](../design/conflict-resolution.md) · [auto-resolve](../design/auto-resolve.md) · [contradiction detection](../design/contradiction-detection.md)
- Reference — [configuration](../reference/configuration.md)
