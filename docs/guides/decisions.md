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

To see only what is worth your attention right now, ask for the digest:

```bash
athenaeum decisions digest           # "3 decisions need you — 2 audits, 1 merge"
athenaeum decisions digest --json    # the same, machine-readable
```

The digest counts everything pending and held, offers at most four decisions
at a time (`decisions.digest_batch_size`), and never offers a held decision or
one of a type that cannot yet be answered (those are counted as
`unanswerable_total`). Per-type daily caps in `decisions.surface_caps` (audit
items default to three a day) limit what is offered, never what you may answer;
a cap is spent by that type's answers today. Its `digest_id` changes only when
the offered decisions change, so a host can notify only on change. The MCP tool
`queue_digest` returns the same digest.

`athenaeum decisions` unifies the two human-decision queues Athenaeum
accumulates — **questions** (contradiction detector) and **merges**
(resolver merge proposals) — so there's one place to look instead of two.
Each merge item is rendered as an answerable question: the source pages
are named by their frontmatter `name:` (not the uuid-slug) with a
one-line gist each, because cosine topic-similarity alone doesn't tell you
whether two pages *should* merge.

**`athenaeum merges` and `athenaeum questions` were removed in 0.24.0** in
favor of this unified surface; see [Upgrading](upgrading.md) for the
command-by-command mapping. The merge queue's maintenance-only modes
(`revalidate`, `recompare`, `scrub-pii`, `propose-fold`, `provenance`) live
here too, as `athenaeum decisions revalidate` / `recompare` / `scrub-pii` /
`propose-fold` / `provenance`. `athenaeum decisions
migrate` additionally materializes every
pending-merge and pending-question record (resolved and unresolved) into a
persisted unified-schema file (`wiki/_decisions_queue.jsonl`) alongside the
two legacy sidecars, with every id and disposition carried over unchanged —
useful for anything that wants to read the whole queue in one shape without
re-deriving it from two different file formats.

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

`athenaeum decisions` carries every mode the removed `athenaeum merges`
offered, plus five merge-only modes — it is the only spelling:

```bash
athenaeum decisions list  [--limit N] [--json]        # all unresolved proposals
athenaeum decisions next  [--json]                     # the oldest unresolved proposal
athenaeum decisions count [--json]                     # "N unresolved (oldest: <iso-date>)"
athenaeum decisions revalidate [--apply] [--json]      # re-check the queue against the CURRENT gate
athenaeum decisions recompare [--apply] [--limit N] [--json]
                                                    # re-run the five-verdict comparator over every
                                                    # unresolved proposal and ledger a verdict per
                                                    # source pair. Dry-run by default; --apply writes
                                                    # to the ledger only — it never approves, rejects,
                                                    # or archives a proposal, and PII-hazard proposals
                                                    # always route to a human.
athenaeum decisions scrub-pii [--apply] [--allowlist F] [--json]
                                                    # redact contact data out of proposal BODIES in
                                                    # place. Zero LLM cost, and it does NOT force the
                                                    # merge decision — the proposal stays unresolved.
                                                    # Dry-run by default.
athenaeum decisions propose-fold --into PAGE --source PAGE [--source PAGE ...] [--apply]
                                                    # propose folding one or more source pages INTO a
                                                    # named canonical page. Dry-run by default.
athenaeum decisions provenance [--canonical-slug S] [--merge-id ID] [--json]
athenaeum decisions revoke-merge-rejection --a PAGE --b PAGE [--apply]
                                                    # reverse a previous reject verdict between two
                                                    # pages. Dry-run by default.
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
sanctioned reader; the `athenaeum decisions` subcommands above are the
sanctioned CLI surface built on top of it. The MCP `list_pending_decisions`
/ `resolve_merge` view also adds derived fields that don't exist in the
file itself, most visibly `sources_omitted` — don't assume a field you
saw in that view is present in the markdown or in `athenaeum decisions`
JSON output.

## Reading an old fold answer about shared content

Some pending-merge and fold decisions carry free-text reasoning that
predates a shared-content page existing as an option at all. If an old
answer's own text says content "belongs on each page" — because, at the
time, copying it onto every page it concerned was the only way to keep it
visible everywhere — read that as "link it from each page," not as
licence to keep stamping fresh copies. A shared page now exists for
exactly this case: content that genuinely concerns two or more distinct
pages gets ONE page of its own, and every page it concerns links to it,
so there is one place to update instead of several that can drift apart.

No existing duplicate is rewritten on the strength of this reading —
re-filing what is already in a live corpus is separate, deployment-scoped
work. The rule only changes what a NEW fold or recompile decision does
going forward: it links, it does not copy.

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

## I want a rejected merge to stop coming back

Rejecting a merge or fold question (either way — editing the file, or
`resolve_merge(..., "reject")` via MCP) does more than flip a checkbox: it
writes a durable `merge_rejected_with:` link onto the two pages, naming
each other. That link means "an operator looked at these two and ruled
they are NOT the same thing" — a fact, not a similarity score, and it
never expires on its own.

Every merge path honors it before proposing or applying anything: the
nightly similarity clusterer splits a linked pair out of a candidate
group rather than letting a third page unite them indirectly,
`propose-fold` refuses to queue a fold naming both sides, and approving a
queued fold is refused outright if it would unite them — whether directly
or because one side has since been folded into (or renamed from) the
other. The link survives both of those: folding a page into a canonical
carries its rejections forward, and renaming a page (or folding a source
into it) keeps the old name resolvable, so a link written before the
rename still matches.

Because the link never expires, reversing a prior ruling is a deliberate
step, not a side effect of approving something else:

```bash
athenaeum decisions revoke-merge-rejection --a PAGE --b PAGE --apply
```

This removes the link from both pages (whichever side(s) still carry it)
so the pair becomes proposable again on the next run. Leave off `--apply`
to see what would change first.

## See also

- Guides — [Daily operation](daily-operation.md) · [Sidecar](sidecar.md)
- Modules — [conflicts](../modules/conflicts.md) · [corrections](../modules/corrections.md) · [mcp](../modules/mcp.md)
- Design — [conflict resolution](../design/conflict-resolution.md) · [auto-resolve](../design/auto-resolve.md) · [contradiction detection](../design/contradiction-detection.md)
- Reference — [configuration](../reference/configuration.md)
