# Session shapes — merged ledger — generated 2026-09-24

Ledger files read (operator paths, both under the deployment's configured
cache/knowledge roots, redacted per this repo's deployment-boundary policy —
see AGENTS.md):

- primary push-records ledger (deployment cache root) — 2785 row(s)
- secondary push-records ledger (deployment knowledge/wiki root) — 0 row(s),
  because the path does not exist on this host at all (not merely empty).
  The script's tolerant reader treats a missing file as zero rows, so it
  still appears in the ledgers-read table for completeness but contributed
  no data. Only the primary ledger held live rows.

Total rows: 2785. Total sessions: 1049.
Split date: 2026-09-14 (session bucketed by its LATEST row's ts).

| window | both | hook-only | pull-only | unknown-only |
|---|---|---|---|---|
| before | 9 | 263 | 1 | 88 |
| after | 14 | 674 | 0 | 0 |
| unknown-window | 0 | 0 | 0 | 0 |

Note on asymmetry: the `source` field distinguishing hook/sidecar pushes from
explicit pulls was only introduced 2026-09-09 (`SOURCE_FIELD_FIRST_SEEN`).
The "before" window (up to 2026-09-14) spans only five days post-cutover and
absorbs all 88 unknown-only sessions (pre-cutover rows with no usable
provenance signal), while "after" has none. The before/after pull-only and
unknown-only figures are not directly comparable as a trend for this reason;
the hook-only vs both comparison is less affected since push rows carry
`source` before and after the cutover.

## Provenance

- Script: `scripts/measure_session_shapes.py` (PR athenaeum#1704,
  athenaeum#1593 AC4).
- Interpreter: athenaeum deploy venv, Python 3.13, invoked with
  `PYTHONPATH=src` against a worktree checked out from `develop` at commit
  `253b1525` (branch `dijkstra/1706-session-shapes`).
- Command (paths shown as documented in the script's own docstring and in
  issue athenaeum#1706; the actual invocation used this repo's operator's
  real home-relative paths, redacted above per AGENTS.md):
  ```
  python scripts/measure_session_shapes.py \
    --ledger ~/.cache/athenaeum/_push_records.jsonl \
    --ledger ~/knowledge/wiki/_push_records.jsonl \
    --split-date 2026-09-14
  ```
- Exit status: 0 (no errors, no stderr output).
