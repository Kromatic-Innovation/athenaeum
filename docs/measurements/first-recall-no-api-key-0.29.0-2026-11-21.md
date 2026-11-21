<!-- SPDX-License-Identifier: Apache-2.0 -->

# First recall without an API key, live check on 0.29.0, 2026-11-21

Issue athenaeum#2307 (epic athenaeum#2296). Re-run of
[the 2026-11-20 run on 0.28.0](first-recall-no-api-key-2026-11-20.md), against
the release published to PyPI on 2026-11-21, following the README quick start as
a stranger would.

**Result: the core loop works and three 0.28.0 caveats are fixed, but the
README's own compile step still fails a new-session recall.** AC3, AC4 and AC5
pass. AC1 passes through `athenaeum session-end` and fails on the literal README
path (`athenaeum run` with nothing after it): recall returns a ghost hit for the
deleted raw note instead of the compiled page (athenaeum#3090). AC2 is measured,
and the disclosure figures need correcting (athenaeum#3092). Three bugs filed
(athenaeum#3090, athenaeum#3091, athenaeum#3092). All people, organisations and
facts in this report are invented. Counts only; no host paths and no credential
material.

The macOS clean-machine half (athenaeum#2319) stays with the operator and is not
covered here.

## Environment

| Item | Value |
| --- | --- |
| athenaeum | **0.29.0** from PyPI, `uv tool install --python 3.13 'athenaeum[mcp]==0.29.0'` (pinned to the then-latest release; `doctor` reported "0.29.0 (latest)") |
| Image | `node:22-bookworm` (Debian 12, arm64), digest `sha256:e13726ca604184d7a91fcacab1fc09eefd642dc28e79ac34e85540dd93b78cfb`, non-root `node` user |
| Claude Code CLI | 2.1.330 (`npm i -g @anthropic-ai/claude-code`) |
| uv / Python | uv 0.13.9, Python 3.13.18 |
| Container A | `docker run`, no volumes or bind mounts. A Claude Code login passed as the `CLAUDE_CODE_OAUTH_TOKEN` environment variable (read from a password manager into the launching command's environment, so the value was on no command line and in no file); `ANTHROPIC_API_KEY` verified unset inside |
| Container B | no volumes, `claude` installed but not logged in, no key |
| Container C | no volumes, no `claude` binary, no key |

The login drew on the operator's plan under the 2026-11-14 operator ruling; no
metered API cost was incurred. The captured output was scanned for the token
prefix afterwards (not present) and all three containers were removed.

## Steps

The README quick start in order, then the documented first session:

```bash
uv tool install --python 3.13 'athenaeum[mcp]'
athenaeum --version
athenaeum test-mcp                    # 3 passed, 0 failed
athenaeum init                        # printed the claude-cli provider and the disclosure
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
athenaeum doctor                      # mcp OK, key_mode OK ("login confirmed"); hooks, search_index WARN
# each step below is a separate `claude -p` process, stdin from /dev/null,
# CLAUDE_CODE_DISABLE_AUTO_MEMORY=1, with the MCP tool allowed or disallowed
claude -p "<question>; answer UNKNOWN if you do not know" --disallowedTools mcp__athenaeum__recall   # control
claude -p "Please remember this using your remember tool: <fact>" --allowedTools mcp__athenaeum__remember
claude -p "<question>; use your recall tool" --allowedTools mcp__athenaeum__recall    # before compile
athenaeum run                         # or: athenaeum session-end
claude -p "<question>; use your recall tool" --allowedTools mcp__athenaeum__recall    # a new process
```

Seven facts were told through the real `remember` tool. No hand edits in
container A. No step failed.

## Results per acceptance criterion

### AC1. No key, real login: documented first session ends with a fact compiled and recalled in a new session. FAIL on the literal README path, PASS via `session-end`

- Control before (recall tool disallowed): **UNKNOWN**. Control after the
  compile (recall still disallowed): **UNKNOWN** again, so every correct answer
  came through the shipped recall path and not shared context.
- Tell: Claude called `remember`; one raw file landed in `raw/claude-session/`.
- Compile with the default `claude-cli` provider and no key: the fact became a
  person page (plus a company page where the fact named one).
- **`athenaeum session-end`, then a new process asking:** the compiled page came
  back and the question was answered from it. No manual reindex needed. PASS.
- **`athenaeum run`, then a new process asking:** the compiled page did **not**
  come back. 3 of 3 attempts. The index still held the raw note that `run` had
  just deleted, so recall returned that path, labelled `[uncompiled]`, with an
  empty snippet, and the model answered that the store had no page on the person.
  A shell `athenaeum recall` after `run` shows the same ghost hit; after
  `athenaeum reindex` it returns the compiled page. The README's compile step is
  `athenaeum run` followed by "a later session asking *who is Priya?* gets the
  compiled page back from `recall`", so this is the path a stranger takes
  first. Filed as athenaeum#3090.
- `athenaeum run` followed by `athenaeum reindex` also works.
- All 7 `remember` calls landed in `raw/claude-session/` (no `source: "manual"`
  misrouting).

### AC2. Dated report with plan usage per compile, and a verdict on the disclosure. MEASURED (tokens, calls, time); disclosure needs correcting; plan share not measurable

**Source.** The librarian spend ledger (`spend.jsonl` in the cache dir, also
summarised by `athenaeum spend`), written by the `claude-cli` provider with
`billing_mode: subscription`. It records API calls, input and output tokens,
cache tokens and a `notional_usd` (the API-price equivalent; the real
`estimated_cost_usd` is 0.0 on this path). Wall time is from the shell `time`
builtin around the command.

**Limits.** These are the CLI's own token counts, not plan units. Nothing the CLI
printed carried a percentage of the plan's rolling limit or a remaining
allowance, so the share of a plan one compile uses cannot be stated, and neither
can how many compiles a plan allows. Four compiles, one login, one container, one
afternoon; wall time depends on load.

| Compile | Command | Notes | API calls | Input tokens | Output tokens | Total (in+out) | Cache written / read | Wall time | Notional USD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | `run` | 1 | 2 | 1,671 | 2,333 | 4,004 | 3,319 / 0 | 24.4 s | 0.0282 |
| B | `session-end` | 1 | 2 | 1,654 | 1,537 | 3,191 | 3,303 / 0 | 18.0 s | 0.0242 |
| C | `run` | 1 (two pages out) | 3 | 1,657 | 1,587 | 3,244 | 6,600 / 0 | 19.8 s | 0.0400 |
| D | `run` | 3 (four pages out) | 7 | 4,962 | 4,271 | 9,233 | 13,183 / 0 | 50.8 s | 0.0854 |

- Per single-note compile: **2 to 3 model calls, 3,191 to 4,004 tokens (in+out),
  18 to 24 s, 0.024 to 0.040 notional USD.** The ledger shows a smaller model for
  the classify step and a larger one for the write step.
- Three notes in one run: 7 calls and 9,233 tokens, about 3,100 per note, so
  batching gives no visible discount, as on 0.28.0.
- `athenaeum spend` after the four compiles: 46k tokens, 14 calls (it counts
  cache tokens as well as input and output).

**Does the disclosure need correcting?** Yes, the figures. `athenaeum init`
prints "Measured 2026-11-18 on 0.26.0: about 3-4 model calls, 3,800-4,400 tokens
(in+out) and 28-36 s per note; 5 notes in one run took 13 calls, about 19,400
tokens and 139 s", and the README and `docs/guides/install.md` repeat it. Against
0.29.0 every single-note sample is below the quoted calls (2 to 3 against 3 to
4) and time (18 to 24 s against 28 to 36 s), and two of three are below the
quoted tokens. Against the 0.28.0 record the quoted token range is also too
narrow on the high side (one note reached 7,025 tokens). The pooled observations
across the two releases fit "about 3,200 to 7,000 tokens, 2 to 3 calls and 18 to
50 s per note". The qualitative statements (draws on plan usage limits; no
percentage available; batching gives no discount) remain true. The run-cap
text was neither tested nor hit. Filed as athenaeum#3092.

### AC3. The `[uncompiled]` label has cleared by session 2. PASS (and one 0.28.0 caveat fixed)

- Right after `remember`, with no index step, a new process calling `recall`
  returned the note and the model quoted the status label **`[uncompiled]`** with
  the note's text. On 0.28.0 this window returned nothing; it is now visible and
  labelled (3 of 3 `remember` calls checked).
- After `session-end` (compile plus reindex in one step), a new process returned
  the compiled page with no `[uncompiled]` marker (only `active`). Cleared by
  session 2, with no manual reindex.
- After `run` alone the label does **not** clear: the stale ghost hit above still
  says `[uncompiled]` until a reindex (athenaeum#3090).
- The shell `athenaeum recall` now prints the `[uncompiled]` marker too (on
  0.28.0 only the MCP tool did).

### AC4. Counter-example: no key and no login; setup says what is missing, no silently empty recall. PASS

Containers B (`claude` present, not logged in) and C (no `claude` binary).

| Step | B: not logged in | C: no `claude` binary |
| --- | --- | --- |
| `claude -p` | `Not logged in · Please run /login`, exit 1 | n/a |
| `athenaeum init` | `warning: setup pending: llm.provider=claude-cli but Claude Code is not logged in ...; compiles cannot run`, with `fix: run claude and /login, or set ANTHROPIC_API_KEY`; exit 0 | `warning: setup pending: no ANTHROPIC_API_KEY ... and no claude binary on PATH; compiles cannot run`, with a fix naming both options; exit 0 |
| `athenaeum doctor` | `key_mode` WARN, same text and fix; no false OK; exit 0 | `key_mode` WARN, same text; exit 0 |
| `athenaeum run` | exit 1; `athenaeum status` then shows `Last compile: FAILED` with `why: claude CLI is not logged in. Run claude, then /login; or set ANTHROPIC_API_KEY ...` | `ANTHROPIC_API_KEY not set`, exit 1 |
| `athenaeum session-end` | exit 0; reindexed 1 page | exit 1 (the compile step failed); **`reindexed: true`**, 1 page |
| `recall` before any index step (hand-placed raw note) | `No results: the search index is missing or empty. Run athenaeum doctor to diagnose.`, exit 0 | same |
| `recall` after `session-end` | returned the uncompiled note, labelled `[uncompiled]` | same (on 0.28.0 this was still empty) |

Changes against 0.28.0: the last two rows. `session-end` with no credential now
reindexes before it exits non-zero, so an uncompiled note is recallable, and an
empty recall now says why instead of printing nothing. Remaining small point: the
`why:` line in `status` still ends with the first 200 characters of the CLI's raw
JSON, cut mid-token (athenaeum#3091).

### AC5. The report contains no real memory content. PASS

Every name is invented for this run. No host path, home directory, address,
email, token or fragment of one appears; paths inside the throwaway containers
are omitted.

## Changed versus the 0.28.0 record

- Recall right after `remember` now returns the raw note, labelled
  `[uncompiled]` (0.28.0 caveat 1).
- Shell `athenaeum recall` shows the `[uncompiled]` marker (0.28.0 caveat 2).
- `session-end` without a credential reindexes; an empty recall names the
  missing index and points at `doctor` (the 0.28.0 AC4 gap).
- A compile is 2 to 3 calls and 18 to 24 s for one note, not 3 calls and 24 to
  50 s.
- New failure: `athenaeum run` alone leaves a stale index entry for the deleted
  raw note, so recall misses the compiled page (athenaeum#3090). The 0.28.0
  record always reindexed after `run`, so it did not exercise this path.
- Still present: the raw CLI JSON on the not-logged-in `why:` line
  (athenaeum#3091); the disclosure range (athenaeum#3092).
- Not retested: the README's sample `doctor` output still shows
  `running 0.28.0; latest release is 0.28.0`.

## Method notes

- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` for every `claude -p`, so the model uses
  the athenaeum `remember` tool and not Claude Code's native memory.
- The per-turn recall hook was not wired (`doctor` `hooks` WARN, as the README
  says), so the stale-index failure is not masked by the SessionStart reindex.
  With the hook wired, a new session would probably rebuild the index first; that
  was not tested.
- The counter-example's raw note was hand-placed with the same frontmatter the
  `remember` tool writes, because `remember` needs a working `claude`.
- All containers were started without volumes and removed afterwards.

## Disposition

AC3, AC4 and AC5 are met. AC1 is met on the `session-end` path and not on the
literal `athenaeum run` path (athenaeum#3090). AC2 is measured; the disclosure
correction is open (athenaeum#3092). athenaeum#2307 stays open for those two and
for athenaeum#3091 to be triaged. The macOS half stays with athenaeum#2319.
