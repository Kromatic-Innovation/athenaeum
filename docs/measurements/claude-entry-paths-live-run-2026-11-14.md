# Claude Code entry-path live run, 2026-11-14

Issue: athenaeum#2161 (live run of the athenaeum#2235 harness). Adapter and its
"Verified vs assumed" table: athenaeum#2149. Epic: athenaeum#2144.

**Result: the run did not exercise the human/interactive entry paths.** One pass
of `tests/integration/test_claude_entry_paths.py` against the real binary
produced 2 passes (one of them inconclusive), 1 skip and 7 failures/errors. The
failures are a driver defect in the harness, not an observed misclassification.
No row was observed to classify wrongly, and no row that matters for human
counting was observed at all. The ledger was therefore **not enabled**.

## Setup

- `claude` version: 2.1.325. Python 3.11, macOS.
- Command shape: `PROMPTWITNESS_LIVE_CLAUDE=1 pytest tests/integration/test_claude_entry_paths.py`
  with the test process's `CLAUDE_CODE_*`, `CLAUDECODE`, `CLAUDE_PID` and
  `CLAUDE_EFFORT` variables unset first. The run was launched from inside
  another Claude Code session, and the harness copies `os.environ` into the
  child, so an inherited `CLAUDE_CODE_BRIDGE_SESSION_ID` would otherwise have
  forced every row to classify as `bridge`.
- Hook command: the deploy install's `athenaeum-claude-hook` (athenaeum 0.23.5),
  not a build of the tested branch. The harness resolves the console script from
  `PATH`.
- Harness used unmodified from `develop`. Each case ran with a temporary
  `--settings` file, a temporary `PROMPTWITNESS_DIR` and a temporary
  `CLAUDE_CONFIG_DIR`.
- One pass; no retry of any case, because the failures below are deterministic.

## Per-path results

| Entry path | Expected | Observed | Verdict |
|---|---|---|---|
| interactive typed | `human` / `interactive`, one `typed` segment | no ledger line; no prompt reached the hook | not exercised (driver) |
| queued (typed while a turn runs) | `human` / `interactive`, own `prompt_id` | no ledger line | not exercised (driver) |
| slash command with args | `slash-command` / `interactive`, one `slash-args` segment | no ledger line | not exercised (driver) |
| pasted content | `human` / `interactive`, `typed` + `pasted` | no ledger line | not exercised (driver) |
| headless `claude -p` | `headless` channel, never human-countable | `prompt_kind: human`, `channel: headless`, one `typed` segment, `resumed: false`; intake miss `channel-not-interactive:headless` | **pass** |
| resumed (`--resume`) | `resumed: true` on the new process's lines | first process produced no line, so the resume step was never reached | not exercised (driver) |
| sub-agent (Agent tool) | `subagent`, or no line (record which) | no line, but the parent session never submitted a turn | **inconclusive**, see below |
| claude.ai bridge | `bridge`, never human | not run | not run, see below |
| background task completion | `task-notification` | no line | not exercised (driver) |
| `claude "<prompt>"` launch | `launch-argument` on the first turn | no line; the case also errored in teardown | not exercised (driver) |

Headless line shape (field names; values omitted): `kind`, `seq`, `ts`,
`ledger_id`, `prev`, `harness`, `harness_version`, `hook_version`, `key`
(`alg`, `id`, `presence`, `strength`), `ledger_meta`, `turn`, `session`,
`prompt_id`, `prompt_kind`, `channel`, `prompt_chars`, `prompt_digest`,
`raw_digest`, `resumed`, `segments` (`kind`, `start`, `end`, `digest`), and
`signals` (`agent_id`, `agent_type`, `attended`, `bridge_present`,
`child_session`, `entrypoint`, `has_tty`, `launcher`). Two `anchor` lines
followed the turn line.

## Why the interactive rows did not run

Two causes, found by looking at the pty screen after the first pass.

1. **The isolated `CLAUDE_CONFIG_DIR` starts logged out.** The first launch in
   a fresh config dir shows the onboarding theme picker. With onboarding and
   folder trust pre-seeded, it opens with "Not logged in". Credentials are tied
   to the default config dir, so no interactive prompt can be answered in an
   isolated one. `claude -p` still reached its `UserPromptSubmit` hook, which
   fires before any model call, so the headless row passed regardless of login.
2. **Enter did not submit over a raw pty.** With the host's own config dir
   (logged in) and a trusted working directory, text typed via the harness
   appeared in the input box but `\r`, `\n` and `CSI 13u` each failed to submit
   it, and no transcript or ledger line was written. The cause is undetermined
   (candidates: no window size or controlling terminal on the pty, the TUI's
   keyboard-protocol mode, or startup readiness).

A third consequence: reusing the host config dir would load the host's own
`UserPromptSubmit` hooks alongside the harness's `--settings` hook, so a case
asserting "exactly one new turn line" could fail even once Enter works. A fix
needs to decide how to isolate hooks as well as credentials.

## "Verified vs assumed" items now settled

From athenaeum#2149's table, on claude 2.1.325:

| Item | Status |
|---|---|
| Headless hook env carries `CLAUDE_CODE_ENTRYPOINT` (`sdk-cli`), `CLAUDE_CODE_SESSION_ATTENDED` (`0`) | confirmed again |
| `CLAUDE_CODE_CHILD_SESSION` in a `claude -p` hook | **observed `1`** (with the variable unset in the parent environment, so claude set it itself) |
| `CLAUDE_CODE_CHILD_SESSION` in a top-level interactive hook | **not obtained** |
| The hook does not fire for sub-agent prompts | **still unsettled**; the `no line` result came from a parent that never submitted a turn |
| Interactive hook stdin has the same keys | still assumed |
| Pasted content is expanded inline in stdin `prompt` | still assumed |
| Queued prompts fire the hook with their own `prompt_id` | still assumed |
| Slash-command stdin carries `<command-args>` inline | still assumed |
| claude.ai bridge sessions expose `CLAUDE_CODE_BRIDGE_SESSION_ID` in the hook env | still assumed |
| `CLAUDE_CODE_SESSION_ATTENDED` semantics are stable | still assumed |
| The first prompt of a `claude "<prompt>"` launch is distinguishable | still assumed |

Two further observations about the written line:

- `harness_version` was an empty string on the headless line, so the claude
  version is not recoverable from the ledger. Whether that is intended is a
  question for athenaeum#2149.
- The `opentimestamps` anchor line recorded `status: failed`,
  `reason: import-failed`, because the anchor extra is not installed in the
  deploy environment. Expected until the editable install in the issue's step 2.

## Claude.ai bridge case

Not run. The harness has no bridge driver by design: with
`PROMPTWITNESS_LIVE_CLAUDE_AI_BRIDGE=1` the case ends in a hard failure, not a
run. A live claude.ai bridge session cannot be established non-interactively
from this environment, and the environment of an agent session is not hook env,
so it was not used to settle the row.

## Enabling the ledger

**Not enabled.** The gate is that the run shows classification is correct. It
showed nothing for the human/interactive channel, which is the channel the
ledger exists to count. No host config was changed: no editable re-install, no
`config.env` edit, no `athenaeum.yaml` edit.

## Follow-up needed before athenaeum#2161 can close

- Harness driver (athenaeum#2235 or a new child): authenticated, isolated
  interactive sessions; a submit method that works over a pty; hook isolation
  from the host's own hooks; and a failing (not passing) result for the
  sub-agent row when no parent turn was submitted.
- Teardown error on the `launch-argument` case.
- Bridge case needs a driver or a documented manual procedure.
- Re-run once the above lands, then record the table and decide on enabling.
