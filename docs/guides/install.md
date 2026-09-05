**Reference:** [librarian](../modules/librarian.md) · [mcp](../modules/mcp.md)

# Installing Athenaeum

## I want to install the package

Requires Python 3.13+.

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
pip install 'athenaeum[mcp]'
athenaeum serve --path ~/knowledge

# Smoke test the round-trip without a live session
athenaeum test-mcp
```

For Claude Code specifically, register the server once and it auto-starts
with every session:

```bash
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
```

That gives an agent `remember`/`recall` and the decision-queue tools. For a
fully passive recall experience (no explicit tool calls needed on every
turn), see [Passive recall via hooks](sidecar.md); for bridging Claude
Code's own auto-memory files into Athenaeum's intake, see
[Claude Code auto-memory integration](claude-code.md).

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

## See also

- Modules — [librarian](../modules/librarian.md) · [mcp](../modules/mcp.md)
- Design — [security posture](../design/security-posture.md) (§2.1, audience scoping)
- Reference — [configuration](../reference/configuration.md) · [exit codes](../reference/exit-codes.md)
- Guides — [Claude Code integration](claude-code.md) · [Passive recall via hooks](sidecar.md) · [Vector search](vector-search.md)
