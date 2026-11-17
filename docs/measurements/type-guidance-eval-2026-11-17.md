# Per-type guidance before/after on Tier-3 merges (2026-11-17)

Issue: athenaeum#2715 (child of athenaeum#2590, which shipped per-type guidance).
Non-blocking: it gates neither athenaeum#2590 nor 0.26.0.

**Question.** Does an operator's per-type guidance fragment change what the
Tier-3 merge does, and does the shipped precedence clause in
`src/athenaeum/prompts/type_guidance.md` earn its place?

**Short answer.** Guidance reliably turns "an observation about a different
person" into an `ESCALATE:` on the patch path (0/20 to 20/20) and only
partially on the full-echo path (0/20 to 44/80). Planted rule phrases were
retained at essentially ceiling in every arm; the precedence clause's
protective effect shows up only in full-echo mode against a fragment that
asks the model to tidy the rules, and the evidence for it is real but thin.
**Verdict: keep the clause** (details and caveats at the end).

All content is invented: one `preference` page ("Ingrid Valmar -- Working
Preferences") and invented observations about invented people. No deployment
content is used.

## Method

Harness: `tests/evals/type_guidance_eval.py` (library plus CLI). Model: the
WRITE model `tier3_merge` uses in production (`claude-sonnet-5`), default
settings, one sample per trial. Calls go through the real entry points
`tiers.tier3_merge` (patch mode, the anchored-ops primary path) and
`tiers.tier3_merge_full` (full-echo mode, called directly so the rewrite path
is measured on every trial, not only when patch mode happens to fall back).
Guidance is installed the production way: a `_schema/_type-guidance-preference.md`
file plus a declared `types.md`, resolved through `resolve_type_guidance` with
`target_type="preference"`.

Arms (the guidance variable):

| Arm | Guidance fragment | Precedence clause |
|---|---|---|
| `off` | none (byte-identical pre-athenaeum#2590 prompt) | n/a |
| `benign` | one bullet per rule; escalate observations about other people | yes (shipped) |
| `benign_noclause` | same | blanked |
| `adversarial` | "tidy and short": consolidate overlapping rules, reword into one imperative voice; plus the same escalate-other-people line | yes (shipped) |
| `adversarial_noclause` | same | blanked |

The no-clause arms patch `tiers.TYPE_GUIDANCE_PROMPT` to `""`, isolating what
the clause itself contributes.

**(a) Retention.** The page carries five planted rule phrases. Four invented
observations about the *same* person are merged sequentially, each merge's
output becoming the next merge's input: a new rule, a re-confirmation, a
one-off exception that sits in tension with an existing rule, and an
observation that overlaps an existing rule (the consolidation temptation).
After every merge, each phrase is checked as an **exact, case-sensitive
substring**. "Chain" = one run of the four merges.

**(b) ESCALATE rate.** Five invented observations about five *different*
people (a name the page does not mention) are routed to Ingrid's page, four
repetitions each (n=20 per mode and arm). Every one should escalate under the
benign guidance, so the escalate rate is also the recall of the intended
behaviour. Same-person observations in the retention chains double as a
false-escalation check.

Intervals are Wilson 95% intervals treating trials as independent. Phrase-level
counts within a chain are not independent (a chain that rewrites the page
loses several phrases at once), so chain-level counts are the honest unit for
retention; phrase counts are shown for completeness.

## Results

### (a) Verbatim retention of planted rule phrases

Final state after the fourth merge. "Phrases" = 5 per chain.

| Mode | Arm | Chains | Phrases kept (final) | Chains with all 5 intact |
|---|---|---|---|---|
| patch | off | 8 | 40/40 | 8/8 |
| patch | benign | 8 | 40/40 | 8/8 |
| patch | benign_noclause | 8 | 40/40 | 8/8 |
| patch | adversarial | 8 | 40/40 | 8/8 |
| patch | adversarial_noclause | 8 | 40/40 | 8/8 |
| full-echo | off | 8 | 40/40 | 8/8 |
| full-echo | benign | 8 | 40/40 | 8/8 |
| full-echo | benign_noclause | 8 | 40/40 | 8/8 |
| full-echo | adversarial | 14 | 70/70 | 14/14 |
| full-echo | adversarial_noclause | 14 | 68/70 | 12/14 |

Pooled across patch arms: 200/200 phrases, 40/40 chains (phrase-level Wilson
95% lower bound about 0.98). Full-echo with `off`/`benign*`: 120/120, 24/24
chains. Retention over *all* intermediate steps (not just the last) is 100%
everywhere except the two phrases in the one cell below (278/280 for that arm).

The only losses in the final-run data are the two phrases in
`full-echo / adversarial_noclause`, both the phrase "send the agenda at least
24 hours ahead", the rule the overlapping observation invites the model to
fold away. They happened in two of 14 chains.

Patch mode needed the full-echo fallback in 4 of 160 steps
(1 `adversarial`, 2 `benign`, 1 `benign_noclause`, 0 elsewhere), always on an
anchor that did not apply; none of those lost a phrase.

**Zero false escalations** on same-person observations: 0 of 304 guided steps
(128 patch, 176 full-echo).

### (b) ESCALATE rate for an observation about a different person

| Mode | Arm | Escalated | Rate (Wilson 95%) | Other person's content appeared in returned body |
|---|---|---|---|---|
| patch | off | 0/20 | 0% (0-16%) | 5/20 |
| patch | benign | 20/20 | 100% (84-100%) | 0/20 |
| patch | benign_noclause | 20/20 | 100% (84-100%) | 0/20 |
| patch | adversarial | 20/20 | 100% (84-100%) | 0/20 |
| patch | adversarial_noclause | 20/20 | 100% (84-100%) | 0/20 |
| full-echo | off | 0/20 | 0% (0-16%) | 0/20 |
| full-echo | benign | 10/20 | 50% (30-70%) | 0/20 |
| full-echo | benign_noclause | 10/20 | 50% (30-70%) | 1/20 |
| full-echo | adversarial | 11/20 | 55% (34-74%) | 0/20 |
| full-echo | adversarial_noclause | 13/20 | 65% (43-82%) | 0/20 |

("Content appeared" is a crude first-name substring check on the returned body;
it is a leak indicator, not a correctness grade.)

Without guidance, patch mode merged the other person's content into Ingrid's
page in 5 of 20 trials (four of them for one observation) and neither
escalated nor, in the other 15, wrote the name. With guidance it escalated
every time. Full-echo mode is different in kind: the un-guided arm never
escalated and never wrote the name, so it was not leaking, and guidance moves
only about half the trials (guided arms pooled 44/80 = 55%, Wilson 44-65%)
to `ESCALATE:`. Per-observation results in full-echo mode are bimodal and not
stable across arms (for example one observation escalated 4/4 in one arm and
0/4 in another with near-identical prompts), so with four trials per cell
nothing should be read into individual observations. The likely mechanism is
that full-echo's contract demands an `ADDS_NEW_CLAIM:` line first and a body
afterwards, and the model resolves "escalate or not" inconsistently; this
report did not test that explanation.

The clause does not suppress the escalate behaviour: with it, 20/20 (patch)
and 10/20 (full-echo) for the benign fragment; without it, 20/20 and 10/20.

## Side finding: an empty full-echo body

While re-running the full-echo adversarial arms, 7 of 68 chains hit an
engine edge: when the model writes prose after the sentinel on the first line
(`ADDS_NEW_CLAIM: false -- re-confirmation only`) and nothing else,
`parse_tier3_merge` strips the line, does not recognise the verdict (it only
accepts an exact `false`), and returns `("", None)`. Production callers treat a
falsy body as "no write" (`if updated_body:`), so no page is damaged; the
cost is that the re-confirming citation is silently dropped and no escalation
is raised. The first harness draft overwrote the page with `""` and so
over-reported loss; that was a harness bug, fixed (`test_empty_full_echo_body_does_not_wipe_the_chain_state`),
and every full-echo adversarial result above is from re-runs under the fix.
Counts are 5 of the 34 `adversarial` chains in all batches and 2 of the 34
`adversarial_noclause` chains, which is not enough to say the guidance drives
it (the other arms saw none in 8 chains each). This is outside the scope of
athenaeum#2715 and is reported here only so it is not lost.

## Verdict on the precedence clause: KEEP

Keep `src/athenaeum/prompts/type_guidance.md` as it is.

For:

- The only observed loss of a planted rule to consolidation happened without
  the clause. Across three batches of the full-echo adversarial arms, the
  clause arm lost a phrase in 0 of 29 chains; the no-clause arm lost one in
  6 of 32 (batches, no-clause: 3/8, 1/10, 2/14; clause: 0/8, 0/9, 0/12;
  two-sided Fisher exact p about 0.025). Chains whose body came back empty
  (the side finding above; 2 no-clause, 5 clause) are excluded from both
  arms. The pooled figure includes two batches run before the harness bug was
  found; the bug only affected chains with an empty body, which are the
  excluded ones. The fresh re-run on its own is 2/14 versus 0/12
  (p about 0.48): consistent with the effect, not evidence for it alone. The
  analysis was post hoc and the batches were not pre-planned as a pooled test.
- It costs nothing measurable: escalation behaviour is identical with and
  without it (above), and there were no false escalations on same-person
  observations.
- Patch mode already preserves rules without it, so the clause is not doing
  harm there either (40/40 in both adversarial arms).

Against / limits:

- The effect is confined to full-echo mode and one hostile fragment, with one
  planted overlap. A less pointed fragment (`benign*`) lost nothing with or
  without the clause (80/80 each), so the clause's value here is insurance
  against a badly written fragment, not a routine gain.
- The clause did not rescue the guided full-echo escalate rate (about half).
  If athenaeum#2590's intent is that guidance can reliably route
  other-person observations to a human, that is a full-echo-prompt issue to
  consider separately (for example whether the `ESCALATE:` instruction in
  guidance competes with the `ADDS_NEW_CLAIM:` sentinel); it is not an
  argument to change the clause.
- Small n (8-14 chains, 20 escalate trials per cell), one model, one invented
  page, one run per trial with default sampling. Differences of under about
  25 percentage points between cells are within noise.

## Reproduce

Live and metered; needs an Anthropic credential in the environment.

```
python -m tests.evals.type_guidance_eval --arms off,benign,benign_noclause,adversarial,adversarial_noclause \
    --modes patch,full --chain-reps 8 --escalate-reps 4 --out results.json
```

The paired guidance-on/off cases for the standard eval suite are in
`tests/evals/data/merge/cases.yaml` (`type_guidance_on_*` / `type_guidance_off_*`)
and run under `pytest -m eval tests/evals/test_merge_eval.py`. The off twins are
recorded as an informational baseline and never count toward the merge floor.
Offline harness checks run in the default suite
(`tests/evals/test_type_guidance_eval_stub.py`).

Spend for this report: about 770 live calls, roughly $8.8 estimated from
reported token usage at the repository's rate table for the WRITE model
($3 / $15 per million input / output tokens), including pilot and discarded
runs. Estimated, not read from a bill.
