# Tier-0 attributed-paste cleanup — apply run (athenaeum#1717)

Generated: 2026-10-01

Records the three measurement/reading passes and the two destructive apply
passes that cleaned the person pages carrying tier-0 attributed pastes.
Companion to `tier0-paste-cleanup-2026-09-24.md` (the proposer-agreement
measurement that set the verifier rule). Counts only — no page uids, names,
or corpus text appear below; the eval set stays host-side
(`~/.cache/athenaeum/1717/`, never committed).

## Three-run history (reading passes)

| run | scope | verifier attempted | spend | outcome |
|---|---|---:|---:|---|
| A (proposer-only) | full corpus | 0 | 28.9M subscription tokens | Proposer-only pass with no verification step; superseded once the issue's own AC required a verifier re-check, since an un-re-checked proposer verdict could not gate a destructive apply. |
| A2 (proposer + verifier) | 4,359 of 4,467 pages (108 out of scope) | 6,134 of 7,976 (agree 5,408, disagree 726) | 41.3M subscription tokens, 14,105 calls; $0.00 API | 1,842 bullets (slices 02, 05, 08) hit a per-day spend ceiling during the verifier pass and were left unverified. Pages carrying any unverified bullet were held out of the apply set whole, so no page applied half-verified. |
| A3 (verifier re-run on the held-out pages) | 631 pages (the 739 held out of A2, minus 108 out of scope) | 2,090 of 2,095 (agree 1,740, disagree 350; 5 unverified are extraction holds, never applied) | 9.9M subscription tokens, 4,180 calls; $0.00 API | 0 ceilings tripped (ceilings lifted further so the day figure could not trip during a verifier pass). Every in-scope page now has a fully verified report. |

## Scope

- Pages in scope: 4,359 of 4,467 total person pages carrying an attributed paste.
- 108 out of scope: 19 live on the excluded surface (never walked by paste-cleanup by design), 89 no longer exist in the corpus.
- Bullets/proposals, corpus-wide: 7,976.
- Final verdicts (verifier wins, corpus-wide): keep 2,609 / remove 3,107 / rewrite 2,250 / hold 10.

## Verifier agreement rates

| pass | attempted | agree | rate |
|---|---:|---:|---:|
| A2 | 6,134 | 5,408 | 88.2% |
| A3 | 2,090 | 1,740 | 83.3% |

## Apply passes (destructive, operator-approved)

### Phase B1 — the 3,728 fully-verified A2 pages

- Rollback tag `pre-1717-cleanup-2026-10-01`, pushed before any slice ran.
- 8 slices, one commit per slice, 2,137 pages changed, 8 commits total.
- Remove: 1,866 proposed, 1,866 applied (100%). Rewrite: 2,107 proposed,
  2,106 applied (1 skipped — a literal duplicate bullet on one page; the
  rewritten content landed correctly, a stale untouched copy of the
  duplicate remains, pre-existing source-data duplication, not a verdict
  failure).
- Spend: 0 subscription tokens, $0.00 API (the replay path builds no LLM client).

### Phase B2 — the 631 fully-verified A3 pages

- Rollback tag `pre-1717-cleanup-b2-2026-10-01`, pushed before any slice ran.
- 7 slices (the 8th A3 slice was the empty, out-of-scope report and was
  skipped), one commit per slice: 409 pages changed across 7 commits.
- One slice's apply collided with a concurrently running, unrelated
  foreign writer (a scheduled contact-sync process actively dirtying a
  non-overlapping set of frontmatter fields on 13 of that slice's target
  pages at the same moment). The collision was resolved by isolating the
  paste-cleanup hunk from the foreign hunk per file before staging —
  confirmed non-overlapping line ranges in every case — so the commit
  carries only the paste-cleanup change and the foreign writer's
  in-flight edit was left undisturbed and uncommitted, exactly as it was
  before this apply touched those pages.
- One separate hand-edit commit after the apply (see below).
- Spend: 0 subscription tokens, $0.00 API.

### Pages changed / commits, B1 + B2 combined

