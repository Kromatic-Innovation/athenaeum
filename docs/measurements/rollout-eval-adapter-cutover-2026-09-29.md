# Rollout eval layer, packaged adapter — 2026-09-29 re-measurement

Issue athenaeum#1907 (carried out of athenaeum#1905's AC3 at ready-flip time).
Re-runs the same recipe as
`docs/measurements/rollout-eval-adapter-cutover-2026-09-26.md` against the
packaged adapter console script (`athenaeum-claude-hook`) now that
athenaeum#1905's fix — the adapter's `UserPromptSubmit` hook
(`src/athenaeum/claude_code_adapter.py`) now emits the same trailing overflow
notice the retired shell hook appends when more matches exist than fit the
breadcrumb budget — is merged (athenaeum#1910, merge commit `0e504b6051`) and
on `develop`, `main`, and the deploy checkout.

Operator authorization: the operator's comment on athenaeum#1907
("I approve the spend.", comment 5896714122). The $5 self-imposed cap on this
run (same as the 2026-09-26 run's cap) is Occam's disposition, recorded on
athenaeum#1907 (comment 5896747520) — the operator's own comment approves
the spend but does not itself state a dollar figure.

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
  (branched off `origin/develop`; same commit as athenaeum#1910's merge —
  confirmed by `git merge-base --is-ancestor` against `origin/main`,
  `origin/develop`, and the deploy checkout `HEAD` before spending anything).
- Hook resolution confirmed via `tests.evals.rollout.resolve_user_prompt_hook()`
  in-process: resolved to `<worktree>/.venv/bin/athenaeum-claude-hook`, an
  editable install of this worktree's `src/athenaeum` (Python 3.13 venv,
  required by this repo's `pyproject.toml`) — not the host's stale
  `athenaeum` 0.15.0 PATH shim.
- `ATHENAEUM_EVAL_HOOK` left unset — the default adapter path.
- No `~/knowledge` write: confirmed by reading `north_star_cli.py`'s
  `materialize_root` handling — when `--materialize-root` is omitted it
  falls back to a fresh `tempfile.mkdtemp(prefix="athenaeum-north-star-")`,
  never a `~/knowledge`-rooted default — and by pointing
  `--materialize-root`/`--store`/`--out-dir` all at a scratch directory
  outside this repo and outside `~/knowledge` for this run.
- Actual wall clock: this run completed in a few minutes, well under the
  `--dry-run` projection of 1:12:00 for the full 384-cell grid — the ceiling
  tripped early (see below), so the grid never ran anywhere near its
  projected length. A future dispatch sizing a `timeout` around the
  `--dry-run` projection alone would over-provision for this recipe.

## Zero-cost diagnostic run first (no spend, no LLM calls)

Before any paid call, `build_push_breadcrumb_context` was run against
`build_corpus("core")`'s 48 probes with the hook env unset (adapter) and
with `ATHENAEUM_EVAL_HOOK=shell` (shell), same diagnostic the 2026-09-26
record ran before the athenaeum#1905 fix, with a fresh `hook_home` directory
per probe per side (avoiding the shared-`SEEN_FILE`/session-dedup bias the
byte-equivalence spike test's own docstring warns a reused home can
introduce):

- **Overflow-notice presence now agrees on every probe.** Across all 48
  probes, there is no case where one side carries the notice and the other
  does not (`notice_only_adapter: 0`, `notice_only_shell: 0`). Before the
  fix (2026-09-26 record), the adapter carried the notice on 0/48 probes
  while the shell hook carried it on more than that — the gap athenaeum#1905
  diagnosed is closed by presence/absence.
- The two context strings are still not byte-identical on any of the 48
  probes. Classified into buckets:
  - **30/48 — the bullet list itself differs** (page names and/or order
    differ between adapter and shell hook, independent of the overflow
    notice).
  - **18/48 — both sides carry the notice, but its text differs** (e.g. the
    withheld-count or category breakdown differs between the two hooks'
    computations).
  - **0/48 — notice present on only one side.**
  - **0/48 — whitespace-only difference.**
  - **0/48 — byte-identical.**
- Reading: the athenaeum#1905 fix closed the specific gap it targeted (the
  notice's presence/absence), but two other, separate divergences remain
  between the adapter and shell-hook context assembly — the bullet-list
  content/ordering (30/48 probes) and the notice's own text when both sides
  emit one (18/48 probes). Neither was diagnosed or fixed by athenaeum#1905;
  both are candidate mechanisms for the residual score gap below, and are
  carried into the defect issue this record's disposition files.

## Result: partial run, stopped by the self-imposed cap

Same as the 2026-09-26 run, the run's own token-ceiling guard (derived from
`--max-spend 4.5`) tripped mid-grid: **376 of 384 planned cells completed**
(47 of 48 core probes across all 8 arms; the 48th probe, `invoiceref_rename`,
was still in flight when the ceiling tripped and was not persisted for any
arm). This is `tests.evals.containment.SpendCeilingExceededError` firing as
designed, not a crash.

- **Run id:** report generated `2026-09-29T19:16:34Z`, `git_sha: 0e504b605122`,
  corpus digest `edcd8dd3286d0135`, grader revision athenaeum#1843. Both the
  corpus digest and grader revision match the 2026-09-19 shell-hook record
  exactly, confirmed by reading that record's own header — this run and the
  2026-09-19 baseline graded the same probe set with the same grader.
  Report: `north-star-2026-09-29-2.md` (generated in scratch, not committed —
  same convention as 2026-09-26, since it carries per-cell probe/answer
  detail this record intentionally omits).
- **Token usage:** the harness's own running total, which tripped the
  ceiling, was **2,730,135 tokens** (ceiling: 2,710,526, derived from
  `--max-spend 4.5` at `claude-haiku-4-5-20251001` pricing). Summing each
  persisted cell's own `turn_tokens` gives 2,568,887 input / 101,780 output
  = 2,670,667 tokens; the gap between that sum and the harness's tripped
  total is the in-flight cell(s) the harness's own error message says can
  overshoot by up to one cell per worker before every worker stops.
- **Spend:** computed two ways. Scaling the harness's own tripped total
  (2,730,135 tokens) by the persisted cells' input/output token ratio
  (96.2%/3.8%) and pricing at `claude-haiku-4-5-20251001` list rates
  ($1/$5 per MTok input/output) gives **~$3.15**. Pricing the persisted
  per-cell sum directly (2,568,887 input / 101,780 output) gives **~$3.08**.
  Both are well inside the operator's approved spend and Occam's $5 cap;
  comparable to the 2026-09-26 run's $3.14.
- Harness failures: 7, all `turn_cap` (3 `native_grep`, 3 `pull`, 1
  `push_breadcrumb_pull` on probe `ratecard_tooling_owner`). That
  `push_breadcrumb_pull` `turn_cap` cell's recorded answer (a mid-turn tool
  preamble, not a graded response) is tallied as an incorrect ("no") answer
  in the report's per-probe-class table and in the correctness rate below —
  it is **not** excluded from the gradable denominator the way an
  abstention-class `n/a` row is. Excluding it instead (treating it as
  ungraded rather than incorrect) would read 33/43 = 76.7% rather than
  33/44 = 75.0%; the headline below keeps the same as-graded convention the
  2026-09-26 record uses.

