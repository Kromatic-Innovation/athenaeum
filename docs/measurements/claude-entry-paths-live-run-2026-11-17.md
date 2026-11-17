# Claude Code entry-path live run, 2026-11-17

Issue: athenaeum#2161 (live run of the athenaeum#2235 harness). Adapter and its
"Verified vs assumed" table: athenaeum#2149. Epic: athenaeum#2144. Previous
run, which could not reach the interactive rows:
[2026-11-14](claude-entry-paths-live-run-2026-11-14.md).

**Result: 9 of 10 rows pass against the real binary; the claude.ai bridge row
was not run.** The interactive rows were reachable this time. Running them
turned up four places where the adapter's assumptions were wrong, and the
harness had three driver defects. All of it is fixed on this branch except two
intake behaviours, which are pinned as clean misses and need follow-up issues.
**The ledger was not enabled**: that step mutates a live deployment and is
outside this run (see the last section).

## Setup

- `claude` 2.1.326 (npm package `@anthropic-ai/claude-code`), Linux container
  (Debian, Python 3.13), run as an unprivileged user.
- Throwaway container with no host mounts except a read-only copy of the
  checkout. The host's Claude config dir, cache dir and corpus were not
  mounted. The login was a long-lived OAuth token passed in the container
  environment (the first credential route in the harness module docstring).
- Command: `PROMPTWITNESS_LIVE_CLAUDE=1 pytest tests/integration/test_claude_entry_paths.py`
  against this branch, with the console script resolved from the container's
  own editable install of the branch.
- One diagnostic wrapper around the hook (not committed) logged the hook
  environment, the process ancestry and the stdin keys before handing off to
  the real hook. It is the source of the signal values below.

## Per-path results

| Entry path | Expected | Observed | Verdict |
|---|---|---|---|
| interactive typed | `human` / `interactive`, one `typed` segment | as expected | pass |
| queued (typed while a turn runs) | `human` / `interactive` | classified correctly; **the queued prompt's hook call carries the previous prompt's `prompt_id`**; intake reports `digest-mismatch` | pass, with a pinned safe miss |
| slash command with args | `slash-command` / `interactive`, one `slash-args` segment | classified correctly after the adapter fix; intake reports `transcript-promptsource:absent` | pass, with a pinned safe miss |
| pasted content | `human` / `interactive`, `typed` + `pasted` | as expected for a large paste (see below) | pass |
| headless `claude -p` | `headless` channel, never human-countable | `launch-argument` / `headless`, intake miss `kind-not-human:launch-argument` | pass |
| resumed (`--resume`) | `resumed: true` on the new process's line | as expected | pass |
| sub-agent (Agent tool) | `subagent`, or no `subagent` line (record which) | no `subagent` line; see below | pass |
| claude.ai bridge | `bridge`, never human | not run | not run |
| background task completion | `task-notification` | as expected | pass |
| `claude "<prompt>"` launch | `launch-argument` on the first turn | as expected after the adapter fix | pass |

## What the live run found

Each item was an assumption in the athenaeum#2149 table or in the harness.

Adapter fixes (in this branch, with unit tests):

1. **`CLAUDE_CODE_CHILD_SESSION` is `1` in every hook environment**, a
   top-level typed prompt included. The interactive rule required it to not be
   `1`, so every real interactive prompt classified as `unknown` (non-human).
   The value is still recorded in `signals`, but it is no longer a classifier
   input. Sub-agent prompts are told apart by `agent_id`, not by this flag.
2. **The hook sees the raw slash line**, e.g. `/name some args`, not the
   `<command-name>`/`<command-args>` wrapper text. The adapter now recognises
   that form (a name made of letters, digits and `_:.-`, then whitespace and
   the args) and records the args as the `slash-args` segment. A path-like
   typed prompt such as `/usr/bin is missing` stays `human`; a single-segment
   path such as `/tmp` is treated as a slash command (a counted false miss).
3. **`ps -o args=` does not preserve argv boundaries**, so a multi-word
   launch prompt never matched a single token and `claude "<prompt>"` and
   `claude -p "<prompt>"` were never detected. The check now reads exact argv
   from `/proc` where available and compares whole arguments; the `ps`
   fallback matches word runs of the prompt's length and fails closed.
4. Local built-in slash commands such as `/compact` **do not fire the hook at
   all**; only a command that expands into a model prompt does. The harness
   row now uses a custom command written into the isolated config dir.

Harness fixes (in this branch):

