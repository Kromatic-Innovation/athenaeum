<!-- SPDX-License-Identifier: Apache-2.0 -->

# First recall without an API key, live check on 0.28.0, 2026-11-20

Issue athenaeum#2307 (epic athenaeum#2296). Re-run of
[the 2026-11-18 run on 0.26.0](clean-machine-first-recall-no-api-key-2026-11-18.md),
against the latest published release, now that athenaeum#2367 (the
implementation) and athenaeum#2592 (the README quick start) have landed.

**Result: PASS on all five acceptance criteria, with three caveats** (the
`[uncompiled]` label only appears after an index step; recall stays silent after
a failed `session-end` with no credential; plan share is not measurable). The
two criteria that failed on 0.26.0 (the no-login counter-example, and the
`[uncompiled]` label) now pass. All people, organisations and facts in this
report are invented. Counts only; no host paths and no credential material.

## Environment

| Item | Value |
| --- | --- |
| athenaeum | **0.28.0**, the latest on PyPI on 2026-11-20 (`uv tool install --python 3.13 'athenaeum[mcp]'`, unpinned, as the README says) |
| Image | `node:22-bookworm` (Debian 12, arm64), digest `sha256:e13726ca604184d7a91fcacab1fc09eefd642dc28e79ac34e85540dd93b78cfb`, non-root `node` user |
| Claude Code CLI | 2.1.330 (`npm i -g @anthropic-ai/claude-code`) |
| uv / Python | uv 0.13.9, Python 3.13.18 |
| Container A | `docker run --rm`, no volumes or bind mounts. A Claude Code login passed as the `CLAUDE_CODE_OAUTH_TOKEN` environment variable (inherited from the launching shell, so the value was on no command line); `ANTHROPIC_API_KEY` verified unset inside |
| Container B | `docker run --rm`, no volumes, neither a key nor a login |
| Runs | Container A twice (a full first session, then a targeted run for the label and per-compile usage), Container B once with two variants |

The login drew on the operator's plan; no metered API cost was incurred. The
token was read from a password manager at launch, never printed or written to
a file, and the captured output was scanned for it afterwards (not present).

## Steps

The README quick start, in order, then the documented first session:

```bash
uv tool install --python 3.13 'athenaeum[mcp]'
athenaeum --version
athenaeum test-mcp                    # 3 passed, 0 failed
athenaeum init                        # printed the claude-cli provider and the disclosure
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
athenaeum doctor                      # mcp OK, key_mode OK ("login confirmed"); hooks, search_index WARN
# session 1: separate `claude -p` processes, stdin from /dev/null,
# CLAUDE_CODE_DISABLE_AUTO_MEMORY=1, each with the MCP tool allowed or disallowed
claude -p "<question>; answer UNKNOWN if you do not know" --disallowedTools mcp__athenaeum__recall   # control
claude -p "Please remember this using your remember tool: <fact>" --allowedTools mcp__athenaeum__remember
athenaeum reindex                     # index raw intake, no compile
claude -p "<question>; use your recall tool" --allowedTools mcp__athenaeum__recall
athenaeum run                         # or: athenaeum session-end
# session 2: a new process
claude -p "<question>; use your recall tool" --allowedTools mcp__athenaeum__recall
```

Five facts were told through the real `remember` tool in each run. No step failed. No hand edits.

## Results per acceptance criterion

### AC1. No key, real login: documented first session ends with a fact compiled and recalled in a new session. PASS

- Control before (recall tool disallowed): **UNKNOWN**.
- Tell: Claude called `remember`; one raw file landed in `raw/claude-session/`.
- Compile with the default `claude-cli` provider, no key: 2 pages created
  (person, company). Run 1 compiled through `athenaeum session-end` (50.3 s);
  run 2 through `athenaeum run` (24.1 s).
- New process, recall allowed: returned the compiled page and answered the
  question, citing the page.
