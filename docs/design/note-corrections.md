<!-- SPDX-License-Identifier: Apache-2.0 -->

# Note-level corrections — why a CLI pass, not a fast-path record or a decompose-page extension

**Status:** IMPLEMENTED. Issue athenaeum#1976; section drop added by issue
athenaeum#2236.

## 1. The problem

A person page named by first name only acts as the tier-1 registry match for
every bare mention of that name, and so it accumulates Notes lines that are
actually about *other* people (the match-magnet pattern — see
[Field corrections](field-corrections.md) §14 for the sibling aggregate-page
failure this shares a root cause with). There was no governed way to move or
delete a single misfiled line.

## 2. Why not `decompose-page`

`decompose-page` already redistributes body-level facts, but it is shaped
for a page that gets rewritten into something new, not one that keeps its
own content: its apply path requires a full `--rewrite-body`, capped at
2,048 bytes, naming what the source page becomes. A person page correcting
a handful of misfiled Notes lines is not becoming anything — it stays
exactly the page it was, minus the lines that were never about it. Forcing
every correction through a whole-page rewrite would make the common case
(move three lines, change nothing else) carry the rare one's contract.

`decompose-page` also refuses an uncited line outright (`no-source`). A
misfiled Notes line is routinely uncited — it was typed straight onto the
page, not compiled from a footnoted source — and refusing to touch it would
leave exactly the lines most in need of correction permanently unmovable.

## 3. Why not a fast-path correction record

The field-correction fast path writes frontmatter fields only; moving or
dropping prose would need a new body-editing record kind read from `raw/`.
That crosses a boundary this repo holds deliberately: write access to
`raw/` is the trust line between "a source can propose a fact" and "a
source can delete wiki content" (see [Field corrections](field-corrections.md)
§12a). A body-edit record would hand every adapter the second power to get
the first.

## 4. The shape this pass takes instead

A deterministic CLI pass, `athenaeum correct-notes`, in the posture
`decompose-page` already established for the same reason: it is athenaeum
code acting AS the librarian under the run lock, reading a host-path batch
file (never `raw/`), resolving every record against one snapshot of the
page, and writing only when every record resolves. Two record kinds cover
the whole problem:

* a `bullet_id` record: `move` a Notes line to the page it is actually
  about, or `drop` it outright. Both cited and uncited lines are eligible
  for either — the pass has no reason to treat a line's provenance as a
  gate on whether it is misfiled.
* a `section_id` record: `drop` a whole `##` heading's span (issue
  athenaeum#2236 — a whole wrong-person enrichment section has no bullet
  ids to address it by). The span removed is the heading through, but not
  including, the next heading of the same or higher level, so a nested
  `###` subsection goes WITH its dropped `##` parent. There is no section
  `move` — that case is a separate field-group move tool, out of scope
  here. `section_id` is a slug of the heading's own text plus a 1-based
  ordinal among same-text headings, so it stays stable across re-reads of
  an unchanged page without being a raw position.

A batch is all-or-nothing: an unknown or ambiguous id (a bullet id that no
longer matches, or a section id two differently-worded headings collide
on) or an unresolvable move target refuses the whole batch before anything
is written, so a partially wrong batch can never leave the page in a state
nobody authored. A dropped section's footnote definitions get the exact
same reference handling a dropped bullet's do: a definition no longer
referenced anywhere on the page is removed, one still referenced elsewhere
is kept. Recovery from a batch that turns out to have been wrong is the
same as every other host-write job in this repo — a pre-run tag — rather
than a second, bespoke undo path.
