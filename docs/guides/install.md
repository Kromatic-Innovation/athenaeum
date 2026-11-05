**Reference:** [librarian](../modules/librarian.md) · [mcp](../modules/mcp.md)

# Installing Athenaeum

## I want to install the package

Requires Python 3.13+. The fastest way to get that, regardless of what your
system's default Python is, is [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv tool install --python 3.13 athenaeum
```

`uv tool install` puts the `athenaeum` entry point in `~/.local/bin`. It does
**not** add that directory to your `PATH` for you — it only warns if it's
missing. If you hit `command not found`, run `uv tool update-shell` (or add
`~/.local/bin` to your shell profile yourself) once; after that it works
from a plain shell with no virtual environment active.

Alternatively, with pip:

```bash
pip install athenaeum
```

## I want a knowledge base to start from

```bash
athenaeum init                  # default: ~/knowledge
athenaeum init --path ~/my-knowledge
```

`init` also accepts `--with-templates` (copies entity-author templates for
person/company/project/concept/source into `<path>/templates/`) and
`--with-rules` (copies example shape rules — shipped in observe mode, so
installing them changes nothing until you promote one to `mode: live`).
`--force` overwrites existing template/rule files at the destination.

## I want to supply an API key (without 1Password)

`athenaeum run` needs `ANTHROPIC_API_KEY` in its own environment, unless you
pass `--dry-run` or set `llm.provider: claude-cli` to run on your Claude Code
login. 1Password is optional; any secret manager works. Where the key goes,
which commands read `~/.cache/athenaeum/config.env`, and how to fetch it from
the macOS Keychain, Secret Service, `pass`, Bitwarden, or 1Password are covered
in [Supplying an API key](../../examples/claude-code/README.md#supplying-an-api-key).
If a macOS dialog keeps asking that "“op” would like to access data from other
apps", see
[Repeated permission prompts](../../examples/claude-code/README.md#repeated-permission-prompts).

## I want to try it before spending any API budget

`athenaeum run` needs `ANTHROPIC_API_KEY` unless you pass `--dry-run`:

```bash
athenaeum run --dry-run         # inspect the plan without writing anything
athenaeum status                # check what's in the knowledge base so far
```

A full run with explicit paths and a lowered per-run budget:

```bash
athenaeum run \
  --raw-root ~/knowledge/raw \
  --wiki-root ~/knowledge/wiki \
  --path ~/knowledge \
  --max-files 50 \
  --max-api-calls 200 \
  --verbose
```

`--max-api-calls` deliberately lowers the run's spend ceiling below the
default of 800 API calls; omit it to accept the default.

## I want agents to write and read memory through MCP

```bash
uv tool install --python 3.13 'athenaeum[mcp]'
athenaeum serve --path ~/knowledge

