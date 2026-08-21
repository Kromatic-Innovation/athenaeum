# Authority manifest + pointer-stub converter (issue athenaeum#426)

> **Status:** standalone, unit-testable slice, shipped and complete (this
> issue's own scope — the manifest schema, `athenaeum.authority`'s
> lookup/detector/converter, and the `authority lint`/`authority convert` CLI —
> is fully implemented; see the sections below). Reasoning-tier consultation of
> the manifest (rejecting/converting live-source duplicates automatically)
> belongs to the consumers — athenaeum#423's T1 duplicate bin and athenaeum#432's T2 rejection,
> both implemented in `src/athenaeum/reasoning_tiers.py`. As of issue athenaeum#602,
> those tiers have **two** gated production callers in `athenaeum.merge`:
> `t1_screen_rejects_merge_proposal` (T1 — screens every proposal, this
> manifest loaded once per merge run via `load_authority_manifest_for_pipeline`
> and threaded in as its `authority_manifest` argument) and
> `t2_screen_merge_proposal` (T2 — consulted on a T1 pass-up, and can consult
> this same manifest again via `reasoning_tiers.safe_class_violation`'s
> live-source-duplicate check before an auto-apply). Both callers are gated
> behind the same flag and **default OFF**
> (`resolve_reasoning_tier_auditing_enabled`, `src/athenaeum/config.py`), so
> production merge behavior is unchanged until an operator sets
> `ATHENAEUM_REASONING_TIER_AUDITING_ENABLED`. Full operator-facing writeup:
> [Reasoning-tier screening (T1/T2)](configuration.md#reasoning-tier-screening-t1t2--off-by-default).
> Running the converter against the live corpus remains operator task athenaeum#437.
> Neither is in scope here.

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
never_ingest_classes:                 # optional (issue athenaeum#968); see below
  - mirror-of-live-source
  - pending-state-todo
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
athenaeum#437, out of scope here.

## Never-ingest classes (athenaeum#968)

An optional `never_ingest_classes:` list — write-refusal classes the
**auto-memory intake path** consults, extending this same manifest mechanism
rather than adding a second config surface. Empty or absent by default: a
manifest written before athenaeum#968, or one that never mentions the key,
enforces nothing new (`AuthorityManifest.never_ingest_classes == ()`).

Two class slugs are recognised (a closed vocabulary —
`athenaeum.authority.NEVER_INGEST_CLASS_SLUGS`; naming anything else raises
`AuthorityManifestError` at parse time, same "loud on malformed" contract as
every other field):

- **`mirror-of-live-source`** — the claim names a value whose system of
  record is a repo/config/doc already declared under `sources:` above.
  Detected by reusing `find_duplicate_source(meta, manifest)` UNCHANGED —
  the exact deterministic topic/tag/name lookup `authority lint` already
  uses for post-hoc wiki-page duplicates, now consulted at intake instead.
- **`pending-state-todo`** — the claim asserts the CURRENT presence/absence
  of something in an external artifact ("X needs updating", "has Y landed
  yet"). Detected by an explicit `pending_state: true` frontmatter flag, or
  a small closed phrase list (`athenaeum.never_ingest._PENDING_STATE_PHRASES`
  — e.g. "has it been added", "still needs", "todo:").

Both classes are seed evidence from athenaeum#968's own filing comment: a
2026-08-07 operator evidence log of three witnessed live wiki pages that
should never have been ingested as durable claims in the first place.

**Enforcement points — both intake tiers, each at its own COMPILE choke
point, never at discovery:**

- **Auto-memory** (`raw/auto-memory/<scope>/...`) —
  `athenaeum.never_ingest.filter_never_ingest`, called from
  `athenaeum.librarian._run_auto_memory_phase` immediately after
  `discover_auto_memory_files` and before clustering. A refused file is
  excluded from that run's `auto_memory_files` list.
- **Entity tier** (`raw/<source>/...`) — `athenaeum.librarian.process_one`
  checks the SAME classifier, via the shared
  `athenaeum.never_ingest.check_and_refuse` primitive, at the very start of
  processing each raw file (right after its frontmatter is parsed, before
  Tier 0 passthrough). Deliberately **not** inside
  `athenaeum.intake.discover_raw_files` itself: that function's return value
  is read directly by `backlog_price_sheet.py` and `ordinary_night_table.py`
  (issue athenaeum#713's measurement instruments, held pending an operator
  decision) for their own backlog counts, and moving what `discover_raw_files`
  returns would silently move those numbers. `discover_raw_files` takes no
  manifest argument and is entirely unmodified by athenaeum#968 — a refused
  entity-tier file is still discovered and still counted in the backlog, it
  is simply not compiled into a wiki page this run (mirrors `result.skipped`,
  the same disposition bucket a Tier 1 old-format skip already uses).

In both cases a refused file is excluded from **that run's compilation**
only — it is **never deleted**. It stays on disk and is re-evaluated (and, if
the class still matches, re-excluded) idempotently on every later run, the
same non-destructive shape `athenaeum.ephemeral`'s own intake drop already
uses.

**Visibility — never a silent drop.** Every refusal, from either tier, is
appended via the same `athenaeum.never_ingest.check_and_refuse` call,
ids-only (a class slug, a closed-vocabulary detail token, an origin
scope/source, a content-free hash of the file's identity — never the
filename itself, which for an auto-memory file can carry a free-text slug —
and a `tier` field: `auto-memory` or `entity`), to
`<cache_dir>/_never_ingest_refusals.jsonl`. This is the OTHER rung of the
"one-ladder rule": raw intake `athenaeum.intake_audit` cannot even
RECOGNISE escalates to a human via the pending-question queue; raw intake
that IS recognised but matches a DECLARED refusal class needs no human
escalation (the class was already declared) but is still ledgered, never
silently dropped.

## Out of scope here

- Reasoning-tier consultation of the manifest (T1/T2, athenaeum#423/#432).
- Running the detector/converter against the live `~/knowledge` corpus (athenaeum#437).
- Syncing skill files across teammates via athenaeum (explicitly deferred;
  see `docs/storage-adapter-contract.md`'s note on the deferred skill-file-sync
  surface).
