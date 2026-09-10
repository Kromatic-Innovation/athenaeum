# `used` column heuristic accuracy (synthetic)

Generated: 2026-09-10T16:43:14+00:00

Measures `athenaeum.push_metrics.determine_references` — the rule behind the viewer's `used` column — against known ground truth. Issue athenaeum#1575. This measurement does not change the heuristic (AC4).

## What `used` means today

A pushed page is marked **used** when its recorded push id appears as an unanchored substring anywhere in the session transcript (user text, assistant text, or tool-result text) — `push_metrics.determine_references` does `pid in blob`, not a whole-token match. No content signal is consulted.

The id recorded is not the same shape on every push path: the MCP `recall` path (`opaque_push_id`) records a compiled entity's full uid, while the sidecar / `UserPromptSubmit` push path (`opaque_push_id_from_filename`) records only the 8-hex uid prefix — and that hook path is the dominant producer of push records in real sessions.

**Scope of this measurement.** The four fixtures below exercise the MCP / full-uid path only. The truncated-prefix (sidecar) path is unmeasured here and is tracked separately in athenaeum#1585.

## Synthetic confusion matrix (AC1)

Unit: **one pushed page**. Positive class: the heuristic says the page was used. Ground truth is known by construction — one fixture per reachable quadrant. **These rates describe the fixture design, not real-session frequency** — see "How to read these rates" below.

| | heuristic: used | heuristic: not used |
|---|---|---|
| **truly used** | 1 (TP) | 1 (FN) |
| **truly unused** | 1 (FP) | 1 (TN) |

- False-negative rate (genuinely-used pages missed): **50.0%** (1/2)
- False-positive rate (unused pages flagged used): **50.0%** (1/2)

### Per-quadrant detail

| quadrant | truly used | heuristic | cell | why the truth value is what it is |
|---|---|---|---|---|
| `used_with_uid` | yes | used | true positive | The agent recalled the page and cited its uid while answering from its content. Genuine use, and the uid is present. |
| `used_without_uid` | yes | not used | false negative | The sidecar injected a breadcrumb line (`name — description`, which carries no uid) and the agent answered from that content without ever calling recall. Genuine use; no uid anywhere in the transcript. |
| `echoed_not_used` | no | used | false positive | A tool result echoed the recall output verbatim — uid included — and the agent then explicitly set the page aside. The uid is in the transcript; nothing downstream drew on it. |
| `not_used` | no | not used | true negative | The page was pushed and neither its uid nor its content ever surfaced. A cheap offer that went unused — the system working as designed. |

**How to read these rates.** The fixture set has one page per quadrant, so each rate is a property of the fixture design, not an estimate of how often each quadrant occurs in real sessions. What the matrix establishes is which quadrants the decision rule can and cannot reach: content-only use is *structurally* invisible to a uid-substring rule, and an echoed uid is *structurally* indistinguishable from a cited one. Neither depends on the sample.

## Agreement with free utilization signals over rollout records (AC2)

Compares the ledger-equivalent signal (uid citation) against the free content signal (distinctive 4-gram overlap between delivered text and the answer) on PUSH arm rows, per `probe_class`. Both signals are already computed by `tests/evals/north_star_report.py`; no judge and no Anthropic API call is involved.

**No rollout result store was supplied — this section was not measured.** That is an absence of records, not an agreement of zero.

## Scope

This is a measurement only; `determine_references` is unchanged (AC4). Both failure modes above are tracked in athenaeum#1585, filed against the heuristic itself.
