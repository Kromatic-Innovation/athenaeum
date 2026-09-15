# Attachment baseline — 2026-09-15

Issue athenaeum#1580. First dated baseline of the intake-attachment (routing)
layer under `docs/measurements/`: given a wiki that already has a page for an
entity and a new raw source about that entity, does the librarian land the
source on the existing page, or mint a second one?

- Corpus: `tests/evals/data/attachment/cases.yaml` (five cases, one per
  `outcome_class`, overlaid onto `tests/evals/corpus.build_corpus("core")`).
- Measure: `tests/evals/attachment.py` (structural, delta-based scoring —
  `score_case`, `diff_wiki`, `attribute_tier`).
- Graded by: `tests/evals/test_attachment_eval.py` (metered, `eval`-marked;
  deselected from default CI, run via `evals.yml` on `workflow_dispatch` /
  `push:main`).
- Librarian under test: `develop` @ `0148c7f9` (`0148c7f995d5fbb9b8d086386397bca74ce5673a`).
- Models: classify `claude-haiku-4-5-20251001`, write `claude-sonnet-5`.
- Run: Evals workflow run
  [34981842660](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/34981842660)
  (`push`, `main`, created 2026-09-15T14:28:09Z, head commit `0148c7f9`, the
  newest main-push Evals run whose Live-API eval suite job actually ran at
  the time this file was written). Its `Live-API eval suite` job reported
  `2 failed, 51 passed, 1 skipped, 163 deselected, 4 warnings in 198.14s`,
  with `test_attachment_eval.py::test_attachment_aggregate_floor` among the
  two failures.
- Threshold in force: `ATTACHMENT_FLOOR = 4` (`tests/evals/test_attachment_eval.py`)
  — pass requires at least 4 of the 5 cases.

## Aggregate result

**3 / 5** (need >= 4) — **FAIL**. From the job log
(`gh run view 34981842660 --log`):

> `AssertionError: attachment below aggregate floor: 3/5 (need >= 4).
> Classify model: claude-haiku-4-5-20251001; write model: claude-sonnet-5.
> Check eval-summary.json for per-case failures and the per-case tier
> attribution.`

Corroborated by the run's own `eval-summary.json` (`layer_scores.attachment
= {"passed": 3, "total": 5}`, `generated_at: 2026-09-15T14:32:20Z` — matching
the job log's test-completion timestamp to the second). Direct download of
that artifact from this lane fails — `gh run download 34981842660 -n
eval-summary -R Kromatic-Innovation/athenaeum` resolves to a presigned
`*.blob.core.windows.net` URL, and this lane's network egress refuses that
host outright (`dial tcp 57.150.87.97:443: connect: connection refused`,
reproducible against all three resolved IPs; `api.github.com` itself is
reachable). The per-case data below was relayed into this environment
out-of-band by the orchestrator after that block, and checked against facts
this lane had already obtained independently before the relay arrived — the
aggregate score and the completion timestamp above — rather than trusted on
its own say-so.

## Per-case result

| Case | id | outcome_class (expected) | Observed | Deciding tier | Pass |
|---|---|---|---|---|---|
| A | `same_name_source_attaches` | `attach_same_name` | attached to `attach-project-quarrowfield`; nothing minted | `write_merge` (Tier 3 WRITE-MERGE; 1 classify call, 4 write calls) | **PASS** |
| B | `name_variant_source_attaches` | `attach_name_variant` | minted a second page, "Bracklemoor transit study phase two" (uid `4eec4ede`); `attach-project-bracklemoor` was also touched | `write_merge` (1 classify call, 3 write calls) | **FAIL** — minted 1 page (max 0); page name carries "Bracklemoor" |
| C | `board_source_attaches_to_entity` | `attach_source_document` | minted "Steepgate" (uid `62461957`, `type=company`, not the allowed `source`); touched `policy-onboarding` — `attach-company-steepgate` was neither touched nor proposed against | `write_merge` (1 classify call, 5 write calls) | **FAIL** — wrong-type mint, entity never reached |
| D | `new_entity_mints_page` | `mint_new_entity` | minted "Fallowdyke Freight Audit"; nothing touched | `write_merge` (1 classify call, 1 write call) | **PASS** |
| E | `two_existing_entities_both_touched` | `attach_two_entities` | touched both `attach-company-steepgate` and `attach-project-quarrowfield`; nothing minted | `write_merge` (1 classify call, 4 write calls) | **PASS** |

Source: `eval-summary.json` for run 34981842660, `cases[]` entries with
`layer == "attachment"` (`case_id`, `expected`, `observed`, `detail`,
`passed`). "Deciding tier" is `attribute_tier(...)`'s `tier` field from
`detail` (`tests/evals/attachment.py`); every one of the five cases in this
run was decided at Tier 3 WRITE-MERGE — none was resolved at Tier 1's
deterministic match or Tier 2's CLASSIFY, and none escalated.

## What the numbers say

**The aggregate matches 2026-09-10's observation (3/5) but the failing cases
moved.** The module docstring's 2026-09-10 baseline records C and E failing,
with A/B/D passing. This run, five days later at commit `0148c7f9`, has **B
and C failing, with A/D/E passing** — E now passes cleanly (both entities
reached, nothing minted) where it used to over-mint, and B newly fails by
minting the exact "name / name (qualifier)" duplicate B exists to catch. The
identical 3/5 score would misread as "nothing changed" if only the aggregate
were recorded; it is not the same 3/5.

**C is the one stable failure, and it is stable in its exact shape.** Both
the 2026-09-10 docstring and this run's `eval-summary.json` describe the same
failure: a second entity-typed page minted for "Steepgate" instead of a thin
`type: source` page, and the existing `attach-company-steepgate` page left
untouched. A board that is evidence FOR an entity is still becoming a page
that competes with it.

**Every one of the five decisions was made at Tier 3 WRITE-MERGE.** None
was resolved deterministically (Tier 1) or at CLASSIFY (Tier 2), and none
escalated — confirming the module docstring's claim that attach-vs-mint
routing here costs the expensive tier on every source, not just on the ones
that fail.

**The floor is aspirational, not descriptive, by design.** `ATTACHMENT_FLOOR
= 4` is set to what a correct librarian scores; the module docstring states
outright that the layer is expected RED on the shipped librarian (issue
athenaeum#1580 AC3's anti-vacuity criterion is that it fails on at least one
of B/C/E while passing D). A red floor here is therefore the intended
starting state for this baseline, not a surprise this file is reporting.

## What would change this baseline

- A routing fix that moves the shipped librarian's score past `ATTACHMENT_FLOOR`
  (4/5) → `test_attachment_aggregate_floor` starts passing and this file needs
  re-dating against the run that proves it.
- A fix targeted at case B or C specifically → re-measure rather than hand-edit
  the affected row: this run already shows the failing pair is not stable
  (E moved from failing to passing between 2026-09-10 and 2026-09-15 while the
  aggregate held at 3/5), so a plausible-looking single-row edit could easily
  describe a case that has since moved for an unrelated reason.
- A change to `ATTACHMENT_FLOOR`, `DEFAULT_CLASSIFY_MODEL`, or
  `DEFAULT_WRITE_MODEL` → the floor or the models named above go stale and
  this file needs re-measuring, not just re-editing.
- Network egress to `*.blob.core.windows.net` opening for a lane directly
  (rather than via an out-of-band relay) → future baselines in this shape
  stop depending on a second party to retrieve their own primary source.
