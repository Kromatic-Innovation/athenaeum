<!-- SPDX-License-Identifier: Apache-2.0 -->

# Recall pull rate under real host config: tool deferral and a competing plugin, 2026-11-21

Issue athenaeum#3057, a blocker of athenaeum#3032.

**Status: the harness is built and tested offline; the measurement has not been
run, and there is no verdict.** The run needs a logged-in `claude` under an
isolated config directory, and the session that built this change had none: a
fresh config directory answers `Not logged in`, and no API key or OAuth token
was available to it. Copying a login out of an operator's own configuration
into a scratch one was ruled out. So the three rows below say `pending`, and
this page does not guess at them.

## 1. The question

The north-star grid's `push_breadcrumb_pull` arm calls `recall` on every task
(pull-arm no-call rate 0.000 in
`native-memory-baseline-2026-11-20-regrade.md`). Real interactive sessions pull
from any memory store in about 6% of pushed windows
(`pull-diagnosis-2026-11-21.md`). Two host differences could explain the gap:

1. **Tool deferral.** In a real session `recall` sits behind `ToolSearch` with
   every other MCP tool. In the grid it is loaded upfront.
2. **A competing coding plugin.** Such a plugin loads its own search tools
   upfront, nudges the model toward them before each tool call, and ships a
   skill with a similar name.

## 2. What is run

Same arm (`push_breadcrumb_pull`), same probes and same grading
(`grade_correctness`) as the grid, through a real `claude -p`, in three host
configurations:

| config | MCP tools | coding plugin |
| --- | --- | --- |
| A. clean | athenaeum only, loaded upfront | off |
| B. deferred | athenaeum plus 36 invented stub tools over three servers, deferred behind `ToolSearch` | off |
| C. deferred + plugin | as B | on |

How each is wired (`tests/evals/host_config_cli.py`, with the small
`HostConfig` option on `run_push_breadcrumb_pull` in `tests/evals/rollout.py`):

- **A** is the grid's argv and MCP config unchanged. Row A is a fresh run
  beside B and C, not the regrade's number, so the three rows share a model,
  date and transport. The regrade is context only.
- **B** adds the stub servers (`tests/evals/stub_mcp_server.py`, invented
  names, every call returns a fixed string) to the generated `--mcp-config`,
  and sets `ENABLE_TOOL_SEARCH=true` in the spawn's environment. A few small
  stubs may not cross Claude Code's automatic deferral threshold, so the
  setting is forced.
- **C** is B plus `--plugin-dir` pointing at a scratch copy of the plugin
  (the original install is never written to). `--strict-mcp-config` ignores
  servers a plugin declares, so the plugin's server is also added to the
  generated config, under the name Claude Code would give it, and
  pre-approved so a non-interactive session does not stall on a permission
  prompt. Analytics identifiers in the plugin's config are not forwarded.

**Wiring check.** Every run is checked against the configuration it is meant to
represent, using the stream's `init` event. In B and C, `ToolSearch` must be
offered and `mcp__athenaeum__recall` must not be loaded upfront. In A, `recall`
must be loaded upfront. A run that fails is recorded as a wiring failure and
left out of the rows, so a B that silently equals A cannot produce a row.

**Metrics, per configuration.**

- Recall call rate: share of runs with at least one `recall` call.
- `ToolSearch` calls before the first `recall`, averaged over runs that called
  it. Counted in call order from the stream.
- Correct-answer rate (`grade_correctness`).
- Cost per correct answer: summed `total_cost_usd` over kept runs, divided by
  the number of correct runs.

## 3. Results

| config | runs (excluded) | recall rate | ToolSearch before first recall | correct rate | cost | cost per correct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A. clean | pending | pending | pending | pending | pending | pending |
| B. deferred | pending | pending | pending | pending | pending | pending |
| C. deferred + plugin | pending | pending | pending | pending | pending | pending |

## 4. Verdict

**None yet.** Reading the rows once they exist:

- B close to A, C close to B: neither difference explains the drop. The gap sits
  elsewhere (relevance of the pushed pages, or how real prompts differ from
  probes).
- B well below A, C close to B: deferral explains it.
- B close to A, C well below B: the plugin explains it.
- B below A and C below B: both contribute; the step sizes say how much each.

What the result means for athenaeum#3032 (push carries the answer plus a
tool-name hint) is written once the rows exist. The expected shape: if
deferral is the cause, the hint should name the callable tool and say it may
need loading, and deferral itself is a cheap secondary fix; if neither is,
the hint cannot be expected to close the whole gap.

## 5. How to run it

Prerequisites: a logged-in `claude` under an isolated `CLAUDE_CONFIG_DIR`
(`tests/evals/README.md`, "Running the cli-mode spot-check"). Configuration C
also needs a coding plugin installed and, if the plugin refuses to run
logged out, its own account in that config directory. Whether the plugin's
tools and hooks work logged out is not known; it is an open item.

Probe subset first, to check the wiring (costs a few dollars):

```sh
CLAUDE_CONFIG_DIR=/path/to/isolated-claude-config \
  python -m tests.evals.host_config_cli \
  --configs A,B,C --plugin-path /path/to/installed/plugin \
  --probes pto_allowance,bluewater_terms,spend_approver_named,office_address_current \
  --max-spend 5 --out /tmp/host-config-probe.jsonl
```

Confirm the printed table has no excluded runs (`runs (excluded)`), then the
core scale (every core probe, three replicates per cell):

```sh
CLAUDE_CONFIG_DIR=/path/to/isolated-claude-config \
  python -m tests.evals.host_config_cli \
  --configs A,B,C --plugin-path /path/to/installed/plugin \
  --replicates 3 --max-spend 25 --out /tmp/host-config-core.jsonl
```

Paste the printed table into section 3 and write section 4.

## Method

Offline checks (no model calls) pin the argv and MCP-config shape, the
default-is-unchanged guarantee, the `ToolSearch` counting, the wiring check
and the aggregation (`tests/evals/test_host_config_cli.py`). No measurement was
taken, so no cost was spent.
