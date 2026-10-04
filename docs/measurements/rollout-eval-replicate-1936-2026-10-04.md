---
title: "Rollout eval layer, paired replicate design — 2026-10-04"
---

# Rollout eval layer, paired replicate design — 2026-10-04

Issue athenaeum#1936 (paired-replicate re-run of the single-reading adapter-vs-shell
gap, second re-run attempt after two prior attempts stopped at preflight). Operator
ruling: "1936: Approved for subscription budget" (comment 5982996041 on the issue),
authorizing the `claude-cli` (subscription) backend only — no metered
`ANTHROPIC_API_KEY` spend. Precondition athenaeum#1951 (eval-only tool passthrough
for the subscription backend) merged and shipped to `main`; its own preflight
blocker on this issue (bridge child crash under `mcp==2.2`, comment 5983087687) was
resolved by athenaeum#1953 / PR athenaeum#1955 (`mcp<2.0` pin), merged to `main` at
`23f86456`, and present in the deploy checkout used for this run.

## Why a replicate design

Every earlier reading in this file family (`rollout-eval-adapter-cutover-*.md`) is a
single sample per arm — one shell-hook run, one adapter run, no replication. The
largest observed gap (13.3 points, `push_breadcrumb_pull`/`core`) could be a real
adapter regression or ordinary run-to-run model noise; a single pair cannot tell the
two apart. This run replicates each hook 4 times, interleaved, and applies the
pre-registered decision rule posted on the issue (comment 5979136004) before any
replicate ran, to decide which it is.

## Run command and design

```bash
env -u ANTHROPIC_API_KEY ATHENAEUM_LLM_PROVIDER=claude-cli [ATHENAEUM_EVAL_HOOK=shell] \
  python -m tests.evals.north_star_cli \
  --mode api --cli-tool-passthrough \
  --scale full --corpus-scales core --search-backend vector \
  --max-spend 4.80 \
  --store <store>/<hook>-r<N>.jsonl --out-dir <store>/<hook>-r<N>-report \
  --claude-binary claude
```

- Deploy checkout: `~/local-deploys/athenaeum`, `main` @ `23f8645614a1`
  (`git_sha` recorded in every generated report). Dependency versions actually
  loaded by the checkout's `.venv` (not the PATH shim): `mcp==1.30.0`,
  `fastmcp==3.4.8` — both satisfy the `mcp>=1.24,<2.0` / `fastmcp>=2.0.0,<4.0` pin
  from athenaeum#1953. `tests/test_cli_tool_bridge_mcp_api_guard.py` and
  `tests/test_cli_tool_bridge_roundtrip.py` (4 tests) pass under this checkout.