# Smoke test the round-trip without a live session
athenaeum test-mcp
```

(`pip install 'athenaeum[mcp]'` into an activated 3.13+ venv works too — see
[I want to install the package](#i-want-to-install-the-package) above for the
wrong-interpreter warning that applies to it.)

For Claude Code specifically, register the server once and it auto-starts
with every session. Once `~/.local/bin` is on your `PATH` (see above), this
works from a plain shell with no virtual environment active:

```bash
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
```

That gives an agent `remember`/`recall` and the decision-queue tools. For a
fully passive recall experience (no explicit tool calls needed on every
turn), see [Passive recall via hooks](sidecar.md); for bridging Claude
Code's own auto-memory files into Athenaeum's intake, see
[Claude Code auto-memory integration](claude-code.md).

## I use a client other than Claude Code, or Linux or Windows

Claude Desktop, Cursor, and other local (stdio) MCP clients can use the MCP
tools, without auto-recall; ChatGPT and Grok are not supported; Linux is
supported and Windows is not. See
[Other clients and platforms](clients.md).

## I want my agent's memories to carry provenance and skip a classifier call

Claude Code's native memory writer emits four frontmatter keys by default
(`name`, `description`, `metadata.type`, `modified`), none of which carries
provenance. Those four are a **prompt convention, not an enforced schema** —
memory files are authored with the ordinary `Write` tool, so a `CLAUDE.md`
can instruct the writer to stamp more, and the extra keys reach intake
verbatim.

Add this to your user-level `~/.claude/CLAUDE.md` (loaded every session, so
it covers every project at once):

```markdown
When writing an auto-memory file, include these keys in the frontmatter:

  originSessionId: <the session UUID — basename of the current
                    ~/.claude/projects/<scope>/*.jsonl transcript>
  claim_kind: fact | observation | opinion | decision | policy | definition

Do not hand-author `sources[]` — Athenaeum resolves provenance itself.
Do not stamp `access`, `tags` or `observed_at` — the auto-memory path does
not read them.
```

What it buys:

- **`claim_kind` saves one LLM call per new memory.** The nightly run
  otherwise classifies each unclassified file in a cheap Haiku call; a valid
  author-supplied value is never overwritten and skips the call entirely.
- **`originSessionId` beats origin-session recovery.** Intake gates recovery
  on `origin_session_id is None and not sources`, so a declared session wins
  — and unlike recovery it never depends on file mtimes surviving your sync
  method. Saves I/O, not tokens.
Keep the convention to keys this path actually reads. `observed_at`, `access`,
`tags` and `aliases` belong to Athenaeum's *entity* intake path, not this one —
stamping them on a memory is a no-op the compile discards.

This is an **optimization over** origin-session recovery, never a replacement:
compliance is best-effort per session, and whether the extra keys survive a
Claude Code rewrite is unestablished. Recovery stays the backstop for every
file the convention missed.

Full field reference — every key intake reads, with vocabularies, defaults,
and which ones your deployment defines rather than core code — is in
[Claude Code auto-memory integration](claude-code.md) §3.

## I want a secondary agent that can't reach my private pages

Pin a restricted audience at serve time — the caller can never widen it:

```bash
athenaeum serve --path ~/knowledge --audience ops
```

Untagged pages fail closed for a restricted audience; the owner (no
audience pinned) still sees everything. See [Passive recall via
hooks](sidecar.md) and the module reference for the exact predicate.

## I want semantic (vector) recall, not just keyword search

```bash
pip install 'athenaeum[vector]'
```

See [Vector search](vector-search.md) for enabling it and what it buys you.

## I want to work from a source checkout

```bash
git clone https://github.com/Kromatic-Innovation/athenaeum.git
cd athenaeum
pip install -e ".[dev,vector]"   # matches CI; [dev] alone works if you won't touch search/clustering

pytest tests/ -v
ruff check src/ tests/
```

Athenaeum follows trunk-style development: `develop` is the active branch
and the GitHub default; pull requests target it. `main` carries the most
recent released revision, and release tags (`vX.Y.Z`) live there. Most
users should install from PyPI instead — only check out a tag if you need
to build against the last released revision from source:

```bash
git checkout "$(git describe --tags --abbrev=0)"
```

## What runs unattended after install

Installing the package and registering the MCP server does not, by itself,
put anything on a timer. Two different things can run without you present —
a sidecar reacting to a live Claude Code session, and a compile step you
schedule yourself — and they write and spend very differently.

| What | When it fires | What it writes | What it can spend |
|---|---|---|---|
| SessionStart hooks (`session-start-recall.sh`, `wiki-context-inject.sh`, `pending-questions-surface.sh`) | Every Claude Code session start, only if wired per the example `settings-snippet.json` | `<cache_dir>/config.env`, `stopwords.txt`, and `recall_overflow_breadcrumb.md`; the FTS5 index, built synchronously; and, only when a vector rebuild or credential fetch is actually due, a detached background job that writes the vector index, a rebuild log, and a durable degradation-status file under the same cache dir | No LLM call in the foreground. The backgrounded vector rebuild runs a local embedding model on CPU — no API spend — and a configured credential fetch (a 1Password reference or an operator-supplied shell command) returns a secret, not tokens |
| UserPromptSubmit hook (`user-prompt-recall.sh`, via the packaged Claude Code adapter) | Every user turn, only if wired | One row per rendered recall hit, appended to a push-record ledger under the cache dir | Up to one small-model LLM call per turn for topic extraction, bounded by a short timeout — skipped entirely (falls back to a regex extractor, $0) with no cached API key, and never metered at all under the Claude-subscription provider |
| PreCompact hook (`pre-compact-save.sh`) | Before Claude Code compacts context, only if wired | Nothing on disk — emits one reminder for the model to call `remember` | None |
| `remember` (MCP tool) | Whenever an agent calls it, in any session | One markdown file per observation under `<knowledge_root>/raw/claude-session/` | None — a plain file write |
| The librarian compile (`athenaeum run`) | **Only if you schedule it.** Neither `athenaeum init` nor the hook install above puts this on a timer — see "Nothing compiles itself" below | Compiled entity pages under `<knowledge_root>/wiki/`, one git commit per run (the knowledge root is a git repository), and a lockfile held for the run's duration | Up to the run's API-call budget (800 calls by default, `--max-api-calls` to override) under the metered API provider; $0 under the Claude-subscription provider; always bounded by a wall-clock deadline regardless of provider |
| SessionEnd index rebuild (`rebuild-index.sh`) | Every session end — **this is wired by the example `settings-snippet.json` itself** (a live `"SessionEnd"` block, detached with `nohup`), not an opt-in the operator adds | The same index files as the SessionStart background job, plus its own rebuild log | Same as that background job: local embedding compute only, no API spend |
| Stop-hook validator (`stop-hook-validate.sh`) | Session stop — **opt-in**; there is no `"Stop"` entry in the example settings snippet, so this one the operator must wire themselves | Nothing — prints warnings to stderr only | None; exits `2` only under `VALIDATE_MODE=block` |

**Nothing compiles itself.** The hooks above only ever populate
`<knowledge_root>/raw/`. Turning that into searchable wiki pages needs
`athenaeum run`, and nothing on this page schedules it — the three ways to
do that (manual, cron, launchd) are in
[the Claude Code integration README, "Install" step 6](../../examples/claude-code/README.md#install).
A `remember` call with no scheduled (or manual) `run` afterward looks like
it worked and produces no `recall` hit; `athenaeum status` reports the
growing raw backlog either way.

## What happens when a prerequisite is missing

Each of these degrades or fails in a different, specific way — none of them
hang silently forever.

**`uv` or the Python interpreter.** The quick start above leads with `uv
tool install` and documents pip as a second option. If `uv` isn't on your
PATH, the shell says so immediately (`command not found`) — nothing subtle.
If you use the pip path instead on an interpreter older than 3.13, pip will
not fail: as the quick start's own warning explains, releases before 0.17.1
declared a floor of 3.11, so pip silently resolves backwards past a dozen
releases and installs a year-old version — nothing in the output names this,
and comparing `athenaeum --version` against the PyPI badge is how you find
out.

**API key.** With no `ANTHROPIC_API_KEY` cached and no `llm.provider:
claude-cli`, the per-turn sidecar does not fail — topic extraction quietly
falls back to a regex-and-stopword extractor, so recall still works with
less topic awareness. The shipped `session-start-recall.sh` does print a
warning for this (`No ANTHROPIC_API_KEY cached — LLM topic extraction will
fall back to regex-only extraction …`), but the example `settings-snippet.json`
wires every hook command with `2>/dev/null`, so that line is thrown away in a
default install. The degradation is still durably recorded under the cache
dir and surfaced once per session on the model's own context channel rather
than stderr, and `athenaeum status` reports the latest one — see
[Claude Code integration](claude-code.md) and
[Troubleshooting](troubleshooting.md) for both. `athenaeum run`, by
contrast, refuses outright: it exits `1` with `ANTHROPIC_API_KEY not set`
unless you pass `--dry-run` or configure `llm.provider: claude-cli`.

**Git identity.** `athenaeum init` creates a git repository for the new
knowledge directory and commits its scaffold. If git has no `user.name` or
`user.email` configured, `init` does not surface a raw git error — it exits
with a direct message naming the two `git config --global` commands to run
and telling you to retry `athenaeum init`.

**Scheduler.** Nothing is scheduled by default. If you never set up cron,
launchd, or a recurring manual run, `remember` keeps appending to
`<knowledge_root>/raw/` indefinitely and `recall` never surfaces anything
newer than your last manual `athenaeum run`. There is no error for this —
the only way to find out is to notice that recall feels stale, or to run
`athenaeum status` and see the raw backlog count grow instead of shrink.

## See also

- Modules — [librarian](../modules/librarian.md) · [mcp](../modules/mcp.md)
- Design — [security posture](../design/security-posture.md) (§2.1, audience scoping)
- Reference — [configuration](../reference/configuration.md) · [exit codes](../reference/exit-codes.md)
- Guides — [Claude Code integration](claude-code.md) · [Other clients and platforms](clients.md) · [Passive recall via hooks](sidecar.md) · [Vector search](vector-search.md) · [Troubleshooting](troubleshooting.md)
