# Retention and decay

**Reference page.** For the fuller design context, see
[shape rules](../design/shape-rules.md) and
[security posture](../design/security-posture.md).

## What it does

Retention is four separate, independently-configured mechanisms that decide
how long a piece of memory stays live, where it lives, and what happens to it
once it stops being current:

- **Decay sweep** (`athenaeum decay-sweep`, `athenaeum.decay_sweep`) —
  deterministic, zero-LLM-call removal of expired `bucket: daily` wiki pages.
  A page joins the kill-list only when it carries `bucket: daily` AND its
  `valid_until` has passed; a daily-bucket page with no `valid_until` (or one
  not yet passed) is retained. Removal is a two-commit `git rm` (a provenance
  snapshot, then the removal), never a bare `unlink` — the module refuses
  outright when `knowledge_root` is not a git repository.
- **Memory tiers** (`athenaeum.memory_tiers`) — a retrieval-cost
  classification (`hot`/`warm`/`cold`/`refused`) layered on top of every
  compiled page, independent of storage location. Only `hot` pages are
  eligible for unprompted push; `warm` pages are reachable by explicit recall
  only. An automatic sweep (`librarian.memory_tier_sweep_enabled`, run inside
  `athenaeum run`) moves pages between `hot` and `warm` on class default, age
  without use, measured recall precision, and promote-on-use signals.
- **Auto-memory prune** (`athenaeum auto-memory prune`,
  `athenaeum.auto_memory_prune`) — retires already-compiled
  `wiki/auto-*.md` pages that `athenaeum.ephemeral.classify_ephemeral_page`
  identifies as operational/throwaway (an explicit `ephemeral: true` flag,
  every origin scope matching a configured ephemeral-scope glob, or a
  multi-signal operational-marker match). Dry-run by default; `--apply`
  `git rm`s the kill-list in one labeled commit.
- **The `preserve` disposition** (`athenaeum.rules`, part of the shape-rules
  engine) — the log-retention mechanism for source documents that should
  never be compiled into wiki prose at all. The operator framing that drove
  it: *"These logs are like a daily diary. We don't need the blow-by-blow
  into the wiki. We need to retain the log as an artifact and point any facts
  that we do ingest to that log as the source."* `preserve` **moves** a
  matching raw file out of `raw/` into an operator-configured preserved area
  and, optionally, compiles one fact from it whose `source` points back at
  the moved artifact as provenance. The file is not merely skipped by
  discovery — moving it makes it *not discoverable*, since
  `intake.discover_raw_files` only ever walks `raw/`.

Retention-pack classification (`athenaeum.erasure`) sits above all four as an
optional authority: when a page's frontmatter carries both `memory_class` and
`data_class`, the active retention pack can override the decay sweep's
`bucket: daily` handling and route a page off-corpus instead of archiving it
in git. No shipped write path stamps `data_class` today, so this gate is
dormant on any corpus produced by shipped code — see "What it refuses" below.

## What it reads

- **Decay sweep** reads `bucket:`/`valid_until` frontmatter on every
  non-underscore `wiki/*.md` page (shallow scan), plus, when a page also
  carries `data_class`, the active retention pack
  (`erasure.retention_pack` / `erasure.retention_packs.<name>`).
- **Memory tiers** reads `memory_tier:`, `memory_class:`, `type:`,
  `superseded_by`/`deprecated`, and `updated`/`created` frontmatter, plus
  push/reference usage via `athenaeum.usage_report.get_claim_usage`.
  `librarian.memory_tier_sweep_enabled` (default `false`) gates whether the
  sweep runs at all; `memory_tiers.demote_after_days` (default `60`) is the
  shared age/precision threshold.
- **Auto-memory prune** reads every `wiki/auto-*.md` page's frontmatter and
  body, plus `librarian.ephemeral_scopes` and `librarian.operational_markers`
  (both default `[]` — off until an operator opts in).
- **The `preserve` disposition** reads `librarian.preserved_log_dir` and
  `librarian.preserved_log_adapter` (see "Config keys" below), plus whatever
  shape rule matched the raw file and its optional `correction:` block.

### Config keys: `preserved_log_dir` and `preserved_log_adapter`

```yaml
librarian:
  preserved_log_dir: logs
  # or, to route outside the knowledge git repo entirely:
  preserved_log_adapter: mural-archive
```

- **`preserved_log_dir`** names a folder *under the knowledge root* —
  relative, versioned by the same git repo as everything else. An absolute
  value, or one that escapes the root via `..`, is rejected with a warning
  and treated as unset.
- **`preserved_log_adapter`** names a registered `storage.adapters.<name>`
  (`athenaeum.storage`) whose `surface_root` may be absolute, so a preserved
  log can land on a different filesystem or mount than the knowledge repo.

