# `used` column heuristic accuracy

Generated: 2026-09-15T00:31:23+00:00

Measures `athenaeum.push_metrics.determine_references` — the rule behind the viewer's `used` column — against known ground truth. The measurement drives the real function; it never reimplements it.

## What `used` means today

A pushed page is marked **used** when either signal fires:

1. **Citation** — the page's recorded push id appears as a *whole token* (not embedded in a longer alphanumeric run) in user- or assistant-written text. A tool result is excluded: recall's own output quoted back is an echo, not a citation.
2. **Content** — the assistant's own text reproduces a distinctive 4-word shingle of the pushed page. This is the only signal that can reach content-only use, where a breadcrumb delivered `name — description` and no id was ever written down.

An id present only in a tool result, with no content signal, is an echo and is not counted — unless the page's own text cannot be resolved, in which case the two readings are indistinguishable and the prior verdict stands. Both signals are local and free: no judge, no model call.

## Synthetic confusion matrix (AC1)

Unit: **one pushed page**. Positive class: the heuristic says the page was used. Ground truth is known by construction — one fixture per reachable quadrant.

**Push-path coverage.** Both production paths are exercised. A compiled entity's uid is `uuid4().hex[:8]` (`athenaeum.models.generate_uid`), so the MCP `recall` path (id read from frontmatter) and the sidecar path (id derived from the filename) record the *same* eight-hex id for the same page — the `short_id_collision` fixture goes through the sidecar builder and the other four through the MCP builder, and a test asserts the two derivations agree. What the paths do not share is raw-intake hits, where both record the whole filename; that shape is not fixtured here.

| | heuristic: used | heuristic: not used |
|---|---|---|
| **truly used** | 2 (TP) | 0 (FN) |
| **truly unused** | 0 (FP) | 3 (TN) |

- False-negative rate (genuinely-used pages missed): **0.0%** (0/2)
- False-positive rate (unused pages flagged used): **0.0%** (0/3)

### Per-quadrant detail

| quadrant | push path | truly used | heuristic | cell | why the truth value is what it is |
|---|---|---|---|---|---|
| `used_with_uid` | mcp | yes | used | true positive | The agent recalled the page and cited its uid while answering from its content. Genuine use, and the uid is present. |
| `used_without_uid` | mcp | yes | used | true positive | The sidecar injected a breadcrumb line (`name — description`, which carries no uid) and the agent answered from that content without ever calling recall. Genuine use; no uid anywhere in the transcript. |
| `echoed_not_used` | mcp | no | not used | true negative | A tool result echoed the recall output verbatim — uid included — and the agent then explicitly set the page aside. The uid is in the transcript; nothing downstream drew on it. |
| `not_used` | mcp | no | not used | true negative | The page was pushed and neither its uid nor its content ever surfaced. A cheap offer that went unused — the system working as designed. |
| `short_id_collision` | sidecar | no | not used | true negative | The page was pushed by the sidecar and never used. The session happens to contain a git SHA whose first eight characters are the push id, so an unanchored substring test scores it used on hex the page had nothing to do with. |

**How to read these rates.** The fixture set has one page per quadrant, so each rate is a property of the fixture design, not an estimate of how often each quadrant occurs in real sessions. What the matrix establishes is which quadrants the decision rule can and cannot reach. Under the original substring rule, content-only use was *structurally* invisible, an echoed id was *structurally* indistinguishable from a cited one, and a push id was *structurally* confusable with any hex that happened to start the same way. Each of those is now reachable by a signal the rule consults; none of it depends on the sample.

## Agreement with free utilization signals over rollout records (AC2)

Compares the ledger-equivalent signal (uid citation) against the free content signal (distinctive 4-gram overlap between delivered text and the answer) on PUSH arm rows, per `probe_class`. Both signals are already computed by `tests/evals/north_star_report.py`; no judge and no Anthropic API call is involved.

**No rollout result store was supplied — this section was not measured.** That is an absence of records, not an agreement of zero.

## Scope

Synthetic fixtures only. The rates above characterize the decision rule, not the frequency of each quadrant in real sessions, and no live session is read here. The rule these numbers score was changed by athenaeum#1585; earlier dated files in this directory score the original substring rule and are kept as the before-picture rather than edited.
