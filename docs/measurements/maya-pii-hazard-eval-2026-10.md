# Maya PII-hazard eval (stub, athenaeum#2049)

Evaluates Maya, a local 435M-parameter yes/no cross-encoder
(`VishalMysore/maya` on Hugging Face), as a candidate classifier for the
PII-hazard question "is this string a way to contact a specific human?" —
the no-egress sibling of athenaeum#2009, which asks the same question of
Jev (a hosted classifier). The regex gate in `athenaeum.pii` /
`athenaeum.sensitivity` stays the live gate regardless of this eval's
outcome; this is a measurement, not a deployed change.

**This table is empty.** It is filled in by running the eval script
host-side, against host-side fixtures, with real Maya weights — none of
that can run in CI or in this PR (no implicit download, zero spend). See
"How to run it" below.

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
first — this stub does not constitute that check.

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
- Re-run and commit the regenerated file in place of this stub once a real
  measurement exists.

## Results

_Not yet measured. Fill in after a host-side run with real Maya weights._

| metric | Maya | Regex gate | Jev (pending, athenaeum#2009) |
|---|---|---|---|
| fixture count (pos / neg) | — | — | — |
| accuracy | — | — | — |
| precision | — | — | — |
| recall | — | — | — |
| Brier score | — | — | — |
| mean latency (s) | — | — | — |
| p95 latency (s) | — | — | — |

## Recommendation

_Not yet measured._ One of: **candidate for the hazard gate** / **not yet**
/ **inconclusive**, with the licence precondition above restated if the
recommendation is "candidate".
