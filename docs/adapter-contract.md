# The source → raw-intake adapter contract

Athenaeum's raw-intake write API is the seam any external source uses to feed
data into the wiki. The rule is structural, not trust-based (see
[`docs/why-athenaeum.md`](why-athenaeum.md)): **a source may only _append_ a
raw file to the intake tree; a separate compiler — the librarian — is the only
writer to the wiki.** An "adapter" is any script or integration that turns some
external source (an API, an export file, a message feed, a scraper) into
raw-intake files shaped so the librarian can compile them.

This document is that contract. It is deliberately narrow and stable:
athenaeum's OSS surface stops at **the on-disk raw-intake shape and the MCP
write API** — everything downstream (clustering, merge, contradiction
detection, retirement) is the librarian's job, not the adapter's (see
[`docs/provenance-shape.md`](provenance-shape.md) §6, "Enricher Protocol").

> **Want a guided walkthrough instead of a spec?** The bundled
> [`adapter-authoring`](../skills/adapter-authoring/SKILL.md) skill teaches an
> agent (or a human) how to build a custom adapter step by step. This document
> is the reference it points at.

---

## Two intake lanes

There are two intake conventions. Pick the one that matches your source.

| | **Lane A — entity intake** | **Lane B — auto-memory intake** |
|---|---|---|
| Path | `raw/<source>/<timestamp>-<uuid8>.md` | `raw/auto-memory/<scope>/<prefix>_<slug>.md` |
| Discovered by | `athenaeum.discover_raw_files` | `athenaeum.librarian.discover_auto_memory_files` |
| Record type | `athenaeum.RawFile` | `AutoMemoryFile` |
| Compilation | tiered compile → entity wiki pages | cluster → merge → `wiki/auto-*.md` |
| Write helper | MCP `remember` tool | none (direct file drop) |
| Best for | **most new adapters** — any external source of facts | agent session-memory bridges (e.g. Claude Code) |

**If in doubt, use Lane A.** It is the general-purpose seam, it has a sanctioned
write helper (the MCP `remember` tool), and it flows through the full tiered
compile. Lane B is a specialised lane for bridging an agent runtime's own
first-party memory directory; it is documented end to end, with a complete
worked example, in
[`docs/integrations/claude-code.md`](integrations/claude-code.md) — read that if
your "source" is really an agent's memory folder.

The rest of this document specifies **Lane A**.

---

## 1. Location convention

Write each intake file to:

```
<knowledge-root>/raw/<source>/<timestamp>-<uuid8>.md
```

- **`<source>`** is a stable, human-readable name for your adapter — it becomes
  the intake subdirectory and is carried through the pipeline so a compiled
  wiki claim can be traced back to the adapter that produced it. Keep it to
  alphanumerics plus `-`/`_` (the MCP `remember` tool sanitises to exactly that
  set). Examples: `raw/press-releases/`, `raw/crm-export/`, `raw/notes/`.
- **`<timestamp>`** is UTC, formatted `YYYYMMDDTHHMMSSZ`.
- **`<uuid8>`** is 8 hexadecimal characters (use `athenaeum.generate_uid()`, or
  the first 8 chars of a `uuid4().hex`).

The canonical filename is matched by
`athenaeum.librarian.RAW_FILE_RE = ^(\d{8}T\d{6}Z?)-([0-9a-f]{8})\.md$`. Files
that do not match this pattern are still discovered (with an empty
timestamp/uuid), but the canonical `<timestamp>-<uuid8>.md` name is what tooling
expects and what keeps files naturally time-ordered.

**Reserved names.** `raw/answers/` is skipped by discovery — it holds
resolution *output*, not new intake — as is any `.gitkeep`. Do not write your
adapter's files there. `raw/auto-memory/` is Lane B's tree; keep Lane A adapters
in their own `raw/<source>/` subdirectory.

`athenaeum init` scaffolds `raw/` (and `raw/sessions/`, `raw/auto-memory/`) so a
fresh knowledge root is ready to receive intake.

---

## 2. File shape / frontmatter

A raw-intake file is **YAML frontmatter + a markdown body**:

```markdown
---
name: acme-widget-launch
description: Acme announced the Widget on 2026-01-15.
source: external:https://example.com/press/widget-launch
---

# Acme Widget launch

Acme announced the Widget on 2026-01-15. Every claim in the body should be
traceable to the declared `source`.
```

Frontmatter is parsed by `athenaeum.parse_frontmatter` and rendered by
`athenaeum.render_frontmatter`. The schema is intentionally **open** — unknown
keys round-trip cleanly (tier-0 passthrough preserves them byte-for-byte), so
you can carry adapter-specific metadata without breaking the compiler.

The keys the compiler actually reads:

| Field | Required | Purpose |
|---|---|---|
| `source` | **strongly recommended** | Provenance. Scalar `<type>:<ref>` (e.g. `external:<url>`, `api:<vendor>:<date>`, `document:<path>`). Declares the *ultimate* origin of the fact. |
| `name` | optional | Short slug for the entity/topic; helps the compiler place and title the entry. |
| `description` | optional | One-line summary. |
| `field_sources` | optional | Per-field / per-value attribution (see [`docs/provenance-shape.md`](provenance-shape.md) §2). |
| `access` | optional | Read-time audience label (intake screening, see the MCP `remember` tool). |

**Cite the ultimate source, never the raw file.** A `source:` of
`raw/notes/…​.md` is wrong — cite the URL, API, document, or person the fact
actually came from. The provenance grammar and the full `source_type`
vocabulary (`external`, `document`, `api:*`, `user-stated`, `agent-observed`,
`model-prior`, …) are specified in
[`docs/provenance-shape.md`](provenance-shape.md) §10 and
[`policies/auto-memory-citation.md`](../policies/auto-memory-citation.md).

