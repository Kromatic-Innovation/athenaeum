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

## Per-case result

| Case | id | outcome_class (expected) | Observed | Deciding tier | Pass |
|---|---|---|---|---|---|
| A | `same_name_source_attaches` | `attach_same_name` | not recorded¹ | not recorded¹ | not recorded¹ |
| B | `name_variant_source_attaches` | `attach_name_variant` | not recorded¹ | not recorded¹ | not recorded¹ |
| C | `board_source_attaches_to_entity` | `attach_source_document` | not recorded¹ | not recorded¹ | not recorded¹ |
| D | `new_entity_mints_page` | `mint_new_entity` | not recorded¹ | not recorded¹ | not recorded¹ |
| E | `two_existing_entities_both_touched` | `attach_two_entities` | not recorded¹ | not recorded¹ | not recorded¹ |

¹ **Genuinely unavailable from this lane, not guessed.** The per-case
`observed` string, `passed` bool and tier `detail` are written only by
`eval_session.record_case(...)` (`tests/evals/test_attachment_eval.py:329-337`)
into `eval-summary.json`, which the `Upload eval summary` step attaches as
the `eval-summary` artifact (confirmed in the job log: `Artifact eval-summary
successfully finalized. Artifact ID 10401967845`, 4368 bytes) — it is not
printed to the job log itself. Both `gh run download 34981842660 -n
eval-summary -R Kromatic-Innovation/athenaeum -D <dir>` and the same command
against the fallback run (`34934473115`) failed identically:

```
error downloading eval-summary: Get "https://productionresultssa4.blob.core.windows.net/...":
dial tcp 57.150.87.97:443: connect: connection refused
```

`gh run download` resolves to a presigned Azure Blob Storage URL
(`*.blob.core.windows.net`) rather than an `api.github.com` or
`objects.githubusercontent.com` URL, and this lane's network egress refuses
that host outright (`curl` to the bare host also returns "Couldn't connect to
server"; `getent hosts` resolves three distinct IPs in the
`blob.core.windows.net` range and connection is refused on all three, so this
is a host/IP-range block, not a transient failure). The job log
(`gh run view 34981842660 --log`), read in full, confirms the aggregate
3/5 line above but never prints `eval-summary.json`'s contents, so the
per-case cells cannot be backfilled from it either. What every per-case
`test_attachment_case[...]` PASSED line in the log DOES confirm (all five
did) is the AC4 invariant each case asserts directly — no page vanished from
the wiki in any case — which is a narrower claim than the routing verdict in
the table above and is not conflated with it.

## What the numbers say

**The floor is missed by exactly one case**, the same margin the module
docstring's own 2026-09-10 observation records (also 3/5, on an earlier
commit) — the two dates agree on the aggregate without this file being able
to independently confirm they agree on *which* case moved, because the
per-case breakdown for this run's `eval-summary.json` could not be read (see
the per-case table's footnote). Treat the case-level narrative in
`tests/evals/test_attachment_eval.py`'s module docstring (C and E failing,
A/B/D passing) as describing 2026-09-10's run, not this one, until a run
whose artifact this lane can actually fetch confirms it still holds.

**The floor is aspirational, not descriptive, by design.** `ATTACHMENT_FLOOR
= 4` is set to what a correct librarian scores; the module docstring states
outright that the layer is expected RED on the shipped librarian (issue
athenaeum#1580 AC3's anti-vacuity criterion is that it fails on at least one
of B/C/E while passing D). A red floor here is therefore the intended
starting state for this baseline, not a surprise this file is reporting.

**Every routing decision is judgment, not a deterministic gate.** Unlike the
decomposition layer (`docs/measurements/decomposition-baseline-2026-09-10.md`),
where every verdict traces to a `len()` check, attach-vs-mint here is decided
by Tier 1 exact/alias match, Tier 2's entity extraction, or the Tier 3
create-name gate — so every one of the five cases costs at least one model
call, and the aggregate result is a live-model measurement, not a code-path
measurement.

## What would change this baseline

- A routing fix that moves the shipped librarian's score past `ATTACHMENT_FLOOR`
  (4/5) → `test_attachment_aggregate_floor` starts passing and this file needs
  re-dating against the run that proves it.
- Network egress to `*.blob.core.windows.net` opening from a future lane, or
  `eval-summary.json` gaining an alternate retrieval path (for example being
  echoed to the job log) → the per-case `observed`/`deciding tier`/`pass`
  cells above can be backfilled from the SAME run (its artifact does not
  expire until 2026-10-15) without re-running the eval.
- A change to `ATTACHMENT_FLOOR`, `DEFAULT_CLASSIFY_MODEL`, or
  `DEFAULT_WRITE_MODEL` → the floor or the models named above go stale and
  this file needs re-measuring, not just re-editing.
- The `eval-summary` artifact expiring 2026-10-15 without ever having been
  read → the per-case backfill option in the second bullet above is lost, and
  reconstructing the per-case breakdown at that point requires a fresh Evals
  run rather than reading this one's artifact.
