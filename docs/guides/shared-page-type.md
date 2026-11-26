**Reference:** [recall](../modules/recall.md) · [mcp](../modules/mcp.md) · [librarian](../modules/librarian.md)

# Sharing one page type with a second person

Jordan Reyes keeps recipes. Priya Raman wants her own assistant to answer questions
about them. This guide builds the smallest version of that: **one page type, kept in
its own git repository, routed there by a rule, readable by exactly one other person.**

It is written from both sides. Part 1 is the owner's (Jordan), part 2 the reader's
(Priya). If you only need the reader's half for a repository somebody already shares
with you, [Reading a shared store from a second install](shared-store-reader.md) is
the shorter page. If you want to move pages you already have into such a repository,
see the `athenaeum storage move-type` entry in the [CLI reference](../reference/cli.md).

Every name here is invented. `git.example.net` and `acme/recipes.git` stand for your
own git host and repository, and the numbers `1001` and `1002` stand for whatever
identifiers your host gives each person.

## What you are building

```
install 1 (Jordan)                     git host                    install 2 (Priya)
recipes/  ── commit, push ──▶  acme/recipes.git  ◀── pull ──  recipes/  (read-only)
  own repository                 decides who                    own repository
  routed to by a rule            may fetch and push             mounted, never written
```

Each install keeps its own `wiki/`, its own raw notes and its own search index. Only
the `recipes/` repository is shared, and what travels is `git push` and `git pull`.

## What enforces what

Three layers, and each can say no independently. None of them can say yes for the
others.

| Layer | What it decides |
|---|---|
| **The git host** | Who can fetch the repository and who can push to it. This is the real protection: an install without fetch rights never holds the bytes, and one without push rights cannot change what the owner published. |
| **The store declaration** (`_store/access.yaml`, kept in the repository) | What each person's install *attempts*. It names who holds `read` and who holds `compile`; an install that is not allowed to compile a store refuses to write it, and an install not allowed to read it does not mount it. It narrows; it grants nothing the host did not already grant. |
| **The local `read_only` flag** | A second, independent refusal on one install: with `read_only: true` that install never writes, merges, repairs, retires or pushes anything in the mount. Defence in depth, not the thing to rely on. |

## 1. The owner: Jordan's install

### Create the repository

Create an empty repository on your git host (`acme/recipes.git` here) and give Priya
**read** access to it, not write access. Hosting is your host's business; how to do that
is in its own documentation.

Then make the repository the root of a directory inside your knowledge directory:

```bash
git clone git@git.example.net:acme/recipes.git ~/knowledge/recipes
```

### Declare the location and route the page type

In `~/knowledge/athenaeum.yaml`, declare the location with `surface_root` pointing at
that clone (its own root), and add a rule that sends the page type there. If you
already have an uncommented `storage:` block, merge these entries into it.

<!-- fixture: owner-config.yaml -->
```yaml
owner:
  asserter: {iss: "https://git.example.net", sub: "1001"}
storage:
  adapters:
    recipes:
      backing_store: markdown
      surface_root: recipes
      visibility: 1
      corpus_policy: {embedded: true, recallable: true}
  routes:
    - {page_type: recipe, to: [recipes]}
```

- `owner.asserter` says who this install is. The declaration below is matched against
  it, using the same identifiers your git host uses for people.
- `adapters.recipes` is the location. `surface_root` is relative to the knowledge
  directory, so `recipes` is `~/knowledge/recipes`; an absolute path also works.
- `visibility: 1` ranks it as more widely shared than your own `wiki/` (0). A page on
  it may not link to anything on a lower-ranked location, which keeps private pages
  from being named in a page other people can read.
