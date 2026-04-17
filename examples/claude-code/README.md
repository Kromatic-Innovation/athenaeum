# Claude Code integration

Three hook scripts that wire Athenaeum into Claude Code as a transparent
recall sidecar. The scripts are plain bash + sqlite3 + (optional) Python;
nothing here is Claude-Code-specific that couldn't be ported to another
agent runtime.

| Hook                     | When it fires        | What it does                                                    |
|--------------------------|----------------------|------------------------------------------------------------------|
| `session-start-recall.sh`| Start of each session| Builds the FTS5 (and optional vector) index, caches config       |
| `user-prompt-recall.sh`  | Each user turn       | Hybrid FTS5+vector search, injects top-3 wiki page names         |
| `pre-compact-save.sh`    | Before compaction    | Reminds the model to call `remember` on anything load-bearing    |

## Install

1. Initialise a knowledge base if you don't have one:

   ```bash
   pip install athenaeum
   athenaeum init --path ~/knowledge
   ```

2. Copy (or symlink) the scripts somewhere on disk and mark them executable:

   ```bash
   mkdir -p ~/.claude/hooks/athenaeum
   cp examples/claude-code/*.sh ~/.claude/hooks/athenaeum/
   chmod +x ~/.claude/hooks/athenaeum/*.sh
   ```

3. Merge `settings-snippet.json` into `~/.claude/settings.json`, replacing
   `/path/to/` with the directory from step 2.

4. (Optional, MCP remember/recall) Register the MCP server. Add to
   `~/.claude/settings.json`:

   ```json
   {
     "mcpServers": {
       "athenaeum": { "command": "athenaeum", "args": ["serve"] }
     }
   }
   ```

5. Restart Claude Code. The session-start message should say
   `[Knowledge] FTS5 index: N wiki pages`.

## Smoke test

Verify the pipeline without a live session:

```bash
# 1. Build the index
bash examples/claude-code/session-start-recall.sh

# 2. Simulate a prompt (stdin is JSON)
echo '{"prompt":"tell me about innovation accounting","session_id":"test"}' \
  | bash examples/claude-code/user-prompt-recall.sh
```

Expected: a single-line JSON object with a `hookSpecificOutput` key listing
matching wiki pages. Empty output means either your wiki has no relevant
pages or the index hasn't been built — check `~/.cache/athenaeum/`.

## Environment variables

| Variable                 | Default                           | Purpose                                                       |
|--------------------------|-----------------------------------|----------------------------------------------------------------|
| `KNOWLEDGE_ROOT`         | `~/knowledge`                     | Knowledge base root                                            |
| `KNOWLEDGE_WIKI_PATH`    | `$KNOWLEDGE_ROOT/wiki`            | Wiki directory (if non-standard layout)                        |
| `ATHENAEUM_CLI`          | `athenaeum`                       | CLI binary (override for editable installs)                    |
| `ATHENAEUM_PYTHON`       | `python3`                         | Python interpreter with athenaeum deps                         |
| `ATHENAEUM_SRC`          | —                                 | Source checkout path (skips `pip install`, runs from source)   |
| `ATHENAEUM_OP_KEY_PATH`  | `op://Agent Tools/Anthropic API Key/credential` | 1Password secret reference for `ANTHROPIC_API_KEY` |
| `ATHENAEUM_HOOK_DEBUG`   | `0`                               | Set to `1` to log vector-backend errors to stderr              |
| `SEARCH_BACKEND`         | from `athenaeum.yaml` (`fts5`)    | `fts5` (default) or `vector`                                   |
| `AUTO_RECALL`            | from `athenaeum.yaml` (`true`)    | Set to `false` to disable per-turn recall                      |

## Hybrid recall: why both backends

Short proper-noun queries like "Return Path" embed in vector space
closer to generic pages containing "path" than to a sparse entity page
about the company. FTS5 phrase matching rescues these. Conversely,
purely semantic queries ("iterative feedback loops" → "Innovation
Accounting") have no lexical overlap and need the vector side.

Removing either backend collapses recall for its rescue class. See
`docs/recall-architecture.md` for the full walkthrough and the four
invariants a future "simplification" must not remove.

## 1Password bootstrap (optional)

The LLM topic extractor (`athenaeum query-topics`) needs a real
`ANTHROPIC_API_KEY` for the Messages API. Claude Code's own
`CLAUDE_CODE_OAUTH_TOKEN` is rejected with `401 OAuth authentication
is currently not supported`, so we can't reuse it.

If you have the [1Password CLI](https://developer.1password.com/docs/cli/)
signed in, `session-start-recall.sh` will `op read` the key at
`ATHENAEUM_OP_KEY_PATH` and cache it at `~/.cache/athenaeum/config.env`
(mode 600, owner-only). Silent on any failure.

Without the key, the hook falls back to a regex+stopword extractor —
still good, just less topic-aware.

## Troubleshooting

| Symptom                                         | Check                                                                                           |
|-------------------------------------------------|-------------------------------------------------------------------------------------------------|
| Session message shows `0 wiki pages`            | `$KNOWLEDGE_ROOT/wiki/` is empty or unreadable                                                 |
| No `[Knowledge context]` on user turns          | Run `sqlite3 ~/.cache/athenaeum/wiki-index.db 'select count(*) from wiki'` — should be > 0     |
| Vector backend silent                           | Re-run with `ATHENAEUM_HOOK_DEBUG=1` — usually `pip install athenaeum[vector]` missing         |
| `query-topics` running without its API key      | `cat ~/.cache/athenaeum/config.env` — should contain `ANTHROPIC_API_KEY=...`                   |
| Hook ran "green" but recall never fires         | Check the settings-snippet was merged correctly: `grep UserPromptSubmit ~/.claude/settings.json`|
