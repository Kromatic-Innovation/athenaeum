<!-- SPDX-License-Identifier: Apache-2.0 -->

# The shipped push on the native-memory grid, 2026-11-21: native memory on against off, the relationship reading and the abstention cost

Issues athenaeum#3073, athenaeum#3068 (grid half) and athenaeum#3036. One run, three readings. The
shipped per-turn push of 0.29.0 (the top-hit excerpt and recall instruction of
athenaeum#3032, the linked notes of every hit of athenaeum#3070) is measured through the real hook
with no harness shaping. The earlier scale report
([`native-vs-push-at-scale-2026-11-21.md`](native-vs-push-at-scale-2026-11-21.md)) simulated
those features as arms PEI, PLA and PLAEI.

**Results in three lines.**

1. **Native memory on hurts, by the pre-fixed cost clause, and by nothing else.** Correctness is the
   same within noise (never more than 2 of 36 apart in net strict answers), but the run costs 30 to
   70% more per correct answer at both sizes and on both models, because the agent never opens a native
   memory file (0 of 330 runs) while the index still rides along on every turn.
2. **The relationship win holds on the grid's strict rule and does not show on the content rule.**
   `multi_hop` strict is 5/9 to 8/9 for the push arms against 0/9 for native, but native answers the
   same facts in 7/9 to 8/9 runs without opening the page.
3. **Abstention cost is under the 2.0x limit at both scales on this harness.** The unmatched-names line
   appeared in 17 of 36 push abstention runs and the agent made no further tool call after it in any of
   them. This is a different harness and model from the grid that produced the 6.8x and 2.5x readings
   (section 6), so it is a reading beside that run, not a replacement for it.

## 1. What was run

| field | value |
| --- | --- |
| harness | `tests/evals/native_baseline_cli.py` at commit `951c37e4` of this branch (arms `SOFF`, `SON` added; `N` unchanged) |
| product code | develop at `cacc57c3`, 0.29.0 (excerpt, recall line, linked notes all shipped) |
| session | real `claude -p`, a fresh empty working directory per session, isolated logged-in config |
| models | `claude-sonnet-5-5` (all sizes), `claude-opus-5-5` (3,000 pages only), resolved model read from each session's init event |
| search backend | `fts5` (the harness's), not the `vector` backend of the north-star grid |
| hook extractor | the model extractor, confirmed from the hook's own spend ledger on every push row |
| grader | `GRADER_REVISION` `athenaeum#3029`, strict `grade_correctness` plus the harness's content-correct check |
| corpus digests | `core` (192 pages) `3c5ea876688b089e`; `medium` (1,000) `42e6ce0dfdc53e02`; `medium3k` (3,000) `0d6a5e89f32ce4cb` |
| probes | the 24-probe Stage 2 set of athenaeum#3061 (12 `unprompted_push` plus the seeded 12 task-shaped); the full 57-probe set at `core` and `medium`; the 3 abstention and 3 `multi_hop` probes at 3 replicates |
| harness failures | 0 of 942 runs |

Arms:

| arm | what the agent gets |
| --- | --- |
| `N` | native memory only: the `MEMORY.md` index (shuffled, seeded, and cut by Claude Code to 200 lines at 1,000 and 3,000 pages) plus a topic file per page; no athenaeum |
| `SOFF` (SHIP-OFF) | the real hook's push, unshaped, and the "Per-turn push" section of the shipped `examples/claude-code/CLAUDE.md.example` as the working directory's `CLAUDE.md`; native auto memory disabled; `recall` available |
| `SON` (SHIP-ON) | `SOFF` with native auto memory on and the index and topic files rendered, as for `N` |

Each `SON` session loaded the index (200 lines and Claude Code's own truncation warning at 1,000 and
3,000 pages; 192 lines at `core`), and no `SOFF` session loaded any auto memory (both read from the
session's own memory attachment and init event; a `SOFF` session that loaded memory would have been a
harness failure).

"Run ids" are the harness output files, which are not committed: `s-1k` and `s-3k` (Sonnet Stage 2, 216
runs each), `o-3k` (Opus Stage 2, 96), `f-core` and `f-1k` (Sonnet, full probe set, 171 each), `x-core`
and `x-1k` (abstention and `multi_hop` extra replicates, 36 each).

## 2. Stage 2 results, Sonnet

Strict-correct (the answer page was delivered: named in the push, shown by `recall`, or opened), with
content-correct (the planted fact is in the answer) beside it. 36 runs per cell. *Touched memory* is any
tool call on the memory directory. *Recall* is the share of runs that called `recall`.

### `unprompted_push`

| size | arm | strict | content | cost per run | cost per correct | touched memory | recall |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | N | 2/36 = 0.056 | 9/36 = 0.250 | $0.109 | $1.961 | 23/36 | 10/36 |
| 1,000 | SOFF | 12/36 = 0.333 | 12/36 = 0.333 | $0.082 | $0.247 | n/a | 36/36 |
| 1,000 | SON | 11/36 = 0.306 | 11/36 = 0.306 | $0.127 | $0.414 | 0/36 | 36/36 |
| 3,000 | N | 0/36 = 0.000 | 0/36 = 0.000 | $0.093 | undefined | 6/36 | 2/36 |
| 3,000 | SOFF | 12/36 = 0.333 | 12/36 = 0.333 | $0.083 | $0.249 | n/a | 36/36 |
| 3,000 | SON | 14/36 = 0.389 | 14/36 = 0.389 | $0.129 | $0.333 | 0/36 | 36/36 |

### Task-shaped

| size | arm | strict | content | cost per run | cost per correct | touched memory | recall |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | N | 0/36 = 0.000 | 32/36 = 0.889 | $0.111 | undefined | 36/36 | 0/36 |
| 1,000 | SOFF | 29/36 = 0.806 | 32/36 = 0.889 | $0.076 | $0.094 | n/a | 36/36 |
| 1,000 | SON | 30/36 = 0.833 | 33/36 = 0.917 | $0.119 | $0.143 | 0/36 | 36/36 |
| 3,000 | N | 6/36 = 0.167 | 32/36 = 0.889 | $0.117 | $0.704 | 36/36 | 7/36 |
| 3,000 | SOFF | 27/36 = 0.750 | 28/36 = 0.778 | $0.075 | $0.100 | n/a | 36/36 |
| 3,000 | SON | 29/36 = 0.806 | 32/36 = 0.889 | $0.122 | $0.151 | 0/36 | 36/36 |

The shipped push (`SOFF`) reproduces the scale report's finding on the shipped code: 0.33 strict on
`unprompted_push` at both sizes against 0.06 and 0.00 for native memory alone, at a lower cost per run.
The native read rate on these tasks matches the scale report (67% and 11% touched at 1,000 and 3,000
pages on `unprompted_push` in that report; 64% and 17% here).

### Opus spot-check, 3,000 pages

24 runs per cell (two replicates). `N` was not re-run on Opus.

| subset | arm | strict | content | cost per run | cost per correct | touched memory | recall |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `unprompted_push` | SOFF | 6/24 = 0.250 | 6/24 = 0.250 | $0.180 | $0.719 | n/a | 24/24 |
| `unprompted_push` | SON | 6/24 = 0.250 | 6/24 = 0.250 | $0.275 | $1.100 | 0/24 | 24/24 |
| task-shaped | SOFF | 14/24 = 0.583 | 16/24 = 0.667 | $0.167 | $0.286 | n/a | 24/24 |
| task-shaped | SON | 15/24 = 0.625 | 17/24 = 0.708 | $0.256 | $0.409 | 0/24 | 24/24 |

## 3. athenaeum#3073: native memory on, the verdict

The rule, fixed in the issue before the run: native memory on **hurts** if it is 3 or more of 36
unprompted answers worse, or costs 20% or more per correct answer, at either size. It **helps** if it is 3
or more better at the same or lower cost. Otherwise no difference. Operational reading used here: "worse"
and "better" are the strict-correct counts out of 36 (content-correct is reported beside them and never
disagrees by more than one answer); cost per correct is total measured `claude -p` USD over strict-correct
answers; "same or lower cost" is cost per correct.

| size | model | strict ON against OFF, unprompted | per-probe better / worse | cost per correct, ON against OFF, unprompted | cost per correct, task-shaped |
| --- | --- | --- | --- | --- | --- |
| 1,000 | Sonnet | 11 against 12 | 0 / 1 | $0.414 against $0.247 (**+68%**) | +51% |
| 3,000 | Sonnet | 14 against 12 | 4 / 2 | $0.333 against $0.249 (**+34%**) | +51% |
| 3,000 | Opus (24 runs) | 6 against 6 | 1 / 1 | $1.100 against $0.719 (**+53%**) | +43% |

**Verdict: native memory on hurts**, through the cost clause at both Sonnet sizes and on Opus. It is not
a correctness harm: the rule's first clause (3 or more of 36 worse) is not met at either size, and at
3,000 pages ON is nominally better by 2 answers (14 against 12, inside the noise of a 36-run cell). The
`helps` clause fails on cost whatever the correctness difference.

What the cost is. A `SON` run costs about $0.045 more than a `SOFF` run on Sonnet ($0.127 against $0.082)
and about $0.095 more on Opus, at 1,000 and 3,000 pages and also at `core` (full-set run at `core`: $1.33 against $0.90
over 12 `unprompted_push` runs, +48%), where the index is the full 192 lines. The agent never used what it paid for: the
memory directory was touched in 0 of 330 `SON` runs (0 of 282 on Sonnet and 0 of 48 on Opus, at every size including `core`). The index being loaded into every session is measured; that its
tokens are the whole cost difference is an inference (the per-run cost difference is the only thing
metered here), not a separate measurement.

**What is not tested.** A real install's native memory holds the agent's own notes, not a rendered copy
of the athenaeum store, and an index of unrelated content may cost less or more to carry. Native memory
also writes: this grid reads and never lets a session add to it.

**Consequence (athenaeum#3073 AC4).** The verdict triggers the build issue for a plugin default that
turns native auto-memory off. What that default would lose is the native-memory write path, which is also
a raw-intake source: with `CLAUDE_CODE_DISABLE_AUTO_MEMORY` set, new auto-memory topic files stop
accruing for the librarian to file, and any already written stay readable. Whether that intake is worth
about 1.3x to 1.7x the per-answer cost is the operator's call; this report supplies the cost and the
absence of any read benefit, not the intake's value. The build issue is filed from this PR.

## 4. athenaeum#3068, the grid half: does the push keep the relationship win

`docs/use-cases.md` section 1 sends a retrieval change to the grid and the grid's condition 1 is that the
verdict arm beats the best native arm on the relationship use case (`multi_hop`: three probes) under the
strictly-greater rule. Here, three replicates of the three `multi_hop` probes per cell (9 runs), at
`core` and at `medium` (1,000 pages), Sonnet:

| scale | arm | strict | content | cost per correct |
| --- | --- | ---: | ---: | ---: |
| core | N | 0/9 | 8/9 | undefined |
| core | SOFF | 6/9 | 6/9 | $0.125 |
| core | SON | 5/9 | 7/9 | $0.197 |
| medium | N | 0/9 | 7/9 | undefined |
| medium | SOFF | 5/9 | 6/9 | $0.154 |
| medium | SON | 8/9 | 8/9 | $0.142 |

**Reading.** On the grid's strict rule the push keeps the win at `medium`: 5/9 (`SOFF`) and 8/9 (`SON`)
against 0/9 for native. That is lower than the 9/9 of run 44783114473 (a different model, harness and
backend, see section 6). Two cautions the number does not carry on its own:

- Native's strict score is zero because the rule requires the answer page to be delivered, and native
  sessions search the memory directory instead of opening a topic file. This is the same reason the scale
  report's strict column understates native. On the content rule, native is **not** beaten: 8/9 and 7/9
  against `SOFF` 6/9 and 6/9 and `SON` 7/9 and 8/9. So the relationship win is a delivery win, not an
  accuracy win, on this harness.
- Nine runs per cell. A difference of one or two answers is noise.

The real-session half of athenaeum#3068 (recall-through, use rate, pushed pages and push tokens per turn
on windows recorded after the excerpt shipped) is **not** covered by this report. It reads host
transcripts and the push ledger and needs real sessions to accrue.

## 5. athenaeum#3036, abstention cost

Per-row counts for the three abstention probes (`abstain_unknown_client`, `abstain_unknown_person`,
`abstain_unknown_policy`), three replicates each, Sonnet, tabulated as
[`abstention-cost-2026-11-21.md`](abstention-cost-2026-11-21.md) section 2 does (turns, tool calls, call
sequence). *Calls* include the `ToolSearch` that loads the deferred `recall` tool.

| scale | arm | runs | correct | mean turns | mean calls | cost per run | cost per correct |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| core | N | 9 | 9 | 2.00 | 1.00 | $0.079 | $0.079 |
| core | SOFF | 9 | 7 | 3.67 | 3.00 | $0.063 | $0.081 |
| core | SON | 9 | 7 | 3.44 | 2.44 | $0.100 | $0.128 |
| medium | N | 9 | 7 | 3.22 | 2.22 | $0.095 | $0.122 |
| medium | SOFF | 9 | 8 | 3.22 | 2.67 | $0.065 | $0.073 |
| medium | SON | 9 | 8 | 3.00 | 2.44 | $0.110 | $0.124 |

Sequences. Every `SOFF` and `SON` row is `ToolSearch`, then one to three `recall` calls. Native rows are one
or two `Bash` greps. No row has the four-`recall` narrowing loop (`full phrase`, `name`, `surname`,
`given name`) that the earlier run recorded; the longest is three `recall` calls.

**Cost against the cheaper defined native arm (`N`), cost per correct:**

| scale | `SOFF` against `N` | `SON` against `N` | limit |
| --- | ---: | ---: | ---: |
| `core` | 1.02x | 1.62x | 2.0x |
| `medium` | 0.60x | 1.02x | 2.0x |

Both are at or under 2.0x at both scales, whichever of the two arms is taken as the verdict arm.
`SOFF` is the push as the issue's wording defines it; `SON` is what a real install, with native memory on,
runs.

**The unmatched-names line (athenaeum#3033).** It appeared in 17 of the 36 push abstention runs (`SOFF`:
4 of 9 at `core`, 4 of 9 at `medium`; `SON`: 3 of 9 and 6 of 9). By probe, it appeared on the client probe
in 12 of 12 push runs and on the person probe in 5 of 12, and on the policy probe in 0 of 12, as the issue
said it would not: a topic-shaped probe has no capitalised name for the signal to catch. After it, **the
agent made no further tool call in any of the 17 runs** (calls after the line: 0 in each). Where it did
not appear, the run ended in two to three calls anyway, so on this harness the line is consistent with the
stop but is not the only thing keeping the loop short (the `N` and `SOFF` rows differ by one grep against
two or three calls, not by a runaway loop).

**Correctness on every non-abstention class, `SOFF` beside the athenaeum#3029 regrade.** One replicate of
the full probe set per cell (3 to 12 runs per class), strict. The regrade's verdict arm scored 1.000 on
every compared class except `single_hop` at `medium` (0.875) and `abstention` at `medium` (0.500).

| class | `core` `N` | `core` `SOFF` | `core` `SON` | `medium` `N` | `medium` `SOFF` | `medium` `SON` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| aggregation | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 |
| contradiction | 1/3 | 3/3 | 3/3 | 1/3 | 3/3 | 3/3 |
| disambiguation | 1/4 | 4/4 | 4/4 | 0/4 | 4/4 | 4/4 |
| distractor_robustness | 0/2 | 1/2 | 1/2 | 0/2 | 1/2 | 1/2 |
| follow_through | 1/6 | 4/6 | 6/6 | 0/6 | 5/6 | 5/6 |
| multi_hop (first replicate) | 0/3 | 2/3 | 2/3 | 0/3 | 2/3 | 3/3 |
| negative_knowledge | 0/3 | 2/3 | 2/3 | 0/3 | 2/3 | 2/3 |
| redundancy | 0/4 | 3/4 | 3/4 | 0/4 | 3/4 | 4/4 |
| single_hop | 4/8 | 5/8 | 5/8 | 2/8 | 5/8 | 6/8 |
| temporal | 1/6 | 5/6 | 5/6 | 0/6 | 5/6 | 5/6 |

Classes where `SOFF` is **lower** than the regrade's 1.000: `distractor_robustness` (1/2 at both scales),
`follow_through` (4/6 at `core`, 5/6 at `medium`), `multi_hop` (2/3 on the first replicate; 6/9 and 5/9
over three), `redundancy` (3/4 at both), `single_hop` (5/8 at both, against 1.000 at `core` and 0.875 at
`medium`) and `temporal` (5/6 at both). `aggregation`, `contradiction` and `negative_knowledge` are not
reported by the regrade. `disambiguation` is at 4/4. Against native memory in this harness `SOFF` is
higher on every class but `aggregation` (tied at 0) and `abstention`. The comparison to the regrade is
loose: a different grader path (strict rule on these sessions' own pages, not the oracle-tolerance
rule), model, backend and 2 to 8 rows per class.

## 6. How this differs from run 44783114473 (not pooled)

The athenaeum#3036 criteria are written for the north-star grid: API mode, Haiku, the `vector` backend,
the arms `native_grep` and `native_index`. This is the `claude -p` harness of athenaeum#3061 and
athenaeum#3066: Sonnet (Opus for the spot-check), the `fts5` backend, a single native arm `N` in which
the agent greps the memory directory with `Bash`, and cost as measured USD per correct answer rather than
read tokens. It is not pooled with run 44783114473 and does not replace it. Condition 3's 2.0x limit is
applied here to cost per correct against `N`. The earlier run's 6.785x and 2.500x came from four-`recall`
loops over full result pages, which this harness did not reproduce (the longest `recall` sequence here is
three). A reader who needs the north-star grid's own reading still has to run that grid.

## 7. Limits

- One invented corpus whose answer is one `related` edge from a page the push finds; real stores have
  weaker links.
- 36 runs per Sonnet cell, 24 per Opus cell, 9 per abstention and `multi_hop` cell, one replicate per
  class in the full-set tables. Differences of one or two answers are noise.
- `SON` renders the athenaeum corpus as native memory. A real install's native memory is the agent's own
  notes, and sessions here cannot write to it.
- Linked notes need the store at the hook's default location. The shipped adapter looks for linked pages
  in `~/knowledge/wiki` and does not read the `KNOWLEDGE_ROOT` the rest of the hook honours, so the eval
  points a throwaway home's `knowledge` at the corpus; an install whose store is elsewhere gets bullets
  and an excerpt but no linked-notes block.
- The hook's "recall index has not finished building" notice is stripped from the push, as in earlier
  reports, because an install with a finished index does not print it.
- The recorded spend excludes the in-flight sessions of one aborted first launch of the Sonnet grids
  (a harness bug in the new row-shape recorder crashed the run after the first 8 to 13 rows of each; the
  fix was committed and the launch resumed from the recorded rows).

## 8. Spend

Measured `claude -p` spend, billed to the plan: **$105.31** over 942 runs (Sonnet 1,000 pages $22.44,
Sonnet 3,000 pages $22.28, Opus 3,000 pages $21.06, full set at `core` $15.41, full set at `medium`
$17.37, extra abstention and `multi_hop` replicates $3.17 and $3.58). The hook extractor's model calls
bill an API key and came to about $0.16 in total (from the hook's own ledger).

## 9. Reproducing

Needs a logged-in `claude` under an isolated `CLAUDE_CONFIG_DIR`; for the model extractor,
`ATHENAEUM_EVAL_HOOK_KEY_FILE` names a `config.env`-style file (read into the hook's environment at spawn
time only).

```sh
CLAUDE_CONFIG_DIR=/path/to/isolated-claude-config \
ATHENAEUM_EVAL_HOOK_KEY_FILE=/path/to/config.env \
  python -m tests.evals.native_baseline_cli \
  --models sonnet --arms N,SOFF,SON --probe-set stage2 \
  --corpus-scale medium3k --replicates 3 --out stage2-3k.jsonl
```

Repeat with `--corpus-scale medium` for 1,000 pages; `--models opus --arms SOFF,SON --replicates 2` for
the spot-check; `--probe-set core --replicates 1` at `core` and `medium` for the full set; and
`--probes abstain_unknown_client,abstain_unknown_policy,abstain_unknown_person,spend_approver_named,portal_design_reviewer,ratecard_tooling_owner --replicates 2`
for the extra replicates. Use one `--out` file per scale: the resume key does not include the scale.
Per-run rows are not committed.