- `corpus_policy` makes the pages searchable and recallable. Both keys must be `true`.
- `routes` sends every new page whose `type:` is `recipe` to that location. Every
  [configuration key](../reference/configuration.md#resolve_storage_adapters) is
  documented in the reference.

### Write the store declaration

The declaration is a file in the repository, `recipes/_store/access.yaml`. It names
two people, one who may read and compile and one who may only read.

<!-- fixture: access.yaml -->
```yaml
version: 1
store_id: st_q7m2xk4vwz5d
roles:
  editor:
    rights: [read, compile]
    members:
      - {iss: "https://git.example.net", sub: "1001"}
  reader:
    rights: [read]
    members:
      - {iss: "https://git.example.net", sub: "1002"}
```

The role names are yours; the engine reads only the rights listed under them.
`store_id` is any `st_` followed by 12 lowercase letters and digits `2` to `7`. Every key is in the
[store declaration reference](../reference/store-declaration.md).

### Write pages, commit, push

New `recipe` pages now land in `~/knowledge/recipes/`. Commit and push them like any
other repository:

```bash
cd ~/knowledge/recipes
git add -A && git commit -m "Add recipes" && git push -u origin main
```

Commit `_store/access.yaml` in the same push. Priya's install reads the declaration from
her clone.

## 2. The reader: Priya's install

Install athenaeum and create your own knowledge directory as in
[Reading a shared store](shared-store-reader.md#1-install-athenaeum). Then clone the
repository you were given read access to **into your knowledge directory**:

```bash
git clone git@git.example.net:acme/recipes.git ~/knowledge/recipes
```

Declare it the same way, with the same rule, as a read-only mount, and name yourself
as the install's owner:

<!-- fixture: reader-config.yaml -->
```yaml
owner:
  asserter: {iss: "https://git.example.net", sub: "1002"}
storage:
  adapters:
    recipes:
      backing_store: markdown
      surface_root: recipes
      read_only: true
      visibility: 1
      corpus_policy: {embedded: true, recallable: true}
  routes:
    - {page_type: recipe, to: [recipes]}
```

Then pull and build your index. `reindex` runs `git pull --ff-only` in every read-only
mount first:

```bash
athenaeum reindex --path ~/knowledge
```

## What each person sees in recall

- **Jordan** finds the recipes on their own install, because they wrote them there.
- **Priya** finds the same pages, shown with the location name as the path prefix
  (`recipes/loaf.md`), after her `reindex` has pulled them. A page Jordan pushed this
  morning is invisible to Priya's recall until her next `reindex`; put it on a schedule
  as described in [Reading a shared store](shared-store-reader.md#6-keep-it-current).
- **A write from Priya's install** (a `remember` that would compile into a recipe, an
  edit, a merge) is refused with a `ReadOnlySurfaceError`, and nothing on disk changes.
  Remove `read_only: true` and it is still refused, because the declaration gives her
  `read` and not `compile`. Each layer refuses on its own.
- **An install the declaration does not list as a reader** does not mount the store: it
  is not indexed and not recalled, and nothing on disk is deleted.

## This is an ordinary location, not a vault

A vault is a location whose contents are kept out of search so that sensitive values
never reach an index. This is the opposite. The `recipes` pages are indexed and
searchable on **both** installs, and no read flag is involved: access to them is
decided by who can fetch the repository, not by a setting that hides them from recall.
Do not put pages here that a person with read access should not be able to search.

## Limits

- **Revoking read stops future fetches; it does not take back a copy.** Removing
  Priya's access to the repository means her next pull fails. The clone she already
  holds, and everything indexed from it, stays on her disk.
- **A second person who should also write needs the store to declare
  `sync.mode: integrate`.** This guide has one writer. With two compiling installs the
  store must say how their histories are merged, by adding `sync: {mode: integrate}` to
  `_store/access.yaml`. Without it, `athenaeum store access check` reports a second
  compiler as an error.
- **A failed pull is reported, not fatal.** The last good copy is still served.
- **Proposing to a store you cannot read** is a separate matter and not covered here.

## See also

- [Reading a shared store from a second install](shared-store-reader.md): the reader's side in more detail.
- [Store declaration](../reference/store-declaration.md): every key of `_store/access.yaml`.
- [Configuration reference](../reference/configuration.md#resolve_storage_adapters): every `storage.adapters` key.
