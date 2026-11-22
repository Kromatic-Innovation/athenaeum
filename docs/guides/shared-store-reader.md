**Reference:** [recall](../modules/recall.md) · [mcp](../modules/mcp.md) · [librarian](../modules/librarian.md)

# Reading a shared store from a second install

One person keeps some pages (say, recipes) in their own git repository. You want your
own assistant to answer questions about those pages, **read-only**, from your own
athenaeum install. This guide sets that up on your machine. It is for the reader; the
person who owns the shared repository has a different job (moving the type into its own
repository) and does not need this page.

Throughout, `git@git.example.net:acme/recipes.git` is the shared repository and
`~/shared-recipes` is where you clone it. Both are invented; substitute your own.

## What you are building

```
shared repository (owner pushes, you only pull)
        │  git pull
        ▼
~/shared-recipes   ── mounted read-only ──▶  your athenaeum install
                                              ├─ your own wiki/ and raw/  (private to you)
                                              └─ your own search index    (built by you)
                                                        │
                                                        ▼
                                              your assistant (recall)
```

Four things are worth knowing before you start:

- **You get a copy, not a connection.** The clone is an ordinary git checkout on your
  disk. Your assistant reads that copy through your own athenaeum index.
- **Each install keeps its own index.** Nothing about the shared repository includes a
  search index, and yours is never uploaded anywhere. You build it locally, from the
  pages you pulled.
- **Write protection comes from repository permission.** You were given read access to
  the shared repository, not push access, so no command on your machine can change
  what the owner published, however it is configured. athenaeum adds a second,
  independent layer: once you declare the mount `read_only: true`, it refuses to write,
  merge, repair, retire or push anything on it. Treat that as defence in depth, not as
  the thing you rely on.