**Both keys are fully wired.** `athenaeum.config.resolve_preserved_log_dir`
and `resolve_preserved_log_adapter` are consumed by two real call sites:
`athenaeum.rules`'s `preserve` disposition handler (`src/athenaeum/rules.py`
around line 2312, which calls both resolvers directly and branches on their
results — adapter wins if both are set, an unknown adapter name raises
`StorageConfigError`, neither set falls through to the reasoning tiers with
the raw file untouched) and `athenaeum.tiers`'s `log_demote` oversize-page
disposition (`src/athenaeum/tiers.py` around line 4571, which calls
`resolve_preserved_log_dir` and falls back to `review` when it resolves to
`None`). Neither key is config-only or dark: both have a real consumer that
reads the value and acts on it.

## What it writes

- **Decay sweep**, on `--apply`: a durable, append-only sweep ledger
  (`_decay_sweep_records.jsonl` under the cache dir) recording which page,
  why (bucket + `valid_until`), when, and the recovering commit SHA — written
  *before* the archival commit, and a ledger-write failure aborts the sweep
  rather than being swallowed. Then a two-commit `git rm` of the kill-list.
- **Memory tiers**, on a live sweep: an updated `memory_tier:` frontmatter
  scalar per changed page (via `atomic_write_text`), and a best-effort
  ledger append (`_tier_sweep_records.jsonl`) — unlike the decay sweep's
  ledger, a write failure here is logged and the sweep continues, since tier
  movement is non-destructive metadata rather than a `git rm`.
- **Auto-memory prune**, on `--apply`: a `git rm` of the kill-list in one
  labeled commit. Refuses against a non-versioned store.
- **The `preserve` disposition**: moves the raw file to
  `<preserved_dir>/<source>/<filename>` (suffixing a same-named collision
  rather than clobbering it), and, if the rule also carries a `correction:`
  block, compiles one fact whose `source.ref` points at the moved artifact
  (`preserved-log:<path>#<locator>`).

## What it refuses

| Reason | Trigger |
|---|---|
| Decay sweep refuses to archive (no commit) | `knowledge_root` is not a git repository — never degrades to a bare `unlink`. |
| Decay sweep aborts before `git rm` | The sweep ledger write fails — a page can never be archived without a durable record of why. |
| A page is retained, not archived | `bucket: daily` with no `valid_until`, or one not yet passed (fail-open: absent `valid_until` means "currently valid"). |
| A page is retained, not archived | Unreadable page content — retained for safety rather than assumed expired. |
| `axiom`-class page never auto-demotes or auto-promotes | `memory_tiers.run_tier_sweep` skips it outright (`skipped_axiom` counter); the only path to move an axiom's tier is `demote_axiom_tier`, which requires a human-supplied reason/by and a matching `_axiom_governance.jsonl` ledger row written first. |
| Automatic tier sweep is a no-op | `librarian.memory_tier_sweep_enabled` is `false` (the default) — no page is scanned or written. |
| Auto-memory prune leaves a page in place | It is not `type: auto-memory`, or `classify_ephemeral_page` finds no ephemeral/operational signal — a page mixing one throwaway origin scope with one real one is retained, not pruned. |
| Auto-memory prune refuses to remove anything | The target store's `capabilities.versioned` is `False` — removal is git-only for recoverability. |
| `preserve` disposition tallies `preserve-unconfigured`, raw file untouched | Neither `preserved_log_dir` nor `preserved_log_adapter` is configured — the feature is opt-in twice over (the area AND a matching rule). |
| `preserved_log_dir` value ignored, with a warning | The value is absolute, or escapes the knowledge root via `..` — that key's contract is "a directory under the knowledge root"; an operator who needs to land outside it must use `preserved_log_adapter`. |
| `preserve` raises `StorageConfigError` | `preserved_log_adapter` names an adapter that is not registered — never a silent fallback to the local directory. |
| `preserve` tallies `preserve-failed`, raw file left in place | The move (local or adapter-routed) fails — an adapter-routed move writes the destination first and only removes the source once that write succeeds, so a cross-device failure never strands a half-moved log. |
| `preserve` tallies `transform-error`, raw file untouched | The optional `correction:` block fails to resolve — the correction is built *before* the move, so a bad transform never strands a half-moved log. |
| Decay sweep's off-corpus routing does not fire | No off-corpus surface is configured, or the page carries no explicit `data_class` — no shipped write path stamps `data_class` today, so this gate is dormant on any corpus produced by shipped code regardless of whether a retention pack is active. |

## See also

- Guides — [Daily operation](../guides/daily-operation.md) · [Decisions](../guides/decisions.md)
- Modules — [corrections](corrections.md) · [shape](shape.md) · [MCP surface](mcp.md)
- Design — [shape rules](../design/shape-rules.md) · [provenance shape](../design/provenance-shape.md) · [security posture](../design/security-posture.md) · [memory taxonomy](../design/memory-taxonomy.md)
- Reference — [configuration](../reference/configuration.md)
