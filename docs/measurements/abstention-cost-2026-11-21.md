<!-- SPDX-License-Identifier: Apache-2.0 -->

# Abstention cost, 2026-11-21: why a correct "I don't know" costs the verdict arm so much, and the signal that cuts it

Issue athenaeum#3033, the second work item of the conditional GO recorded on
athenaeum#2805. The regrade of run 44783114473
([`native-memory-baseline-2026-11-20-regrade.md`](native-memory-baseline-2026-11-20-regrade.md))
puts `abstention` behind every condition-3 (cost) failure of the verdict arm
`push_breadcrumb_pull`: 6.785x the cheaper native arm at `core`, and 2.500x at
`medium`, where it is also the one condition-2 loss. This document counts the
calls behind that cost from the stored rows, names the mechanism shipped against
it, and projects what the mechanism would have saved on the same rows.

**Nothing was run and nothing was spent.** Every number below is read from, or
replayed over, the stored rows of run 44783114473. The projection in §4 is
arithmetic on those rows. It is not a measurement, and the next grid is what
measures it.

## 1. Source

- Run 44783114473 (`Evals`, `workflow_dispatch` on `develop`, 2026-11-20), artifact
  `north-star-report`, file `north-star-store-vector.jsonl`: 1,026 distinct cells,
  15,426,572 bytes, sha256
  `e0ce912210f8a4c9f87b97a87bd47e5f3b7e38f0f38b9d3cb4a50921fe1d453f`. It was
  relayed out of Actions by a throwaway workflow that gzipped it on the runner
  and printed checksums, which were verified locally after the fetch.
- Rows are read last-row-wins by cell key, as `load_rollout_rows_and_diagnostics`
  reads them. Search backend `vector`, model `claude-haiku-4-5-20251001`, mode
  `api` throughout.
- "Read tokens" is input plus output tokens summed over a row's turns. That is the
  read half of `cost_per_correct`. The three verdict-arm rows at `core` sum to
  91,379, which matches the regrade report's `read_tokens` for that cell.

## 2. Per-row counts: verdict arm against native

