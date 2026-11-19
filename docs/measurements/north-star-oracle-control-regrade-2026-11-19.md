<!-- SPDX-License-Identifier: Apache-2.0 -->

# North-star grid: the 30 failing oracle cells, classified and re-graded offline (2026-11-19)

Issue athenaeum#2918, following
[`native-memory-baseline-2026-11-19.md`](native-memory-baseline-2026-11-19.md)
(athenaeum#2805). That report found the `oracle` positive control failing on
five classes of run 44438673846 and treated its reading of the cause as
unconfirmed. This note confirms or corrects that reading cell by cell, by
re-grading the run's stored rows. It spends nothing and runs no model.

**Method.** The run's `north-star-store-vector.jsonl` (1026 rows, no torn rows,
no duplicates) was loaded with `load_rollout_rows_and_diagnostics` and every
`oracle` row re-graded with `grade_correctness`. Before any change this
reproduces the recorded counts exactly (single_hop 5/8 and 6/8, redundancy 2/4
and 2/4, aggregation 1/3 and 2/3, negative_knowledge 2/3 and 2/3,
unprompted_push 5/12 and 3/12, core and medium), which is the check that the
re-grade is the same grader on the same rows. In all 30 failing cells the
answer-bearing page was delivered (`marker_miss_with_delivery`), so none is a
retrieval miss; each is an answer that lacked the planted marker.

## Classification of the 30 cells

Four buckets:

- **Probe defect** - the marker is not a fact the question asks for, so a
  correct, responsive answer cannot be expected to contain it.
- **Matcher defect** - the answer contains the marker and the matcher cannot
  see it.
- **Model failure, paraphrase** - the answer states the planted fact in its own
  words and the literal marker is absent. This is not a defect of the probe or
  of the matcher as specified; whether to accept it is a ruling (see below).
- **Model failure, figure omitted** - the answer never states what the marker
  plants.

| probe_class | probe_id | scale | bucket |
| --- | --- | --- | --- |
| `single_hop` | `confidentiality_rule` | core | probe defect |
| `single_hop` | `bramfield_retainer_renewal` | core | probe defect |
| `single_hop` | `bramfield_retainer_renewal` | medium | probe defect |
| `single_hop` | `triform_vendor_consolidation` | core | probe defect |
| `single_hop` | `triform_vendor_consolidation` | medium | probe defect |
| `redundancy` | `keelbridge_programme_scope` | core | probe defect |
| `redundancy` | `keelbridge_programme_scope` | medium | probe defect |
| `redundancy` | `mira_castellane_role` | core | matcher defect |
| `redundancy` | `mira_castellane_role` | medium | matcher defect |
| `aggregation` | `aggregation_delivery_roles` | core | model failure, paraphrase |
| `aggregation` | `aggregation_inhouse_tools` | core | model failure, paraphrase |
| `aggregation` | `aggregation_inhouse_tools` | medium | model failure, paraphrase |
| `negative_knowledge` | `vendor_migration_rerun_lesson` | core | model failure, paraphrase |
| `negative_knowledge` | `vendor_migration_rerun_lesson` | medium | model failure, paraphrase |
| `unprompted_push` | `relay_sync_fix` | core | model failure, figure omitted |
| `unprompted_push` | `relay_sync_fix` | medium | model failure, figure omitted |
| `unprompted_push` | `invoiceref_rename` | core | model failure, paraphrase |
| `unprompted_push` | `invoiceref_rename` | medium | model failure, paraphrase |
| `unprompted_push` | `rate_limit_removal` | core | model failure, paraphrase |
| `unprompted_push` | `rate_limit_removal` | medium | model failure, paraphrase |
| `unprompted_push` | `cache_layer_merge` | core | model failure, paraphrase |
| `unprompted_push` | `cache_layer_merge` | medium | model failure, paraphrase |
| `unprompted_push` | `connection_pool_widen` | core | model failure, paraphrase |
| `unprompted_push` | `connection_pool_widen` | medium | model failure, paraphrase |
| `unprompted_push` | `export_flag_deletion` | core | model failure, paraphrase |
| `unprompted_push` | `export_flag_deletion` | medium | model failure, paraphrase |
| `unprompted_push` | `webhook_backoff_lower` | core | model failure, paraphrase |
| `unprompted_push` | `webhook_backoff_lower` | medium | model failure, paraphrase |
| `unprompted_push` | `tls_library_bump` | medium | model failure, paraphrase |
| `unprompted_push` | `migration_spot_check_cut` | medium | model failure, paraphrase |

