---
name: project_acme_corp
description: Acme Corp is a Series B logistics platform led by Priya Shah.
type: project
originSessionId: 01JZ8X6P4Q2K7N1F8V4S9W3R0T
originTurn: 12
sources:
  - session: 01JZ8X6P4Q2K7N1F8V4S9W3R0T
    turn: 12
    excerpt: "Priya confirmed the Series B closed 2026-03-12, led by Acme Growth Partners."
---

# Acme Corp

Acme Corp is a Series B logistics platform. Priya Shah is the CEO; she
confirmed in session turn 12 that the Series B round closed 2026-03-12
and was led by Acme Growth Partners.

## Notes

- Prior wiki entry had the company at Series A — the 2026-04 turn above
  supersedes that.
- Use this file as a template when bootstrapping new auto-memory entries:
  every claim in the body should be traceable to at least one entry in
  `sources[]`.

## How Athenaeum reads this file

When `athenaeum run` processes the surrounding `raw/auto-memory/<scope>/`
directory, the librarian:

1. Parses the frontmatter and body into an `AutoMemoryFile` record.
2. Clusters this file with any near-duplicate auto-memories (same or
   similar entity).
3. Emits a consolidated wiki entry at `wiki/auto-project-acme-corp.md`
   (or similar), propagating `origin_scope` and union-ing `sources[]`
   across the cluster.
4. Leaves this source file untouched.

Append to `sources[]` when a future Claude Code turn corroborates or
extends the claim — do not rewrite the existing entries. The merge pass
dedupes by `(session, turn)`, so duplicate appends are harmless, but
rewriting destroys provenance.
