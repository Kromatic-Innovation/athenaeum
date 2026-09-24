# Tier-0 attributed-paste cleanup — proposer agreement (athenaeum#1717)

Generated: 2026-09-24

Measures `athenaeum.paste_cleanup.propose_page` (the `classify`-knob
proposer pass, `claude-haiku-4-5-20251001`) against a 32-row eval set the
operator hand-ruled on 2026-09-24: label agreement, a confusion matrix, and
the verifier rule the issue's own AC selects from the result. Counts only —
no page uids, names, reasons, or corpus text appear below or in the source
run's committed record; the eval set and its per-row output are host-side
only (`~/.cache/athenaeum/1717/`, never committed).

## What the proposer pass does

For each extracted attributed paste (`athenaeum.paste_cleanup.extract_paste_span`
splits a `## Notes` bullet into the paste and any fused legitimate content,
returning `hold` instead of a split when a workshop/mural paste's boundary
can't be located reliably), the proposer asks `claude-haiku-4-5-20251001` for
one of `keep` / `rewrite` / `remove`, a one-line reason, and a confidence.

## Method

32 rows, stratified by kind (bio / workshop-engagement-summary /
internal-engineering-ops note) crossed with tier (confident / broad-tail),
drawn from the athenaeum#1717 step-1 sampling frame. Ground truth is the
operator's 2026-09-24 ruling on each row (`accept`, or a corrected label for
2 rewrites). The proposer ran once per row against the LIVE bullet content
in `~/knowledge/wiki` at measurement time (no content quoted here); a `hold`
extraction short-circuits before any model call and is excluded from the
agreement denominator, never counted as a disagreement.

**One proposer defect found and fixed before this measurement was taken.**
An early pass showed the proposer defaulting to `remove` on genuine
on-topic contact-bio facts whenever the paste carried a trailing
`Source: <adapter/pipeline>` provenance footer — it was reading the
footer as evidence the whole paste was administrative noise. Fixed by
adding an explicit rule to `prompts/paste_cleanup_propose.md`: a
provenance footer does not by itself make a paste off-topic; judge the
substantive claim that precedes it. The measurement below is the pass
taken AFTER that fix.

## Results (32 rows, 1 held, 31 scored)

| | count |
|---|---:|
| total rows | 32 |
| held (extraction ambiguous, excluded from agreement) | 1 |
| scored | 31 |
| agree with operator | 24 |
| **label agreement rate** | **77.4%** |

### Confusion matrix (proposed → operator)

| proposed \ operator | keep | rewrite | remove |
|---|---:|---:|---:|
| **keep** | 4 | 0 | 0 |
| **rewrite** | 1 | 0 | 0 |
| **remove** | 3 | 3 | 20 |

Reading it: every `remove→remove` and `keep→keep` cell agrees (24 total).
The 7 disagreements are 3× `remove→keep` (proposer over-removed a genuine
on-topic fact), 3× `remove→rewrite` (proposer removed instead of
recognizing a fact needing correction, not removal), and 1× `rewrite→keep`
(proposer proposed a correction where the operator judged the original fine
to keep as-is). The proposer never disagreed in the other direction — it
did not keep or rewrite anything the operator ruled `remove`.

### Agreement by confidence bucket

The proposer reported `confidence: "high"` on all 31 scored rows — it never
used `medium`/`low` even on the rows it got wrong. Confidence as currently
prompted does not discriminate correct from incorrect proposals in this
sample, so it cannot be used on its own to target the verifier pass at the
proposer's own uncertain rows.

### Extraction status (no-LLM pass, all 32 rows)

| status | count |
|---|---:|
| clean (no fusion suspected) | 21 |
| split (fusion boundary found, paste isolated from legitimate tail) | 10 |
| hold (fusion suspected, no reliable boundary — never guessed) | 1 |

All 7 CSV-flagged `[fused]` rows were correctly identified as fusion
candidates by the no-LLM extractor: 6 split cleanly (the isolated paste
span was what the proposer then judged, matching the operator's `remove`
ruling in all 6) and 1 correctly fell back to `hold`.

## Verifier rule (issue athenaeum#1717's own AC)

> a stronger model re-checks low-confidence proposals plus a fixed 10%
> sample of the rest, **or all proposals if agreement < 90%**.

Measured agreement is 77.4%, below the 90% threshold, **and** the
confidence signal does not discriminate (see above) — even the "verify the
low-confidence tier" half of the sampled rule would not have targeted the
actual disagreements, since every one of them was reported `high`. Both
facts independently point the same direction.

**Rule selected: verify every proposal with `claude-sonnet-5`** (`--verify-rule all`, `choose_verify_rule`'s
threshold path). Re-measure after a further proposer prompt iteration
before switching to the sampled rule — the eval set stays reusable for that
via `athenaeum.paste_cleanup.measure_agreement`.

## Cost (proposer pass only, this 32-row run)

- Model: `claude-haiku-4-5-20251001`
- Total tokens: 64,248 input / 2,424 output
- Cost: **$0.076** (Haiku $1.00 / $5.00 per MTok)

Verifier-pass and full-corpus dry-run costs are reported separately on
athenaeum#1717 (step 4), once measured against the step-1 sampling frame.

## Scope

This measurement is against real corpus content (necessarily — the whole
point is measuring the proposer's judgment on real pastes), so the eval
rows, their content, and the per-row verdicts stay host-side. Only the
counts above are committed here.
