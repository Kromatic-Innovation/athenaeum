# Rollout eval layer, packaged adapter — 2026-09-29 re-measurement

Issue athenaeum#1907 (carried out of athenaeum#1905's AC3 at ready-flip time).
Re-runs the same recipe as
`docs/measurements/rollout-eval-adapter-cutover-2026-09-26.md` against the
packaged adapter console script (`athenaeum-claude-hook`) now that
athenaeum#1905's fix — the adapter's `UserPromptSubmit` hook
(`src/athenaeum/claude_code_adapter.py`) now emits the same trailing overflow
notice the retired shell hook appends when more matches exist than fit the
breadcrumb budget — is merged (PR #1910, merge commit `0e504b6051`) and on
`develop`, `main`, and the deploy checkout
(`/Users/tristankromer/local-deploys/athenaeum`).

Operator authorization: TriKro's own comment on athenaeum#1907
("I approve the spend.", comment
[5896714122](https://github.com/Kromatic-Innovation/athenaeum/issues/1907#issuecomment-5896714122)).

## Run command

```bash
export ANTHROPIC_API_KEY=...   # from ~/.cache/athenaeum/config.env, exported only into this subshell
python -m tests.evals.north_star_cli \
  --mode api --scale full --corpus-scales core --replicates 0 \
  --search-backend vector --max-spend 4.5 --workers 4 \
  --store <scratch>/store-adapter.jsonl \
  --out-dir <scratch>/report --materialize-root <scratch>/materialize
```

- Repo/worktree: `dijkstra/1907-rollout-eval-remeasure`, git sha `0e504b605122`
  (branched off `origin/develop`; same commit as PR #1910's merge — confirmed
  by `git merge-base --is-ancestor` against `origin/main`, `origin/develop`,
  and the deploy checkout `HEAD` before spending anything).
- Hook resolution confirmed via `tests.evals.rollout.resolve_user_prompt_hook()`
  in-process: resolved to `<worktree>/.venv/bin/athenaeum-claude-hook`, an
  editable install of this worktree's `src/athenaeum` (Python 3.13 venv,
  required by this repo's `pyproject.toml`) — not the host's stale
  `athenaeum` 0.15.0 PATH shim.
- `ATHENAEUM_EVAL_HOOK` left unset — the default adapter path.
- No `~/knowledge` write: `--materialize-root`/`--store`/`--out-dir` all
  pointed at a scratch directory outside this repo and outside
  `~/knowledge`; confirmed the eval harness materializes its own throwaway
  knowledge root per cell.

## Zero-cost diagnostic run first (no spend, no LLM calls)

Before any paid call, `build_push_breadcrumb_context` was run against
`build_corpus("core")`'s 48 probes with the hook env unset (adapter) and
with `ATHENAEUM_EVAL_HOOK=shell` (shell), same diagnostic the 2026-09-26
record ran before the #1905 fix:

- **Adapter probes carrying the overflow notice: 27/48.** Shell-hook probes
  carrying the same notice: 25/48. Before the fix (2026-09-26 record), the
  adapter carried the notice on **0/48** probes.
- This confirms the mechanism #1905 diagnosed is now present in the code
  this run executed, before any budget was spent on the live measurement.
  The residual byte differences between adapter and shell context strings
  (14/48 exactly byte-identical after stripping a trailing-newline
  formatting difference) are not the overflow-notice gap #1905 fixed; no
  further investigation of them was in scope for this issue.

## Result: partial run, stopped by the self-imposed cap

Same as the 2026-09-26 run, the run's own token-ceiling guard (derived from
`--max-spend 4.5`) tripped mid-grid: **376 of 384 planned cells completed**
(47 of 48 core probes across all 8 arms). This is
`tests.evals.containment.SpendCeilingExceededError` firing as designed, not
a crash.

- **Run id:** report generated `2026-09-29T19:16:34Z`, `git_sha: 0e504b605122`,
  corpus digest `edcd8dd3286d0135`, grader revision athenaeum#1843. Report:
  `north-star-2026-09-29-2.md` (generated in scratch, not committed — same
  convention as 2026-09-26, since it carries per-cell probe/answer detail
  this record intentionally omits).
- **Token usage:** the harness's own running total, which tripped the
  ceiling, was **2,730,135 tokens** (ceiling: 2,710,526, derived from
  `--max-spend 4.5` at `claude-haiku-4-5-20251001` pricing). Summing each
  persisted cell's own `turn_tokens` gives 2,568,887 input / 101,780 output
  = 2,670,667 tokens — the harness's own trip figure is the authoritative
  one; the per-cell sum is a lower-bound cross-check (the gap is the one
  in-flight group the harness's own error message says can overshoot by up
  to one cell per worker before every worker stops).
- **Spend:** approximately **$3.08–$3.15** at `claude-haiku-4-5-20251001`
  list rates ($1/$5 per MTok input/output), computed both from the harness's
  own tripped total and from the per-cell sum above. Well inside the
  operator's $5 cap on this issue (comment 5896714122) and the $5
  self-imposed cap Occam's disposition recorded (comment 5896747520);
  comparable to the 2026-09-26 run's $3.14.
- Harness failures: 7, all `turn_cap` (3 `native_grep`, 3 `pull`, 1
  `push_breadcrumb_pull`) — one more than the 2026-09-26 run had on the
  push arms (that run had none on the two push arms; this run has one on
  `push_breadcrumb_pull`). That one `turn_cap` cell is excluded from the
  gradable denominator below the same way the reader already treats it (a
  harness failure, not a graded yes/no).

## Scores: 2026-09-29 re-measurement vs. both prior readings, `core` scale

Correctness tallied the same way as the 2026-09-26 record (parsed from the
per-probe-class correctness table, `core` scale only, `push_breadcrumb` /
`push_breadcrumb_pull` rows; `n/a` rows are abstention probes graded outside
a plain yes/no and excluded from the rate below, same denominator convention
all three runs use):

| arm | shell hook (2026-09-19) | packaged adapter, pre-fix (2026-09-26) | packaged adapter, post-#1905 fix (2026-09-29) |
| --- | --- | --- | --- |
| `push_breadcrumb` | 0/45 (0.0%) | 0/44 (0.0%) | 0/44 (0.0%) |
| `push_breadcrumb_pull` (**verdict arm**) | 36/45 (**80.0%**) | 31/44 (**70.5%**) | **33/44 (75.0%)** |

Deltas for `push_breadcrumb_pull`, the verdict/shipped arm:

- **vs. the 2026-09-26 pre-fix adapter reading (70.5%): +4.5 pts.** The
  score moved in the direction #1905 predicted.
- **vs. the 2026-09-19 shell-hook reading (80.0%): -5.0 pts.** The adapter
  still reads below the recorded shell-hook floor after the fix.

`push_breadcrumb` itself is unchanged (0% across all three runs), carrying
no signal either way, same as both prior records note.

## Reading this against sampling noise

At n=44-45 gradable probes, a one-cell flip is roughly a 2.2-2.3-point swing;
a rough binomial-proportion standard error at these sample sizes and
observed rates is on the order of ±6-7 points. The original 9.5-point drop
(80.0% -> 70.5%) was itself within roughly 1.5 SE of noise, and this run's
4.5-point recovery (70.5% -> 75.0%) is within roughly 1 SE. Read the
`push_breadcrumb_pull` trajectory as a partial, direction-consistent
recovery, not as proof the #1905 fix fully closes the gap: **the adapter has
not recovered to the 80.0% shell-hook floor**, and the residual 5.0-point
gap is large enough, and the sample small enough, that a single re-run
cannot distinguish "a smaller residual real effect" from "noise alone."

## Disposition

- The #1905 fix is confirmed present in the code this run executed (byte-diff
  diagnostic above) and the shipped verdict arm's score moved toward the
  shell-hook floor (+4.5 pts) but did not reach it (still -5.0 pts vs.
  80.0%).
- Per this issue's acceptance criteria, since the score has not recovered to
  the 80.0% shell-hook floor, a separate defect issue names the next
  suspected mechanism: see the companion issue filed alongside this record
  for what beyond the overflow notice might still differ between the
  adapter and shell-hook context assembly (or scoring/sample-noise
  considerations) on the `push_breadcrumb_pull` arm.
- Default hook path is unchanged (still the packaged adapter). This record
  does not revert it.
- This record does not edit `rollout-eval-adapter-cutover-2026-09-26.md` or
  the 2026-09-19 baseline.
