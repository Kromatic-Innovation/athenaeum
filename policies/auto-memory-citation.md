# Auto-memory citation policy

Status: active (issue #260, slice A of #259)

This is the spec the worked example (`~/knowledge` commits `59e6070d1` →
`9a921d820`, page `wiki/a545c038-tristan-kromer.md`) followed by hand. Slice A
makes the librarian carry it programmatically.

## Principle: cite the ultimate source, never the raw file

`raw/auto-memory/` is an **expiring intake queue** (#259), not a permanent
source. When a fact is compiled into the wiki it must cite where the fact
*ultimately* came from — never the raw `auto-memory/<scope>/<prefix>_<slug>.md`
filename, which is a transient view that retires on move.

## Schema: `source_type` + `source_ref`

Every `sources[]` entry (and the `AutoMemoryFile` / `WikiEntity` /
`MergedWikiEntry` models) carries:

| Field         | Meaning |
|---------------|---------|
| `source_type` | One of `user-stated`, `external`, `document`, `inferred`. |
| `source_ref`  | The ultimate reference — session-id+turn, URL, or document path. **Never** the raw `auto-memory/...` filename. |

Canonical definitions live in `athenaeum.models.SOURCE_TYPES` /
`DEFAULT_SOURCE_TYPE`. Coerce unknown/missing values with
`athenaeum.models.coerce_source_type` (defaults to `inferred`).

### `source_type` values

- **`user-stated`** — the user said it. Verified against the session
  transcript (`~/.claude/projects/<scope>/*.jsonl`) as a user-authored
  message. `source_ref` = `<session>#turn<N>`.
- **`external`** — a subagent quoted an external source (e.g. a URL).
  `source_ref` = the URL.
- **`document`** — a permanent or updated document is the source.
  `source_ref` = the document path/reference.
- **`inferred`** — an agent leap that cannot be verified, OR the transcript
  has rolled off / is missing. Labeled honestly as `inferred` — **never**
  silently promoted to `user-stated`. `source_ref` = best-effort
  session-anchored ref (`<session>#turn<N>` or the bare session id), never a
  filename.

### Default + backward compatibility

Missing `source_type` ⇒ `inferred`. Sources written before this policy (bare
session UUID strings, or dicts with no `source_type`) still parse cleanly;
`source_ref` is back-filled from `session`+`turn`.

## Footnote rendering

Compiled wiki bodies render `[^name]: **Source:** ...` footnotes carrying
`source_type` + `source_ref`, matching the worked-example style:

```
[^src-1]: **Source:** user-stated — `abc123#turn4` (origin scope `-Users-...-voltaire`).
[^src-2]: **Source:** external — `https://www.hbs.edu/startup`.
```

See `athenaeum.merge.render_source_footnotes`. Sources still render to
frontmatter as well; the footnotes are the human-readable, ultimate-source
citation.

## Transcript verification

`athenaeum.transcript_verify.verify_user_stated(scope, session_id, turn,
claim, projects_root=None)` returns `(source_type, source_ref)` by reading the
session transcript read-only. `projects_root` is injectable (tests pass a temp
tree; production defaults to `~/.claude/projects`). The librarian gains
read-only access to transcripts; it never writes them.

## Constraints

- **Append-only provenance.** Never rewrite existing `sources[]` entries; only
  enrich/append.
- **Read-only transcripts.** Verification never mutates session logs.
- **No raw filename as `source_ref`.** This is the load-bearing invariant —
  enforced in parsing, footnote rendering, and verification fallbacks.