- Provider: `claude-cli` (subscription backend), `ANTHROPIC_API_KEY` unset on every
  invocation (`env -u ANTHROPIC_API_KEY`, never read or logged per
  `docs/modules/provider.md`'s transport design). `--cli-tool-passthrough` enabled
  for every replicate — required for `--mode api` under `claude-cli` since
  athenaeum#1951; refused with exit code 2 before any cell runs otherwise.
- Hook selection: `ATHENAEUM_EVAL_HOOK=shell` for the 4 shell replicates (resolves
  to `examples/claude-code/user-prompt-recall.sh`, confirmed in-process via
  `tests.evals.rollout.resolve_user_prompt_hook()`), left unset for the 4 adapter
  replicates (resolves to `.venv/bin/athenaeum-claude-hook`, the packaged console
  script, the live path since the athenaeum#1361 cutover). Hook choice is a
  per-invocation environment variable, not a per-row field, so it is recorded here
  and in each invocation's own store filename / driver log line rather than in the
  result rows themselves.
- Design: `--scale full --corpus-scales core --search-backend vector`, matching the
  backend/search-backend of every prior reading in this family. 4 replicates per
  hook, interleaved shell/adapter/shell/adapter/.../shell/adapter (one continuous
  session, not scheduled as 4+4 blocks), one host, one driver script
  (`driver_1936.sh`) logging START/END lines to `driver.log` for each of the 8
  invocations. `--max-spend 4.80` kept as the token-ceiling knob only — the
  subscription backend meters tokens, not dollars
  (`tests.evals.containment.tokens_for_spend(4.80, model="claude-haiku-4-5-20251001")`
  = 2,880,000 tokens per replicate).
- Store directory: `~/.local/share/athenaeum-eval-stores/1936-rerun-2026-10-04/`
  (a new dated directory; the invalid first attempt's stores under
  `.../1936/` and the empty preflight artifact from the second attempt are left
  untouched/removed as directed, not edited).

## Preflight (step 1 of the lane brief)

Smallest possible `push_breadcrumb_pull`/`core` cell, `--scale smoke
--corpus-scales core`, same provider/passthrough/unset-key configuration as above.
Result: completed without the bridge raising `CliToolBridgeError` (the guard that
raises on `apiKeySource != "none"` or any `hook_started` event — see
`docs/modules/provider.md`'s transport section; the guard runs inside the bridge
itself, there is no row-level `apiKeySource` field to print, so this is reported as
"guard executed, run did not raise" rather than a directly observed value). The
`push_breadcrumb_pull`/`core` row recorded:

```
tool_calls = [{'name': 'mcp__athenaeum__recall', 'query': 'firm PTO allowance'}]
recall_called = True
harness_failure = None
llm_provider = 'claude-cli'
mode = 'api'
```

This is the condition the lane's brief required before proceeding to replicates:
a recorded `recall` tool call, under the claude-cli provider, with no raised guard
error.

## Result: all 8 replicates completed in full — no truncation, no exclusions

All 8 invocations (4 shell, 4 adapter) ran to completion with exit code 0, in
continuous interleaved sequence on one host, 2026-10-04T19:55:08Z –
2026-10-04T22:24:04Z (about 2h29m total; this spans the 20:00–22:30Z window, not
"one day" in the sense of calendar date boundaries — flagged for the operator since
the design's "one host on one day" language is satisfied but the literal runtime
crossed no midnight boundary to worry about). No replicate tripped the
2,880,000-token ceiling (`SpendCeilingExceededError` never raised; actual per-
replicate usage ran 1.65M–1.69M tokens, about 58% of the ceiling) and every
replicate persisted all 384 planned cells. Per the pre-registered edge case, a run
that tripped its ceiling would have been excluded from the paired analysis and the
design requires at least 3 of 4 replicate-pairs per hook to complete in full for
the design to be adequately powered; this run has **4 of 4 for both hooks**, so
no power-grounds exclusion applies.

| Replicate | Hook | Cells persisted | Harness failures | push_breadcrumb_pull/core correct (of 45) |
| --- | --- | --- | --- | --- |
| r1 | shell | 384/384 | 3 | 37 |
| r1 | adapter | 384/384 | 8 | 39 |
| r2 | shell | 384/384 | 7 | 37 |
| r2 | adapter | 384/384 | 5 | 36 |
| r3 | shell | 384/384 | 8 | 38 |
| r3 | adapter | 384/384 | 6 | 40 |
| r4 | shell | 384/384 | 10 | 36 |
| r4 | adapter | 384/384 | 6 | 37 |

Harness failures are per-replicate `tests.evals.north_star_report` counts across
the full 384-cell grid (every arm, not just `push_breadcrumb_pull`); none of them
is a truncated/excluded replicate — every replicate still persisted its full 384
cells, and `grade_correctness` returning `None` on a harness-failure cell is
handled by `paired_regrade.py`'s existing (unchanged) grading path, not a defect in
this run.

**Token totals** (summed directly from each replicate's persisted `turn_tokens`,
across all 384 cells, all 8 arms):

| Replicate | Tokens (input / output) |
| --- | --- |
| shell-r1 | 1,668,765 (1,461,828 / 206,937) |
| adapter-r1 | 1,654,032 (1,457,815 / 196,217) |
| shell-r2 | 1,685,787 (1,488,799 / 196,988) |
| adapter-r2 | 1,671,753 (1,476,377 / 195,376) |
| shell-r3 | 1,689,457 (1,494,980 / 194,477) |
| adapter-r3 | 1,653,494 (1,463,624 / 189,870) |
| shell-r4 | 1,662,435 (1,467,112 / 195,323) |
| adapter-r4 | 1,693,180 (1,489,191 / 203,989) |
| **Total** | **13,378,903 (11,799,726 / 1,579,177)** |

No dollars were metered for these eval runs (subscription backend, `claude-cli`,
`ANTHROPIC_API_KEY` unset throughout). The token total's equivalent spend at
`claude-haiku-4-5-20251001` list rates ($1/$5 per MTok input/output) is **~$19.70**,
reported for cost-accounting comparability with the `api`-backend readings in this
file family, not as an actual charge.

Run ids: every report's `git_sha` is `23f8645614a1`, `corpus_digest[core]` is
`edcd8dd3286d0135`, `grader_revision` is `athenaeum#1935` — all three identical
across all 8 replicates and matching the prior readings in this family, so this
run compares on the same probe set and the same grader as every earlier one.

## Paired analysis (`tests/evals/paired_regrade.py`, McNemar)

Paired by probe within each of the 4 interleaved (shell, adapter) replicate pairs,
using the unchanged `grade_correctness` / `marker_miss_with_delivery` grading path
(no model call; reads only the already-persisted stores).

### Shell-hook floor check

Mean shell correctness across the 4 replicates: (37+37+38+36)/4 = **37.0/45
(82.2%)**, meeting the corrected athenaeum#1935 floor (37/45, 82.2%) exactly — no
shortfall, no possible shell-path regression to report.

### Aggregate 45-probe `push_breadcrumb_pull`/`core` rate

| | r1 | r2 | r3 | r4 | Mean |
| --- | --- | --- | --- | --- | --- |
| shell correct (of 45) | 37 | 37 | 38 | 36 | 37.0 (82.2%) |
| adapter correct (of 45) | 39 | 36 | 40 | 37 | 38.0 (84.4%) |

Mean adapter-vs-shell gap: **+1.0 points** (adapter minus shell, averaged per-pair
gap: +2, -1, +2, +1), i.e. the adapter reads very slightly *above* the shell hook
on average this time — the opposite direction from the single-reading 13.3-point
gap this issue was opened to investigate. Well under the 6-point noise threshold
either way.

Pooled McNemar (two-sided, continuity-corrected), over all 4 replicate pairs'
discordant cells: b (shell correct / adapter incorrect) = 6, c (shell incorrect /
adapter correct) = 10, pooled over 180 paired observations (4 x 45).
chi-squared = (|6-10|-1)^2 / 16 = **0.5625**, **p = 0.453** (uncorrected:
chi-squared = 1.0, p = 0.317). Both are far above 0.05.

**Verdict: within noise.** McNemar p >= 0.05 and mean gap <= 6 points both hold;
neither condition for "real adapter regression at the aggregate level" is met.

### Per-probe rule — the 3 residual probes named in athenaeum#1932

Counting pairs discordant in the regression direction only (shell correct /
adapter incorrect — `discordance.correct_to_incorrect` in `paired_regrade.py`'s
output), over the 4 paired replicates:

| Probe | Discordant in regression direction (of 4) | Verdict |
| --- | --- | --- |
| `gilcrest_vendor_decision` | 0 | within noise |
| `relay_sync_fix` | 1 (r2 only; discordant the *other* direction — adapter correcting a shell miss — in r1, r3 is concordant, r4 discordant the other direction) | within noise |
| `nightly_job_drop` | 0 | within noise |

All three probes named in the pre-registered rule score **within noise**: none
reaches the 2-of-4 ("inconclusive") or 3-/4-of-4 ("real adapter regression")
thresholds.

### Input equality (zero-spend diagnostic, same as athenaeum#1932)

Every `pushed_context` pair (48 of 48 in every one of the 4 replicate pairs) and
every paired `recall` tool-call input (14, 19, 15, 14 pairs across r1-r4, all
equal) matched byte-for-byte after `tests.evals.hook_divergence.normalize`,
confirming the shell and adapter hooks are feeding the model identical retrieval
inputs in this run — any correctness difference traces to model sampling noise
across turns, not to a divergence between the two hooks' delivered content.

## Verdict

**No real adapter regression, at either the aggregate or the per-probe level, in
this paired-replicate reading.** The single-reading 13.3-point gap that opened this
issue does not reproduce under replication: the 4-replicate mean gap is 1.0 points
in the adapter's favor, McNemar is non-significant (p = 0.45), the shell-hook floor
holds, and none of the three probes singled out by athenaeum#1932 shows a discordance
pattern consistent with a reproducible shell-vs-adapter difference. This reading is
consistent with the athenaeum#1932 finding (3/43 adapter-vs-adapter flip rate at zero spend)
that run-to-run model noise, not the adapter cutover, explains the earlier single
readings' spread.

The pre-registered decision rule posted on athenaeum#1936 (comment 5979136004) is
now applied to valid, fully-powered data (4 of 4 replicate-pairs complete for both
hooks) for the first time on this issue.

## Known differences (eval-only transport residuals, `docs/modules/provider.md`)

Two residuals of the `--cli-tool-passthrough` transport apply to every
`claude-cli`-backend row in this run (not to the `api`-backend readings elsewhere
in this file family):

- **2048-character tool-description truncation**, affecting `read_entity`'s
  declared tool spec when served through the bridge.
- **Native-arm tool names** appear to the model as `mcp__harness__grep` /
  `mcp__harness__read` rather than the bare `grep`/`read` name the `api` backend's
  own tool schema uses (`native_index`/`native_grep` arms only; every
  `CliToolLoopResult.events` entry is mapped back to the original spec name before
  it reaches the result store, so this residual never reaches the persisted rows
  used for grading above — it is a model-visible-name difference only).

Neither residual is fixed by athenaeum#1951/athenaeum#1953; both are recorded here per the
lane's instructions so a reader comparing this run against an `api`-backend reading
can account for them.

## See also

- Issue: athenaeum#1936
- Pre-registered decision rule: athenaeum#1936 comment 5979136004
- Prior single readings: `docs/measurements/rollout-eval-adapter-cutover-2026-09-26.md`,
  `docs/measurements/rollout-eval-adapter-cutover-2026-09-29.md`,
  `docs/measurements/rollout-eval-adapter-cutover-2026-10-01.md`
- Zero-spend discordance baseline: athenaeum#1932
- Tool-passthrough transport: `docs/modules/provider.md`