- Control after (recall disallowed): still **UNKNOWN**, so the answer came
  through the shipped recall path and not shared context.
- Repeated for further facts: every compiled fact came back from recall.
- All 10 `remember` calls landed in `raw/claude-session/`. The `source: "manual"`
  misrouting seen on 0.26.0 (athenaeum#2764) did not occur in these 10 calls.

### AC2. Dated report with plan usage per compile, and a verdict on the disclosure. PASS (tokens and notional cost measured; plan share not measurable)

**Source used.** The librarian spend ledger (`spend.jsonl` in the cache dir,
also summarised by `athenaeum spend`), written by the `claude-cli` provider
with `billing_mode: subscription`. It records API calls, input and output
tokens, cache tokens, and a `notional_usd` (the API-price equivalent; the
real `estimated_cost_usd` is 0.0 on this path). The wall time is from the
shell `time` builtin around the command.

**Limits.** These are the CLI's own token counts, not plan units. The CLI
output we recorded carried no percentage of the plan's rolling limit and no
remaining-allowance figure, so the share of a plan one compile uses cannot be
stated, and neither can how many compiles a plan allows. Tokens and notional
dollars only. Small sample (three single-note compiles, two multi-note runs, one
account); variance between single-note compiles was large (see below).

| Compile | Command | Notes | API calls | Input tokens | Output tokens | Total (in+out) | Cache written / read | Wall time | Notional USD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | `run` | 1 | 3 | 1,662 | 1,963 | 3,625 | 6,591 / 0 | 24.1 s | 0.0410 |
| B | `session-end` | 1 | 3 | 1,653 | 2,242 | 3,895 | 6,602 / 0 | 28.1 s | 0.0450 |
| C | `run` | 3 | 7 | 4,951 | 6,246 | 11,197 | 10,554 / 1,421 | 85.7 s | 0.1120 |
| D (first run) | `session-end` | 1 | 3 | 1,663 | 5,362 | 7,025 | 6,596 / 0 | 50.3 s | 0.0579 |
| E (first run) | `session-end` | 4 | not captured | not captured | not captured | not captured | not captured | 63.2 s | not captured |

(Compile E's ledger row was not printed by the harness, so only its wall time
is reported.)

- Per single-note compile: **3 model calls, 3,625 to 7,025 tokens (median of
  three, 3,895), 24 to 50 s, 0.041 to 0.058 notional USD.**
- Three notes in one run: 7 calls and 11,197 tokens, about 3,700 per note, so
  batching gave no visible discount.
- For scale, each surrounding `claude -p` tell or ask reported about 0.012 to
  0.047 notional USD in the same sessions (3 to 7 turns), so a one-note
  compile costs about the same as one chat turn pair.
- `athenaeum spend` after three compiles (A, B, C): 44k tokens, 13 calls
  (it counts cache tokens as well as input and output).

**Does the disclosure in the docs need correcting?** Not corrected, but one
number wants widening. What `athenaeum init` prints and
`docs/guides/install.md` says (draws on the plan's usage limits; no first-run
cap, ordinary per-run caps of 50 files / 800 calls for `run` and 20 files /
100 calls for `session-end`; "3 to 4 calls, 3,800 to 4,400 tokens, 28 to 36 s
per note") is right on calls (always 3 here), batching (about 3,700 to 3,900
per note) and the missing plan percentage. Its single-note token range is too
narrow: two of three measurements sit at the documented range (3,625 and
3,895) and one reached 7,025 with 5,362 output tokens, and time ranged 24 to 50 s. A range
of "about 3,600 to 7,000 tokens and 24 to 50 s per note" would fit all
observations. The caps text matched what shipped; the run-cap ruling of
2026-11-05 is documented as not implemented, and this run neither tested nor
hit a cap.

### AC3. The `[uncompiled]` label has cleared by session 2. PASS, with a caveat

- After `remember` and `athenaeum reindex` (index raw intake, no compile), a new
  process calling `recall` returned the note and the model quoted the hit's
  status label as **`[uncompiled]`**, with the text of the raw note.
- After `athenaeum run` and a reindex, a new process returned the compiled
  pages with no `[uncompiled]` marker (only `active` and the footnote marker
  `[^1]`).
- **Caveat 1:** a note is not recallable at all, labelled or not, between
  `remember` and the next index step. A recall right after `remember` returned
  "No wiki pages matched query" (4 searches in the second run, and again
  for a second fact in the first run). On the documented path the `[uncompiled]` window is therefore usually
  invisible: `session-end` compiles and indexes in one step.
- **Caveat 2:** the shell `athenaeum recall` prints the raw hit with no
  `[uncompiled]` marker; the label is shown to the agent through the MCP tool
  only (observed in the one MCP recall and one CLI recall of the same state).

### AC4. Counter-example: no key and no login; setup says what is missing, no silently empty recall. PASS on setup messages, with a gap on recall

Container B, two variants.

| Step | Variant 1: `claude` installed, not logged in | Variant 2: no `claude` binary, fresh home |
| --- | --- | --- |
| `claude -p` | `Not logged in`, exit 1 | n/a |
| `athenaeum init` | `warning: setup pending: llm.provider=claude-cli but Claude Code is not logged in ...; compiles cannot run`, with `fix: run claude and /login, or set ANTHROPIC_API_KEY`; exit 0 | `warning: setup pending: no ANTHROPIC_API_KEY ... and no claude binary on PATH; compiles cannot run`, with a fix naming both options; exit 0 |
| `athenaeum doctor` | `key_mode` WARN with the same text and fix; no false OK | `key_mode` WARN, same text; exit 0 |
| `athenaeum run` | Names `ClaudeCliNotLoggedInError`, says to run `claude` then `/login` or set a key; exit 1 | `ANTHROPIC_API_KEY not set`, exit 1 |
| `athenaeum status` | `Last compile: FAILED`, with the reason and fix | Not run |
| `session-end` | exit 0; reindexed 1 page | **exit 1; `reindexed: false`** |
| `recall` of a hand-placed raw note, before any index | Empty, exit 0, no message | Empty, exit 0, no message |
| `recall` after `session-end` | Returned the uncompiled note | **Still empty, no message** |

The three 0.26.0 failures are fixed: `init` and `doctor` no longer claim a
login that is absent (athenaeum#2762), and the failed run states the reason
(athenaeum#2763, apart from the cosmetic point below). The remaining gap is the
last row: with no credential at all, `session-end` stops before it reindexes, so
an uncompiled note stays invisible and `recall` stays empty with no hint (only
the `session-end` error and exit 1 say why).

Smaller point: the not-logged-in error in variant 1 embeds the first 200
characters of a JSON blob from the CLI ("it said: '{"duration_api_ms":0 ...")
before the fix text.

### AC5. The report contains no real memory content. PASS

Every name is invented for this run. No host path, home directory, address,
email, token or fragment of one appears; paths mentioned are in the throwaway
containers and are omitted here.

## Method notes

- `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` for every `claude -p`, so the model uses
  the athenaeum `remember` tool and not Claude Code's native memory.
- The per-turn recall hook was not wired (`doctor` `hooks` WARN, as the README
  says), which keeps the after-compile control clean.
- The first container's script called `/usr/bin/time`, absent from the image,
  so its `athenaeum run` steps did not run; those notes were compiled by the
  `session-end` step instead. The second run used the shell `time` builtin and
  produced compiles A to C.
- `doctor` after the first session: `search_index` is now OK on the fts5
  fallback (athenaeum#2765 fixed); only the optional `hooks` WARN remains.
- All containers were started with `--rm` and are gone.

## Disposition

All five acceptance criteria are met on 0.28.0. athenaeum#2307 can close on
this report once the three caveats are triaged; the macOS half stays with
athenaeum#2319.
