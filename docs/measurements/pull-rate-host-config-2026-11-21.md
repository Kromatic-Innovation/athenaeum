<!-- SPDX-License-Identifier: Apache-2.0 -->

# Recall pull rate under real host config: tool deferral and a competing plugin, 2026-11-21

Issue athenaeum#3057, a blocker of athenaeum#3032.

**Result in one line.** Neither tool deferral nor a coding plugin lowers the
pull rate on task-shaped probes. On those, `recall` is called in about 99% of
runs in all three configurations. The only place the pull rate drops is the
`unprompted_push` class, where the task never mentions memory; that drop is
about the same in all three configurations, so it comes from the class, not
the host configuration.

## 1. The question

The north-star grid's `push_breadcrumb_pull` arm calls `recall` on every task
(pull-arm no-call rate 0.000 in
`native-memory-baseline-2026-11-20-regrade.md`). Real interactive sessions pull
from any memory store in about 6% of pushed windows
(`pull-diagnosis-2026-11-21.md`). Two host differences could explain the gap:

1. **Tool deferral.** In a real session `recall` sits behind `ToolSearch` with
   every other MCP tool.
2. **A competing coding plugin.** Such a plugin loads its own search tools
   upfront, nudges the model toward them before each tool call, and ships a
   skill with a similar name.

## 2. What was run

Same arm (`push_breadcrumb_pull`), same `core` probes (57) and same grading
(`grade_correctness`) as the grid, through a real `claude -p`, three replicates
per probe and configuration, model `claude-haiku-4-5` (the harness default), fts5
retrieval backend. Harness: `tests/evals/host_config_cli.py`.

| config | MCP tools | `ToolSearch` | coding plugin |
| --- | --- | --- | --- |
| A. clean | athenaeum only | forced off (`ENABLE_TOOL_SEARCH=false`), tools upfront | off |
| B. deferred | athenaeum plus 36 invented stub tools over three servers | forced on, tools deferred | off |
| C. deferred + plugin | as B, plus the plugin's MCP server | forced on | on (`--plugin-dir` at a scratch copy) |

**What the grid itself ran with.** The CI grid dispatches `north_star_cli.py
--mode api` (`.github/workflows/evals.yml`): a Messages API tool-use loop in
which `recall` is simply one of the supplied tools. There is no `claude -p`,
no `ToolSearch` and no deferral in that path, so the grid's 0.000 no-call rate
was measured with `recall` upfront. Row A reproduces that condition in the real
CLI; it is a fresh run beside B and C, not the regrade's number.

**Finding that changed the design.** A first probe run left the host's own
`ToolSearch` default in A, and every run reported `ToolSearch` before `recall`.
The raw stream shows why. Current Claude Code defers MCP tools by default, even
with athenaeum as the only server (a `ToolSearch` result reported 38 deferred
tools). The `init` event's `tools` list names deferred tools too, so it cannot
tell upfront from deferred. A (`ENABLE_TOOL_SEARCH=false`) is therefore a
deliberate setting here, and "default host" is closer to B than to A. An
earlier wiring check that read the `init` list wrongly excluded every B and C
run; it was replaced as follows.

**Wiring check.** Per run, from the stream:

- A passes only if `ToolSearch` is absent and no `tool_reference` result
  appears.
- B and C pass only if `ToolSearch` is offered and, when `recall` was called, a
  `ToolSearch` result referencing `recall` came first. A B or C run that never
  called `recall` shows only that `ToolSearch` is offered, which is necessary
  but not proof; 49 of 337 B and C runs are in that state, all recall-free.

**Config C.** `--strict-mcp-config` ignores servers a plugin declares, so the
plugin's server is added to the generated config under the name Claude Code
gives it, with `alwaysLoad` kept so its tools stay upfront (a check: 4 fewer
deferred tools with it than without). The stream's `init` event confirmed the
plugin and its skills loaded and its server connected. Not verified: whether
the plugin's `PreToolUse` hook fired, or whether its tools work without a
plugin account. In the one raw stream inspected, the model used `ToolSearch`
only to load `recall`; plugin tool use was not counted across the run.

**Metrics.** Recall rate is the share of runs with at least one `recall` call.
`ToolSearch` before first recall is averaged over runs that called `recall`.
Cost is the stream's `total_cost_usd`; cost per correct is summed cost over kept
runs divided by the number of correct runs.

