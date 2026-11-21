<!-- SPDX-License-Identifier: Apache-2.0 -->

# Recall pull rate under real host config: tool deferral and a competing plugin, 2026-11-21

Issue athenaeum#3057, a blocker of athenaeum#3032.

**Result in one line.** On task-shaped probes `recall` is called in about 99% of
runs in all three configurations, so neither tool deferral nor a coding plugin
lowers the pull rate there. On the `unprompted_push` class, where the task never
mentions memory, the pull rate is low everywhere (0.33 clean, 0.42 deferred) and
lowest with the plugin (0.14); nothing in this class was answered correctly in
any configuration, for a reason that is not the host (section 4).

**Correction, same day.** The first version of this page was wrong for
`unprompted_push`. The probe sessions ran with the repository as their working
directory, and on those coding-style tasks the agents explored and edited it
(section 2, "Working directory"). The first `unprompted_push` row is superseded
by a clean re-run below.

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

**Working directory.** The first run spawned `claude -p` with no `cwd`, so every
probe session ran inside the repository checkout. On `unprompted_push` tasks
("make X synchronous ... adjust the implementation now") the agents went to work
on the repo: reproduced in a disposable clone, they edited the same corpus,
source and requirements files that showed up in the checkout, with 3, 3 and 27
write-tool calls in A, B and C (24 of C's from the plugin's Edit tool). They cost
about three times as much per run as in an empty directory. Every pull-family
probe session now runs in a fresh, empty directory (`_scratch_cwd`, with a
test). The native-arm spawns in `rollout.py` have the same gap and were left
alone, because changing them changes what the grid measures.

**Metrics.** Recall rate is the share of runs with at least one `recall` call.
`ToolSearch` before first recall is averaged over runs that called `recall`.
Cost is the stream's `total_cost_usd`; cost per correct is summed cost over kept
runs divided by the number of correct runs.

## 3. Results

**Task-shaped probes** (45 probes, 3 replicates each; 4 runs excluded: permission
requests). These ran with the repository as working directory (see above). Recall
is saturated, so it cannot have been inflated, and correctness needs the answer
page delivered through `recall`, but cost may include some wandering and these
rows were not re-run.

| config | runs | recall rate | ToolSearch before first recall | correct rate | cost per correct |
| --- | ---: | ---: | ---: | ---: | ---: |
| A. clean | 134 | 0.993 | 0.00 | 0.522 | $0.050 |
| B. deferred | 133 | 0.985 | 1.00 | 0.526 | $0.054 |
| C. deferred + plugin | 134 | 0.993 | 1.00 | 0.537 | $0.056 |

**`unprompted_push`** (12 probes, 3 replicates each, fresh empty working
directory, corpus checked identical to `origin/develop` before the run; no
exclusions). Supersedes the earlier row of 0.457 / 0.333 / 0.353.

| config | runs | recall rate | ToolSearch before first recall | correct rate | tool calls besides `recall`/`ToolSearch` (plugin tools) | cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A. clean | 36 | 0.333 (12/36) | 0.00 | 0.000 | 64 (0) | $1.46 |
| B. deferred | 36 | 0.417 (15/36) | 1.07 | 0.000 | 55 (0) | $1.33 |
| C. deferred + plugin | 36 | 0.139 (5/36) | 1.00 | 0.000 | 60 (29) | $1.25 |

No file-writing tool was called in any clean run. Tool calls other than `recall`
were made in every configuration (mostly `Bash` and `Read` looking for code that
is not there); 29 of C's 60 came from the plugin's tools.

## 4. Verdict

**Deferral does not explain the drop.** On task-shaped probes the pull rate is
0.985 to 0.993 with or without it, at a cost of one `ToolSearch` call. On
`unprompted_push`, B (0.417) is not below A (0.333).

**The plugin may.** On `unprompted_push` the plugin configuration pulls least:
5/36 against 12/36 clean (Fisher exact p = 0.09) and 15/36 deferred (p = 0.017).
That is suggestive, on one small model and 12 probes, and the plugin's hook and
account behavior were not confirmed, so treat it as a lead worth a larger sample,
not a finding. It is also a large part of the effect: in C the model reached for
the plugin's search tools (29 calls) instead of `recall`.

**Why nothing in `unprompted_push` was correct, in any configuration.** Read from
graded rows: the task asks for a code change, and the answer is a buried decision
page that conflicts with it, reachable only by following a link from the system
page. The push shows the system page plus decoy "safe to change" notes, never the
decision; the agent recalls the system page or the decoy and never reaches the
decision, so the grader (which requires the decision page delivered and its fact
in the answer) correctly returns false. The empty working directory adds a second
effect: with no code present the agent asks the user for the code. The cause is
the push and follow-through design of the class, not the grader, the corpus, or
the host configuration. The scratch environment's hook also ran without a cached
API key and fell back to regex topic extraction, which may weaken the push; the
CI grid's hook setup was not checked.

Limits: one small model, fts5 retrieval, an invented corpus. The real-session gap
of about 6% is still mostly unexplained.

**What it means for athenaeum#3032.**

- A hint that names `recall` can assume deferral costs one turn, not a pull.
- Forcing tools upfront is not a useful secondary fix.
- The grid's 1.0 is an upper bound for questions that name their subject. A lever
  target should be set on tasks like `unprompted_push`, where the clean baseline
  here is 0.33 to 0.42, and where correctness is 0 until the push or the agent
  reaches the linked decision page.
- A competing plugin is the one host difference worth a bigger sample.

## 5. Reproducing

Needs a logged-in `claude` under an isolated `CLAUDE_CONFIG_DIR`
(`tests/evals/README.md`, "Running the cli-mode spot-check") and, for C, a
coding plugin installed locally. Costs about $26 for the task-shaped probes plus $4 for `unprompted_push`. Output is
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
measurement above is from runs on 2026-11-21; per-run rows are not committed.