Provenance validation is **fail-open**: a malformed or unknown `source_type`
downgrades to `inferred` with a breadcrumb rather than crashing the nightly
compile. Aim to get it right, but a typo will never wedge the pipeline.

---

## 3. Idempotency

Idempotency comes from **convention**, not from a shared dedupe helper an
adapter must call:

- **Write-once, unique filenames.** Each `<timestamp>-<uuid8>.md` is unique per
  write, so an adapter never overwrites a prior intake file. Re-running your
  adapter appends new files; it does not mutate old ones.
- **Do not dedupe against prior intake before writing.** Near-duplicate
  collapsing happens at *compile* time — the librarian clusters near-identical
  files by similarity and merges them into a single wiki entry — so an adapter
  can safely re-emit a fact it has emitted before. The compiler absorbs it.
- **Prefer append over rewrite for corroboration.** If your source re-confirms a
  fact, write a new intake file (or, for list-valued provenance, append a new
  entry to `sources[]`) rather than rewriting an existing file. Rewrites destroy
  provenance; the merge pass dedupes corroborating sources by identity, so
  duplicate appends are harmless.
- **Write atomically.** Write to a same-directory temp file and `os.replace` it
  into place. A crash mid-write must never leave the librarian a half-written
  `---` block to parse. (The MCP `remember` tool writes brand-new unique files,
  so a plain write suffices there; a direct-drop adapter should still prefer the
  atomic pattern shown in the example.)
- **Stay inside `raw/`.** Resolve your target path and verify it is inside the
  `raw/` tree (use `Path.is_relative_to`, not a string prefix compare) and never
  under `wiki/`. The wiki has exactly one writer — the librarian.

---

## 4. Compilation reconciliation

Once files are in `raw/<source>/`, a single `athenaeum run` reconciles them.
An adapter does **not** perform any of these steps — it only needs to know they
happen so it can shape its writes sensibly:

1. **Discover** — `discover_raw_files` walks `raw/<source>/` and returns
   `RawFile` records (path, source, timestamp, uuid, content).
2. **Compile** — the tiered pipeline classifies each raw file and creates or
   updates the corresponding entity wiki page. Tier-0 passthrough preserves
   frontmatter byte-for-byte for already-shaped files.
3. **Dedupe / merge** — near-duplicate claims are coalesced; `source` and
   `field_sources` are carried forward and merged (canonical wins per key,
   absorbed-only keys retained) — see
   [`docs/conflict-resolution.md`](conflict-resolution.md).
4. **Detect contradictions** — when a new claim disagrees with an existing one,
   the pair surfaces in `wiki/_pending_questions.md` for review rather than
   silently overwriting.
5. **Retire (expiry).** Raw intake is an *expiring queue*, not a permanent
   store: once a fact is folded into the compiled wiki (and is not
   contradiction-flagged or still referenced by an open question), the raw file
   may be retired. Retirement is a `git rm` after a provenance-snapshot commit —
   the file stays recoverable from git history, never hard-deleted.

The whole pipeline is idempotent: a second `athenaeum run` with no new intake is
a no-op.

---

## 5. How to write intake files

There are two supported mechanisms.

### 5a. The MCP `remember` tool (highest level)

If your integration can speak the MCP protocol, the sanctioned write path is the
`remember` tool exposed by `athenaeum serve` (see the
[MCP memory server](../README.md#mcp-memory-server) section of the README). It:

- writes to `raw/<source>/<timestamp>-<uuid8>.md` for you,
- path-guards the write (stays in `raw/`, never `wiki/`),
- screens content for sensitive material and stamps an `access:` label,
- injects/merges the `source` / `field_sources` provenance frontmatter.

```text
remember(
  content="Acme announced the Widget on 2026-01-15.",
  source="press-releases",
  sources="external:https://example.com/press/widget-launch",
)
```

The `sources=` argument grammar (scalar shorthand, `_source`,
`_field_sources`, and the channel-split extras `_source_type` / `_source_ref` /
`_model` / `_on_behalf_of` / `_asserter`) is specified in
[`docs/provenance-shape.md`](provenance-shape.md) §4 and §10.4.

### 5b. Direct file drop

If MCP is not available (a cron job, a one-shot importer, a language other than
Python calling the CLI), write the file directly following §1–§3. A minimal,
synthetic, runnable example lives at
[`examples/adapters/minimal_adapter.py`](../examples/adapters/minimal_adapter.py):
it uses only the public `render_frontmatter` / `generate_uid` helpers, writes
atomically, path-guards, and declares provenance. Copy it as a starting point.

```bash
python -m athenaeum init --path /tmp/kb            # scaffold raw/ + wiki/
python examples/adapters/minimal_adapter.py /tmp/kb --source press-releases
python -m athenaeum run --path /tmp/kb             # compile raw → wiki
ls /tmp/kb/wiki                                     # see the compiled entity
```

---

## See also

- [`skills/adapter-authoring/SKILL.md`](../skills/adapter-authoring/SKILL.md) — the bundled, user-invocable adapter-authoring skill (the guided version of this contract).
- [`examples/adapters/minimal_adapter.py`](../examples/adapters/minimal_adapter.py) — the synthetic worked example (Lane A, direct drop).
- [`docs/integrations/claude-code.md`](integrations/claude-code.md) — a complete worked adapter for Lane B (auto-memory), including the frontmatter/citation contract.
- [`docs/provenance-shape.md`](provenance-shape.md) — the design lock for on-disk provenance shape and the MCP `remember` API.
- [`docs/why-athenaeum.md`](why-athenaeum.md) — why the append-only-intake / single-compiler split exists.
- [`policies/auto-memory-citation.md`](../policies/auto-memory-citation.md) — "cite the ultimate source, never the raw file."
