# Storage-adapter layer (entity class → surface + corpus policy)

> **Status:** internal seam (issue #429). The Python API
> (`athenaeum.storage`) is importable but not part of the stable `__all__`
> surface; signatures may change between minor releases until this contract is
> promoted to a stable extension point.

Athenaeum persists every compiled entity as a markdown page in a flat `wiki/`
tree, and every corpus consumer — the embedder, `recall`, and the wiki-dedup
merge engine — scans that tree. That single hardcoded decision is fine until a
class of entity should live *somewhere else with a different corpus policy*:
archival contact data that must stay out of recall (#427), or skill files that
want an athenaeum-like sync without joining the recalled corpus (#426,
deferred).

The **storage-adapter layer** makes that decision a **configuration choice,
changeable later**. Each entity class resolves to a **storage surface**:

```
entity class  ──►  storage adapter  ──►  { backing_store, corpus_policy }
   (type:)          (named surface)        where it lives + what it joins
```

## Not to be confused with intake adapters

This is a **storage-surface adapter**, a different concept from the
source → raw-intake **adapter** in [`adapter-contract.md`](adapter-contract.md)
and the bundled `adapter-authoring` skill:

| | Intake adapter (`adapter-contract.md`) | Storage adapter (this doc) |
|---|---|---|
| Turns | an external source → `raw/` files | an entity **class** → a persisted **surface** |
| Governs | how data *enters* the pipeline | where a compiled class *lives* + its corpus policy |
| End of pipeline | upstream (before compile) | downstream (persistence) |

They never collide. If you are feeding a new external source in, you want an
*intake* adapter. If you want a class of pages to live off the recalled corpus,
you want a *storage* adapter.

## The three corpus capabilities

A surface's `corpus_policy` declares participation in three **orthogonal**
capabilities:

| Capability | Meaning |
|---|---|
| `embedded` | pages are indexed into the FTS5 / vector store |
| `recallable` | pages are eligible to be returned by `recall` |
| `merge_eligible` | pages may be proposed for wiki-dedup consolidation |

## Built-in adapters

Two adapters ship built in and need no configuration:

### `wiki-markdown-embedded` (the default)

Backing store: the flat `wiki/` markdown tree. Corpus policy: **all-true**.
**Every entity class maps here unless config says otherwise**, so a knowledge
base with no `storage:` config behaves byte-for-byte as it did before this
layer existed. "The wiki is just the default adapter."

### `excluded`

Backing store: markdown on a surface **outside `wiki/`** (default `excluded/`).
Corpus policy: **all-false** — nothing on it is embedded, recalled, or merged.
This is what #427's PII / archival-contact surface consumes.

**Exclusion is by construction.** An excluded surface's root lives outside the
corpus scanners' search set (`wiki/` plus the configured
`recall.extra_intake_roots`), so its pages are excluded from embed / recall /
merge *without any change to those scanners* — the fail-closed property #427
requires. A `pii: true` flag would fail *open* (one unflagged page leaks); a
separate path fails *closed* (a new page under the excluded root is invisible to
the corpus by default).

## Configuration

Everything lives under the `storage:` key in `athenaeum.yaml`. Unset = every
class on the default wiki surface.

```yaml
storage:
  # Route entity classes (the wiki `type:`) onto adapters.
  mapping:
    pii: excluded            # send the pii class to the built-in excluded surface

  # Optionally define custom adapters (built-ins are always available).
  adapters:
    contacts-excluded:
      backing_store: markdown
      surface_root: contacts   # relative to the knowledge root, or absolute;
                               # keep OUTSIDE wiki/ to be excluded by construction
      corpus_policy:
        embedded: false
        recallable: false
        merge_eligible: false
```

### Fail-closed policy defaults

Each `corpus_policy` key **fails closed**: an omitted (or malformed) capability
defaults to `false`. A custom surface participates in the corpus only where it
*explicitly* opts in, so a half-written policy excludes — it never leaks a
surface into recall. Only the built-in `wiki-markdown-embedded` adapter is
all-true.

### Loud on misconfiguration

A `mapping` that names an adapter that does not exist, a custom adapter that
reuses a built-in name, or an adapter definition missing `backing_store` /
`surface_root` raises `StorageConfigError` at resolution time. The layer never
silently falls back to the default surface — that would route a class the
operator meant to *exclude* straight into the corpus.

## Extending: add a surface with no core change

Adding a new surface is **config + an adapter**, with no change to the
embed / recall / merge core:

- **From config** — define it under `storage.adapters` and map a class to it
  under `storage.mapping` (as above).
- **From code** — call `athenaeum.storage.register_adapter(...)` at import time
  (the seam #426's deferred skill-file-sync surface would use):

  ```python
  from athenaeum.storage import StorageAdapter, CorpusPolicy, register_adapter

  register_adapter(
      StorageAdapter(
          name="skill-sync",
          backing_store="sqlite",
          surface_root="skills",
          corpus_policy=CorpusPolicy.none(),
      )
  )
  # then map a class to it in athenaeum.yaml:  storage.mapping.skill: skill-sync
  ```

A code-registered adapter can never shadow a built-in, and a config-defined
adapter of the same name overrides a code-registered one (config wins).

## Consumer / writer API

`athenaeum.storage` exposes the resolution helpers a writer or a corpus
consumer needs:

| Function | Returns |
|---|---|
| `resolve_adapter_for_class(cls, config)` | the resolved `StorageAdapter` |
| `surface_root_for_class(cls, config, knowledge_root)` | absolute on-disk root where the class lives (the writer entry point #427 consumes instead of a hardcoded path) |
| `is_embedded / is_recallable / is_merge_eligible(cls, config)` | the individual policy bits |
| `is_excluded(cls, config)` | `True` when the class joins no corpus capability |

The wiki-dedup merge pass already consults `is_merge_eligible` (see
`wiki_dedupe.discover_wiki_dedupe_candidates`): a class routed to a
non-merge-eligible surface is dropped from merge candidates even if a page of
that class happens to sit in `wiki/` — fail-closed defense-in-depth on top of
the by-construction path exclusion.
