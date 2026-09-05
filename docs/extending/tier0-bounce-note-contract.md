<!-- SPDX-License-Identifier: Apache-2.0 -->

# Contract: the Tier-0 bounce note a producer must emit (athenaeum#854)

This document pins the exact raw-intake note shape the deterministic Tier-0
hard-bounce gate (`librarian.tier0_bounce_mark`, athenaeum#765) recognizes, so
a producer in another repository can implement against it instead of inferring
it from the source. It describes what the shipped gate already does; it does
not redesign it, and it changes no runtime behaviour on the intake path.

`docs/deprecated-email-tracking.md` § Q3 describes this same path in prose, as
a design decision. This is the implementable version of that prose: every
field, where it must appear, what makes it satisfied, and what happens when it
is not.

## Nothing is rejected

The single most important property of this contract, and the reason it needs
writing down at all:

> **A note that does not conform is not rejected. There is no error, no
> warning, and no bounce mark. It falls through to the ordinary Tier 1/2/3
> reasoning path and is compiled as an ordinary free-text memory.**

For one note that is correct and harmless — the fall-through is the design
(athenaeum#765: "nothing is rejected, a non-conformant note just climbs a
tier"). At **backfill volume** it is a contamination vector: a producer
emitting a few hundred near-miss notes compiles a few hundred
addresses-plus-diagnostics into the corpus as prose, and never sees a single
failure signal telling it so.

The near-miss is not hypothetical. A survey of historical bounce evidence
found only **13 of 189** entries carrying a `5.x.x` code at all; the rest were
list-verification verdicts or bare SMTP replies. A producer replaying that
material without checking first would take the fall-through path for the large
majority of it.

**So: check before you submit in bulk.** See [Checking a
note](#checking-a-note-before-you-submit) below.

## The note

A bounce fact is an **ordinary free-text raw-intake note** — the same
`remember()` call every other fact uses. There is no bespoke schema, no
`type:` field, and no dedicated intake path. What makes it a Tier-0 bounce
note is that four conditions happen to hold across its frontmatter and body.

```markdown
---
observed_at: 2026-08-05
source: script:bounce-relay
---

alex@example.org hard-bounced. Diagnostic: 550 5.1.1 user unknown.
```

Through `remember()` (note which parameter carries which value — this is the
one place producers reliably get it wrong):

```python
remember(
    content=(
        "---\n"
        "observed_at: 2026-08-05\n"
        "---\n\n"
        "alex@example.org hard-bounced. "
        "Diagnostic: 550 5.1.1 user unknown."
    ),
    source="bounce-relay",              # picks the raw/<session>/ directory
    sources="campaign:example/msg-abc",  # per-claim provenance -> the note's `source:`
)
```

`remember()`'s `source` parameter selects the `raw/<session>/` landing
directory. The **`sources`** parameter is the per-claim provenance, and it is
what lands as the note's own `source:` frontmatter key — which is the key the
gate reads.

## The four conditions

All four must hold. Each is checked against the note as submitted, and none
is inferred, defaulted, or guessed at.

| # | Where | Condition | Satisfied when |
|---|-------|-----------|----------------|
| 1 | frontmatter | `observed_at` | The note's **own** frontmatter carries a non-empty `observed_at` — the date the bounce was observed. Whitespace-only does not count. Written to the mark, and used as the `valid_until` close. |
| 2 | frontmatter | `source` | The note's **own** frontmatter carries a non-empty `source` — per-claim provenance. Must be the bare shorthand string or the per-value mapping shape; any other YAML type is not a source the mark can be attributed to. |
| 3 | body | exactly one identifier | The body names **exactly one** email-shaped token (`pii.find_inline_emails`, deduped). Zero, or several, is ambiguous — Tier 0 does not pick one. |
| 4 | body | a `5.x.x` code | The body carries an RFC 3463 enhanced-status code of the permanent-failure class, matching `\b5\.\d{1,3}\.\d{1,3}\b`, anywhere in the text. Both `550 5.1.1 user unknown` and a bare `5.1.1` satisfy it. |

Both frontmatter fields are **pre-existing generic per-claim fields**
(athenaeum#424, athenaeum#90) that any `remember()` call can already carry —
not new schema invented for bounces.

### `4.x` is deliberately out of scope

Condition 4 keys on the `5.x.x` permanent-failure class only. A `4.x`
transient give-up — a live address behind a temporary routing
misconfiguration, for instance — never matches, and must not: marking a
transiently-failing address non-deliverable would be wrong. A `4.x` note
declines with `missing_hard_bounce_code` and falls through like any other
non-conforming note. Widening the gate to other evidence classes is a separate
question, not a producer-side workaround.

### What a match does

On a match, `pii.mark_bounced` upserts `identifier`, `pii: true`,
`bounce_diagnostic`, `observed_at`, `source` and `valid_until` (set to
`observed_at`) onto the identifier's own contact record on the excluded
contacts surface — never a ledger row. The `diagnostic` recorded is the
verbatim line the `5.x.x` code was found on. Re-reporting the identical fact
is idempotent, and an identifier is never deleted, only ever gains fields.

`pii.is_bounced` is the single read-side predicate a consumer calls to tell
"present but non-deliverable" apart from "never seen".

## Checking a note before you submit

`athenaeum bounce-contract` answers "would Tier 0 recognize this note?" for a
candidate note **without writing anything** — no mark, no intake submission,
no store mutation, no network, no LLM call. Run it over a batch before
submitting the batch.

```console
$ athenaeum bounce-contract --file candidate.md
conforms: Tier 0 would mark this note as a hard bounce
  identifier: alex@example.org
  diagnostic: Diagnostic: 550 5.1.1 user unknown.
  observed_at: 2026-08-05

$ athenaeum bounce-contract --text "$(cat near-miss.md)"
does NOT conform: 2 unmet condition(s). Tier 0 would leave this note to the
reasoning tiers (not an error, and not a bounce mark):
  [frontmatter] missing_source: Add a non-empty `source:` ...
  [body] missing_hard_bounce_code: The body must carry an RFC 3463 `5.x.x` ...
```

The note is read from `--file`, `--text`, or stdin. `--json` emits a
machine-readable verdict:

```json
{
  "conforms": false,
  "identifier": null,
  "diagnostic": null,
  "observed_at": null,
  "declines": [
    {"reason": "missing_source", "where": "frontmatter", "detail": "..."}
  ]
}
```

**Exit codes:** `0` — conforms; `2` — does not conform; `1` — a genuine error
(unreadable file, bad usage). The non-zero-on-decline convention lets a
producer gate a submission in a shell:

```bash
athenaeum bounce-contract --file note.md && submit note.md
```

**A CLI is the shipped surface** because the consumer of this contract is a
producer in another repository, and the nearest one is TypeScript
(`voltaire#117`'s backfill). A Python function is not callable from there; a
subprocess with `--json` is. `bounce_contract.check_tier0_bounce_conformance`
stays importable for a Python producer, but the CLI is the portable surface
and the one this contract points at. (Per `AGENTS.md`, only names in the
package root's `__all__` carry semver guarantees; the CLI subcommand and its
exit codes are what this contract commits to.)

### Decline reasons

Every reason the check can report. All unmet conditions are reported, not just
the first — a producer fixing a batch needs the whole list.

| Reason | Where | Means |
|--------|-------|-------|
| `frontmatter_not_a_mapping` | frontmatter | The frontmatter parsed to something that is not a YAML mapping (a list, a bare scalar), so the per-claim fields cannot be read from it at all. |
| `missing_observed_at` | frontmatter | Condition 1 unmet: no non-empty `observed_at`. |
| `missing_source` | frontmatter | Condition 2 unmet: no non-empty `source`. |
| `unsupported_source_type` | frontmatter | `source` is present but is neither a string nor a mapping. |
| `no_email_identifier` | body | Condition 3 unmet: the body names no email-shaped token. |
| `several_email_identifiers` | body | Condition 3 unmet: the body names more than one. Emit one note per address. |
| `missing_hard_bounce_code` | body | Condition 4 unmet: no `5.x.x` code. A `4.x` transient note declines here. |

These tokens are stable and safe to branch on. The table is pinned to
`bounce_contract.DECLINE_REASONS` by a test
(`tests/test_bounce_contract.py::TestContractDocumentAgreement`), so a reason
added to the code without being documented here fails CI.

## How the contract and the gate are kept in agreement

A contract document that drifts from the gate it describes is worse than no
document, so agreement is enforced three ways rather than asserted:

1. **One code path.** `librarian.tier0_bounce_mark` calls
   `bounce_contract.check_tier0_bounce_conformance` for its whole recognition
   decision and does nothing but write the mark on top of it. The check cannot
   answer differently from the gate, because it *is* the gate's answer.
2. **The production predicates, called directly.** The body conditions use
   `pii.find_inline_emails`, `pii.find_hard_bounce_code` and
   `pii.detect_hard_bounce_fact` — the same functions the mark path uses,
   never a re-derivation of the email or `5.x.x` shapes.
3. **Tests that pin both seams.** `tests/test_bounce_contract.py` asserts, for
   a matrix of synthetic notes, that the check's verdict matches
   `tier0_bounce_mark`'s own accept/decline (run in `dry_run`, so it writes
   nothing) including the identifier and diagnostic it recovers — and that
   this document's reason table matches `DECLINE_REASONS` exactly.

## Related

- `docs/deprecated-email-tracking.md` § Q3 — the design decision this contract
  implements, in prose.
- `src/athenaeum/bounce_contract.py` — the check.
- `src/athenaeum/librarian.py` — `tier0_bounce_mark`, the gate.
- `src/athenaeum/pii.py` — `detect_hard_bounce_fact`, `mark_bounced`,
  `is_bounced`.
