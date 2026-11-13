---
name: resolve-questions
description: Walk the user through unresolved entries in `_pending_questions.md` one at a time. Use when the SessionStart hook surfaces pending memory questions, when the user says "resolve pending questions" / "go through my questions" / "what questions do I have", or when the user wants to clear the contradiction queue.
---

# resolve-questions

Interactive walk-through for `~/knowledge/wiki/_pending_questions.md`. Each
entry is a contradiction or ambiguity the librarian flagged for human review.
Some entries carry an Opus-backed proposed resolution (issue athenaeum#126); some
don't. The user is the final authority — never resolve without confirmation.

## Surface

- CLI: `athenaeum decisions count|next|list [--with-proposal] [--json]` — the
  unified decision queue. (`athenaeum questions` was removed in 0.24.0.)
- MCP tools: `list_pending_questions`, `resolve_question(id, answer)`
- Snooze file: `~/.cache/athenaeum/pending-questions-snoozed-until` (ISO-8601 UTC)

## Flow (six steps)

1. Run `athenaeum decisions count --json`. If `questions` is 0, tell the user
   "no pending questions" and stop.

2. Loop. On each iteration call
   `athenaeum decisions next --with-proposal --json`. The result has the
   shape `{type, id, created_at, summary, confidence, payload}`; a question
   item carries `type: "question"` and
   `payload: {entity, source, question, conflict_type, description,
   raised_by, proposal}`. The queue is unified, so the oldest item may be a
   `merge`, `confirmation`, `retraction` or `audit` instead — this skill only
   handles `type: "question"`; for anything else, tell the user the oldest
   decision is that other kind and point them at
   `athenaeum decisions list --json`. Render a question to the user as a
   short block:

   - `payload.entity`, `payload.question`, `payload.conflict_type`
   - `payload.description` (verbatim)
   - `payload.proposal` when non-empty

3. Ask the user one of four choices:

   - **accept** the proposed resolution
   - **override** with a different answer
   - **defer** (snooze) — stop now, surface again tomorrow
   - **stop** — stop now without snoozing

4. **accept**: call the `resolve_question` MCP tool with `id` and the
   proposed action (extract the action from the `**Proposed resolution**:`
   line in `payload.proposal`, e.g. `keep_a`, `merge`,
   `retain_both_with_context`). Then loop back to step 2.

5. **override**: ask the user for the answer text they want recorded. Call
   `resolve_question` with `id=<id>, answer=<their text>`. Then loop back
   to step 2.

6. **defer**: write `~/.cache/athenaeum/pending-questions-snoozed-until`
   containing an ISO-8601 UTC timestamp `ATHENAEUM_PQ_SNOOZE_HOURS` (default
   24) hours from now. The SessionStart hook reads this and stays silent
   until then. Stop. (**stop** is the same minus the snooze write.)

## Snooze write — exact format

The hook reads the file with a lexicographic compare against
`date -u +%FT%TZ`. Match that format exactly:

```bash
mkdir -p ~/.cache/athenaeum
date -u -v+24H +%FT%TZ > ~/.cache/athenaeum/pending-questions-snoozed-until
```

(GNU date uses `-d '+24 hours'` instead of `-v+24H`.)

## Constraints

- Never call `resolve_question` without explicit user confirmation of the
  exact action.
- If `athenaeum` CLI isn't installed, fall back to `list_pending_questions`
  MCP tool and walk the list directly.
- If the user says "skip this one and show the next", advance the cursor
  by calling `list_pending_questions` and showing index 1 instead of 0 —
  do NOT resolve the current one.
- Resolved entries (`[x]`) are archived by `athenaeum ingest-answers` on
  the next librarian run — you don't need to handle archival yourself.
