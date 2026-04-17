# Recall architecture

How the UserPromptSubmit hook surfaces wiki context — the hybrid FTS5 + vector pipeline, the optional LLM query-topic preprocessor, and the load-bearing invariants that a future "simplification" must not remove.

## Pipeline

```
┌─────────────────┐    ┌──────────────────┐    ┌────────────┐    ┌───────────┐
│ UserPromptSubmit│ -> │ query-topics     │ -> │ FTS5 + vec │ -> │ injected  │
│  (raw prompt)   │    │ (Haiku, optional)│    │ hybrid     │    │ context   │
└─────────────────┘    └──────────────────┘    └────────────┘    └───────────┘
                              │
                              ▼ (any failure)
                       regex + stopword
                       fallback extractor
```

1. **Source config.** `~/.cache/athenaeum/config.env` is sourced under `set -a` / `set +a` so that `ANTHROPIC_API_KEY` propagates to child processes. Without `set -a`, the LLM topic extractor silently runs without its key.

2. **Query-topic extraction (optional, LLM).** `athenaeum query-topics "$PROMPT" --timeout 3` calls a cheap Haiku classifier that returns a JSON array of substantive topics, ignoring meta-instructions like "quote verbatim" or "don't call tools". On any failure (missing CLI, missing API key, timeout, bad JSON), the hook falls back silently to a regex + stopword extractor.

3. **Hybrid search.**
   - **FTS5** (`wiki-index.db`) — lowercased-and-phrase-quoted OR query, top 3 by BM25 rank, excluding session-seen filenames.
   - **Vector** (`wiki-vectors/`, runs when `SEARCH_BACKEND=vector`) — embeds the *concatenated topics* (not the raw prompt; meta-instructions drift the embedding), queries chromadb, returns top 3.
   - **Merge** — FTS5 first, then vector, dedupe by filename, cap at 3.

4. **Session dedup.** `/tmp/knowledge-seen-${SESSION_ID}` accumulates already-surfaced filenames across turns.

5. **Emit.** `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}`. A flat `{"additionalContext":...}` payload is silently ignored by Claude Code.

## Why hybrid — and why both layers are load-bearing

**FTS5 alone** is fragile on synonym or paraphrase queries. Asking about "iterative feedback loops" won't match the wiki page titled "Innovation Accounting" because there's no lexical overlap.

**Vector alone** is fragile on short proper-noun queries. Concrete failure:

> Query: `"Return Path"`
> Nearest vector neighbour: `reference_local_paths.md`
> Distance from the actual entity page: larger than the collision

Short strings are dominated by their common-word components. Out-of-the-box sentence embeddings place "Return Path" closer to `local paths` (sharing "path") than to the sparse entity page for the company "Return Path". FTS5 phrase match on `"return path"` resolves this trivially — no embedding can out-match a literal phrase hit.

**Conclusion:** the hybrid merge is not defence-in-depth. **Each backend rescues a class of queries the other handles poorly.** Removing either collapses recall on its rescue class. When `SEARCH_BACKEND=vector`, the example SessionStart hook still builds FTS5 as a secondary index for the same reason — FTS5 rebuild is ~1s on a 3k-page wiki, cheap next to vector's ~45s.

## Why the LLM preprocessor exists

The regex+stopword fallback extractor sorts words alphabetically and takes the first 8. On a meta-heavy prompt like

> *"Without calling any tools, quote the block about Return Path verbatim."*

the word `return` lands 10th alphabetically and gets dropped. Vector embedding of the raw prompt drifts toward *"without tools / quote / verbatim"* — hook/tooling pages, not entities.

The LLM preprocessor returns `["Return Path"]`, ignoring the meta-wrapper, and both backends then land correctly. It's a cheap Haiku call (~200ms) with a 3s timeout and silent fallback.

## ANTHROPIC_API_KEY bootstrap (SessionStart)

Claude Code authenticates with `CLAUDE_CODE_OAUTH_TOKEN` (starts `sk-ant-o`), scoped to its inference endpoint. Passing that token to the general Anthropic Messages API returns:

```
401 OAuth authentication is currently not supported
```

So the LLM preprocessor needs a real console API key, which it reads from `ANTHROPIC_API_KEY`. The reference `session-start-recall.sh` fetches it from 1Password when:

- `ANTHROPIC_API_KEY` isn't already exported, AND
- the `op` CLI is signed in

```bash
op read "op://Agent Tools/Anthropic API Key/credential"
```

Override path via `ATHENAEUM_OP_KEY_PATH`. The fetched key is cached in `~/.cache/athenaeum/config.env` with `0600` perms. Every failure mode is silent — the recall hook then degrades to the regex fallback.

## Failure modes and diagnostics

| Symptom | First check |
|---|---|
| Recall misses proper noun in meta-heavy prompt | `grep ANTHROPIC_API_KEY ~/.cache/athenaeum/config.env` — if missing, `op whoami` |
| Hook runs but model never references the injection | Shape mismatch — output must be `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",...}}`, not flat `{"additionalContext":...}` |
| Short entity name returns unrelated page | Vector collision — verify the FTS5 db exists and is being merged first |
| LLM extraction returns `[]` every time | `set -a` / `set +a` missing around `source config.env` — var not exported to child process |
| `athenaeum query-topics` hangs | 3s timeout should kick in; if not, check `ATHENAEUM_PYTHON` points to an env with the athenaeum CLI |

## Load-bearing invariants

Do not simplify any of these without reading this page and the related commit history.

- `set -a` around `source "$CONFIG_ENV"` — required for subprocess env inheritance.
- FTS5 is maintained even when vector is primary — required for short-query rescue.
- JSON output shape includes `hookSpecificOutput.hookEventName` — a flat `{"additionalContext":...}` payload is silently ignored.
- Console API key is fetched separately from the Claude Code OAuth token — OAuth is rejected by the Messages API with 401.

## References

- Live production hook (hardened): `Kromatic-Innovation/code-workspace-config/scripts/hooks/knowledge-recall-on-turn.sh`
- Fix commits: cwc 781a306 (JSON shape), 99fbc1a (set -a), 1496814 (hybrid merge), e38fb0e (op-read bootstrap)
- Related PRs: athenaeum #40 (JSON shape), #42 (query-topics CLI)