## Scores: 2026-09-29 re-measurement vs. both prior readings, `core` scale

Correctness tallied the same way as the 2026-09-26 record (parsed from the
per-probe-class correctness table, `core` scale only, `push_breadcrumb` /
`push_breadcrumb_pull` rows; `n/a` rows are abstention probes graded outside
a plain yes/no and excluded from the rate below, same denominator convention
all three runs use):

| arm | shell hook (2026-09-19) | packaged adapter, pre-fix (2026-09-26) | packaged adapter, post-fix (2026-09-29) |
| --- | --- | --- | --- |
| `push_breadcrumb` | 0/45 (0.0%) | 0/44 (0.0%) | 0/44 (0.0%) |
| `push_breadcrumb_pull` (**verdict arm**) | 36/45 (**80.0%**) | 31/44 (**70.5%**) | **33/44 (75.0%)** |

Deltas for `push_breadcrumb_pull`, the verdict/shipped arm:

- **vs. the 2026-09-26 pre-fix adapter reading (70.5%): +4.5 pts.** The
  score moved in the direction the athenaeum#1905 fix predicted.
- **vs. the 2026-09-19 shell-hook reading (80.0%): -5.0 pts** (full
  denominators, 44 vs. 45). **Restricted to the 44 probes graded on both the
  2026-09-19 and 2026-09-29 runs** (same probe set, controlling for the
  different partial-run denominators): 2026-09-19 scores 36/44 (81.8%) on
  that set, 2026-09-29 scores 33/44 (75.0%) — a **-6.8 pt** delta on the
  identical probe set. The adapter still reads below the recorded shell-hook
  floor either way.

`push_breadcrumb` itself is unchanged (0% across all three runs), carrying
no signal either way, same as both prior records note.

## Reading this against sampling noise

At n=44-45 gradable probes, a one-cell flip is roughly a 2.2-2.3-point swing;
a rough binomial-proportion standard error at these sample sizes and
observed rates is on the order of ±6-7 points. The original 9.5-point drop
(80.0% -> 70.5%) was itself within roughly 1.5 SE of noise, and this run's
4.5-6.8-point recovery (depending on denominator choice above) is within
roughly 1 SE. Read the `push_breadcrumb_pull` trajectory as a partial,
direction-consistent recovery, not as proof the athenaeum#1905 fix fully
closes the gap: **the adapter has not recovered to the 80.0% shell-hook
floor**, and the residual gap is large enough, and the sample small enough,
that a single re-run cannot distinguish "a smaller residual real effect"
from "noise alone."

## Disposition

- The athenaeum#1905 fix is confirmed present in the code this run executed,
  and confirmed to close the specific presence/absence gap it targeted
  (0/48 probes now show the notice on only one side). The shipped verdict
  arm's score moved toward the shell-hook floor (+4.5 to +6.8 pts depending
  on denominator) but did not reach it (-5.0 to -6.8 pts vs. 80.0%).
- Two other divergences between the adapter and shell-hook context strings
  remain unexplained by athenaeum#1905: the bullet list itself differs on
  30/48 probes, and the overflow notice's own text differs on 18/48 probes
  where both sides emit one. Per this issue's acceptance criteria, since the
  score has not recovered to the 80.0% shell-hook floor, a separate defect
  issue (athenaeum#1912) names these as the next suspected mechanisms.
- Default hook path is unchanged (still the packaged adapter). This record
  does not revert it.
- This record does not edit `rollout-eval-adapter-cutover-2026-09-26.md` or
  the 2026-09-19 baseline.
