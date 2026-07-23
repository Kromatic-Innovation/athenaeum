---
name: adapter-authoring
description: Build a custom source → raw-intake adapter for Athenaeum. Use when someone wants to feed an external source (an API, an export file, a message feed, a scraper, another tool's output) into an Athenaeum knowledge base, asks "how do I write an adapter / integration for athenaeum", or wants to turn some data source into wiki entities the librarian can compile.
---

# adapter-authoring

Teach the user (or yourself, as an agent) how to build a **source adapter** for
Athenaeum: a script or integration that turns an external source into
raw-intake files the librarian compiles into the wiki. This skill is
self-contained — it does not depend on any private Kromatic repo or context.

The authoritative contract this skill operationalises is
[`docs/adapter-contract.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/adapter-contract.md).
The runnable reference is
[`examples/adapters/minimal_adapter.py`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/examples/adapters/minimal_adapter.py).
This skill file ships inside the published `athenaeum` package (under `skills/`);
the contract doc and example live in the repository and the source distribution
(links above), so it stays self-contained however it was installed.

## The one rule

A source **only appends raw files** to the intake tree. A separate compiler —
the librarian — is the **only** writer to the wiki. Your adapter's entire job is
to write well-shaped raw-intake files; everything downstream (clustering, merge,
contradiction detection, retirement) is the librarian's job, not yours. Safety
comes from this structure, not from trusting the source.

## Which lane?

- **Lane A (default)** — `raw/<source>/<timestamp>-<uuid8>.md`. Use for almost
  every external source. This skill walks Lane A.
- **Lane B (auto-memory)** — `raw/auto-memory/<scope>/…`. Only for bridging an
  agent runtime's own memory folder. If that's your case, follow
  [`docs/integrations/claude-code.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/integrations/claude-code.md)
  instead — it is a complete worked Lane-B adapter.

## Build it in six steps

1. **Name the adapter.** Pick a stable, human-readable `<source>` label
   (alphanumerics + `-`/`_`), e.g. `press-releases`, `crm-export`, `notes`. It
   becomes the `raw/<source>/` subdirectory and is how a compiled claim is
   traced back to your adapter. Do not use the reserved `answers` or
   `auto-memory` names.

2. **Extract facts from the source.** Pull each unit of knowledge you want in
   the wiki. One raw file per coherent fact/entity is the natural grain — the
   compiler will cluster and merge related ones, so err toward more small files
   rather than one giant file.

3. **Shape each file** as YAML frontmatter + markdown body:
   - `source: <type>:<ref>` — the **ultimate** origin (e.g.
     `external:<url>`, `api:<vendor>:<date>`, `document:<path>`). Never cite the
     raw filename. This is the most important field.
   - optional `name:` (a short slug) and `description:` (one line).
   - the body: markdown; every claim in it should trace to `source`.
   The frontmatter schema is open — extra keys round-trip untouched, so you can
   carry adapter-specific metadata. See
   [`docs/provenance-shape.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/provenance-shape.md) for the full
   `source_type` vocabulary and per-field/per-value attribution.

4. **Write it — pick a mechanism:**
   - **MCP `remember` tool** (if your integration speaks MCP): call
     `remember(content=…, source="<adapter>", sources="<type>:<ref>")`. It
     handles the filename, path-guarding, sensitive-content screening, and
     provenance injection for you.
   - **Direct file drop** (cron job, importer, non-MCP): write
     `raw/<source>/<timestamp>-<uuid8>.md` yourself. Use
     `athenaeum.generate_uid()` for the uuid8 and a UTC `YYYYMMDDTHHMMSSZ`
     timestamp; build frontmatter with `athenaeum.render_frontmatter`. **Copy
     [`examples/adapters/minimal_adapter.py`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/examples/adapters/minimal_adapter.py)
     as your starting point** — it already does the atomic write, path-guard,
     and provenance correctly.

5. **Respect idempotency:**
   - Write-once: never overwrite a prior intake file; each filename is unique.
   - Don't dedupe before writing — re-emitting a fact is safe; the compiler
     collapses near-duplicates at compile time.
   - Write atomically (same-dir temp + `os.replace`) so a crash never leaves a
     half-written frontmatter block.
   - Stay strictly inside `raw/`; never write under `wiki/`.

6. **Verify the round-trip:**
   ```bash
   athenaeum init --path /tmp/kb            # once, to scaffold raw/ + wiki/
   python your_adapter.py /tmp/kb           # your adapter writes raw/<source>/*.md
   ls /tmp/kb/raw/<source>/                 # confirm files landed, named right
   athenaeum run --path /tmp/kb             # compile raw → wiki
   ls /tmp/kb/wiki/                          # confirm the entity page appeared
   ```
   Re-run `athenaeum run` — with no new intake it should be a no-op (the
   pipeline is idempotent).

## Guardrails

- **No private specifics.** Keep adapters and any examples generic and synthetic
  — no PII, no credentials, no machine-local paths, no private-repo details.
  Real production adapters belong in their own host repositories; athenaeum's
  contract stops at the on-disk raw-intake shape.
- **Fail loudly.** If your source yields nothing (auth failure, empty response),
  exit non-zero / raise — do not silently write zero files and report success.
- **Cite the ultimate source, never the raw file.** A `source:` pointing at a
  `raw/…​.md` path is always wrong.

## Reference

- [`docs/adapter-contract.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/adapter-contract.md) — the full contract this skill operationalises.
- [`examples/adapters/minimal_adapter.py`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/examples/adapters/minimal_adapter.py) — synthetic, runnable Lane-A adapter.
- [`docs/provenance-shape.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/provenance-shape.md) — provenance grammar and the MCP `remember` API.
- [`docs/integrations/claude-code.md`](https://github.com/Kromatic-Innovation/athenaeum/blob/main/docs/integrations/claude-code.md) — a complete Lane-B (auto-memory) adapter.