| phase | pages changed | apply commits | hand-edit commits |
|---|---:|---:|---:|
| B1 | 2,137 | 8 | 0 |
| B2 | 409 | 7 | 1 |

### Rollback tags

- `pre-1717-cleanup-2026-10-01` (before B1)
- `pre-1717-cleanup-b2-2026-10-01` (before B2)

## Operator hand corrections

The apply mechanism (`--from-report --apply`) only ever writes the model's
own claim text, never an operator-supplied correction — see the mechanism
limitation below. Two eval-set rows carried an operator correction to the
model's wording; a third carried an operator "keep" ruling the apply
removed instead. All three were hand-corrected in one commit after the B2
apply, distinguishable from the model's own verdicts, restoring content
from the two rollback tags above where the original bullet was needed as a
reference.

## Eval-sheet results

32-row hand-labelled eval set, checked against the applied corpus in two
passes (B1 then B2, since 6 rows' relevant page only entered the verified
set in Phase A3):

| pass | matched | mismatched | unresolved | deferred |
|---|---:|---:|---:|---:|
| B1 (26 rows) | 9 | 11 | 6 | 6 |
| B2 (the 6 B1-deferred rows) | 4 | 2 | 0 | 0 |
| **combined (32 rows)** | **13** | **13** | **6** | **0** |

Mismatch effect split (B1 pass; counts only): 2 rows where content was
deleted against an operator keep/rewrite ruling (one of which is the
operator-correction row restored by hand after B1), 2 rows where an
operator remove ruling instead left a reworded claim on the page, 9 rows
reworded-only (direction unchanged, wording differs from the operator's
ruling) across both passes. The 6 unresolved rows (all operator-remove,
multi-candidate pages) are directionally consistent with the operator's
ruling but could not be isolated to one bullet by length-based matching;
not counted as matched or mismatched.

**Run-to-run drift finding.** The B1 pass also compared the live corpus
against the 2026-09-25 dry run the eval sheet was originally labelled from:
9 of 20 checkable rows matched that earlier dry run, 7 differed in wording
only, 2 left a reworded claim where the dry run's ruling was remove, 2
deleted content where the dry run's ruling was keep or rewrite. The model's
verdict on a given paste is not perfectly stable run to run even with the
same verifier rule; eval-sheet checks against a live apply should expect
some fraction of wording-only drift independent of the apply mechanism
itself.

## Known findings tracked as follow-up issues

- **Ceiling behavior during the verifier pass** (athenaeum#1923): when a
  spend ceiling trips mid-batch during the verifier pass, the remaining
  bullets in that batch are left unverified (`verify_attempted: false`)
  rather than resumed once budget is available, forcing a dedicated re-run
  pass (A3) over the affected pages.
- **Operator-corrected text cannot be applied through `--from-report`**
  (athenaeum#1924): the apply path only ever writes the model's own claim
  text; an operator's hand-corrected wording has no mechanism to land
  through apply and must be hand-committed separately every time.
- **Hook-suppression finding** (athenaeum#1908): tracked separately; not
  re-measured in this apply pass.

## Spend by provider (both apply passes)

- Subscription: 0 tokens.
- API: $0.00.
- The replay path (`--from-report --apply`) builds no LLM client, so every
  apply slice in both phases spent $0 and 0 tokens under paste-cleanup.
  Reading passes (A, A2, A3) are the only spend in this body of work; see
  the per-pass table above.

## Mechanism limitation

`apply_paste_cleanup_report` writes only `remove`/`rewrite` final verdicts,
and on `rewrite` writes only the model's own claim text (the verifier's
claim when it overrode the proposer, otherwise the proposer's claim) —
there is no field an operator's corrected text can occupy so that `--apply`
writes it automatically. Every operator correction observed in this body of
work therefore required a manual hand-edit commit after the apply, rather
than landing through the apply mechanism itself. Tracked as
athenaeum#1924.

## Scope note

As with the proposer-agreement measurement, this record is against real
corpus content by necessity. Only counts, rates, and commit/tag identifiers
are committed here; the eval set, its per-row content, and the live wiki
pages stay host-side or in the private `TriKro/knowledge` repo.