## 3. Results

All 57 probes, 3 replicates each (6 of 513 runs excluded: five answers that
ended on a permission request, one timeout).

| config | runs (excluded) | recall rate | ToolSearch before first recall | correct rate | cost | cost per correct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A. clean | 169 (2) | 0.882 | 0.00 | 0.414 | $8.45 | $0.121 |
| B. deferred | 169 (2) | 0.846 | 1.00 | 0.414 | $8.39 | $0.120 |
| C. deferred + plugin | 168 (2) | 0.863 | 1.01 | 0.429 | $8.82 | $0.123 |

The all-probe rate mixes two populations. Split by whether the task is shaped
like a question the memory answers:

| subset | config | runs | recall rate | correct rate | cost per correct |
| --- | --- | ---: | ---: | ---: | ---: |
| task-shaped (45 probes) | A | 134 | 0.993 | 0.522 | $0.050 |
| | B | 133 | 0.985 | 0.526 | $0.054 |
| | C | 134 | 0.993 | 0.537 | $0.056 |
| `unprompted_push` (12 probes) | A | 35 | 0.457 (16/35) | 0.000 | n/a |
| | B | 36 | 0.333 (12/36) | 0.000 | n/a |
| | C | 34 | 0.353 (12/34) | 0.000 | n/a |

`unprompted_push` probes are coding-style tasks whose answer lives in a memory
page the task text never asks about. Correctness there is 0 in every
configuration, so cost per correct is undefined; the grader needs the decision
page to be delivered and cited.

## 4. Verdict

**Neither deferral nor the plugin explains the drop.** On task-shaped probes the
pull rate is 0.985 to 0.993 in all three configurations, and correctness and
cost per correct move by no more than the noise of three replicates. Deferral
costs exactly one `ToolSearch` call before the first `recall` (mean 1.00) and
nothing measurable in pulls or correctness. The plugin adds nothing on top of
that.

**The drop sits with the task, not the host.** Where the pull rate falls, on
`unprompted_push`, it falls in the clean configuration too (0.457). B and C are
lower (0.333, 0.353), the direction deferral would predict, but 16/35 against
12/36 is not distinguishable at this sample size; treat it as a lead, not a
finding. It does not close the gap to the real-session figure of about 6%
either, so most of that gap is still unexplained by anything tested here.

Limits: one small model, fts5 retrieval, an invented corpus, and a plugin whose
hook and account behavior was not confirmed. The real-session gap may still
involve the plugin's live nudges or relevance of the pushed pages, which this
harness does not reproduce.

**What it means for athenaeum#3032.**

- The hint that names `recall` can be written for a deferred tool without
  expecting a pull penalty from deferral: say the callable name and that it may
  need loading. The cost is one extra turn, not a lost pull.
- Deferral is not a cheap secondary fix. Forcing tools upfront (A) did not raise
  the pull rate over B or C on task-shaped probes, and it raised nothing on
  `unprompted_push` beyond noise.
- The grid's 1.0 pull rate is an upper bound for questions that name their
  subject, not a prediction for real sessions. A target for the lever should be
  set on tasks like `unprompted_push`, where the baseline is 0.33 to 0.46 here,
  not against 1.0.

## 5. Reproducing

Needs a logged-in `claude` under an isolated `CLAUDE_CONFIG_DIR`
(`tests/evals/README.md`, "Running the cli-mode spot-check") and, for C, a
coding plugin installed locally. Costs about $26 for the full run. Output is
resumable: a rerun with the same `--out` skips rows already recorded.

```sh
CLAUDE_CONFIG_DIR=/path/to/isolated-claude-config \
  python -m tests.evals.host_config_cli \
  --configs A,B,C --plugin-path /path/to/installed/plugin \
  --replicates 3 --max-spend 25 --search-backend fts5 --out host-config.jsonl
```

For a quick wiring check first, add
`--probes pto_allowance,bluewater_terms,spend_approver_named,office_address_current`
and `--max-spend 5`.

## Method

Offline tests (`tests/evals/test_host_config_cli.py`) pin the argv and
MCP-config shape, that the default (no host config) is unchanged, the
`ToolSearch` counting, the stream-based wiring check and the aggregation. The
measurement above is from one run on 2026-11-21; per-run rows are not committed.
