# Rollout eval layer, packaged adapter — 2026-09-26

Issue athenaeum#1894 (AC2 of athenaeum#1887). First live-spend measurement of
the four/six-arm rollout runner (`tests/evals/rollout.py`, Layer 4 in
`tests/evals/README.md`) against the **packaged adapter** console script
(`athenaeum-claude-hook`), the default `UserPromptSubmit` hook since the
athenaeum#1361/#1887 cutover, compared against the last shell-hook run.

## Which layer, and what "floor" means here (read explicitly, per the dispatch)

This layer has no `pytest -m eval`/`-m rollout` aggregate-floor assertion the
way Layers 1-2 do — `tests/evals/README.md`'s own north-star grid report says
plainly "this is a measurement, not a regression gate." So "the recorded
floor" is read the way `tests/evals/README.md`'s PUSH_BREADCRUMB/
`ATHENAEUM_EVAL_HOOK` section frames it: the **last shell-hook score on the
same arm, scale, and probe set** —
`docs/measurements/north-star-grid-2026-09-19-run-35443944736.md` (workflow run
35443944736, 2026-09-19, `develop` @ `f7c00a0c4fbe`, which predates
`ATHENAEUM_EVAL_HOOK` and therefore ran the shell hook as the only hook that
existed then). The verdict/shipped arm per that report's design-doc §7 ruling
is `push_breadcrumb_pull` (breadcrumbs injected + `recall` tool available);
`push_breadcrumb` (breadcrumbs only, no tool) is single-shot and scores 0% in
both runs below, so it carries no signal either way.

## Run command

```bash
export ANTHROPIC_API_KEY=...   # from ~/.cache/athenaeum/config.env, exported only into this subshell
python -m tests.evals.north_star_cli \
  --mode api --scale full --corpus-scales core --replicates 0 \
  --search-backend vector --max-spend 4.5 --workers 4 \
  --store <scratch>/store-adapter.jsonl \
  --out-dir <scratch>/report --materialize-root <scratch>/materialize
```

- Repo/worktree: `dijkstra/1894-rollout-eval-score`, git sha `3ca766eccb57`
  (branched off `develop`).
- Hook resolution confirmed via `tests.evals.rollout.resolve_user_prompt_hook()`
  in-process: resolved to `<worktree>/.venv/bin/athenaeum-claude-hook`, an
  editable install of this worktree's `src/athenaeum` — not a stale PATH-shim
  binary (`athenaeum` 0.15.0 elsewhere on this host does not shadow it).
- `ATHENAEUM_EVAL_HOOK` left unset — the default adapter path, matching what
  a real `UserPromptSubmit` hook resolves to on this host today.
- No `~/knowledge` write: `--materialize-root`/`--store`/`--out-dir` all
  pointed at a scratch directory outside this repo and outside
  `~/knowledge`; the eval harness materializes its own throwaway
  knowledge root per cell and never touches the operator's real corpus.

## Result: partial run, stopped by the self-imposed cap

The run's own token-ceiling guard (derived from `--max-spend 4.5`) tripped
mid-grid: **376 of 384 planned cells completed** (47 of 48 core probes across
all 8 arms; one probe's group was still in flight when the ceiling tripped
and was not persisted). This is `tests.evals.containment.SpendCeilingExceededError`
firing as designed, not a crash — the partial report and result store are
both usable, and the operator's $5 cap on this issue was never approached
(see spend below), so no resume was dispatched.

- **Actual spend:** 2,729,403 tokens (2,625,982 input / 103,421 output),
  `claude-haiku-4-5-20251001` — **$3.14** at that model's list rate
  ($1/$5 per MTok). Well inside the operator's $5 cap on
  Kromatic-Innovation/athenaeum#1894 (comment 5849096799).
- Harness failures: 12, all `turn_cap` (7 `native_grep`, 3 `native_index`, 2
  `pull`) — none on the two push arms this record is about.
- Report: `north-star-2026-09-26.md` (generated in scratch, not committed —
  it carries per-cell probe/answer detail this record intentionally omits).

## Scores: adapter vs. the 2026-09-19 shell-hook run, `core` scale

Correctness tallied the same way in both reports (`grep`-parsed from each
report's per-probe-class correctness table, `core` scale only; `n/a` rows are
abstention probes graded outside a plain yes/no and are excluded from the
rate below, same denominator convention both runs use):

| arm | shell hook (2026-09-19) | packaged adapter (2026-09-26) | delta |
| --- | --- | --- | --- |
| `push_breadcrumb` | 0/45 (0.0%) | 0/44 (0.0%) | no change |
| `push_breadcrumb_pull` (**verdict arm**) | 36/45 (**80.0%**) | 31/44 (**70.5%**) | **-9.5 pts** |

**Reading: the adapter arm scores below the recorded floor** on the shipped
verdict arm (`push_breadcrumb_pull`, 70.5% vs. the shell hook's 80.0% on the
same probe set and scale). `push_breadcrumb` itself is unchanged (both 0%).
Per athenaeum#1894's own framing, this is a finding about the *adapter*, not
a reason to revert the athenaeum#1361/#1887 default — filed separately below.

## Zero-cost diagnostic run first (no spend, no LLM calls)

Before any paid call, `build_push_breadcrumb_context` was run against
`build_corpus("core")`'s 48 probes with the hook env unset (adapter) and with
`ATHENAEUM_EVAL_HOOK=shell` (shell), diffing the two context strings per
probe with no model involved:

- **0 of 48 probes were byte-identical.**
- The bullet list of page names/order was identical in every probe checked
  by hand. The consistent difference: the shell hook appends a trailing
  overflow notice —
  `"\n  - \nmemory has at least N more matching results (...) that were
  withheld by the relevance cap — call \`recall\` to see them.\n"` — whenever
  more matches existed than fit the breadcrumb budget; the adapter's output
  ends after the last bullet, with no such notice.

This is a plausible mechanism for the `push_breadcrumb_pull` score drop above:
without the "more results exist, call recall" cue, a model in the PULL arm
has one less signal that calling `recall` would surface additional pages.
Offered as a lead for whoever picks up the defect issue below, not asserted
as the proven cause — the live scores are the only causal evidence recorded
here.

## Disposition

- Adapter scores below the recorded shell-hook floor on the verdict arm →
  filed as its own defect: athenaeum#1905.
- Default hook path is unchanged (still the packaged adapter, per
  athenaeum#1887) — this record does not revert it.
- AC2 of athenaeum#1887 / the two ACs of athenaeum#1894 are satisfied by this
  record: the layer was run against the packaged adapter and the score is
  recorded here, next to the last shell-hook score, with date and run
  provenance.
