# Person-hint strict floor — 2026-10-09

Issue athenaeum#2043. Second measurement of the `person_hint` eval layer
(`tests/evals/test_person_hint_eval.py`, floor `PERSON_HINT_FLOOR = 4` of 5),
taken because the floor was red on the `develop` baseline itself and blocking
the Eval Receipt gate for every PR touching an LLM surface. The first
measurement is `person-hint-baseline-2026-09-19.md` (0/5, offline, pre-
athenaeum#1866).

**The floor is not re-baselined.** It stays at 4/5. What changed is the
librarian, the hint classify prompt, and one fixture title — see "What
changed" — and the floor now clears on two consecutive live runs.

## Last green, first red

| | Run | `develop` sha | Floor result |
|---|---|---|---|
| Last "green" | [35467028369](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/35467028369), 2026-09-19 20:19Z | `4455199d` | **XFAIL** — the aspirational-floor marker from athenaeum#1867 still on |
| First red | [36010923246](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/36010923246), 2026-09-24 14:11Z | `2f8b8383` | **FAILED 2/5** — first strict run after athenaeum#1866 |
| Red | [36099358949](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/36099358949), 2026-09-25 | `853f1f9b` | FAILED 2/5 |
| Red (issue) | [37926419814](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/37926419814), 2026-10-09 | `9d6ee351` | FAILED 1/5 |
| Red, `record=true` | [37930589001](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/37930589001), 2026-10-09 | `e7331780` (branch head before any fix) | FAILED 2/5 |

**There was never a strict green run to regress from.** The `xfail` marker
came off in commit `150af132` ("un-xfail floor"), merged to `develop` as PR
athenaeum#1876 at `d49bb46a` on 2026-09-19 22:02Z, and that PR's body records that no
`evals.yml` dispatch was made ("Not done by this lane"). Every live
measurement of the strict floor, from the first one five days later to the
one this issue cites, scored 2/5 or 1/5.

## Root cause: neither judge drift nor a regression

- **Not judge drift.** The grader (`tests/evals/person_hint.py`) is
  structural — it classifies each person page's before/after delta and
  checks for a non-person page citing the raw ref. There is no LLM judge to
  drift. `DEFAULT_CLASSIFY_MODEL` (`claude-haiku-4-5-20251001`) and
  `DEFAULT_WRITE_MODEL` (`claude-sonnet-5`) are unchanged since 2026-09-19,
  and neither `prompts/person_hint_classify.md` nor
  `prompts/person_hint_verify.md` was touched between `4315621a` (their
  introduction) and this issue.
- **Not a regression in the classify/write path.** Nothing on the path moved
  the score: the per-case shape is identical across all four strict runs
  (A over-claims, B drops or goes uncited, C inverts, D passes, E passes or
  goes uncited), and the LLM-surface commits since `d49bb46a` are in
  unrelated subsystems (self-tuning rails, signal mining, convergence,
  decisions, PII, C4 retirement).
- **What it is:** the athenaeum#1866 implementation was asserted at 4/5
  without being measured, and measured 2/5. The `record=true` run above
  captured the raw responses, which name three causes.

## Per-case diagnosis (from the recorded responses, run 37930589001)

| Case | Expected | Observed | Cause |
|---|---|---|---|
| **A** `passing_mention_retrospective` | Oakmoor `unchanged`; programme page cited | Oakmoor `footnoted_claim` ("She sat in for the floor handover section ... and had nothing to add."); programme page cited | **Prompt gap.** The classifier emitted a `candidate_uid` item for a sat-in note. `person_hint_classify.md` said only "a passing mention ('talked to Alice')"; presence was not named as a non-claim. |
| **B** `role_change_note` | Selmire `footnoted_claim` | Varied by run: `dropped` (2 runs), `uncited_change` (1), pass (1) | **Anchor across a hard line wrap.** The Selmire page wraps "She owns the\nshutdown calendar". When the model's `replace` anchor kept the literal `\n` the merge applied; when it quoted the sentence as prose the anchor missed, and a hint-derived anchor miss is a silent drop (athenaeum#1866). Which happened decided the case. |
| **C** `memo_names_four_asserts_two` | Vantry + Oakmoor `footnoted_claim`; Selmire + Pelloway `unchanged`; a non-person pointer | Vantry `dropped` every run; Selmire + Pelloway `footnoted_claim` ("attended the relining programme review"); no non-person pointer | Three causes. (1) Vantry's claim is a `replace` of a sentence wrapped across two lines ("He has no signing authority of his own and routes every order through the works manager."); the recorded anchor has no newline and `str.find` missed it. (2) Same prompt gap as A: an attendee list was classified as a claim about everyone on it. (3) The memo named the programme only as "relining programme" — never "Draymouth Relining", the page's name — so tier-1 name matching could not reach the project page, and the classifier correctly emitted no new entity for it. The retrospective (case A) names it in its title and is reached. |
| **D** `same_name_different_person` | Pelloway `unchanged`; supplier page cited | Pelloway `unchanged` (`subject_mismatch`), supplier page edited but **uncited** on the recorded run (pass on the three other runs) | **Footnotes under the wrong key.** The recorded merge put its definition in a top-level `"footnotes": ["[^1]: sessions/..."]` list instead of an `append_section` op, so the page gained `[^1]` markers and no definition — no pointer to the ref. |
| **E** `restates_known_fact` | `citation_only` or `footnoted_claim`, no duplicated sentence | pass (3 runs), `uncited_change` (1) | Same shape as D, from the eval-summary detail; not recorded. |

## What changed

Two commits on `occam/2043`. The first (`575d581b`) covered the causes the
`record=true` run named; the second (`8cbe3166`) covered the two residuals
the runs after it exposed.

1. `tiers._locate_anchor` (new, used by `apply_merge_ops`): exact match
   first, unchanged; when the exact form occurs **zero** times, a
   whitespace-tolerant match (each whitespace run in the anchor matches any
   whitespace run in the body, including a line break), still required to be
   unique, returning the body's original span. An exact match's uniqueness
   check is never widened; an all-whitespace anchor is "not found".
   Pinned by `tests/test_2043_anchor_line_wrap.py`.
2. `tiers._coerce_merge_ops`: a top-level `"footnotes"` list of
   `[^label]: ...` definitions is folded into one trailing `append_section`,
   skipping definitions already present in an op.
3. `tiers.define_dangling_footnotes` (new, run after `apply_merge_ops` on
   both transports): every footnote marker the merge introduced but did not
   define is resolved to the merge's own `source_ref`. A marker that was
   already dangling before the merge is left alone. This is the deterministic
   answer to the dominant residual — three of the four live runs at
   `575d581b` read `uncited_change` on one or more person pages, and the
   recording at run 37933459953 shows why: Sonnet emitted the `[^1]`
   markers inside the ops and the `[^1]:` definition *outside* the JSON
   object (after the closing fence), or omitted it. Eight exact-byte pins in
   `tests/test_tiers.py` / `tests/test_prompt_safety.py` whose canned ops
   added `New.[^2]` with no definition now expect `[^2]: ref`.
4. `prompts/person_hint_classify.md`: a rule that being present is not a
   claim — an attendee list, a sign-off, "sat in", "had nothing to add" — and
   that in a file naming several people only the ones it says something
   about are emitted. This fixed Selmire and Pelloway in case C on every run
   after it, but Haiku still emitted "sat in for the floor handover" as a
   hint for Oakmoor in case A on three of four runs. So:
5. `prompts/person_hint_verify.md`: the write model is asked the same
   question against the full page; a `"presence_only": true` reply leaves
   the page byte-identical and is recorded as `not_asserted`. Scoped to
   hint-derived actions; an ordinary merge ignores the key. Goldens
   regenerated for both prompts.
6. `tests/evals/data/person_hint/raw/memo_names_four_asserts_two.md`: the
   title now reads "Memo — Draymouth Relining review: ...", naming the
   programme page the way the retrospective fixture already does. The case's
   `require_non_person_pointer` check is a claim about compiling the rest of
   the file, and it needs the file to be reachable by the shipped routing;
   a file that never names the page was testing name-matching recall, not
   person-hint selectivity.

Not changed: the floor, the grader, the other four fixtures, the models.

## Post-fix runs

| Run | Head | person_hint | Per-case |
|---|---|---|---|
| [37931770585](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/37931770585) | `575d581b` | 4/5 PASSED | C: Vantry `uncited_change` |
| [37932595871](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/37932595871) | `575d581b` | 2/5 FAILED | A: Oakmoor claimed; C: Vantry `uncited_change`; E: Oakmoor `uncited_change` |
| [37933459953](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/37933459953), `record=true` | `575d581b` | 1/5 FAILED | A claimed; B, C, E `uncited_change` — the recording that named cause 3 and motivated cause 5 |
| [37934843834](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/37934843834) | `8cbe3166` | **5/5 PASSED** | all cases ok; every other floor in the suite green on this run too |
| [37935669187](https://github.com/Kromatic-Innovation/athenaeum/actions/runs/37935669187) | `8cbe3166` | **5/5 PASSED** | all cases ok; whole workflow green |

The two runs at `8cbe3166` are the AC's "two consecutive runs".

Unrelated: `audit_retirement` scored 4/6 against its floor of 5 on runs
37931770585 and 37932595871 (`name_only_person_stub`, the pinned known miss,
plus `name_plus_one_affiliation_line`) and 5/6 on 37934843834. That layer's
prompt is not on this diff; it is the sampling noise athenaeum#1877 measured.
