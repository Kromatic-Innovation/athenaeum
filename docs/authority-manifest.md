# Authority manifest + pointer-stub converter (issue #426)

> **Status:** standalone, unit-testable slice. Reasoning-tier consultation
> (rejecting/converting live-source duplicates automatically) belongs to the
> consumers — #423's T1 duplicate bin and #432's T2 rejection — which carry
> `blocked_by` edges on this issue. Running the converter against the live
> corpus is operator task #437. Neither is in scope here.

## Why

A memory that copies content a **live source** (a skill file, a code path, a
config file) already owns can drift out from under that source silently — the
skill gets edited, the memory doesn't, and now two answers exist. A *pointer*
that names the live location cannot go stale the same way: recall always
resolves to whatever the source currently says.

## The manifest

**Format: YAML.** Every other athenaeum config artifact (`athenaeum.yaml`, the
eval `cases.yaml` fixtures) is YAML; a second format for one more small,
human-maintained registry would be pure inconsistency with no offsetting
benefit.

**Location:** `<knowledge_root>/authority-manifest.yaml` by default — a
sibling of `athenaeum.yaml` at the knowledge root. Resolved by
`athenaeum.config.resolve_authority_manifest_path`, following the module's
standard precedence:

1. `ATHENAEUM_AUTHORITY_MANIFEST` env — explicit path override.
2. `librarian.authority_manifest_path` in `athenaeum.yaml` — relative values
   resolve against the knowledge root; absolute values pass through.
3. Default: `<knowledge_root>/authority-manifest.yaml`.

**Schema** (top-level):

```yaml
version: 1
sources:
  - slug: skill-dijkstra              # unique id; referenced by stubs
    location: .claude/skills/dijkstra/SKILL.md
    kind: skill                       # skill | code | config | doc (free text)
    topics:                           # slugs/topics this source OWNS
      - lean-development-workflow
      - clean-commit-discipline
```

`version` must be the literal integer `1` (a schema-evolution seam — a future
incompatible schema bumps it and the loader can dispatch on it). Each source
requires a unique, non-empty `slug`, a non-empty `location`, and a non-empty
`topics` list of non-empty strings. `kind` is optional free text — operators
name their own source kinds; it is not validated against a closed vocabulary.

A missing manifest file is treated as "no authoritative sources configured yet"
(an empty, inert manifest) — not an error. A manifest file that **exists but is
malformed** (bad YAML, wrong version, a source missing a required field, a
duplicate slug, …) raises `athenaeum.authority.AuthorityManifestError` with a
message naming the specific defect, never a bare stack trace.

## The detector — lookup, not vibes

`athenaeum.authority.find_duplicate_source(meta, manifest)` decides whether a
memory's frontmatter duplicates a manifest-listed source by **deterministic
lookup**: it reads the page's `topics:` / `topic:` / `tags:` frontmatter and
checks each entry (case-insensitively, whitespace-trimmed) against the
manifest's owned topic strings. There is no semantic-similarity/embedding
comparison anywhere in this path — a memory either names a topic the manifest
says a source owns, or it doesn't.

`athenaeum.authority.find_duplicates_in_wiki(wiki_root, manifest)` runs the
same lookup over every top-level `wiki/*.md` page (mirroring the shallow scan
`athenaeum.wiki_dedupe.discover_wiki_dedupe_candidates` uses) and is
**read-only** — it never mutates a page. The CLI lint (below) is a thin
wrapper over this function.

## The converter

`athenaeum.authority.convert_to_pointer_stub(text, source, title=None)` turns a
duplicating memory's full markdown text into a **one-line pointer stub**: the
frontmatter is kept (with `pointer_stub: true` added) and the body is replaced
with a single line naming the title and the authoritative location:

```
<title> — see <source.location> (authoritative: <source.slug>)
```

This is deliberately **not a bare delete** — recall still needs to find
*something* that points at the skill/source. `convert_page_to_pointer_stub`
is the file-reading convenience wrapper; neither function writes — callers
decide when/whether to persist, matching the read/transform/write split used
elsewhere in this codebase (e.g. `athenaeum.repair`).

## Stub hygiene

A converted stub carries `pointer_stub: true` in its frontmatter
(`athenaeum.authority.POINTER_STUB_FLAG`), checked via
`athenaeum.authority.is_pointer_stub(meta)` — the single source of truth for
stub detection, consulted at two call sites so a stub is excluded **by
construction**, not by convention:

- **Merge eligibility** — `athenaeum.wiki_dedupe.discover_wiki_dedupe_candidates`
  drops any page with a truthy `pointer_stub` flag, alongside its existing
  `archived` / `superseded_by` exclusions, so a stub is never proposed as a
  wiki-dedup merge source.
- **Embed input** — `athenaeum.search.VectorBackend._add_records` embeds only
  the page's body (the one pointer line) instead of the full frontmatter+body
  for any record whose frontmatter is a pointer stub, so a stub contributes
  nothing beyond its pointer line to the vector index.

## CLI

```
athenaeum authority lint --path ~/knowledge [--json]
```

Lists wiki pages that duplicate a manifest-listed source. **Read-only** — no
`--apply` flag exists on `lint` at all; it never opens a page for writing.

```
athenaeum authority convert --path ~/knowledge \
  --page wiki/some-page.md --source-slug skill-dijkstra [--title "..."] [--apply]
```

Converts **one** page (given explicitly via `--page`) into a pointer stub for
the named manifest source. Default is dry-run (prints the converted text to
stdout without writing); `--apply` writes it. This command never walks the
corpus — running the converter against the whole live corpus is operator task
#437, out of scope here.

## Out of scope here

- Reasoning-tier consultation of the manifest (T1/T2, #423/#432).
- Running the detector/converter against the live `~/knowledge` corpus (#437).
- Syncing skill files across teammates via athenaeum (explicitly deferred;
  see `docs/storage-adapter-contract.md`'s note on the deferred skill-file-sync
  surface).
