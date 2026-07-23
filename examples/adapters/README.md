# Source adapters — examples

An **adapter** turns an external source (an API, an export file, a message feed,
a scraper) into raw-intake files that Athenaeum's librarian compiles into the
wiki. The contract every adapter follows is documented in
[`docs/adapter-contract.md`](../../docs/adapter-contract.md); the bundled
[`adapter-authoring`](../../skills/adapter-authoring/SKILL.md) skill is the
guided walkthrough.

## Contents

- **[`minimal_adapter.py`](minimal_adapter.py)** — a synthetic, generic,
  runnable Lane-A adapter. It writes one raw-intake file under
  `raw/<source>/<timestamp>-<uuid8>.md` using only Athenaeum's public
  `render_frontmatter` / `generate_uid` helpers, writes atomically, path-guards
  the write to stay inside `raw/`, and declares provenance. Copy it as a
  starting point for a real adapter.

  ```bash
  athenaeum init --path /tmp/kb                          # scaffold raw/ + wiki/
  python examples/adapters/minimal_adapter.py /tmp/kb --source press-releases
  athenaeum run --path /tmp/kb                           # compile raw → wiki
  ls /tmp/kb/wiki                                        # see the compiled entity
  ```

These examples are intentionally **synthetic and generic** — no PII, no
credentials, no private integration details. Real production adapters live in
their own host repositories by design; Athenaeum's OSS contract stops at the
on-disk raw-intake shape (see [`docs/provenance-shape.md`](../../docs/provenance-shape.md) §6).

For an adapter that bridges an agent runtime's own memory folder (Lane B —
`raw/auto-memory/<scope>/…`), see
[`docs/integrations/claude-code.md`](../../docs/integrations/claude-code.md)
instead.
