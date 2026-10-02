---
title: "Rollout eval layer, packaged adapter — 2026-10-01 re-measurement"
---

# Rollout eval layer, packaged adapter — 2026-10-01 re-measurement

Issue athenaeum#1919 (paid re-measurement authorized by operator budget approval,
session 2026-10-01). Re-runs the same recipe as
`docs/measurements/rollout-eval-adapter-cutover-2026-09-29.md` against the
packaged adapter console script (`athenaeum-claude-hook`) now that
athenaeum#1912's query-term parity fix is merged (PR athenaeum#1920, merge
commit `a101fb61`) and on `develop`, `main`, and the operator's deploy
checkout — all three confirmed by
`git merge-base --is-ancestor a101fb61 <ref>` before any spend.

Operator authorization: "budget approved" (session 2026-10-01), recorded as
`occam:disposition` on athenaeum#1919 (comment 5944550606), following the
athenaeum#1907 pattern. Backend: `api`. Self-imposed cap: $5, same as the
2026-09-26 and 2026-09-29 runs ($3.14 and ~$3.15 actual).

## Run command

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,vector]"   # [vector] is required: plain .[dev] omits chromadb
set -a
source ~/.cache/athenaeum/config.env   # exported only into this subshell
set +a
python -m tests.evals.north_star_cli \
  --mode api --scale full --corpus-scales core --replicates 0 \
  --search-backend vector --max-spend 4.5 --workers 4 \
  --store <scratch>/store/store-adapter.jsonl \
  --out-dir <scratch>/report --materialize-root <scratch>/materialize
```

- Repo/worktree: `dijkstra/1919-rollout-eval-remeasure-2`, git sha
  `3303548b9167` (branched off `origin/develop`; athenaeum#1920's merge
  commit `a101fb61` confirmed an ancestor of this worktree's HEAD, `origin/main`,
  and the deploy checkout's HEAD before spending anything).
- Hook resolution confirmed via `tests.evals.rollout.resolve_user_prompt_hook()`
  in-process: resolved to `<worktree>/.venv/bin/athenaeum-claude-hook`, an
  editable install of this worktree's `src/athenaeum` (Python 3.13.12 venv,
  built from Homebrew's `python3.13` since the host's `pyenv` default is
  3.11) — not a stale PATH-shim binary.
- `ATHENAEUM_EVAL_HOOK` left unset — the default adapter path.
- No `~/knowledge` write: `--materialize-root`/`--store`/`--out-dir` all
  pointed at this session's scratchpad directory, outside this repo and
  outside `~/knowledge`. Confirmed in addition by reading
  `north_star_cli.py`'s own fallback (`tempfile.mkdtemp`, never a
  `~/knowledge`-rooted default) before the run.
- The $5 cap meant no `timeout` wrapper was placed around the paid call
  itself — the 2026-09-29 run's own record notes a foreground `timeout`
  sized to the `--dry-run` wall-clock projection badly over-provisions on
  time while a token-ceiling guard already bounds spend; a hard kill mid-run
  would also pay for in-flight cells that never persist. The run was
  launched in the background and waited on in the foreground via a PID
  poll loop instead, per the same reasoning.

## Result: full grid completed, ceiling tripped at the very end

Unlike the 2026-09-26 and 2026-09-29 runs (376/384 cells, one probe's group
still in flight when the ceiling tripped), **this run persisted all 384
planned cells** — the token-ceiling guard
(`tests.evals.containment.SpendCeilingExceededError`) fired only after every
cell had already been written to the store, reporting
`2842990 > 2710526` and halting before a next group could start. This is the
same guard firing as designed, not a crash, and it means every `core`-scale
probe for every arm — including `push_breadcrumb_pull` — has a persisted
result this time, with no partial-run denominator mismatch against the
2026-09-19 shell-hook baseline.

- **Token usage:** summed directly from the 384 persisted cells'
  `turn_tokens`: **2,852,651 tokens** (2,744,770 input / 107,881 output).
- **Spend:** at `claude-haiku-4-5-20251001` list rates ($1/$5 per MTok
  input/output): **~$3.28**. Within the operator's $5 cap and comparable to
  the 2026-09-26 ($3.14) and 2026-09-29 (~$3.15) runs.
- **Run id:** report generated `2026-10-02T02:46:23Z`, `git_sha: 3303548b9167`,
  `corpus_digest[core]: edcd8dd3286d0135`, `grader_revision: athenaeum#1843`.
  Both the corpus digest and grader revision match the 2026-09-19,
  2026-09-26, and 2026-09-29 records exactly — the same probe set, the same
  grader, across all four readings. Report:
  `north-star-2026-10-02.md` (generated in scratch, not committed — same
  convention as the two prior records, since it carries per-cell
  probe/answer detail this record intentionally omits).
