# Witness-attestation readout — TEMPLATE

Issue athenaeum#2160. A **template**, not a reading: copy it to
`witness-attestation-readout-<YYYY-MM-DD>.md`, fill every `<...>`, link the
copy from `docs/index.md`, and leave this file unfilled so the next reading
has a shape to start from. Every number below comes from one command, so a
reading is reproducible from the two artefacts the command reads and from
nothing else.

## Method

One command, read-only, no key and no run lock:

```
athenaeum receipts attest-readout [--since YYYY-MM] [--json]
```

- Sources: the receipt ledger (`wiki/_transcript_receipts/<YYYY-MM>.jsonl`)
  and the witness index (`promptwitness` ledger dir), plus
  `<cache_dir>/witness_hook_errors.jsonl`. **No transcript is opened**, so no
  claim text or digest can reach this file —
  `athenaeum.witness_readout.build_readout` is the whole computation and
  `tests/test_witness_readout.py::TestNoPlaintext` pins the absence of any
  64-hex run in either rendering.
- Exit status: `0` with no incidents, `5` when any incident exists. Operator
  ruling 10 (2026-10-28) removed any tolerated miss rate and any readout
  window, so there is no threshold to record here — a single incident is an
  incident.
- Take the reading with `--json` and paste the counts; the text rendering is
  built from the same object and cannot disagree with it.
- Record the flag state: `librarian.witness_attestation_enabled` and
  `librarian.witness_ledger_enabled` (both default off). With attestation off
  every `user-stated` receipt carries `witness_miss: flag-off`, which is NOT
  an incident — a reading taken in that state measures nothing and should say
  so rather than report a clean night.

- Reading taken: `<YYYY-MM-DDTHH:MM:SSZ>`
- Window: `<--since value, or "all months">`
- Build under test: `<branch> @ <short sha>`
- `librarian.witness_attestation_enabled`: `<on|off>`
- `librarian.witness_ledger_enabled`: `<on|off>`
- Command exit status: `<0|5>`

## Hits

| Metric | Count |
|---|---|
| `user-stated` receipts | `<n>` |
| `witnessed` receipts | `<n>` |
| &nbsp;&nbsp;`typed` | `<n>` |
| &nbsp;&nbsp;`slash-args` | `<n>` |

## Misses by reason

Bucketed by the prefix before the first `:` — `kind-not-human`,
`channel-not-interactive` and `transcript-promptsource` each carry a
`:`-suffixed value, so the bucket is the stable key. Rows that read `0` are
worth keeping: a reason that stopped occurring is a finding.

| Reason | Count |
|---|---|
| `ledger-absent` | `<n>` |
| `no-entry` | `<n>` |
| `kind-not-human` | `<n>` |
| `channel-not-interactive` | `<n>` |
| `digest-mismatch` | `<n>` |
| `session-mismatch` | `<n>` |
| `chain-broken` | `<n>` |
| `transcript-origin-not-human` | `<n>` |
| `transcript-promptsource` | `<n>` |
| `sidechain` | `<n>` |
| `flag-off` (not an incident) | `<n>` |

## Witness index

| Metric | Value |
|---|---|
| Witness lines in window | `<n>` |
| Unsigned lines (`sig is null`) | `<n>` |
| Anchored lines | `<n>` |
| Anchor coverage | `<pct>` |
| Anchored through (`<ledger_id>` -> `seq`) | `<pw-xxxxxxxxxxxxxxxx -> n>` |
| `session-close` turns / witnessed (cumulative snapshot, latest per session) | `<n>` / `<n>` |
| Superseded receipt lines (history, not counted) | `<n>` |

## Incidents

Three classes. The nightly librarian logs one WARNING per class with its
count; the per-incident detail below comes from the CLI verb.

| Class | Count |
|---|---|
| `provenance-miss` | `<n>` |
| `hook-error` | `<n>` |
| `session-close-deficit` | `<n>` |

### `provenance-miss`

Every miss other than `flag-off`, one row each. `likely false negative` means
the receipt carried `transcript_origin: human` and
`transcript_entrypoint: cli` — it looked human-and-interactive and still did
not attest, which is the shape most worth a human's attention.

| Reason | Session | `witness_ref` | Likely false negative |
|---|---|---|---|
| `<reason>` | `<session id>` | `<pw-xxxxxxxxxxxxxxxx:seq>` | `<yes/no>` |

### `hook-error`

From `witness_hook_errors.jsonl`, one row per exception class.

| Error class | Count |
|---|---|
| `<ExceptionClassName>` | `<n>` |

### `session-close-deficit`

Per ENDED session (one with a `session-close` witness line): the number of
witness-ELIGIBLE turn lines minus the number that are witnessable-human.

This is deliberately **not** the close line's own `turns - turns_witnessed`
pair. `session_close` counts `turns` as every `kind="turn"` line for the
session, which on a healthy session includes a `<system-reminder>`
(`system`), a finished subagent's `<task-notification>`, a subagent prompt,
and the first turn of a plain `claude "<prompt>"` TTY launch
(`launch-argument`) — all by design, none a miss. The literal pair is still
reported above for reference; only the eligible-population figure drives an
incident.

| Session | Deficit |
|---|---|
| `<session id>` | `<n>` |

## Reading

`<What the numbers say. If every incident count is 0 and the flags were on,
say so plainly — ruling 10's target state is zero provenance misses, so a
clean reading is the result, not an absence of one. If any count is non-zero,
name the single most likely cause and the next action; a non-zero
`session-close-deficit` means an interactive turn the kind classifier could
not recognise (`prompt_kind: unknown`), so check the prompt shapes
`promptwitness.harness.claude_code.WRAPPERS` covers, and a non-zero
`hook-error` names the exception class that explains it.>`

## Not measured here

- Whether a witnessed hit proves a human typed the prompt. It does not — see
  `src/athenaeum/witness_attestation.py`'s "Known limits" note and
  `docs/design/conflict-resolution.md` §14. This readout counts agreement
  between two ledgers, nothing more.
- Any rate, ratio or trend over a time window beyond `--since`. Deliberately
  out of scope.