- **Your own notes stay yours.** See [What your own `remember` calls do](#what-your-own-remember-calls-do).

## 1. Install athenaeum

Follow [Install](install.md) and install **with the MCP extra** (the section "I want
agents to write and read memory through MCP"), because your assistant will talk to
athenaeum through the MCP server. Check that it worked:

```bash
athenaeum --version
```

Read-only mounts need a release that lists `read_only` under `storage.adapters` in the
[configuration reference](../reference/configuration.md#resolve_storage_adapters). If
you are unsure, `athenaeum --version` and that page are the two things to compare.

## 2. Create your own knowledge directory

```bash
athenaeum init --path ~/knowledge
```

This is **your** store: your own notes, your own index. The shared pages are mounted
next to it, not copied into it. You do not need an API key to read a shared store (see
[Compiling your own notes](#compiling-your-own-notes) for when you would).

## 3. Clone the shared repository

Clone it **outside** the knowledge directory, using the read-only access the owner gave
you:

```bash
git clone git@git.example.net:acme/recipes.git ~/shared-recipes
```

Confirm the permission is what you were told it is. Try to push an empty commit; it
should be refused. If it succeeds, tell the owner, because you have more access than
you were meant to. Then throw the test commit away so the clone matches the shared
repository again:

```bash
cd ~/shared-recipes
git -c user.name=Reader -c user.email=reader@example.com commit --allow-empty -m "push test"
git push origin HEAD
git reset --hard @{upstream}
```

The push is refused by the server (or, for a local or file-system-backed repository, by
file permissions).

The pages are plain markdown files with YAML frontmatter, each carrying `type:` (here
`recipe`).

## 4. Declare the mount

Add this to `~/knowledge/athenaeum.yaml`. `athenaeum init` leaves the `storage:` key
commented out; if you already have an uncommented `storage:` block, merge these entries
into it, because a second top-level `storage:` key is not valid YAML.

```yaml
storage:
  adapters:
    shared-recipes:                   # the surface's name; you choose it
      backing_store: markdown
      surface_root: ~/shared-recipes  # where you cloned it; absolute (or ~) works
      read_only: true                 # pulled and indexed, never written or pushed
      visibility: 1                   # more widely shared than your own wiki (0)
      corpus_policy:
        embedded: true                # build it into your search index
        recallable: true              # let recall return its pages
  mapping:
    recipe: shared-recipes            # the type that lives on this surface
```

What each key does:

| Key | Meaning |
|---|---|
| `surface_root` | The clone. An absolute path, or one starting with `~`, is used as-is; a relative path is resolved against `~/knowledge`. The directory must exist when the index is built; keep it outside your `wiki/`. |
| `read_only` | `true` makes the surface a mount: pulled before every index build, indexed, and never written, merged, repaired, retired, committed into or pushed. Default `false`. |
| `visibility` | How widely the surface is shared; higher is more widely shared, default `0` (your own wiki). A page may link to a page on an equal or more widely shared surface, never the reverse, and pages on different surfaces are never merged. |
| `opaque_filenames` | `true` names every NEW page written to the surface `<uid>.md`, with no slug of the page's name, so a listing of the store (a host's web view, notification emails, CI logs) does not show whose records it holds. Default `false`. See [Opaque file names](#opaque-file-names). |
| `corpus_policy` | `embedded` builds the surface into your index; `recallable` lets `recall` return it. Both must be `true` for the pages to be found. Anything you omit defaults to `false`, so a half-written policy hides the surface rather than leaking it. |
| `mapping` | Which page `type:` belongs to which surface. It is how a `recipe` is recognised as living on the mount. |

Every key is documented in the
[configuration reference](../reference/configuration.md#resolve_storage_adapters).

### Opaque file names

By default a page is named `<uid>-<slug-of-its-name>.md`, so `a1b2c3d4-jane-example.md` shows that the store holds a record about Jane Example. On a store you write to, set `opaque_filenames: true` on its `storage.adapters` entry and new pages are named `<uid>.md` instead. The uid is random, so the name carries nothing about the page.

Limits, so the setting is not read as more than it is:

- Only pages written after you turn it on. Pages already on disk keep their names; nothing is renamed.
- Commit messages and the page bodies and frontmatter still contain names. Someone who can read the pages learns who is in the store; the setting only hides the file listing.
- It is not encryption.
- It applies to a surface you configure under `storage.adapters`; the default `wiki/` surface cannot opt in. Pages are found by their uid, so recall, `read_entity`, links and `athenaeum uid-collision-lint` behave the same on both name shapes.

Then tell your install that `recipe` is a type it should expect, so type-filtered
recall (`--type recipe`) does not warn that the class is unrecognised. Append a row to
`~/knowledge/wiki/_schema/types.md`:

```bash
echo '| recipe | A cooking recipe | Sourdough Bread |' >> ~/knowledge/wiki/_schema/types.md
```

## 5. Pull and build your index

```bash
athenaeum reindex --path ~/knowledge
```

`reindex` first runs `git pull --ff-only` in every read-only mount, then builds your
index from your own pages plus the mount. A fresh clone is already current, so the
first run just indexes it. Check that your assistant's search will find something:

```bash
athenaeum recall "lemon curd" --path ~/knowledge
athenaeum recall "sourdough" --path ~/knowledge --type recipe
```

Hits from the mount are shown with the surface name as their path prefix
(`shared-recipes/lemon-tart.md`). `recall` reads the **index**, so a page the owner
pushed this morning is invisible to it until a `reindex` has pulled and indexed it.

**Which search backend you get.** athenaeum's configured default is `vector`, which
needs the optional `chromadb` extra. A plain install does not have it, so `reindex` and
`recall` print a notice and fall back to keyword search (FTS5) automatically. That is
enough for a shared store, and nothing else needs installing. Install the vector extra
(see [Vector search](vector-search.md)) only if you want semantic search, or set
`search_backend: fts5` in `athenaeum.yaml` to silence the notice.

### What a failed pull does

A pull that fails (you are offline, the host is down, your credentials lapsed, the
owner force-pushed so the history no longer fast-forwards) is **reported and not
fatal**. `reindex` prints a warning naming the surface, exits normally, and indexes
the checkout you already have. Your assistant keeps answering from the last good
copy; it never sees a half-updated one. Fix the cause (network, credentials) and the
next `reindex` catches up. If a force-push left your clone diverged, re-clone it: you
never commit to it, so there is nothing to lose.

## 6. Keep it current

Nothing refreshes the mount by itself, so put the refresh on a schedule. The command
to schedule is `athenaeum reindex`; it is the thing that pulls.

A cron line that refreshes every 30 minutes. cron has a minimal `PATH` and does not
expand `~` in every shell, so write the full path to `athenaeum` (`which athenaeum`
prints it) and to your knowledge directory in place of `<home>`:

```cron
*/30 * * * * <home>/.local/bin/athenaeum reindex --path <home>/knowledge >> <home>/athenaeum-reindex.log 2>&1
```

`reindex` is incremental, so an idle tick is cheap. [Remote mode](remote.md) shows the
same job as a systemd timer if you prefer one.

Two things that are easy to get wrong:

- **`athenaeum schedule install` does not do this.** It installs a nightly
  `athenaeum run`, which compiles your own raw notes and does not refresh the index.
  It also refuses to install when it finds no LLM key source. You only need it if you
  compile your own notes (see below), and even then you still want the `reindex` job.
- **Do not rely on `session-end` to refresh the mount.** `athenaeum session-end` (see
  [Day-to-day operation](daily-operation.md)) only reindexes, and so only pulls, when
  your own notes or index show pending work. A scheduled `reindex` is the dependable
  way to pick up what the owner pushed.

## 7. Connect your assistant

Register the MCP server against **your** knowledge directory, exactly as for any
install. For Claude Code:

```bash
claude mcp add --scope user athenaeum -- athenaeum serve --path ~/knowledge
```

For Claude Desktop, Cursor and other local clients, follow
[Other clients and platforms](clients.md) (use absolute paths for both the binary and
`--path`) or [Cursor](cursor.md). For per-turn recall in Claude Code, see
[Sidecar](sidecar.md).

Then ask the assistant something only the shared pages can answer ("what is in the
sourdough recipe?"). It calls `recall`, and the result includes the shared page with
its `Path`, `Uid` and `Type`. To check the server without an assistant, start
`athenaeum serve --path ~/knowledge` from any MCP client and call `tools/list`, then
`recall`; the tool list includes `recall`, `read_entity`, `enumerate_entities` and
`remember`.

Several remote-hosted assistants cannot launch a local process at all; for those see
[Remote mode](remote.md), and read its section on what leaves your machine first,
because a remote host receives every recalled page.

## What your own `remember` calls do

Your assistant can still `remember` things for you. Those calls are **yours**:

- `remember` writes a note under `~/knowledge/raw/`, in **your** knowledge directory.
  It never writes into `~/shared-recipes` and never reaches the shared repository. The
  mount has no path through which a `remember` could be written.
- The compile step (`athenaeum run`, or `athenaeum ingest`) turns your raw notes into
  pages under **your** `~/knowledge/wiki/`. It never writes a page onto a read-only
  mount, whatever the note says.
- **If a note is about the shared type.** With the `mapping` entry above, a note that
  would compile into a `recipe` page is refused for that page: nothing is written to
  the mount, the refusal is logged and counted, and your raw note is **kept** (not
  deleted) so you lose nothing. It stays in your `raw/` and is retried on later runs,
  so a note like that does not become a page. A raw note that merely updates an existing
  shared page (a structured note that names its uid) is refused the same way. Notes of
  any other type compile normally into your own wiki.
- If you would rather your own recipe notes become private pages, leave the `recipe`
  line out of `mapping`. Your `recipe` notes then compile into your own wiki, alongside
  (not into) the shared pages; the shared pages are still indexed, because the surface
  is declared. The two are separate pages in separate stores, and neither is merged into
  the other.

Either way, the only thing that ever travels between you and the owner is `git pull`
into your clone.

### Compiling your own notes

Reading a shared store needs no LLM and no API key. Compiling your own `remember`
notes does: see [Install](install.md) (supplying a key, or `llm.provider: claude-cli`)
and [Day-to-day operation](daily-operation.md) for `ingest` and `run`. Until a compile
has run, your raw notes are simply stored.

## If something looks wrong

| You see | Likely cause |
|---|---|
| `recall` returns nothing from the shared pages | You have not run `athenaeum reindex` since cloning or pulling; or `embedded`/`recallable` is not `true`; or `surface_root` does not exist. |
| `read-only surface pull failed` warning | The pull failed (offline, credentials, diverged history). The last good copy is still being served. |
| `StorageConfigError` naming `storage.adapters.<name>` | A missing `backing_store` or `surface_root`, or a `read_only` / `visibility` / `opaque_filenames` of the wrong type (`read_only` and `opaque_filenames` must be `true` or `false`). |
| `ReadOnlySurfaceError` | Something tried to write to the mount. That is the protection working; nothing was changed. |
| `git push` is refused | Expected. You were not given push access. |

For the rest, see [Troubleshooting](troubleshooting.md).

## See also

- [Install](install.md) — installing athenaeum and the MCP extra.
- [Other clients and platforms](clients.md) — connecting a local assistant.
- [Day-to-day operation](daily-operation.md) — `reindex`, `ingest`, `run`, `session-end`.
- [Remote mode](remote.md) — serving to a host that cannot launch a local process.
- [Configuration reference](../reference/configuration.md#resolve_storage_adapters) — every `storage.adapters` key.