- **Harness failures: 4** (`turn_cap`: 2 `native_grep`, 1 `pull`, 1
  `push_breadcrumb_pull`). The `push_breadcrumb_pull` failure is probe
  `abstain_unknown_person`, one of the probe ids inside the `abstention`
  probe class — it does not fall inside the 45-probe set this record's
  headline score is computed over, so it does not create an
  included-vs-excluded ambiguity for the headline number (see "Harness
  failure accounting" below).

## Correction to earlier records

The 2026-09-26 and 2026-09-29 records both cite the 2026-09-19 shell-hook
floor as `push_breadcrumb_pull` 36/45 (80.0%). Re-summing the 2026-09-19
report's own per-probe-class correctness rows for `push_breadcrumb_pull`,
`core` scale, excluding the `abstention` class the same way those records
do, gives **37/45 (82.2%)**, not 36/45 (80.0%) — the 36 was not independently
derived from the 2026-09-19 source; it originated in the 2026-09-26 record
and was copied forward into the 2026-09-29 record and the first draft of
this one. This record uses the correct 37/45 (82.2%) figure throughout.
Neither the 2026-09-26 nor the 2026-09-29 record is edited to fix this.

An earlier draft of this record also claimed a probe-class taxonomy change
had landed between 2026-09-29 and this run (separate `abstain_unknown_*`
classes merging into one `abstention` class). That claim was wrong:
`abstain_unknown_client` / `abstain_unknown_person` / `abstain_unknown_policy`
are PROBE IDS, not probe classes, in every one of the four reports including
the 2026-09-19 baseline — all three belong to the one `abstention` class
throughout, and no cell moved between classes. No taxonomy change occurred.

The headline score below still excludes the `abstention` class, for the same
reason the 2026-09-19, 2026-09-26, and 2026-09-29 records already do:
landing on the same 45 non-abstention core probes (48 total minus the 3
`abstention`-class probes) that all four readings grade in full. The
`abstention` class's own count is reported separately, not folded into the
headline.

## Zero-spend retrieval-parity check on this tree (no additional spend)

Before writing this record, `tests.evals.hook_divergence.run(mode="adapter-vs-shell", backend="vector", scale="core")` —
the classifier athenaeum#1912 built and the issue's own "Not in scope" section
names as zero-spend and re-derivable — was run once against this worktree's
tree (git sha `3303548b9167`, same as this paid run) to confirm the fix still
holds here, not re-derive its original counts:

```
counts: {'byte-identical-after-normalization': 48, 'bullet-list-differs': 0, 'notice-text-only-differs': 0, 'other': 0}
```

**All 48 of 48 core probes are byte-identical-after-normalization** between
the packaged adapter and the shell hook on this tree — the retrieval-parity
proof holds completely here, a stronger result than athenaeum#1912's own
29/47-bullet-list-differs / 18/47-notice-text-differs findings on the
pre-fix-adjacent tree it was built against.

## Scores: 2026-10-01 re-measurement vs. all three prior readings, `core` scale

Correctness tallied the same way as all three prior records (parsed from the
per-probe-class correctness table, `core` scale only, pooled over every
non-abstention probe class, same denominator convention as the 2026-09-19,
2026-09-26, and 2026-09-29 readings):

| arm | shell hook (2026-09-19) | adapter, pre-fix (2026-09-26) | adapter, overflow-fix (2026-09-29) | adapter, term-parity-fix (2026-10-01) |
| --- | --- | --- | --- | --- |
| `push_breadcrumb` | 0/45 (0.0%) | 0/44 (0.0%) | 0/44 (0.0%) | 0/45 (0.0%) |
| `push_breadcrumb_pull` (**verdict arm**) | 37/45 (**82.2%**) | 31/44 (**70.5%**) | 33/44 (**75.0%**) | **31/45 (68.9%)** |

The shell-hook figure above (37/45, 82.2%) corrects the 36/45 (80.0%) cited
by the 2026-09-26 and 2026-09-29 records — see "Correction to earlier
records" above.

`push_breadcrumb` itself remains unchanged (0% across all four runs),
carrying no signal either way, same as all three prior records note.

### Harness-failure accounting (both ways)

The only `push_breadcrumb_pull`/`core` harness failure this run (probe
`abstain_unknown_person`) falls inside the `abstention` class, which the
headline 45-probe set above already excludes for comparability (see
"Correction to earlier records" above). **The headline 68.9% figure is therefore identical whether the
harness failure is counted as incorrect or excluded** — unlike the
2026-09-29 run, where the one `push_breadcrumb_pull` harness failure
(`ratecard_tooling_owner`) fell inside a scored class and changed the
reported rate (75.0% counted vs. 76.7% excluded).

For completeness, the full-denominator reading (all 48 core probes,
`abstention` included) is **33/48 (68.8%)** counting the harness failure as
incorrect, or **33/47 (70.2%)** excluding it as ungraded.

### Deltas for `push_breadcrumb_pull`, the verdict/shipped arm

- **vs. the corrected 2026-09-19 shell-hook floor (82.2%, n=45): -13.3 pts.**
  This run does not recover toward the floor — it reads further below it
  than either prior adapter reading did against this same corrected floor
  (2026-09-26: 82.2 - 70.5 = -11.7 pts; 2026-09-29: 82.2 - 75.0 = -7.2 pts).
- **vs. the 2026-09-29 overflow-fix reading (75.0%, n=44): -6.1 pts.**
- **vs. the 2026-09-26 pre-fix reading (70.5%, n=44): -1.6 pts.**

Both the 2026-09-19 and this run graded the full 45-probe non-abstention set
(no partial-run denominator mismatch this time), so no further "same probe
set" restriction is needed to compare against the shell-hook floor — the
45-probe headline above already is that comparison.

## Reading this against sampling noise

Two-sample standard error of the difference between independent proportions,
`sqrt(p1(1-p1)/n1 + p2(1-p2)/n2)`:

| comparison | SE (pts) | delta (pts) | delta / SE |
| --- | --- | --- | --- |
| 2026-09-19, corrected (82.2%, n=45) vs. this run (68.9%, n=45) | 9.0 | 13.3 | **1.49** |
| 2026-09-29 (75.0%, n=44) vs. this run (68.9%, n=45) | 9.5 | 6.1 | 0.64 |
| 2026-09-26 (70.5%, n=44) vs. this run (68.9%, n=45) | 9.7 | 1.6 | 0.16 |

The residual gap to the corrected shell-hook floor (1.49 SE) is the largest
of any reading taken so far on this arm. The move vs. the 2026-09-29 reading
itself (0.64 SE) and vs. the 2026-09-26 reading (0.16 SE) are both within a
single SE and could be sampling noise alone at this sample size — but the
trend across all four readings (82.2% -> 70.5% -> 75.0% -> 68.9%) is not
monotonically recovering, and this reading's gap to the floor, at ~1.5 SE,
is the strongest evidence yet that the gap is more than noise. **Read this
as: the gap has not closed, reads as more than noise alone at this sample
size, and did not narrow after athenaeum#1912's fix.** (The 2026-09-26 and
2026-09-29 records' own internal SE statements against the floor used the
uncorrected 36/45 figure and are not restated here; they are not edited.)

