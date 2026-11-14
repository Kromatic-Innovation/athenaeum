<!-- SPDX-License-Identifier: Apache-2.0 -->

# Clean-machine first-recall test, Linux only, 2026-11-14

Issue athenaeum#2319 (epic athenaeum#2296). Result: **FAIL as documented.** The
fact compiled, but a new session could not recall it until a hand edit outside
the README steps.

## Scope

- **Linux only.** The macOS half is deferred by operator ruling on 2026-11-14
  ("Cheap Linux-only now").
- **PyPI quick-start only.** The Claude Code plugin/marketplace path
  (athenaeum#2308) is **untested because it is not published yet**: the thin
  plugin repo does not exist, no install docs mention it, and the staged tree
  under `examples/claude-code-plugin/` is not in the 0.23.5 wheel. athenaeum#2308
  is open. This run covers the README quick start from PyPI and nothing else.
- Claude login was supplied as a Claude Code OAuth token in an environment
  variable at `docker run` time. Nothing was mounted from the host.
- `claude -p` (headless) was used for both the "tell" and the "ask in a new
  session" steps. No interactive session was used.

## Environment

| Item | Value |
| --- | --- |
| Image | `node:22-bookworm` (stock Debian 12, arm64), run as the non-root `node` user, no volumes |
| System Python | 3.11.2 (so the README's 3.13 requirement is met only through `uv`) |
| Claude Code CLI | 2.1.325, `npm i -g @anthropic-ai/claude-code` |
| uv | 0.13.8, official installer script |
| athenaeum | **0.23.5** from PyPI (latest on PyPI at run time), Python 3.13.18 managed by uv |
| Prior state | none; container created fresh and destroyed afterward |

## Steps (repeatable)

```bash
docker run -d --name clean --user node -e HOME=/home/node \
  -e CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" node:22-bookworm sleep 3600
# inside the container:
npm config set prefix ~/.npm-global; export PATH=~/.npm-global/bin:~/.local/bin:$PATH
npm i -g @anthropic-ai/claude-code
curl -LsSf https://astral.sh/uv/install.sh | sh
# README quick start, verbatim:
uv tool install --python 3.13 'athenaeum[mcp]'
athenaeum init
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
# tell, compile, ask in a new process:
claude -p "Please remember this using your remember tool: <fact>" --allowedTools mcp__athenaeum__remember </dev/null
athenaeum run
claude -p "<question>; use your athenaeum recall tool" --allowedTools mcp__athenaeum__recall </dev/null
```

Redirect stdin of `claude -p` from `/dev/null` when scripting, or it consumes
the rest of a piped script.

## Observations

| Step | Result |
| --- | --- |
| `athenaeum --version` | `athenaeum 0.23.5` |
| `athenaeum init` first run | **Failed**: "Git identity not configured." Needed `git config --global user.name/user.email`, then retry. The README prerequisites do not mention git identity. The directory skeleton was left half-created by the failed run. |
| `athenaeum init` second run | Initialized; printed "LLM provider: claude-cli (Claude Code subscription login)" and the plan-usage disclosure. **0.23.5 includes the claude-cli default.** `athenaeum.yaml` has `llm.provider: claude-cli`. |
| `claude mcp add --scope user ...` | OK; `claude mcp list` shows the server Connected. |
| `athenaeum doctor` right after | `mcp` reported FAIL ("no .../.claude/.claude.json found") although the server was registered and Connected: the registration was written to the home-level `.claude.json`, a different file than the one doctor checks. `hooks` and `search_index` FAIL as the README predicts. `key_mode` OK (claude-cli, login detected). |
| Tell (session 1, `claude -p`) | Claude called `remember`; one raw file landed under `raw/manual/`. |
| `athenaeum run` (claude-cli, no API key) | Worked. One to three compile calls per file, about 9 s to 39 s. |
| Recall in a new `claude -p` process | **NOT FOUND**, on every attempt, even though the wiki pages existed on disk. |
| `athenaeum session-end` / `athenaeum reindex` | **Failed**: "Vector backend requires chromadb. Install with: pip install athenaeum[vector]". |
| Diagnostic only: set `search_backend: fts5`, `athenaeum reindex`, ask again in a new process | **Recalled correctly** (partner and where they met, with two open questions). This is a hand edit and does not count as a pass. |

## Counter-example (fact came back through the shipped path, not the same context)

Each ask was a separate `claude -p` process with no shared context.

- A control process with the recall tool disallowed, asked about the first
  invented fact, answered "UNKNOWN". So the model does not know it unprompted.
- After the diagnostic backend change, a new process asked about the second
  fact returned content that exists only in the compiled wiki page. This shows
  recall works through the shipped MCP server once the index exists; it does not
  show the README path works.

## Facts used

- Fact A (invented): a person's favourite pastry. Compiled to **0 entities**;
  the raw file was deleted after the run and no wiki page resulted, so nothing
  was ever recallable. Likely the observation filter; the user gets no signal.
  Not filed (design question).
- Fact B (the README's own example, invented people): "Jordan's partner is
  Priya; they met at Stanford GSB." Compiled to 2 entity pages.

## Findings

1. **README quick start installs `athenaeum[mcp]`, but the default
   `search_backend: vector` needs the `[vector]` extra.** `recall` silently
   returns nothing, `session-end` and `reindex` error. This is the blocker.
2. `athenaeum init` fails on a machine with no git identity, which the README
   does not list as a prerequisite, and leaves a half-created directory.
3. `athenaeum doctor`'s `mcp` check looks in a different file than the one
   `claude mcp add --scope user` writes, so it fails after a correct install.
4. A fact that compiles to zero entities is removed from raw with no message.
   Observation only.
5. Plugin/marketplace path unpublished (athenaeum#2308 open), so untested.

## Can this run in CI?

Partly. Everything except the model calls can run in a stock container:
install, `init`, `claude mcp add`, `doctor`, and (with a mocked `claude` binary,
as athenaeum#2367 does) the compile. A live run needs a Claude Code OAuth token
as a secret and about six model calls per run, so it fits a scheduled or
manual workflow better than every PR. A check that needs no login: assert that
`athenaeum[mcp]` alone yields a working `recall` against a fixture wiki, which
would have caught finding 1. athenaeum#2161, athenaeum#2235 and athenaeum#2266
are the nearest existing work.

## Not done

- macOS (deferred by operator ruling).
- Plugin/marketplace path (not published).
- Anthropic directory submission: not attempted, since the test did not pass.
- Plan-usage measurement (athenaeum#2307): not measured.
