# Jev comparator eval (athenaeum#2008)

Evaluates Jev (TypeSafe AI) as a Gate-2-equivalent five-verdict comparator against the pairs the live comparator (athenaeum.comparator) already judged on 2026-10-08, by asking Jev a single Choice question per pair with the five verdict values as criteria. Offline: no code change to the live comparator, no corpus mutation -- see the script's module docstring for the full scope note, including why this reads as verdict-only agreement.

## Pairs selected and skipped

- `v1.gate2` ledger entries read: 137
- skipped, missing/unreadable source: 0
- skipped, PII hazard: 0
- skipped, content drifted since the verdict was decided (no matching git revision found): 0
- recovered from git history (current text had drifted, an earlier revision matched the verdict's recorded content hash): 115
- **selected for this eval: 137**

- Jev calls attempted: 137 of 137
- total input tokens: 155670
- total cost: $0.0065 (cap: $5.00)

## Agreement

137 scored, 0 errored; overall agreement: 39.4%

| verdict (Claude ground truth) | Jev agreement |
|---|---|
| duplicate | 10/12 (83.3%) |
| contradiction | n/a (0 pairs) |
| specialization | n/a (0 pairs) |
| distinct | 44/94 (46.8%) |
| underdetermined | 0/31 (0.0%) |

High-stakes verdicts -- **duplicate**: 10/12 (83.3%); **contradiction**: n/a (0 pairs) (0 pairs carry this ground-truth verdict in this sample -- see scope note).

Jev's top choice probability was below 0.5 on 28 of 137 scored pair(s).

### Confusion matrix (Claude verdict -> Jev choice)

| Claude \ Jev | duplicate | contradiction | specialization | distinct | underdetermined |
|---|---|---|---|---|---|
| duplicate | 10 | 2 | 0 | 0 | 0 |
| contradiction | 0 | 0 | 0 | 0 | 0 |
| specialization | 0 | 0 | 0 | 0 | 0 |
| distinct | 11 | 1 | 38 | 44 | 0 |
| underdetermined | 2 | 6 | 10 | 13 | 0 |

## Latency

Jev per-call wall-clock: mean 0.453s, p95 0.520s, over 137 call(s).
These figures were measured over a slow hotel internet connection abroad; treat the absolute latency numbers as an upper bound on what a normal connection would show, not a clean model-only measurement.
Claude's per-pair latency is not recorded in the verdict ledger (`VerdictEntry` has no latency field) and re-running the Claude comparator was out of scope for this eval (it would spend Anthropic budget) -- so no side-by-side latency comparison is reported.

## Recommendation

Overall verdict-only agreement: 39.4%.

Pre-registered framing (set before this run's results were read):
- **Pre-screen** (Jev's `distinct` call skips a Claude Gate 2 call): acceptable if few of Claude's non-distinct pairs are misclassified `distinct` by Jev (a false-distinct here means a real duplicate/conflict pair never reaches Claude at all).
- **Replacement** (Jev's verdict is recorded instead of Claude's): needs high overall agreement AND a low false-duplicate rate on Claude-distinct pairs (a false duplicate can drive an incorrect fold proposal).
- **Not suitable**: neither bar is cleared.

Only 12 duplicate-verdict pair(s) exist in this sample -- any duplicate-agreement rate carries a wide interval; do not treat it as a settled number without a larger sample.
**Recommendation: not suitable.** Agreement with the recorded Claude verdicts is too low on this sample to recommend Jev as either a pre-screen or a replacement for Gate 2 today.