Totals: 7 probe defect, 2 matcher defect, 19 paraphrase, 2 figure omitted.

What the reading in athenaeum#2805 got right and what it did not:

- The four probes it named as asking for one fact and grading another
  (`keelbridge_programme_scope`, `bramfield_retainer_renewal`,
  `triform_vendor_consolidation`, `confidentiality_rule`) are confirmed, and
  are the whole probe-defect bucket. Each marker was a real fact on the right
  page, but not the fact the question asks about (a budget line for "what does
  it cover", a pending request for "what is the renewal timeline", an
  escalation contact for "can we share", a later clause for "why").
- `mira_castellane_role` is confirmed as a matcher defect: the answer reads
  "since **March 2019**" and the marker is "since March 2019".
- `vendor_migration_rerun_lesson` is a paraphrase, as it said, but it is one of
  19, not a special case. The answer says the script "did not save progress
  checkpoints" against the marker "did not checkpoint progress". The same shape
  accounts for `aggregation` (two role/function descriptions reworded),
  `unprompted_push` (the constraint is explained correctly in different words,
  for example "silently drops entire records" against the marker "drops the
  whole record silently") and one `negative_knowledge` probe. In 14 of the 16
  `unprompted_push` cells the answer gives the planted reason in other words; the
  miss is wording only. `relay_sync_fix` is the exception
  (the answer gives the reason but not the "couple of seconds" figure the
  marker plants), and one `connection_pool_widen` answer opens "I do not know
  the specific steps" but then states the constraint.

## What changed, and the offline re-grade

Two changes that do not touch the page corpus:

1. `_normalize_marker_for_match` (tests/evals/corpus.py) now strips markdown
   emphasis delimiters (`*`, and `_` at a word boundary) before lowercasing.
   It is delimiter removal only. A paraphrase still misses, and the test pins
   that with counter-examples.
2. Four probe markers in `tests/evals/data/corpus/probes/probes.yaml` now name
   the fact the question asks for: `confidentiality_rule` ->
   "never discloses one client's engagement details",
   `keelbridge_programme_scope` -> "quayside handheld app",
   `bramfield_retainer_renewal` -> "eighteen-month",
   `triform_vendor_consolidation` -> "bundled discount".

**The corpus digest does not move.** `Corpus.fingerprint()` digests pages
only, and neither change touches a page, so `corpus_digest[core]` and
`corpus_digest[medium]` stay `3c5ea876688b089e` and `42e6ce0dfdc53e02`. What
does move is the grading rule, which the digest cannot show, so
`GRADER_REVISION` goes from `athenaeum#1935` to `athenaeum#2918`. A report
stamped `athenaeum#2918` and one stamped `athenaeum#1935` over the same rows
are not comparable, and run 44438673846's own render stays stamped `athenaeum#1935`.

Re-grade of the stored oracle rows with both changes, per class (correct over
graded, core then medium; before in parentheses):

| probe_class | enrolled in §7 | oracle core | oracle medium |
| --- | --- | --- | --- |
| `abstention` | yes | 3/3 (3/3) | 3/3 (3/3) |
| `disambiguation` | yes | 4/4 (4/4) | 4/4 (4/4) |
| `distractor_robustness` | yes | 2/2 (2/2) | 2/2 (2/2) |
| `follow_through` | yes | 6/6 (6/6) | 6/6 (6/6) |
| `multi_hop` | yes | 3/3 (3/3) | 3/3 (3/3) |
| `redundancy` | yes | 4/4 (2/4) | 4/4 (2/4) |
| `single_hop` | yes | 8/8 (5/8) | 8/8 (6/8) |
| `temporal` | yes | 6/6 (6/6) | 6/6 (6/6) |
| `aggregation` | report-only | 1/3 (1/3) | 2/3 (2/3) |
| `contradiction` | report-only | 3/3 (3/3) | 3/3 (3/3) |
| `negative_knowledge` | report-only | 2/3 (2/3) | 2/3 (2/3) |
| `unprompted_push` | report-only | 5/12 (5/12) | 3/12 (3/12) |

All eight enrolled classes now read 1.000 at oracle at both scales, on the
stored answers. The 21 cells still failing are all in report-only classes and
are all model failures (19 paraphrase, 2 figure omitted). Two cautions on
reading this:

- It is a re-grade of the stored answers against changed markers, not a new
  measurement. A model answering a re-run may still miss a marker (the
  `aggregation_delivery_roles` cell failed at core and passed at medium on
  the same page and question). Only a paid run shows whether the control holds.
- The four repaired markers are shorter and more generic than the ones they
  replace. `weak_probes` (the `none` arm already answering correctly) is the
  guard against a marker a floor answer can hit, and it needs the next run to
  speak.

## What this does not decide

- **Paraphrase.** Accepting a close paraphrase would change what every arm is
  graded on, so this change does not do it; it needs the operator's ruling.
  The 19 paraphrase cells are the size of that question, and they sit entirely
  in the report-only classes, so they do not gate the §7 decision block.
- **The paid re-run and the cutoff ruling** stay with athenaeum#2805.

## The 2026-09-19 oracle rows, re-counted

Counted from the stored render
[`north-star-grid-2026-09-19-run-35443944736.md`](north-star-grid-2026-09-19-run-35443944736.md)
(Correctness table, `oracle` rows; rate times n, correct over graded). That
report records no control check; this is the first one. It is the grader of
that day (`athenaeum#1843`), not re-graded here, because the per-cell answers
are not in-tree.

| probe_class | enrolled in §7 | oracle core | oracle medium |
| --- | --- | --- | --- |
| `abstention` | yes | 3/3 | 3/3 |
| `disambiguation` | yes | 4/4 | 3/4 |
| `distractor_robustness` | yes | 1/2 | 1/2 |
| `follow_through` | yes | 6/6 | 6/6 |
| `multi_hop` | yes | 3/3 | 2/3 |
| `redundancy` | yes | 0/4 | 2/4 |
| `single_hop` | yes | 5/8 | 6/8 |
| `temporal` | yes | 6/6 | 6/6 |
| `aggregation` | report-only | 1/3 | 0/3 |
| `contradiction` | report-only | 3/3 | 3/3 |
| `negative_knowledge` | report-only | 3/3 | 2/3 |
| `unprompted_push` | report-only | 2/3 | 2/3 |

Five of the eight enrolled classes (`single_hop`, `redundancy`,
`distractor_robustness`, `disambiguation` at medium, `multi_hop` at medium)
were below 1.000 at oracle, so under the rule now enforced by the report
renderer the September decision block would have printed as void. The table
agrees with the one in `native-memory-baseline-2026-11-19.md` for the five
classes that report listed.

## The renderer gate

`render_decision_block` in `tests/evals/north_star_report.py` now prints the
decision block as void when any enrolled class is below
`ORACLE_CONTROL_FLOOR` (1.0, read strictly, as the 2026-09-18 report applied
it) at oracle at any scale: a `VOID: oracle control failed` heading, a table of
the failing class/scale groups, and no conditions, cutoff scale or verdict
table. Run against the stored rows of run 44438673846 under the markers as
they stood at the run (matcher fix applied), it prints four enrolled
class/scale shortfalls (`single_hop` and `redundancy`, at both scales) in
place of the cutoff table that read `medium: pass`.