- The pty was not the child's controlling terminal (`start_new_session` calls
  `setsid` but never acquires a tty), so `has_tty` was false and every
  interactive row classified `unknown` for a second reason. The spawn now makes
  the pty the controlling terminal.
- The append-only ledger guard (athenaeum#2335) errored at teardown of every
  live row, because the hook runs in a child process. The module now clears
  the flag under each case's temp dir.
- The background-task row took its "before" marker after the trigger turn and
  raced the notification it asserts on. It now takes the marker first and
  selects the `task-notification` line.

## "Verified vs assumed" items now settled

On claude 2.1.326, Linux:

| Item | Status |
|---|---|
| `CLAUDE_CODE_CHILD_SESSION` in a top-level interactive hook | **observed `1`**, same as in a `claude -p` hook |
| Interactive hook environment: `CLAUDE_CODE_ENTRYPOINT=cli`, `CLAUDE_CODE_SESSION_ATTENDED=1`, `CLAUDE_PID` set to the `claude` process, which has a controlling tty | confirmed |
| Interactive hook stdin has the same keys as headless | confirmed: `cwd`, `hook_event_name`, `permission_mode`, `prompt`, `prompt_id`, `scratchpad_dir`, `session_id`, `transcript_path` |
| Sub-agent prompts fire the hook | **no**: no line carried an `agent_id` or `subagent` kind. The parent session's turn that receives the sub-agent's report does fire, with an `<agent-message ...>` wrapper that classifies `unknown` (non-human), which is the safe outcome |
| Pasted content is expanded inline in stdin `prompt` | **confirmed for a large paste**: it arrives wrapped as `<pasted_content id=...>...</pasted_content>`. A short paste (about 30 characters) arrived as plain text with no wrapper, so a short paste is indistinguishable from typing |
| Queued prompts fire the hook with their own `prompt_id` | **false**: the queued prompt reuses the id of the prompt it queued behind |
| Slash-command stdin carries `<command-args>` inline | **false**: raw `/name args` line (see above) |
| A slash command's transcript record carries `promptSource` | **false**: absent, so intake cannot attest it today |
| claude.ai bridge sessions expose `CLAUDE_CODE_BRIDGE_SESSION_ID` | still assumed, not observable here |
| `CLAUDE_CODE_SESSION_ATTENDED` semantics are stable | consistent across this run (`1` interactive, `0` headless) |
| The first prompt of a `claude "<prompt>"` launch is distinguishable | **confirmed live for a single-line prompt** once the `ps` matching was fixed. Multi-line and long (over 400 words) prompts are covered by unit tests only; they were not run live |

Also observed: `harness_version` is an empty string on every written line,
because the hook environment carries no `CLAUDE_CODE_VERSION`. The version
tested is therefore recorded here, not recoverable from the ledger.

## Follow-ups needed

These are behaviours of the adapter or intake that differ from the athenaeum#2149
assumption and are not fixed here. Each currently ends in a clean miss, never a
false attestation, and the harness rows pin that outcome.

- **Queued prompts share the previous prompt's `prompt_id`.** Intake joins on
  that id and so compares against the wrong transcript record. Needs a join
  that does not rely on the id being unique per prompt. `LedgerIndex` keeps the
  last line per `prompt_id`, so the earlier typed prompt also loses attestation.
- **A slash command's transcript record has no `promptSource`.** Intake
  requires `typed` or `queued`, so no slash command can be attested. Needs a
  decision on whether `origin.kind: human` alone is enough for that kind.
- **Short pastes are indistinguishable from typing.** The adapter can only
  see the wrapper that claude adds above a size threshold.
- **`harness_version` is empty.** Whether to source it elsewhere (for example
  from the transcript) is a question for athenaeum#2149.

## Claude.ai bridge case

Not run. A claude.ai remote session cannot be opened from a container or
non-interactively, and the harness has no bridge driver by design (the row is
skipped with a recorded reason). It needs an operator in a real bridge session,
and the same hook-logging approach used here to capture the hook environment and
the resulting ledger line.

## Enabling the ledger

**Not enabled, and not attempted.** Enabling edits the live deployment's
configuration and re-installs its package, which is deployment state, not
engine code. The acceptance criteria that depend on it (enable the ledger,
then read `athenaeum receipts attest-readout` after the first nightly) remain
with the operator. This run supplies part of the evidence they were gated on:
classification was correct on the interactive paths run here (not the bridge
row, and not multi-line or long launch prompts, which only unit tests cover),
and the two intake gaps above produced misses in this run, not false
attestations. Whether that is enough to enable the ledger is the operator's
call.
