**Reference:** [librarian](../modules/librarian.md) · [mcp](../modules/mcp.md)

# Installing Athenaeum

## I want to install the package

Requires Python 3.13+ and `git` on your `PATH` (`athenaeum init` creates a git
repository for your knowledge directory; slim container images often lack it, so
`apt-get install git` or `brew install git` first). Nothing in athenaeum or its dependencies needs a
feature new in 3.13: it is the floor because CI tests one Python version at a time and the package
declares only the version CI verifies (see
[Supported Python version](../../CONTRIBUTING.md#supported-python-version)). The fastest way to get that, regardless of what your
system's default Python is, is [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv tool install --python 3.13 athenaeum
athenaeum --version                                    # compare with the PyPI badge above
```

`uv tool install` puts the `athenaeum` entry point in `~/.local/bin`. It does
**not** add that directory to your `PATH` for you — it only warns if it's
missing. If you hit `command not found`, run `uv tool update-shell` (or add
`~/.local/bin` to your shell profile yourself) once; after that it works
from a plain shell with no virtual environment active.

Alternatively, with pip:

```bash
pip install athenaeum
athenaeum --version                                    # compare with the PyPI badge above
```

**Check what you actually got.** On an interpreter older than 3.13, `pip`
either stops with `No matching distribution found for athenaeum` (listing only
old versions such as 0.15.0, when none of them fits the request) or, more
often, does not fail at all. Because releases before 0.17.1 declared `>=3.11`,
pip silently resolves *backwards* past a dozen releases and installs a
year-old version instead of erroring. Nothing in the output tells you this happened, and the CLI will
appear to work. Older releases are **not** yanked (see the CHANGELOG for
why), so they stay installable. Compare what `--version` prints against the
live [PyPI version badge](https://pypi.org/project/athenaeum/) rather than a
number written out here: the PATCH digit moves on every merge to `develop`
(see [Versioning and releases](../../CONTRIBUTING.md#versioning-and-releases)),
so any floor pinned in this paragraph by hand would be stale within days. If
`--version` reports anything below **0.17.1** — the first release to raise
the declared floor to 3.13, so anything older is a version pip could only
have chosen for an older interpreter — you are on the wrong interpreter, not
looking at a broken install.

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

**Which provider a fresh root picks, and what `init` tells you.** On a
*brand-new* knowledge directory, `init` prints which LLM provider the
written `athenaeum.yaml` selected and what it draws on:

- No `ANTHROPIC_API_KEY` in the environment, and a logged-in `claude`
  binary already on `PATH` — the config selects `llm.provider: claude-cli`,
  and `init` says so: compiles run on your Claude Code plan's subscription
  login, so they draw on that plan's usage limits rather than a metered
  API bill. For scale, measured on 0.28.0 (2026-11-20) and 0.29.0
  (2026-11-21): compiling one note took about 2 to 3 model calls, 3,200 to
  7,000 tokens (input plus output) and 18 to 50 seconds; three notes in one
  run took 7 calls, 9,233 tokens and 50.8 seconds. The CLI reports no percentage of the plan's
  limit, so the share of your plan this uses is not known. There is no
  separate first-run cap; the ordinary per-run caps apply: `athenaeum run`
  stops at 50 files or 800 API calls, and `athenaeum session-end` at 20 files
  or 100 API calls.
- `ANTHROPIC_API_KEY` set — unchanged: the config selects `llm.provider:
  api`, and compiles draw on your Anthropic API usage/billing.
- Neither a key nor a usable `claude` binary — the config still defaults to
  `api` (nothing to select), and `init` prints exactly what is missing and
  how to fix it, the same check `athenaeum doctor` runs. `remember`/`recall`
  still work in this state (a saved fact surfaces labelled `[uncompiled]`
  until a compile can run); only the compile step itself needs the
  credential.

An *existing* `athenaeum.yaml` is never rewritten by `init` — this selection
only happens once, the moment the file is first created.

## I want to check what is set up

```bash
athenaeum doctor
```

`doctor` is read-only. On a fresh install, steps you have not done yet show
as `WARN` with `setup pending` and the command that finishes each one; `doctor`
still exits `0`. Only something present but broken (an unparseable
`settings.json`, a hook whose script is missing, an unreadable index) is a
`FAIL` and exits non-zero. Abridged output right after `athenaeum init`, before
`claude mcp add` (paths shortened):

```text
Athenaeum Doctor
========================================
[OK] package_version: running X.Y.Z; latest release is X.Y.Z
[OK] interpreter: Python 3.13.1
[WARN] mcp: setup pending: no 'athenaeum' entry under mcpServers in ~/.claude.json
    fix: claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
[WARN] hooks: setup pending: no Claude Code settings file found at ~/.claude/settings.json
    fix: merge examples/claude-code/settings-snippet.json into ~/.claude/settings.json ...
[WARN] key_mode: setup pending: llm.provider=claude-cli but Claude Code is not logged in ...
    fix: run `claude` and `/login`, or set ANTHROPIC_API_KEY
[WARN] search_index: setup pending: no vector index found under ~/.cache/athenaeum/wiki-vectors
    fix: run `athenaeum reindex --path <knowledge-root>` to build it, ...
...
All checks passed, with 4 warning(s).
```

## I want first-person facts ("I prefer...") to land on my own page

A fresh knowledge base has no page for you, so a note like "I prefer terse
status updates" has nowhere to attach and is dropped by the compile. Tell
Athenaeum who the owner is, once:

```bash
athenaeum owner set --name "Ada Example"
athenaeum owner set --name "Ada Example" --alias "Ada J. Example"   # optional, repeatable
athenaeum init --path ~/my-knowledge --owner-name "Ada Example"     # the same thing, at init time
```

`owner set` is non-interactive and idempotent. It creates your person page in
`wiki/` and records `owner.uid` plus your name and aliases in
`athenaeum.yaml`, leaving every other key and comment in that file untouched,
so an existing knowledge base can opt in. Running it again changes nothing; a
new `--alias` adds only that alias. Use full names (two or more words): the
name matcher ignores single-word aliases, and `owner set` warns when you pass
one. Pass `--path` to point it at a knowledge directory other than the default.

**What lands on the owner page.** Once an owner is set, the compile always
offers the owner page to the classifier, so first-person statements ("I
decided...", "my team...", "the user...") in a raw note, whether it came from
a hook, `remember`, or an imported memory file, become observations on that
page. A preference, habit or standing instruction is filed under a
`## Preferences` section of the page as a bullet. The shipped observation
filter still applies: communication and workflow preferences are always
captured, while topics such as food or travel are only kept once they are
reinforced. With no owner set, behaviour is unchanged, and a dropped fact is
recorded in the drop ledger (`athenaeum drops`) so you can see it.

**Access level.** The owner page is created at the `personal` access level, so
a restricted caller (for example a remote client served with a narrower
`serve.audience`) cannot read it.

**Contact data is never written to it.** `owner set` does not ask for, accept
or store an email address or phone number, and the page holds only your name,
aliases and the facts compiled onto it. Contact details live on the separate
excluded surface, not on wiki pages.

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
| The librarian compile (`athenaeum run`) | **Only if you schedule it.** Neither `athenaeum init` nor the hook install above puts this on a timer — see "Nothing compiles itself" below | Compiled entity pages under `<knowledge_root>/wiki/`, one git commit per run (the knowledge root is a git repository), a lockfile held for the run's duration, and, after the compile, the search index under the cache dir for the pages it changed | Up to the run's API-call budget (800 calls by default, `--max-api-calls` to override) under the metered API provider; $0 under the Claude-subscription provider; always bounded by a wall-clock deadline regardless of provider. The index refresh after the compile is local CPU work with no API spend; see "`run` refreshes the search index" below |
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

**`run` refreshes the search index.** After its compile, `athenaeum run`
brings the search index up to date with the pages it wrote and the raw notes
it deleted, so `recall` right after a scheduled run returns the compiled
pages. The refresh embeds only the pages that changed, on the CPU, and makes
no model calls. Measured on a 12-vCPU Linux container, it adds about 4 s to a
run that changes 5 pages on the default `vector` backend, and under 0.2 s on
`fts5`. When nothing changed it is a file-list comparison: about 0.02 s at
1,000 pages and 0.6 s at 10,000. A run that changes many pages adds roughly
a third of a second per page on `vector`. See
[the measurement](../measurements/run-index-refresh-cost-2026-11-22.md). If
the refresh fails, `run` keeps the compile's exit code and prints a warning
on stderr naming `athenaeum reindex`. The next `run`, `session-end` or
`reindex` catches the index up, so a cron job does not need its own
`reindex` step.

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
knowledge directory and commits its scaffold. You do not need a git identity
configured: if git has none, `init` sets a repo-local `user.name`/`user.email`
on the knowledge repo only (never your global config), prints a notice saying
so, and shows the two `git config` commands to change it. If `init` fails for
any other reason it removes what it created, so retrying `athenaeum init`
starts clean; a directory that already existed keeps its own content.
`init` makes its seed commit with git hooks disabled, so a global
`core.hooksPath` commit hook (for example a protected-branch guard) does not
block it.

**Scheduler.** Nothing is scheduled by default. If you never set up cron,
launchd, or a recurring manual run, `remember` keeps appending to
`<knowledge_root>/raw/` indefinitely and `recall` never surfaces anything
newer than your last manual `athenaeum run`. There is no error for this —
the only way to find out is to notice that recall feels stale, or to run
`athenaeum status` and see the raw backlog count grow instead of shrink.

## Supported dependency ranges

| Component | Supported range |
|-----------|-----------------|
| Python | 3.13 and newer (`requires-python = ">=3.13"`) |
| `fastmcp` (`[mcp]` extra) | `>=4.0.0,<5.0` -- 4.0.0 is the oldest release that installs alongside `mcp` 2.x (the 3.x releases declare `mcp<2.0`) |
| `mcp` (`[mcp]` extra) | `>=2.0,<3.0` |
| `anthropic` | `>=0.50.0,<3.0` -- older releases lack the `OverloadedError` class the retry layer classifies |

These are the ranges the package metadata accepts. CI tests these versions:

- **The locked set.** `requirements-ci.txt` in the repository pins every
  dependency, and the main test jobs install it, so those exact versions are
  what the full suite runs against.
- **The newest releases.** One CI job installs the package with no lock at
  all, which exercises the newest releases the ranges allow.
- **The floors.** A separate `floors` CI job installs the oldest releases the
  ranges admit (`anthropic` 0.50.0, `fastmcp` 4.0.0, `mcp` 2.0.0) with
  everything else at the lock, and runs the tests that touch those packages.
  That job is advisory: a red result there means a floor should be raised, so
  it is not part of the required merge gate.

`requirements-ci.txt` governs CI installs only (it is applied with `-c`); a plain `pip install athenaeum` or `uv tool install athenaeum` resolves from the ranges in the package metadata, and no lockfile (`uv.lock` is git-ignored) is committed or used by CI, so the two can resolve different patch versions.

Versions inside a range but between the floor and the lock are not exercised;
treat them as best effort. `anthropic` in particular spans many majors, and
only the floor, the locked release and the newest one are tested.

## Support policy

- Only the current release is supported.
- The Python floor and the dependency floors move up whenever that is
  convenient, with no advance notice.
- Breaking changes ship without a deprecation period or a warn-first release.
- The bundled `promptwitness` package is experimental: it is not part of the
  athenaeum public API, is not covered by semver, and is subject to change
  without notice.
- If you need a longer deprecation notice, or support for an older Python or
  dependency version, open an issue asking for it.

Running athenaeum in production under this policy (pin an exact version,
upgrade one minor at a time) is covered in
[Running athenaeum in production](stability.md#running-athenaeum-in-production).

## See also

- Modules — [librarian](../modules/librarian.md) · [mcp](../modules/mcp.md)
- Design — [security posture](../design/security-posture.md) (§2.1, audience scoping)
- Reference — [configuration](../reference/configuration.md) · [exit codes](../reference/exit-codes.md)
- Guides — [Claude Code integration](claude-code.md) · [Other clients and platforms](clients.md) · [Passive recall via hooks](sidecar.md) · [Vector search](vector-search.md) · [Troubleshooting](troubleshooting.md)
