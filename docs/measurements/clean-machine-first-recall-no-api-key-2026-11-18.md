<!-- SPDX-License-Identifier: Apache-2.0 -->

# First recall without an API key, clean-machine run, 2026-11-18

Issue athenaeum#2307 (epic athenaeum#2296). Result: **FAIL on two of five
acceptance criteria, one partial.** The core loop works: with no
`ANTHROPIC_API_KEY` and a Claude Code login, a fact told in one `claude -p`
process was compiled by the default `claude-cli` provider and returned by
`recall` in a separate process. The counter-example (no login) and the
`[uncompiled]` label criteria fail, for reasons recorded below.

All people, organizations and facts in this report are invented. The sibling
Linux re-run of the README quick start (athenaeum#2319) is in
[clean-machine-first-recall-linux-rerun-2026-11-18.md](clean-machine-first-recall-linux-rerun-2026-11-18.md);
both reports come from the same containers.

## Environment

| Item | Value |
| --- | --- |
| Image | `node:22-bookworm` (stock Debian 12, aarch64), digest `sha256:e13726ca604184d7a91fcacab1fc09eefd642dc28e79ac34e85540dd93b78cfb`, non-root `node` user, no volumes |
| Claude Code CLI | 2.1.327 (`npm i -g @anthropic-ai/claude-code`) |
| uv / Python | uv 0.13.9, Python 3.13.18 (uv-managed; image system Python is 3.11.2) |
| athenaeum | **0.26.0** from PyPI (`uv tool install --python 3.13 'athenaeum[mcp]==0.26.0'`) |
| Container A | Claude Code login supplied as `CLAUDE_CODE_OAUTH_TOKEN`; `ANTHROPIC_API_KEY` unset (verified inside the container) |
| Container B | neither a key nor a login; `claude` installed but not logged in |
| Prior state | none; both containers were created fresh and removed afterward |

The token was read from a password manager into a mode-600 env file and passed
with `docker run --env-file`. It was never printed or logged, and the file was
deleted afterward. The compile ran under the operator's plan, so no metered
cost was incurred.

## Commands (token redacted)

```bash
docker run -d --name am-a --user node --env-file <mode-600 file with CLAUDE_CODE_OAUTH_TOKEN> node:22-bookworm sleep 7200
docker run -d --name am-b --user node node:22-bookworm sleep 7200
# in each container
npm config set prefix ~/.npm-global; export PATH=~/.npm-global/bin:~/.local/bin:$PATH
npm i -g @anthropic-ai/claude-code
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install --python 3.13 'athenaeum[mcp]==0.26.0'
# README quick start, verbatim
athenaeum init
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
athenaeum doctor
# tell, compile, ask: separate processes, stdin from /dev/null
claude -p "Please remember this using your remember tool: <fact>" --allowedTools mcp__athenaeum__remember --output-format json </dev/null
athenaeum run
athenaeum session-end
claude -p "<question>; use your athenaeum recall tool" --allowedTools mcp__athenaeum__recall --output-format json </dev/null
# control: same question with the recall tool disallowed
claude -p "<question>; answer UNKNOWN if you do not know" --disallowedTools mcp__athenaeum__recall --output-format json </dev/null
```

`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` was set for every `claude -p` after the
first contaminated attempt (see Method notes).

## Results per acceptance criterion

### AC1. No key, real login: fact compiled and returned by `recall` in a new session. PASS

- Control before: a process with `recall` disallowed answered **UNKNOWN** about
  the first invented fact.
- Tell: Claude called `remember`; one raw file landed in `raw/claude-session/`.
- `athenaeum run` (default `claude-cli`): 3 entity pages created (a person, a
  company, a project) in 35.9 s.
- New process, `recall` allowed: returned the person page and answered the
  question ("prefers green tea", where she works) with a citation to the page.
- Control after: a process with `recall` disallowed still answered
  **UNKNOWN**, so the answer came through the shipped recall path and not from
  shared context.
- Repeated with a second fact (a person who plays an instrument): control
  UNKNOWN before, `remember`, compile, recall hit, control UNKNOWN after.

### AC2. Dated report with measured plan usage per compile. PASS (this report)

Plan usage per compile, from the librarian spend ledger
(`~/.cache/athenaeum/spend.jsonl`, `billing_mode: subscription`). Three
compiles were run, covering seven notes saved through the `remember` tool.

| Compile | Files | API calls | Input tokens | Output tokens | Total (in+out) | Cache written / read | Wall time | Notional USD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 4 | 1,645 | 2,765 | 4,410 | 9,787 / 0 | 35.9 s | 0.0607 |
| 2 | 1 | 3 | 1,643 | 2,200 | 3,843 | 5,915 / 0 | not timed (LLM calls sum to 27.9 s) | 0.0418 |
| 3 | 5 | 13 | 8,189 | 11,244 | 19,433 | 16,634 / 7,105 | 139 s | 0.1536 |

- Per remembered note: about **3,900 tokens in plus out** (3,843 to 4,410 for a
  single-note compile), **2.6 to 4 model calls**, **about 28 to 36 s**, about
  **0.04 to 0.06 notional USD**. Batching five notes cost about 3,900 tokens per
  note, so there was no visible batching discount on the subscription path.
- Models used: a small model for the classify step (about 85 percent of the
  in/out tokens) and Claude Sonnet for page writes (the rest, plus all the
  cache tokens).
- `estimated_cost_usd` is 0.0 on this path (subscription-covered) and
  `notional_usd` carries the API-price equivalent. The CLI reports no
  percentage of the plan's rolling limit, so **the share of the plan consumed
  could not be measured**; only tokens and notional cost can.
- The surrounding Claude sessions cost more than the compile: each `claude -p`
  tell or ask reported about 0.035 to 0.042 notional USD (3 to 5 turns, about
  50k to 90k cache-read tokens, 8 to 12 fresh input tokens, 300 to 600 output
  tokens). A compile of one note is comparable to one chat turn pair.

**Does the docs' disclosure need correcting?** The sentence in `athenaeum init`
output and `docs/guides/install.md` ("compiles draw on your Claude Code plan's
usage limits") is true. It needs adding to, not correcting:

1. It gives no magnitude. The measured figures above (about 4,000 tokens and
   30 s per remembered note) would let a user judge it.
2. The ruling of 2026-11-05 said first-run compiles have a run cap. No first-run
   cap was observed (a 5-file compile ran uncapped) and a search of the 0.26.0
   source found none, so the docs must not promise one unless it ships.
3. The README quick start still says
   `export ANTHROPIC_API_KEY=...     # or configure the claude-cli backend`
   before `athenaeum run`, although `init` has already selected `claude-cli`.
   A new user reading the README thinks a key is needed.

### AC3. The `[uncompiled]` label from athenaeum#2313 has cleared by session 2. NOT DEMONSTRATED (FAIL as shipped)

- After compile, recall hits for compiled pages carried only their `active`
  tag and type, with no `[uncompiled]` marker. That half holds.
- The first half, seeing the label before compile, **could not be shown through
  a real Claude session**. In 6 of 7 `remember` calls the model passed
  `source: "manual"` (the tool's own docstring example), which writes to
  `raw/manual/`. That directory is not in the default
  `recall.extra_intake_roots` (`raw/auto-memory`, `raw/claude-session`,
  `raw/remote-session`), so a recall before compile returned **"No wiki pages
  matched query"** every time. Only the very first note, saved without a
  `source`, landed in `raw/claude-session/`.
- A raw note placed in `raw/claude-session/` by hand and indexed by
  `athenaeum session-end` was returned by `athenaeum recall` before any
  compile, so the roots mechanism itself works.
- Net: the documented promise ("a saved fact surfaces labelled `[uncompiled]`
  until a compile can run") fails in the common model-driven case. Draft issue
  recorded for the lane owner.

### AC4. Counter-example: neither key nor login, setup says what is missing. FAIL

Container B (claude installed, no login, no key).

| Step | Observed |
| --- | --- |
| `claude -p "say hi"` | `Not logged in · Please run /login`, exit 1 |
| `athenaeum init` | Selected `llm.provider: claude-cli` and printed the plan-usage disclosure as if a login existed. Nothing about the missing login. |
| `athenaeum doctor` | `[OK] key_mode: llm.provider=claude-cli; Claude Code login detected`. **False positive.** |
| `athenaeum run` | Python traceback, ending `RuntimeError: claude CLI exited 1: (no stderr)`. The real reason ("Not logged in") went to stdout and was dropped. The file is marked a failed/stuck file and retried later. |
| `athenaeum status` | "Raw files pending: 1" and a drain advisory, no credential hint. |
| `athenaeum doctor` after the failed run | `last_compile` OK, `key_mode` still OK. |
| `recall` | Not silent for the raw note: after `session-end` reindex the uncompiled note came back. The silent part is the compile, not recall. |

Second variant, no `claude` binary on `PATH` and no key (fresh `HOME`):

| Step | Observed |
| --- | --- |
| `athenaeum init` | Warning that "topic extraction silently falls back to a regex extractor", fix "supply an API key, or set llm.provider: claude-cli". It names the wrong consequence (topic extraction, not compile) and offers the provider that cannot work without the missing binary. `docs/guides/install.md` says `init` "prints exactly what is missing"; it does not say that the `claude` CLI is absent. |
| `athenaeum doctor` | `key_mode` FAIL, same text. |
| `athenaeum run` | `ANTHROPIC_API_KEY not set (required unless dry_run=True)`, exit 1. Clear, but it does not mention the claude-cli option. |

### AC5. The report contains no real memory content. PASS

All names (people, studio, event) were invented for this run and appear here
only as that. No real host, home path, address, email or corpus content is
included; paths are those of the throwaway container.

## Method notes

- One early attempt was discarded: with Claude Code's own auto memory on, the
  model wrote the fact to its native memory file instead of calling the
  athenaeum `remember` tool, which contaminated that fact's controls. That
  memory directory was deleted and auto memory was disabled
  (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) for the remaining runs. This is model
  behaviour, not an athenaeum defect, but a user with auto memory on will see
  it.
- The session-end step printed a one-time warning that `search_backend: vector`
  needs chromadb and fell back to fts5. Recall worked on fts5 (the fix for
  athenaeum#2592 holds).
- Compiled pages echoed the word "invented" from the prompt wording; that is the
  tell text, not a defect.

## Disposition

- Not every criterion passed, so athenaeum#2307 stays open. The part that works:
  the default no-key login path, end to end, with measured usage.
- Findings for follow-up: the credential check lies about a missing login;
  the compile error drops the real message; `remember` with a model-chosen
  `source` lands outside the recall roots; the README quick start and
  disclosure need the edits listed under AC2.
