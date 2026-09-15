# Comparator `merge`-class scope classification (athenaeum#1664)

- generated: 2026-09-15 (analysis lane, zero LLM calls, no code touched — this issue
  classifies, it does not fix)
- base: `develop` at `a010d301986f`
- inputs: `measurements/shadow-parity-2026-09-15.md` (rows `:53`, `:60`, `:226`, `:233`,
  sections `:122`, `:130` — unedited by this lane),
  `tests/evals/data/{detector,resolver}/cases.subject-scope.yaml` (read-only, unedited),
  `scripts/annotate_shadow_parity_subject_scope.py`, `src/athenaeum/comparator.py`,
  `src/athenaeum/dimensions.py`, athenaeum#1664, athenaeum#1508, athenaeum#1483, athenaeum#1256

## Verdict: comparator defect

Not a bug in `_strict_containment` or `compare_hierarchy` — both are proven correct below.
The defect is architectural: `compare_pages`'s Gate 1 has no coordinate that can separate a
general-rule-plus-exception pair (`merge`) from two flatly conflicting facts that happen to
share a topic stem (`contradict`) when `claimed_scope` is derived from member names alone. Any
label-blind, name-only annotation rule that encodes containment at all encodes it on **both**
classes identically, because both classes use the same naming convention (a shared stem plus a
distinguishing suffix). The comparator needs a non-coordinate signal to make this
distinction — Gate 1 cannot, in principle, supply one from `claimed_scope` alone. **Follow-up
needed**, not filed by this issue: the fix is a comparator-behaviour change (e.g. a Gate 2
signal that distinguishes "exception to a general rule" from "contradicts a general rule"),
which athenaeum#1664's own Out-of-scope section reserves for a separate issue.

## Step 1 evidence — `compare_pages` unit test (zero LLM calls)

`tests/test_comparator_merge_class_scope.py`, run against a `MagicMock` client (no network, no
`ANTHROPIC_API_KEY`):

| # | `claimed_scope` (a / b) | content relation (stubbed) | verdict | separator | route | specific_side |
| --- | --- | --- | --- | --- | --- | --- |
| `docs-tool` pair (real `refinement_editor_general_and_csv` shape) | `docs-tool` / `docs-tool` | CONFLICTING | `contradiction` | `[]` | `None` | n/a |
| `docs-tool` pair, containing | `docs-tool` / `docs-tool/csv-exception` | CONFLICTING | `specialization` | `["scope"]` | n/a | `b` |
| `tickets` pair (real `propose_merge_ticketing_general_and_exception` shape) | `tickets` / `tickets` | CONFLICTING | `contradiction` | `[]` | `None` | n/a |
| `tickets` pair, containing | `tickets` / `tickets/ticketwell-exception` | CONFLICTING | `specialization` | `["scope"]` | n/a | `b` |

The equal-scope rows reach the fall-through return at `comparator.py:925` (`separator == []`,
`route is None` — never the OVERLAPS branch at `:896`, which always sets both). The
containing-scope rows reach `:904-916` (`separator == ["scope"]`, correct `specific_side`).
`tests/test_comparator_merge_class_scope.py::TestContainingClaimedScopeReachesSpecialization::test_reversed_member_order_flips_specific_side`
additionally confirms `specific_side` tracks the raw coordinates, not argument order — this
fails if `_strict_containment` or `compare_hierarchy` regresses. **`specialization` is reachable
once `scope` encodes containment.** The comparator mechanism itself is not broken.

## Step 2 evidence — probe annotation rule and the 18-case Gate-1 table

