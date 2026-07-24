# Source-Handle Registry — Data Model (issue #453)

> **Status:** schema + index builder only (this document + the templates +
> `athenaeum registry`). Populating real handles for existing client/company
> pages is a separate, operator-only, private-store operation —
> [#454](https://github.com/Kromatic-Innovation/athenaeum/issues/454), blocked
> by this issue. The builder is designed so #454 is **not** a precondition: it
> emits a well-formed registry even when zero handles are populated. Part of
> epic [#422](https://github.com/Kromatic-Innovation/athenaeum/issues/422);
> consumed downstream by the prospector adapters
> ([`athenaeum-adapters#51`](https://github.com/Kromatic-Innovation/athenaeum-adapters/issues/51)).

## 1. Goal

The fact-mining pipeline needs a canonical mapping from a **wiki entity** to
the **corpus handles** that identify the same real-world entity across external
sources — the domains, email aliases, Slack channels/user-ids, LinkedIn URL,
partner domains, Drive folder ids and Mural board ids that the adapters resolve
against.

Those handles are **knowledge about the entity**, so they belong ON the entity
page as frontmatter — owned by the wiki, consumed by `athenaeum-adapters`.
Athenaeum's tier0 passthrough already round-trips arbitrary custom-namespace
frontmatter byte-for-byte (see `docs/why-athenaeum.md` and
`athenaeum.librarian.tier0_passthrough`), so these keys survive compilation
without any schema change — `WikiBase` is `extra="allow"` by design
(`src/athenaeum/schemas.py`).

## 2. Where this sits among the frontmatter axes

This is **not** a new type axis. It layers on the existing entity schema
(`docs/memory-taxonomy.md` §2): a person/company page keeps `type: person` /
`type: company` and simply gains the source-handle keys below. Nothing is
retyped; the keys pass through validation via `extra="allow"`, exactly like
`apollo_*` / `current_title` already do.

## 3. The schema — source-handle keys

Added to the `person.md` and `company.md` scaffold templates
(`src/athenaeum/templates/`). All are **optional** and default to empty; an
entity with none populated simply does not appear in the registry.

| Key | Shape | Meaning |
|---|---|---|
| `domains` | `list[str]` | Web/email domains owned by the entity (e.g. `example.com`). |
| `alt_emails` | `list[str]` | Additional email addresses beyond the page's primary `emails`. |
| `slack_channels` | `list[str]` | Slack channel names/ids associated with the entity. |
| `slack_user_ids` | `list[str]` | Slack user ids for the entity's people. |
| `linkedin_url` | `str` | Canonical LinkedIn profile/company URL (pre-existing template key, reused here). |
| `partner_domains` | `list[str]` | Domains of partners/affiliates that map to this entity. |
| `drive_folder_ids` | `list[str]` | Google Drive folder ids holding the entity's material. |
| `mural_board_ids` | `list[str]` | Mural board ids associated with the entity. |
| `handles_verified` | `str` (date) | ISO date the handle set was last human-verified. |

The canonical key list and their list/scalar split live in one place —
`SOURCE_HANDLE_KEYS` (`LIST_HANDLE_KEYS` + `SCALAR_HANDLE_KEYS`) in
`src/athenaeum/registry.py`. Keep this table and that tuple in sync.

### No client data in the public repo

These templates and this repo are public OSS. **No real domains, emails, Slack
ids, board ids, or LinkedIn URLs land here** — the scaffolds ship empty and the
tests use synthetic fixtures. Real handle values are seeded only against the
private `~/knowledge/` store by the operator-only #454 workflow, via raw
intake / tier0 — never hand-committed to this repo.

## 4. The index builder — `athenaeum registry`

A deterministic, LLM-free `compile`-style step (sibling to `compile-as-of` and
`people`) that reads wiki entity frontmatter and emits `registry.json`:

```
athenaeum registry [--path ~/knowledge] [--out PATH] [--stdout]
```

- `--path` / `--knowledge-root` — the knowledge directory (default `~/knowledge`).
- `--out` — where to write (default `<knowledge-root>/registry.json`).
- `--stdout` — print the JSON instead of writing a file.

It walks `wiki/*.md` (skipping `_`-prefixed non-entity pages), and records
every entity carrying **at least one** populated source handle. Output is
deterministic: entities sorted by `uid`, handle sets in canonical key order,
so re-running on an unchanged wiki is byte-identical.

### registry.json shape

```json
{
  "version": 1,
  "entity_count": 1,
  "entities": {
    "company-acme": {
      "type": "company",
      "name": "Acme",
      "handles": {
        "domains": ["acme.example"],
        "linkedin_url": "https://www.linkedin.com/company/acme",
        "handles_verified": "2026-07-24"
      }
    }
  }
}
```

Only populated keys appear under `handles` — an entity with an empty
`slack_channels: []` simply omits it. When **no** entity has any populated
handle (the seed-not-landed-yet case), the registry is still well-formed:
`entity_count` is `0` and `entities` is `{}`. That degenerate case is a
first-class, tested behaviour — the builder never requires #454 to have run.
