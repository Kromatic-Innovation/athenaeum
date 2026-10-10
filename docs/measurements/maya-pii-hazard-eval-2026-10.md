# Maya PII-hazard eval (athenaeum#2049)

Evaluates Maya, a local 435M-parameter yes/no cross-encoder
(`VishalMysore/maya` on Hugging Face), as a candidate classifier for the
PII-hazard question "is this string a way to contact a specific human?" —
the no-egress sibling of athenaeum#2009, which asks the same question of
Jev (a hosted classifier). The regex gate in `athenaeum.pii` /
`athenaeum.sensitivity` stays the live gate regardless of this eval's
outcome; this is a measurement, not a deployed change.

## Unredacted, but local

The fixture string reaches Maya exactly as typed, with no redaction first —
redacting a contact-shaped string before asking "is this a way to contact a
specific human?" would erase the exact signal the question is about (same
rationale as athenaeum#2009). Unlike athenaeum#2009, that unredacted string
never leaves this machine: Maya's weights run entirely locally, so there is
no network egress for the fixture to travel over.

## Licence note

Maya is Apache 2.0, but its base model's training data reportedly includes
non-commercial datasets. This eval is research use. Production adoption of
Maya as a live classifier needs a licence check recorded on athenaeum#2049
first — this measurement does not constitute that check.

## How to run it (host-side only)

```sh
export ATHENAEUM_MAYA_WEIGHTS_PATH=/path/to/local/maya/weights
pip install -e ".[maya-eval]"

python scripts/eval_maya_pii_hazard.py \
    --fixtures /path/to/host-side/positives.jsonl \
    --allowlist ~/knowledge/pii-allowlist.yaml \
    --output docs/measurements/maya-pii-hazard-eval-2026-10.md
```

- `--fixtures` takes a JSON-Lines file of `{"text": ..., "label": true|false}`
  rows (true = a way to contact a specific human) — host-side only, never
  committed to this public repo.
- `--allowlist` reads the adjudicated PII allowlist through the sanctioned
  loader (`athenaeum.pii.load_pii_allowlist`); each exact-`value` entry
  becomes a `label=false` row (a human has already ruled it is not a way to
  contact a specific human).

## Fixture provenance (counts only — no fixture content in this repo)

- **Positives (275, label=true):** real contact-shaped values pulled through
  the sanctioned `athenaeum entity --uid <uid> --include-excluded` CLI path
  (the same path `recall --with-pii` uses), sampled from 251 of 16,923
  enumerated `person` entities within a 30-minute host-side time budget.
  270 emails, 5 phones. 62 of the 251 sampled entities had no contact record
  and were skipped. This is a small, randomly-sampled slice of the person
  corpus, not a census — read the metrics below as directional for this
  corpus's contact-value shapes, not a definitive bound.
- **Negatives (547, label=false):** every exact-`value` entry in the live
  adjudicated PII allowlist (`~/knowledge/wiki/_pii-allowlist.yml`), loaded
  through `athenaeum.pii.load_pii_allowlist`. The allowlist held 0 pattern
  (shape-only) entries at measurement time, so none were skipped. Of the 547
  exact values, roughly 500 are digit-shaped (ticket IDs, epoch timestamps,
  numeric slugs — already-adjudicated hard negatives) and 46 are
  email-shaped service/placeholder addresses; this comfortably covers the
  50–100 hard-negative target without hand-picking a subset.
- Fixture files exist only under this host's scratch directory and were
  never committed or inlined into this PR, per the acceptance criteria.

## Results

Two runs against the same 822-row fixture set (275 positive / 547 negative),
CPU inference, `maya-eval` extra installed in a dedicated venv:

### Run 1 — default threshold (0.5)

| metric | Maya | Regex gate |
|---|---|---|
| scored rows | 822 / 822 | 822 / 822 |
| accuracy | 0.951 | 0.824 |
| precision | 0.904 | 0.659 |
| recall | 0.956 | 0.982 |
| TP / FP / TN / FN | 263 / 28 / 519 / 12 | 270 / 140 / 407 / 5 |
| Brier score | 0.0897 | n/a |
| mean latency (s/row) | 0.1296 | 0.0000 |
| p95 latency (s/row) | 0.1421 | 0.0001 |
| wall clock (822 rows) | 112 s | (included above) |

### Run 2 — abstention band [0.3, 0.7]

181 of 822 rows (22.0%) fell inside the abstention band and were excluded
from the confusion counts below (they would route to a fallback/manual path
in a deployed gate rather than receive an automatic verdict):

| metric | Maya (scored rows only) | Regex gate |
|---|---|---|
| scored rows | 641 / 822 | 822 / 822 |
| accuracy | 0.970 | 0.824 |
| precision | 0.929 | 0.659 |
| recall | 0.996 | 0.982 |
| TP / FP / TN / FN | 236 / 18 / 386 / 1 | 270 / 140 / 407 / 5 |
| Brier score (all 822 rows, not band-filtered)\* | 0.0897 | n/a |
| mean latency (s/row) | 0.1293 | 0.0000 |
| p95 latency (s/row) | 0.1426 | 0.0001 |
| wall clock (822 rows) | 110 s | (included above) |

\* `compute_metrics` does not exclude abstained rows from the Brier
calculation, unlike the confusion-matrix metrics above it, so this number is
identical to Run 1's and should not be read as "scored rows only." A
scored-rows-only Brier score would need a rerun with a fixed script; tracked
as follow-on work, not blocking this measurement doc.

(Jev, athenaeum#2009's hosted sibling, is pending — no comparable column
exists yet.)

## Recommendation

**Candidate for the hazard gate**, conditional on the licence precondition
above being resolved first (Apache 2.0 code licence, but base-model training
data reportedly includes non-commercial sets — needs an explicit legal/licence
check recorded on athenaeum#2049 before any production adoption).

Supporting observations:

- At the default threshold, Maya cuts the regex gate's false-positive rate by
  roughly 5x (28 vs. 140 on this fixture set) while recall stays comparable
  (0.956 vs. 0.982), i.e. materially higher precision for a similar miss
  rate — exactly the asymmetry a hazard gate wants (fewer good values wrongly
  withheld, without letting materially more real contact values through).
- Adding a [0.3, 0.7] abstention band pushes both precision and recall higher
  on the rows it does score (0.929 / 0.996), at the cost of routing 22% of
  rows to a fallback path — worth sizing against how expensive that fallback
  is before picking a deployed abstention width.
- Per-row latency (~130 ms mean on CPU, no GPU used) is far higher than the
  regex gate's but well within range for a batch/offline gate; a live
  per-request gate would need to confirm this against its own latency budget.
- This measurement draws positives from a 251-entity sample (of 16,923
  person entities) gathered in a bounded time window — a larger or
  differently-sampled positive set could shift these numbers; re-run with a
  larger sample before treating this as final.

Not "not yet" or "inconclusive": the margin over the regex gate is large
enough on this fixture set, and consistent across both runs, to justify
moving forward — contingent on the licence check above.
