**Reference:** [recall](../modules/recall.md) · [mcp](../modules/mcp.md)

# Passive recall via hooks

For a fully passive experience where Claude auto-recalls relevant context
on every prompt and saves observations without explicit commands, wire
Claude Code hooks around the commands this repo ships. Athenaeum does not
install a hook for you — it ships the `athenaeum session-end` command and
an **example** hook set under `examples/claude-code/`; the actual hook
wiring in `~/.claude/settings.json` is something you (or your workspace
config) own.

## I want auto-recall and auto-remember on every turn

1. Copy the example hooks from `examples/claude-code/` to your scripts
   directory:

   ```bash
   mkdir -p ~/.claude/hooks/athenaeum
   cp examples/claude-code/*.sh ~/.claude/hooks/athenaeum/
   chmod +x ~/.claude/hooks/athenaeum/*.sh
   ```

2. Merge `examples/claude-code/settings-snippet.json` into
   `~/.claude/settings.json`, replacing `/path/to/` with the directory
   from step 1.

3. Add CLAUDE.md instructions for proactive memory — see
   `examples/claude-code/CLAUDE.md.example`.

4. Restart Claude Code. The session-start message should say
   `[Knowledge] FTS5 index: N wiki pages`.

This gives you:

- **Auto-recall** — an FTS5 index is built at session start (~300ms); each
  user message triggers a search that injects relevant wiki pages into
  context.
- **Auto-remember** — Claude proactively saves important facts without
  being asked.
- **Context checkpointing** — observations are saved before
  context-window compaction.

Seven example hook scripts ship in `examples/claude-code/`:

| Hook | When it fires | What it does |
|---|---|---|
| `session-start-recall.sh` | Start of each session | Builds the FTS5 (and optional vector) index, caches config |
| `wiki-context-inject.sh` | Start of each session | Cheap cwd-keyword grep — surfaces wiki pages relevant to the project being opened |
| `user-prompt-recall.sh` | Each user turn | Hybrid FTS5+vector search, injects the top matching wiki page names |
| `pre-compact-save.sh` | Before compaction | Reminds the model to call `remember` on anything load-bearing |
| `pending-questions-surface.sh` | Start of each session | Surfaces unresolved `_pending_questions.md` entries with a snooze cache |
| `rebuild-index.sh` | SessionEnd (optional) | Out-of-band index rebuild with atomic dir lock — wire when a synchronous SessionStart rebuild becomes painful |
| `stop-hook-validate.sh` | Stop (optional) | Warns when auto-memory frontmatter is missing citation fields — see [Claude Code auto-memory integration](claude-code.md) |

## I want to understand why `remember` didn't make something recallable

Athenaeum has two phases, and the hooks only handle one of them:

1. **Intake (immediate, hook-driven).** When Claude calls `remember`, an
   observation lands in `~/knowledge/raw/claude-session/` as a timestamped
   markdown file. The hooks and MCP server write to `raw/` only.
2. **Compile (batched, you schedule it).** `athenaeum run` (or the
   on-demand `athenaeum ingest` / `athenaeum session-end`, see [Daily
   operation](daily-operation.md)) reads pending `raw/` files, passes them
   through the tiered librarian, and writes compiled entity pages to
   `wiki/`. The hooks and `recall` only search `wiki/`.

There is no automatic bridge between the two. If you `remember` five
things and then `recall` returns nothing, the observations are safe on
disk — they just haven't been compiled yet.

## I want the actual round-trip verified without a live session

```bash
# 1. Build the index
bash examples/claude-code/session-start-recall.sh

# 2. Simulate a prompt (stdin is JSON)
echo '{"prompt":"tell me about innovation accounting","session_id":"test"}' \
  | bash examples/claude-code/user-prompt-recall.sh
```

Expected: a single-line JSON object with a `hookSpecificOutput` key
listing matching wiki pages. Empty output means either the wiki has no
relevant pages, or the index hasn't been built — check
`~/.cache/athenaeum/`.

## I want to bridge Claude Code's own auto-memory into Athenaeum

That's a separate integration — see [Claude Code auto-memory
integration](claude-code.md). It's complementary to the per-turn recall
hooks above and can be run alongside them.

## I want to know which knobs the hooks read

The hooks read `AUTO_RECALL` (per-turn recall on/off) and `SEARCH_BACKEND`
(`fts5` or `vector`) from the shell environment after sourcing
`~/.cache/athenaeum/config.env` — so an export in your shell profile beats
the cached config. That's intentional (it lets you A/B-test a backend
without editing `athenaeum.yaml`), and it's the first thing to check when
a hook seems to "ignore" a config change. The full environment-variable
table lives in [reference/configuration.md](../reference/configuration.md);
troubleshooting the hooks themselves is in [Troubleshooting](troubleshooting.md).

## See also

- Guides — [Claude Code integration](claude-code.md) · [Daily operation](daily-operation.md) · [Vector search](vector-search.md) · [Troubleshooting](troubleshooting.md)
- Modules — [recall](../modules/recall.md) · [mcp](../modules/mcp.md)
- Design — [recall architecture](../design/recall-architecture.md)
- Reference — [configuration](../reference/configuration.md)