## Did the retrieval-parity proof translate into score? No.

athenaeum#1912 proved retrieval-parity between the adapter and the shell
hook offline (0/48 probes diverge after normalization, re-confirmed on this
exact tree above: 48/48 byte-identical). **That proof held completely in
this paid run's own environment, and the score did not recover — it moved
further from the shell-hook floor than any prior reading.** This is a clean
result in the sense that it rules out context-string divergence as the
explanation for the residual gap: the adapter and the shell hook are
sending the model byte-identical `additionalContext` on every core probe,
and the scores still diverge. Whatever produces the shell hook's higher
score on this corpus, it is not the breadcrumb text the model receives.

## Next suspected mechanism, from this run's own transcripts (no extra spend)

With context-string parity ruled out, the report's own `report_only`
"Marker miss with delivery" dimension (issue athenaeum#1842) gives a
transcript-grounded count for a different candidate: cells where the
answer-bearing page WAS delivered to the model, but the final answer still
did not carry the correct `answer_markers` value — a model-side miss on
already-correct retrieval, not a retrieval gap.

For `push_breadcrumb_pull`/`core`, by probe class:

| probe_class | n (gradable) | marker_miss_with_delivery |
| --- | --- | --- |
| aggregation | 3 | 2 |
| contradiction | 3 | 0 |
| disambiguation | 4 | 1 |
| distractor_robustness | 2 | 1 |
| follow_through | 6 | 0 |
| multi_hop | 3 | 0 |
| negative_knowledge | 3 | 0 |
| redundancy | 4 | 3 |
| single_hop | 8 | 1 |
| temporal | 6 | 0 |
| unprompted_push | 3 | 2 |

**10 of 45** non-abstention `push_breadcrumb_pull`/`core` cells show the
correct page delivered with the answer still missing the marker — out of
the 14 cells this run scored incorrect on that same 45-probe set, 10 (71%)
are accounted for by delivery-confirmed marker misses rather than a failure
to retrieve. `redundancy` (3/4 cells) and `aggregation`/`unprompted_push`
(2/3 cells each) carry the heaviest concentration.

This is the obligation athenaeum#1907 and athenaeum#1912 each placed on
their own re-measurements, carried into a new defect issue: athenaeum#1932.

## Disposition

- The score has NOT recovered to the corrected 82.2% shell-hook floor. The
  residual gap (-13.3 pts, ~1.49 SE) is the largest recorded across all four
  readings on this arm, and the 2026-09-19/2026-10-01 comparison uses the
  full 45-probe set on both sides — not a partial-run restriction.
- athenaeum#1912's retrieval-parity fix is confirmed present and re-verified
  byte-identical on this exact tree (48/48 core probes). It did not
  translate into a score improvement; if anything the score moved further
  from the floor than the pre-fix 2026-09-26 reading (68.9% vs. 70.5%,
  though this delta alone, 0.16 SE, is well within noise).
- Per this issue's acceptance criteria, a new defect issue is filed naming
  the next suspected mechanism grounded in this run's own transcripts:
  athenaeum#1932, built on the `marker_miss_with_delivery` counts above
  (10/45 `push_breadcrumb_pull`/`core` cells, correct page delivered,
  answer still missing the marker).
- Default hook path is unchanged (still the packaged adapter). This record
  does not revert it.
- This record does not edit `rollout-eval-adapter-cutover-2026-09-26.md`,
  `rollout-eval-adapter-cutover-2026-09-29.md`, or the 2026-09-19 baseline.
