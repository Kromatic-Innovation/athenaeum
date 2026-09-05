# Raw intake

**Reference page.** For the task-shaped version, see
[Claude Code integration](../guides/claude-code.md).

## What it does

Raw intake is the append-only landing zone every fact passes through before it can become a
wiki page. `remember` (MCP) and `athenaeum remember` (CLI) both write one new file under
`raw/<source>/`; nothing edits an existing raw file in place. The librarian's compile
pipeline reads this tree on its next run — `remember` never touches the wiki, classifies
nothing, and never calls an LLM.

Three shapes get special handling once a raw file reaches the pipeline:

- **Ordinary intake** — free text or a structured note. Tier 2/3 classify and merge it into
  the wiki (see [librarian](librarian.md)).
- **Tier-0 passthrough** — a raw file whose frontmatter already looks like a finished wiki
  entity (`uid`/`type`/`name` present, `type` in the schema's allowlist, `uid` not already
  indexed) is copied to `wiki/` byte-for-byte, stamping only `created`/`updated`. No LLM call
  runs. This exists for upstream producers (contact-sync scripts, warm-wiki generators) that
  already emit valid wiki shape — sending that through Tier 2/3 would be wasteful and lossy
  (the LLM path rebuilds frontmatter from a fixed allowlist and drops unlisted fields).
- **A correction batch** — a `.jsonl` file whose first line parses as a valid batch envelope
  is claimed by the correction phase instead of ordinary intake (see
  [corrections](corrections.md)). Every other `.jsonl` — malformed envelope, unknown
  `schema_version`, not JSON at all — falls through to ordinary intake like any `.md` file.

Every file Jordan Reyes's agent writes via `remember` — a Priya Raman meeting note, a
decision about Acme Corp — sits in `raw/` until the next `athenaeum run` compiles it.

## What it reads

- `raw/<source>/` — one directory per session/source handle, each holding files named
  `{timestamp}-{uuid8}.md` or `.jsonl`.
- `raw/<extra-intake-root>/<scope>/` — auto-memory intake (default
  `raw/auto-memory/<scope>/`), where `<scope>` groups a Claude Code project or session.
  Files named `feedback_*`/`project_*`/`reference_*`/`user_*`/`Recall_*` are claimed by
  filename; a file that misses that convention is still claimed if its own frontmatter
  declares a recognized `metadata.type` (or top-level `memory_type`).
- `wiki/_schema/observation-filter.md` — read at Tier-2 classify time (see below), not by
  `remember` itself.
- `librarian.non_intake_sources` / `KNOWLEDGE_RAW_PATH` / `KNOWLEDGE_WIKI_PATH` and the
  extra-intake-roots config, which decide which source directories are even visible to
  discovery.

## What it writes

- `remember` appends exactly one new file under `raw/<safe-source>/`. Provenance
  (`source`, `field_sources`, `source_type`, `source_ref`, `model`, `on_behalf_of`,
  `asserter`, `bucket`, `valid_until`) is stamped into that file's frontmatter — merged into
  frontmatter the caller already supplied, or a fresh block if the caller sent none.
- Nothing else. `remember` never writes to `wiki/`, never edits a prior raw file, and never
  deletes anything — retirement of a compiled raw file is the librarian's job, not intake's.

### The observation filter

`src/athenaeum/schema/observation-filter.md` ships as a bundled default. `athenaeum init`
copies it **write-once** into `wiki/_schema/observation-filter.md` — the copy is skipped
entirely if a file is already there, so `init` never overwrites an operator's edits.
`tiers.py` reads the live copy at Tier-2 classify time and injects it into the classify
prompt under a `## Observation filter (what to capture)` heading, in prompt *instruction
position* next to (not inside) the fenced `<user_document>` block that holds the untrusted
raw text. The fragment is length-bounded — truncated at 8,192 characters with a logged
warning, never silently dropped — and defanged so an edited fragment cannot forge the
adjacent fence's boundary markers.

Each `librarian-run-summary` log line reports
`schema_fragments=observation-filter:default|<hash>`, so an operator can tell from the run
log alone whether the live file still matches the shipped default or has been edited (and to
what byte-state), without diffing the file by hand.

**What this is not.** The shipped file's own "Pattern Detection" and "Decay Rules" sections
describe a self-tuning filter — the librarian never implements either. `librarian.py` never
reads or writes this fragment at all outside the one `tiers.py` read above: it does not
analyze raw intake for patterns, does not propose additions, and does not decay or promote
filter entries. The file is also not a wiki page — it lives under `wiki/_schema/`, outside
the compiled corpus `recall` searches. Editing it is a manual, human (or Claude-Edit-tool)
action; nothing enforces that an agent keep it in sync with user feedback, and nothing
audit-trails an edit to `raw/` automatically — the shipped file's "Tuning" section asks the
editor to also call `remember`, but that is a convention an operator's own
`CLAUDE.md`/agent instructions must adopt, not a guarantee this codebase provides.

## What it refuses

`remember` validates before touching the filesystem — a rejected call writes nothing:

| Condition | Result |
|---|---|
| Content exceeds 10 MB | `Error: content exceeds 10 MB limit.` |
| `bucket` set to anything outside `daily`/`weekly`/`durable` | `Error: invalid \`bucket\`: ...` |
| `sources` malformed (bad shape, bare dict without `_source`/`_field_sources`) | `Error: invalid \`sources\`: ...` |
| `source` has no alphanumeric character | `Error: source must contain at least one alphanumeric character.` |
| Resolved target directory escapes `raw/` | `Error: path traversal detected — writes are restricted to raw/.` |
| Resolved target directory falls under `wiki/` | `Error: writes to wiki/ are not allowed.` |
| `screening` config itself is invalid | `Error: invalid \`screening\` config: ...` |
| The kill switch is on (`athenaeum disable`, "capture" or "all" scope) | `athenaeum is disabled (kill switch): knowledge writes are off. Run 'athenaeum enable' to restore.` |

Two things it deliberately does **not** refuse:

- **No `sources` supplied.** The write still lands, stamped `source: claude:inferred`, with
  a server-side warning logged — a caller that forgets provenance is not blocked, just
  flagged.
- **A restricted MCP audience.** `remember` stays open for every pinned audience (see
  [mcp](mcp.md)) — intake screening, not audience gating, is what classifies sensitive
  content on the way in.

At discovery time, a file that fits none of the recognized shapes is never silently dropped:
`intake_audit.py`'s unclaimed-file sweep flags it (grouped by reason and sibling directory)
into the pending-decision queue rather than leaving it invisible on disk.

## See also

- Guides — [Claude Code integration](../guides/claude-code.md)
- Modules — [librarian](librarian.md) · [corrections](corrections.md) · [mcp](mcp.md)
- Design — [provenance shape](../design/provenance-shape.md) · [sensitivity value routing](../design/sensitivity-value-routing.md)
- Extending — [source handles](../extending/source-handles.md) · [adapter contract](../extending/adapter-contract.md)
- Reference — [configuration](../reference/configuration.md)
