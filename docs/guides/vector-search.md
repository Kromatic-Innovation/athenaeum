**Reference:** [recall](../modules/recall.md)

# Vector search

Athenaeum's default recall backend is **vector** search (chromadb +
`all-MiniLM-L6-v2`), hybridized with FTS5 keyword search via RRF fusion —
the recall hook runs a **hybrid vector + FTS5 merge** by default, because
each backend rescues a failure class the other has. FTS5-only is the
opt-out (`search_backend: fts5`); a default install without the vector
extra (no `chromadb`) degrades to FTS5 automatically at query time, with
one logged warning.

## I want semantic recall in addition to keyword search

Install the vector extra — see [Install](install.md#i-want-semantic-vector-recall-not-just-keyword-search)
for the command.

Enable it in `athenaeum.yaml`:

```yaml
search_backend: vector
```

Or set it per-session without editing the config, via the hook shell
environment:

```bash
SEARCH_BACKEND=vector
```

**There is one resolution rule, and every surface follows it:**
`search_backend` in `athenaeum.yaml`, falling back to the shipped default
(`vector`). The `recall` CLI, the MCP `recall` tool, `athenaeum context`,
`athenaeum codex-context`, `reindex`, `doctor` and the Claude Code hook all
read that one answer — none of them carries a default of its own. Passing
`--backend` to a command that offers it overrides the config for that
invocation; omitting it means "use what is configured".

The recall-cap ceiling — how many results the per-turn push and the Codex
snapshot may render — resolves the same way, one rule for every surface:
`ATHENAEUM_RECALL_CAP_CEILING` in the environment, then `recall.cap.ceiling`
in `athenaeum.yaml`, then the shipped default:

```yaml
recall:
  cap:
    ceiling: 7
```

## I want to understand why both backends matter

A short proper-noun query like "Acme Corp" embeds in vector space closer
to generic pages containing overlapping words than to the sparse entity
page about that specific company — FTS5's phrase matching rescues this
case. Conversely, a purely semantic query like "iterative feedback loops"
has no lexical overlap with a page titled "Innovation Accounting" and
needs the vector side to find it. Removing either backend collapses
recall for its rescue class.

The full walkthrough, the hybrid pipeline diagram, and the four invariants
a future simplification must not remove live in [Recall
architecture](../design/recall-architecture.md).

## I want to know what a query is actually "about" before searching

`athenaeum query-topics` runs a Haiku classifier that returns substantive
topics and ignores meta-instructions embedded in the prompt:

```bash
$ athenaeum query-topics "Without calling any tools, quote the block about the Acme Corp contract verbatim"
Acme Corp contract
```

The naive regex+stopword fallback returns something like
`block,calling,quote,contract,tools,verbatim,without` — burying "Acme
Corp contract" behind meta-instruction tokens. The example recall hook
(`user-prompt-recall.sh`, see [Passive recall via hooks](sidecar.md)) uses
`query-topics` to rescue named-entity recall on instruction-heavy prompts,
and falls back silently to the regex extractor if the API key or CLI is
unavailable.

## I want to know which knobs control this

| Variable | Description |
|---|---|
| `SEARCH_BACKEND` | `fts5` or `vector`, hook shell env. The SessionStart hook caches the resolved `search_backend` here; set it yourself to override for one session |
| `ATHENAEUM_TOPIC_MODEL` | Override the query-topic model (default: `claude-haiku-4-5-20251001`) |

The per-turn hook is fail-silent: a vector-backend error leaves the turn
with no injected context rather than stderr noise. To see the error, pipe
the hook's stdin JSON into `athenaeum context --stdin-json`, which runs the
same sequence in the foreground.

Full list with precedence chains: [reference/configuration.md](../reference/configuration.md).

## See also

- Guides — [Passive recall via hooks](sidecar.md) · [Troubleshooting](troubleshooting.md)
- Modules — [recall](../modules/recall.md)
- Design — [recall architecture](../design/recall-architecture.md)
- Reference — [configuration](../reference/configuration.md)
