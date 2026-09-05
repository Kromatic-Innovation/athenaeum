**Reference:** [recall](../modules/recall.md)

# Vector search

Athenaeum's default recall backend is FTS5 keyword search. A vector
backend (chromadb + `all-MiniLM-L6-v2`) is available for semantic recall
alongside it — the recall hook runs a **hybrid FTS5 + vector merge** once
vector is configured, because each backend rescues a failure class the
other has.

## I want semantic recall in addition to keyword search

```bash
pip install 'athenaeum[vector]'
```

Enable it in `athenaeum.yaml`:

```yaml
search_backend: vector
```

Or set it per-session without editing the config, via the hook shell
environment:

```bash
SEARCH_BACKEND=vector
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
| `SEARCH_BACKEND` | `fts5` or `vector`, hook shell env (default: `fts5`) |
| `ATHENAEUM_TOPIC_MODEL` | Override the query-topic model (default: `claude-haiku-4-5-20251001`) |
| `ATHENAEUM_HOOK_DEBUG` | Set to `1` to log vector-backend errors from the recall hook to stderr |

Full list with precedence chains: [reference/configuration.md](../reference/configuration.md).

## See also

- Guides — [Passive recall via hooks](sidecar.md) · [Troubleshooting](troubleshooting.md)
- Modules — [recall](../modules/recall.md)
- Design — [recall architecture](../design/recall-architecture.md)
- Reference — [configuration](../reference/configuration.md)
