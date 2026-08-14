# Storage-adapter layer (entity class → surface + corpus policy)

> **Status:** internal seam (issue athenaeum#429). The Python API
> (`athenaeum.storage`) is importable but not part of the stable `__all__`
> surface; signatures may change between minor releases until this contract is
> promoted to a stable extension point.

Athenaeum persists every compiled entity as a markdown page in a flat `wiki/`
tree, and every corpus consumer — the embedder, `recall`, and the wiki-dedup
merge engine — scans that tree. That single hardcoded decision is fine until a
class of entity should live *somewhere else with a different corpus policy*:
archival contact data that must stay out of recall (athenaeum#427), or skill files that
want an athenaeum-like sync without joining the recalled corpus (athenaeum#426,
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
capabilities. Each is **enforced** — a class routed to a surface that opts out
of a capability is dropped from it, even for a page that physically sits inside
`wiki/` (issue athenaeum#532). The enforcement point per capability:

| Capability | Meaning | Enforced at |
|---|---|---|
| `embedded` | pages are indexed into the FTS5 / vector store | index **build** — a non-`embedded` class is dropped by the scan (`search._scan_indexed_records`), the same way a `pii:`-flagged page is (athenaeum#427). No persisted index for the keyword backend, so it is inert there. |
| `recallable` | pages are eligible to be returned by `recall` | recall **render** — a non-`recallable` class is dropped against fresh on-disk frontmatter (`mcp_server._recall_via_backend`, and the `athenaeum recall` CLI), the same fail-closed re-check the audience predicate uses (athenaeum#312 Layer C). Applies to every backend. |
| `merge_eligible` | pages may be proposed for wiki-dedup consolidation | candidate discovery — a non-`merge_eligible` class is dropped from merge candidates (`wiki_dedupe.discover_wiki_dedupe_candidates`). |

All three are **defense-in-depth on top of the by-construction path exclusion**
(a restricted surface lives outside `wiki/`, so its pages are never scanned):
they additionally cover the case where a page of a restricted class happens to
sit inside `wiki/`. All three are a **strict no-op for the default,
unconfigured knowledge base** — every class maps to the all-true
`wiki-markdown-embedded` surface, so nothing is ever dropped.

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
This is what athenaeum#427's PII / archival-contact surface consumes.

**Exclusion is by construction.** An excluded surface's root lives outside the
corpus scanners' search set (`wiki/` plus the configured
`recall.extra_intake_roots`), so its pages are excluded from embed / recall /
merge *without any change to those scanners* — the fail-closed property athenaeum#427
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

  # Optionally name which frontmatter fields on an EXCLUDED record hold data
  # (athenaeum#883). Keyed by SURFACE class — the `mapping` key above, not a
  # wiki page's `type:`. Unset = the built-in policy below.
  excluded_fields:
    pii: [emails, phones, former_emails, alt_emails]
```

### Which fields on an excluded record are data

An excluded record carries both data and its own bookkeeping (`uid`, `type`,
`pii`, the bounce/classification marks). A read of that record has to know
which is which. `storage.excluded_fields` names it explicitly per surface
class; absent an entry, the default resolves in two branches:

- **`pii`** keeps its built-in allowlist (`emails`, `phones`, `former_emails`,
  `alt_emails`) verbatim, so a person read is byte-identical to what it was
  before the read became class-generic.
- **Any other class** defaults to *every frontmatter field on the record minus
  a bookkeeping denylist* (`uid`, `type`, `pii`, `identifier`,
  `identifier_validity`, `contact_classification`, `folded_into`, `source`,
  `observed_at`, `valid_until`, `bounce_diagnostic`).

The denylist-complement is deliberate for the unknown class. An allowlist for a
class nobody has enumerated makes the redaction marker **dishonest by
omission**: a field the allowlist forgot is reported neither as a value nor as a
marker, so "withheld" and "absent" collapse into the same shape — precisely the
failure the marker exists to prevent. The complement is honest by construction,
and its failure direction is only noise (a bookkeeping key surfaced as a field),
never a silent hole. An explicitly empty list is honoured literally as "this
class has no data fields", which is a different statement from not configuring
the class at all.

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
  (the seam athenaeum#426's deferred skill-file-sync surface would use):

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
| `surface_root_for_class(cls, config, knowledge_root)` | absolute on-disk root where the class lives (the writer entry point athenaeum#427 consumes instead of a hardcoded path) |
| `is_embedded / is_recallable / is_merge_eligible(cls, config)` | the individual policy bits |
| `is_excluded(cls, config)` | `True` when the class joins no corpus capability |

Every corpus consumer honors the matching policy bit (issue athenaeum#532):

- The embedder drops a non-`embedded` class at index build
  (`search._scan_indexed_records`).
- `recall` drops a non-`recallable` class at render
  (`mcp_server._recall_via_backend`, and the `athenaeum recall` CLI).
- The wiki-dedup merge pass drops a non-`merge_eligible` class from candidates
  (`wiki_dedupe.discover_wiki_dedupe_candidates`).

Each is fail-closed defense-in-depth on top of the by-construction path
exclusion, and each is a no-op for the default all-true wiki surface.

## Extension point: supported (with a contract test)

The in-process extension point — `register_adapter`, `resolve_adapter_for_class`,
`available_adapters`, and the `StorageAdapter` / `CorpusPolicy` dataclasses — is
**intentional, supported public API of this internal seam** (issue athenaeum#532, M34).
It is importable from `athenaeum.storage`; like the rest of this module it is
not yet on the stable `__all__` surface (signatures may change between minor
releases until the seam is promoted), but it is exercised end to end by a
contract test — `tests/test_storage_enforcement.py::TestAdapterExtensionPointContract`
drives a code-registered custom adapter through resolve → index → recall — so it
is a live, guarded seam rather than untested rot. A downstream consumer (e.g.
athenaeum#426's deferred skill-file-sync surface) can rely on it.
