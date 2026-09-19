# Person-hint baseline — 2026-09-19

Issue athenaeum#1867. First measurement of whether a person page gains a claim
only when the raw file actually **asserts** something about that person, and
whether the rest of the file is still compiled.

- Golden set: `tests/evals/data/person_hint/` (invented; leakage denylist green
  against a real local knowledge tree, no skip).
- Grader: `tests/evals/person_hint.py`, unit-tested offline by
  `tests/evals/test_person_hint_grading.py` (39 tests, 95% line coverage of the
  grader).
- Layer: `tests/evals/test_person_hint_eval.py` (`pytest -m eval`).
- Librarian under test: `develop` @ `5956b795`.
- Registry: `PersonRegistry` built over each case's own wiki and passed as
  `person_registry=` to `librarian.process_one`. The `attachment` layer does
  not pass one, which is why tier 0 is invisible to it.

## How this was measured, and why offline is sound here

Every case in this layer is claimed by the shipped tier-0 person-registry
consult, which makes **zero model calls**. So unlike every other eval layer,
this one's shipped-path score can be taken with no API key:

```bash
ATHENAEUM_PERSON_HINT_OFFLINE_BASELINE=1 \
  pytest -m eval tests/evals/test_person_hint_eval.py -o addopts="" \
  --eval-summary=person-hint-baseline.json
```

That env var substitutes a client which **raises** on `messages.create`. A run
that completes is therefore positive evidence that no tier past 0 ran — the
"zero calls" reading is established by construction rather than counted after
the fact, which is a stronger reading than a live run gives, not a weaker one.
A run that reaches a tier errors loudly instead of passing quietly.

**This measurement expires with athenaeum#1866.** The day the routing change
lands, the cases that stop being claimed at tier 0 will call a model and this
offline run will error. That is the correct signal; the baseline must then be
re-taken live.

## Per-case result

| Case | Names | Expected | Observed | `decided_by` | Pass |
|---|---|---|---|---|---|
| **A** `passing_mention_retrospective` | Oakmoor, in passing | page `unchanged`; the programme page gains the source | **`notes_bullet`** on `ph-person-oakmoor`; **no pointer** to the raw ref on `ph-project-relining` | `deterministic` | **FAIL** |
| **B** `role_change_note` | Selmire, role change | `footnoted_claim` | **`notes_bullet`** on `ph-person-selmire` | `deterministic` | **FAIL** |
| **C** `memo_names_four_asserts_two` | four named, two asserted | Vantry + Oakmoor `footnoted_claim`; Selmire + Pelloway `unchanged` | **`notes_bullet` on all four**; no non-person page changed at all | `deterministic` | **FAIL** |
| **D** `same_name_different_person` | a different Ivo Pelloway | known page `unchanged`; the supplier page gains the source | **`notes_bullet`** on `ph-person-pelloway`; **no pointer** on `ph-company-carrowfen` | `deterministic` | **FAIL** |
| **E** `restates_known_fact` | Oakmoor, restated | `citation_only` or `footnoted_claim`, no duplicated sentence | **`notes_bullet`**, and the page now carries the same sentence **twice** | `deterministic` | **FAIL** |

**Layer score on the shipped librarian: 0 / 5.** Model calls: **0**. Pages
minted: **0**. Aggregate floor `PERSON_HINT_FLOOR = 4`, `xfail(strict=False)`
until athenaeum#1866.

## What the numbers say

**Every verdict is reached without a model call, and that is the finding.**
`decided_by=deterministic` on all five. There is no tier that asks whether a
file *asserts* anything about the person it names: `match_person_mentions` is a
word-boundary substring test over the registry's keys, and any hit is enough.
AC4's "which tier decided" therefore has the same answer for every case, and
the sameness is the result rather than a formality.

**C is the clearest reading.** A memo that names four people and makes an
assertion about two produces four identical Notes bullets. `matched=4` — the
fan-out cap (`PERSON_OBSERVATION_MAX_FANOUT = 5`) bounds this at five pages per
file but does nothing to make it selective. There is no signal anywhere in the
pipeline that distinguishes the two people the memo is about from the two it
merely lists.

**D shows the match is on the name, not on the person.** The file says in
plain terms that this Ivo Pelloway drives a supplier's flatbed and holds no
audit role — the exact opposite of the known page's `description:`. The literal
name hit wins anyway.

**A and D fail twice over, and the second failure is the larger one.** Tier 0
early-returns the moment it attributes an observation, so the raw file is
claimed *whole*: tiers 1–3 never see it. The retrospective that is about the
relining programme leaves the programme page untouched, and the delivery note
about the supplier leaves the supplier page untouched. Nothing else in the file
is compiled. This is why the grader requires a non-person pointer (AC3) rather
than grading person pages alone — a layer blind to it would report A as one
wrong bullet instead of one wrong bullet *plus a file that was never compiled*.

**E shows the damage is not only a wrong location.** The bullet pastes the raw
file's sentence onto a page that already carried it verbatim, so the page now
says the same thing twice. `duplicated_sentences` catches it.

**Nothing was minted and nothing vanished.** `minted=[]` and `removed_uids=[]`
on every case, so the AC4/`docs/north-star.md` §2.8 irreversibility invariant
holds throughout — this failure mode is additive noise on existing pages, not a
destructive one.

## Token spend

**Zero.** The measured spend of this layer on the shipped librarian is 0 input
and 0 output tokens, because tier 0 claims every case before any client is
called. `EVAL_TOKEN_CEILING` is therefore **not raised** in this PR: there is
no spend to accommodate, and raising a budget guard against a hypothetical
would defeat the guard.

When athenaeum#1866 lands, the cases that fall through to tiers 1–3 will spend
like an `attachment` case (one Tier-2 classify call plus one Tier-3 call per
action). Re-measure the ceiling then, against a real number, and raise it in
the same PR as the routing change if the full run exceeds it.