The three abstention probes are `abstain_unknown_client` (payment terms for
"Harrowgate Industrial", a client on no page), `abstain_unknown_person` ("what is
Yusuf Adeyemi responsible for?") and `abstain_unknown_policy` ("what is the
firm's parental leave policy?"). Native arms call `grep`/`read` over the native
memory files. The verdict arm calls `recall`/`read_entity`.

#### `core`

| arm | probe | turns | tool calls | call sequence | read tokens | graded |
| --- | --- | --- | --- | --- | --- | --- |
| `push_breadcrumb_pull` | `client` | 5 | 4 | recall, recall, recall, recall | 35978 | yes |
| `push_breadcrumb_pull` | `person` | 5 | 4 | recall, recall, recall, recall | 31876 | yes |
| `push_breadcrumb_pull` | `policy` | 4 | 3 | recall, recall, recall | 23525 | yes |
| `native_grep` | `client` | 6 | 9 | grep, grep, grep, grep, grep, grep, grep, grep, grep | 13218 | no (turn cap) |
| `native_grep` | `person` | 4 | 3 | grep, grep, grep | 4489 | yes |
| `native_grep` | `policy` | 6 | 6 | grep, grep, grep, read, grep, grep | 9342 | no (turn cap) |
| `native_index` | `client` | 2 | 1 | grep | 11848 | yes |
| `native_index` | `person` | 2 | 1 | grep | 11755 | yes |
| `native_index` | `policy` | 2 | 3 | grep, grep, grep | 11992 | yes |

#### `medium`

| arm | probe | turns | tool calls | call sequence | read tokens | graded |
| --- | --- | --- | --- | --- | --- | --- |
| `push_breadcrumb_pull` | `client` | 6 | 6 | recall, recall, recall, recall, read_entity, recall | 42671 | no (turn cap) |
| `push_breadcrumb_pull` | `person` | 5 | 4 | recall, recall, recall, recall | 30310 | yes |
| `push_breadcrumb_pull` | `policy` | 4 | 4 | recall, read_entity, read_entity, recall | 22322 | yes |
| `native_grep` | `client` | 6 | 7 | grep, grep, grep, read, read, grep, grep | 9914 | no (turn cap) |
| `native_grep` | `person` | 4 | 3 | grep, grep, grep | 4536 | yes |
| `native_grep` | `policy` | 5 | 5 | grep, read, read, grep, grep | 10505 | yes |
| `native_index` | `client` | 2 | 1 | grep | 12414 | yes |
| `native_index` | `person` | 2 | 1 | grep | 12363 | yes |
| `native_index` | `policy` | 3 | 5 | grep, grep, grep, read, read | 20220 | yes |

## 3. The shape

**The verdict arm does not take many more turns than `native_grep`. It pays far
more per turn.** At `core` it averages 4.7 turns and 3.7 tool calls per
abstention row, against `native_grep`'s 5.3 turns and 6.0 calls. But a `grep`
result is a few matching lines, while each `recall` result is five to ten whole
pages, and every later turn re-reads all of them. So a 4-recall row costs about
32,000 to 36,000 tokens, while a 3-grep row costs about 4,500. `native_index`
answers in one call, because the index it carries in the prompt already shows
there is no such entry.

**The recall sequence is the same narrowing loop each time.** Every name probe
searched the full phrase, then the name, then each part of the name:

- `person` (both scales): `Yusuf Adeyemi responsible`, `Yusuf Adeyemi`,
  `Adeyemi`, `Yusuf`;
- `client` (`core`): `Harrowgate Industrial payment terms`,
  `Harrowgate Industrial`, `Harrowgate`, `Industrial`.

Every one of those calls returned pages, because vector search always returns
its nearest neighbours. None of those pages mentioned the name. Nothing in the
result told the agent that, so it searched again.

**The turn-cap row has the same pattern.** The `medium` `client` row that was not
graded made four `recall` calls and opened a distractor note with `read_entity`.
It was issuing a sixth call (`Harrowgate` again) when it hit the turn cap.

**The policy probe is a different case.** "Parental leave policy" names a topic,
not an entity, and at `medium` the corpus carries distractor notes that mention
parental leave without stating a policy. The agent read them and answered that
the firm "does not currently have a formal parental leave policy". By elimination
this is the wrong graded row at `medium`: the other graded row, `person`, is a
clean "I do not know". The person row's answer was read directly. The policy
grade is inferred, because the grader was not re-run for this note.

## 4. The mechanism, and why this one

The issue offered three options:

- a low-relevance signal in the `recall` result;
- tool-description guidance on when to stop;
- the push path's relevance floor, applied to pull results.

**A score floor does not separate the classes on these rows.** The smallest
vector distance per probe (`retrieval_hit_scores`, lower is closer) runs 0.754
to 1.399 for abstention. It runs 0.947 to 1.223 for `multi_hop` and up to 1.184
for `aggregation`, classes the verdict arm answers correctly. Any floor that
withholds the abstention hits withholds answerable `multi_hop` hits too. That
violates the issue's constraint that the mechanism not lower correctness
elsewhere. The same overlap is why a generic "stop after N searches" instruction
was not chosen: `multi_hop` and `follow_through` legitimately use several
recalls.

**What does separate them: the query names something no returned page
mentions.** Shipped as a signal in the `recall` result
(`athenaeum.recall_overflow.unmatched_name_terms` and
`render_unmatched_names_line`, wired into the MCP `recall` renderer). A
name-like query word is one that is capitalised, three or more letters long,
and not a question or request word. An all-capitals acronym never counts, and
neither does a sentence-initial capital followed by lowercase prose. Both
exclusions are conservative on purpose: a false line tells the agent to stop,
while a missed one only costs another search. When a name-like word appears in
none of the rendered result pages, one line is appended:

> No page shown above mentions "Yusuf" or "Adeyemi". If the question is about
> that name, the store most likely has no page on it: searching again for part
> of the name or another spelling is unlikely to find one, and the honest answer
> is that the store does not know.

The check is over exactly the text the agent is about to read, so the line can
only say something true about what was shown. It is not added on the unprompted
push path, and not to a result that already says no page matched.

**Replay over every stored `recall` call.** The function as shipped was run over
the query and returned text of every `recall` call in every arm of the run: 522
calls that returned pages. It would have fired on **49 of the 71 abstention
calls**, and on **0 of the 451 calls in every other class**:

| class | single_hop | multi_hop | temporal | disambiguation | distractor_robustness | redundancy | follow_through | aggregation | contradiction | negative_knowledge | unprompted_push |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| calls | 41 | 60 | 30 | 21 | 13 | 19 | 71 | 41 | 20 | 16 | 119 |
| fired | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

So on these rows every non-abstention `recall` result is byte-identical with and
without the mechanism. That is the offline evidence for "does not lower
correctness on the non-abstention classes". The next grid is the real test.

It fired on the **first** `recall` call of all four `client` and `person`
verdict-arm rows. It never fired on a `policy` row: that query names no entity,
and its distractor pages do contain its words.

## 5. Projection on the same rows

This assumes the agent answers on the turn after the first firing call. It sums
the stored tokens of the turns up to and including that answer turn. The answer
turn's own output length is the stored next turn's, so the estimate is
approximate.

| scale | probe | stored read tokens | projected read tokens |
| --- | --- | --- | --- |
| core | client | 35978 | 7475 |
| core | person | 31876 | 7268 |
| core | policy | 23525 | 23525 (no signal) |
| medium | client | 42671 (turn cap) | 7628 |
| medium | person | 30310 | 7177 |
| medium | policy | 22322 | 22322 (no signal) |

Against the cheaper defined native arm (`native_grep`), under the regrade's own
cost definitions:

- **`core`:** 38,268 read tokens over 3 correct gives 12,756 per correct, against
  4,489. That is **about 2.84x, still above 2.0x.** The ratio was 6.785x. The
  denominator is a single graded `native_grep` row, because the other two hit the
  turn cap. The remaining cost is the policy row.
- **`medium`:** the read half falls from 52,632 to 37,127, and the write share
  (234,032 over three rows) dominates. With the client row now graded and
  correct, and the policy row still wrong, this gives (37,127 + 234,032) / 2 =
  135,580 per correct, against 83,472: **about 1.62x, within 2.0x.** If the
  policy row also graded correct, it would be about 1.08x. Condition 2 at
  `medium` still turns on that policy row, which this mechanism does not change.

## 6. What this leaves open

- **The projection is not the next grid.** Acceptance criterion 3 of
  athenaeum#3033 is read on the next native-memory grid, run under the re-run
  rule in `docs/use-cases.md` §1. That reading is tracked as its own follow-up.
- **On these rows, `core` condition 3 stays over the limit after this mechanism.**
  The remaining excess is the topic-shaped abstention (`policy`). A lexical
  signal cannot reach it, because the store's distractor pages do contain the
  query's words. Covering it needs a decision this document does not make. The
  candidate is a stop rule for topic queries, and such a rule risks the
  multi-recall classes named in §4. The `core` denominator also rests on one
  graded native row.
- **Lowercase names are not caught.** The signal keys on capitalisation. On these
  rows every name query was capitalised, but an agent that writes `yusuf adeyemi`
  gets no line.
