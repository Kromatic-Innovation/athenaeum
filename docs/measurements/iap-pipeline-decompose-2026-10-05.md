# Page-decompose host apply — IAP Outreach pipeline tool page, 2026-10-05

Issue athenaeum#1931 (host apply, `needs:host-write`), decomposed with the
clause-level mode added by athenaeum#1947. Counts only — no entity names,
uids, or page content, per the issue's own constraint on what this note may
carry.

- Mechanism: `athenaeum decompose-page --split-clauses`, tier-0, no LLM call.
- Deploy checkout: `develop` @ `df6b2f7c`.
- Rollback tag: `pre-1931-iap-pipeline-decompose-2026-10-05`.

## Page shape

Unlike the Streak CRM tool page (athenaeum#1914), this page did not carry
one subject per list item: 2 of its 10 top-level list items each bundled
many footnote-cited subject clauses into a single run-on item. The
clause-level split (`--split-clauses`) turned those into one candidate per
cited clause before classification.

- List items: **10**.
- Items split into clauses: **2**.
- Clauses emitted: **209**.
- Malformed clauses (drop-only): **10**.
- Units classified (whole items + clauses): **217**.
- Subject resolution: **200 resolved / 17 unresolved**.
- Conflicting footnote labels: **16**.
- Orphan footnote definitions: **3**.

## Dispositions (dry run, re-confirmed immediately before apply)

| Disposition | Count |
|---|---|
| attached | 122 |
| already-present | 72 |
| unresolved | 14 |
| no-source | 3 |
| ambiguous-source | 6 |

The re-run dry run, taken inside the same quiesce/lock window immediately
before `--apply`, reproduced this table and every per-unit disposition
exactly (id, disposition, uid, source) — the apply proceeded only because
that gate held.

## Operator rulings

23 blocking units (14 unresolved — of which 10 malformed, 3 no-source, 6
ambiguous-source) needed an explicit ruling before `--apply` would run.

- Ruled-attach: **0**.
- Ruled-drop: **23**.

All 23 were ruled `drop` by the operator (recorded on the issue as
`occam:disposition` comments before this apply ran). The three `no-source`
units were the page's own open-question checklist lines about the tool
itself; the rewritten tool page (below) carries them under an "Open
questions" heading, so dropping them from decomposition loses nothing. The
remaining dropped units named per-contact pipeline facts whose source could
not be confirmed at a selectable level; recovering what is recoverable from
the underlying export, where the entity page does not already carry it, is
filed as a follow-up in the private adapters repo:
Kromatic-Innovation/athenaeum-adapters#252.

## Apply outcome

- Attached: **122**.
- Dropped by ruling: **23**.
- Already-present (skipped): **72**.
- Source page rewritten: **yes**.
- Target pages changed: **122** (one write per attached unit; no target
  received more than one write).
- Tool page size: **before** 56,907 bytes / **after** 1,697 bytes.

## Verification

- A `git status` diff of the knowledge corpus taken before and after the
  apply shows exactly 123 changed paths, all under `wiki/` (122 attached
  target pages plus the rewritten source page); no `raw/` path was touched
  by the apply.
- Rewritten tool page: same `uid`, `type`, and `name` as before; rewrite
  body and frontmatter description screened against the full subject list
  from the dry-run report (202 distinct subjects) — no match.

## Housekeeping

- Corpus was quiesced for the apply window (`athenaeum quiesce --for 1h
  --reason athenaeum#1931`); released immediately after the commit landed.
- The shared knowledge-writer lock (gmail-backfill) was held at dispatch
  time; the apply waited for it to clear before taking any quiesce or
  write action.
- Commit pushed to `develop`.