**The rule** (`tests/evals/data/probe_subject_scope_containment.py`, written down and committed
before `gate1_separator_relations` was run against its output — see that module's docstring for
the full statement and its own "Methodological note"): for each cluster whose two members
already share an annotated `claimed_scope` stem, the member whose `name` equals the stem
exactly (or, failing that, the member with the shorter `name`) keeps the bare stem; the other
member's `claimed_scope` becomes `<stem>/<its own distinguishing suffix>`. The one cluster with
no shared stem (`meeting_cadence_different_scenarios`) is left unchanged. This is the rule
athenaeum#1664 itself proposes, transcribed near-verbatim; it is a pure function of `name` /
`claimed_scope` strings, never `outcome_class`, so it cannot be tuned toward or away from any
verdict — same label-blindness test `scripts/annotate_shadow_parity_subject_scope.py`'s own
`shared_stem` rule already passes (athenaeum#1508 AC).

**The table** (`gate1_separator_relations(DEFAULT_REGISTRY, …)`, zero LLM calls, computed by
`tests/test_comparator_merge_class_scope.py::TestProbeGate1RelationTable`):

| source | case_id | outcome_class | member `name:` values | probe `claimed_scope` (a / b) | valid-time | scope | subject |
| --- | --- | --- | --- | --- | --- | --- | --- |
| detector | `standup_time` | contradict | standup-time, standup-time-updated | standup-time / standup-time/updated | equal | **contains** | equal |
| detector | `invoice_cadence_refinement` | pass | invoice-general, invoice-acme-exception | invoice / invoice/acme-exception | equal | contains | equal |
| detector | `deploy_target_sequential_snapshot` | pass | portal-deploy-march, portal-deploy-may | portal-deploy/march / portal-deploy | disjoint | contains | equal |
| detector | `office_address_undated` | escalate | office-address-kingsway-works, office-address-regent | office-address/kingsway-works / office-address | equal | contains | equal |
| detector | `expense_reimbursement_receipts` | contradict | expense-receipt-rule, expense-small-rule | expense/receipt-rule / expense | equal | **contains** | equal |
| detector | `client_owner_thornhollow_pass_1` | pass | owner-thornhollow, owner-thornhollow-alias | owner-thornhollow / owner-thornhollow/alias | equal | contains | equal |
| detector | `tool_choice_editor` | contradict | docs-tool-pagemoor, docs-tool-tallyfold | docs-tool / docs-tool/tallyfold | equal | **contains** | equal |
| detector | `meeting_cadence_different_scenarios` | pass | client-weekly-sync, internal-monthly | client-weekly-sync / internal-monthly (no shared stem — unchanged) | equal | disjoint | unknown |
| detector | `budget_approver_undated` | escalate | budget-approver-priya, budget-approver-amir | budget-approver/priya / budget-approver | equal | contains | equal |
| detector | `pto_policy_restatement` | pass | pto-days, pto-restatement | pto / pto/restatement | equal | contains | equal |
| resolver | `refinement_editor_general_and_csv` | **merge** | docs-tool-pagemoor, docs-tool-csv-exception | docs-tool / docs-tool/csv-exception | equal | contains | equal |
| resolver | `restatement_pto_days` | pass | pto-days, pto-restatement | pto / pto/restatement | equal | contains | equal |
| resolver | `decision_conflict_hosting_migration` | contradict | hosting-hostmoor, hosting-fly | hosting/hostmoor / hosting | equal | **contains** | equal |
| resolver | `undated_office_address` | escalate | office-address-kingsway-works, office-address-regent | office-address/kingsway-works / office-address | equal | contains | equal |
| resolver | `undated_budget_approver` | escalate | budget-approver-priya, budget-approver-amir | budget-approver/priya / budget-approver | equal | contains | equal |
| resolver | `sequential_snapshot_headcount` | pass | headcount-jan, headcount-jun | headcount / headcount/jun | equal | contains | equal |
| resolver | `precedence_user_over_unsourced_contact` | contradict | northwind-contact-inferred, northwind-contact-user | northwind-contact/inferred / northwind-contact | equal | **contains** | equal |
| resolver | `propose_merge_ticketing_general_and_exception` | **merge** | tickets-linear, tickets-ticketwell-exception | tickets / tickets/ticketwell-exception | equal | contains | equal |

**The critical check fails.** Both `merge` cases reach `scope: contains`, as intended — but so
do **all 5 of 5** `contradict` cases (bolded above): `standup_time`, `expense_reimbursement_receipts`,
`tool_choice_editor`, `decision_conflict_hosting_migration`, `precedence_user_over_unsourced_contact`.
The only case that does NOT reach `contains` is `meeting_cadence_different_scenarios`, which has
no shared name stem at all (unrelated topics, not a same-topic conflict) and was already
correctly `distinct` via Gate 1 before this probe rule was ever applied.

The reason is structural, not a rule-design mistake: `contradict` cases in this corpus
(two directly opposed facts about one topic, e.g. `hosting-hostmoor` vs `hosting-fly`,
`standup-time` vs `standup-time-updated`) use **the same general-stem-plus-suffix naming shape**
as `merge` cases (a general rule plus a named exception, e.g. `docs-tool-pagemoor` vs
`docs-tool-csv-exception`). A rule that reads containment off names alone cannot tell them
apart — the "shorter name / name-equals-stem" heuristic wins arbitrarily on both classes alike.
Per athenaeum#1664's own classification rule ("Comparator defect: … the rule cannot tell
contradict pairs from merge pairs, which means the comparator needs a non-coordinate signal"),
this is decisive for **comparator defect**, not fixture artifact — even though Step 1 proves the
`specialization` code path itself is reachable and correct in isolation.

## What this does NOT mean

- It does not mean `_strict_containment` or `compare_hierarchy` are buggy — Step 1 proves both
  work exactly as designed on a genuinely-containing pair.
- It does not mean the 2026-09-15 run's fixture is "wrong" in isolation — its `claimed_scope ==
  subject` shape (from `scripts/annotate_shadow_parity_subject_scope.py`) is unrelated to this
  finding; even a fixture with a "better" name-derived `claimed_scope` would hit the same wall.
- It does not mean a fix is included here. Per athenaeum#1664's Plan step 3 ("If it is a defect,
  file the fix as a follow-up; do not fix it here") and its Out-of-scope section ("Changing
  comparator behaviour… any fix is a follow-up"), no `src/athenaeum/*.py` file is touched by
  this issue. **A follow-up issue is needed**: the comparator's Gate 1 `scope` dimension, when
  populated from member names, cannot discriminate `merge` from `contradict`; a real fix likely
  needs either a genuinely content-derived `claimed_scope` (not name-derived) or a second Gate 2
  signal (e.g. classifying "exception to a general rule" vs "flat contradiction" alongside the
  existing `conflicting`/`equivalent`/`compatible` judgement).
