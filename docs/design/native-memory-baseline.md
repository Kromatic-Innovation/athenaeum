<!-- SPDX-License-Identifier: Apache-2.0 -->

# Native-memory baseline — the eval that decides whether this project continues

**Status:** DESIGN RECORD, operator decision 2026-09-16. Specifies the
comparison named as the kill criterion in [`../use-cases.md`](../use-cases.md)
§1. Implementation is tracked on the issues linked at the end.

---

## 1. The question

Every host agent that can run Athenaeum already has a memory of its own.
For Claude Code that is **auto memory**: a per-project directory of markdown
topic files plus a `MEMORY.md` index, written by the model itself and loaded
at session start. It costs nothing to set up and nothing to compile.

If that native memory answers the [current use cases](../use-cases.md#2-current-use-cases)
as well as Athenaeum does, then intake, the librarian, provenance, the
decision queue and the sidecar are all cost with no recall value, and the
project should be shelved. Nothing else in the eval suite tests this. The
existing six-arm rollout runner (`tests/evals/rollout.py`) compares ways of
delivering *Athenaeum's* memory; every arm reads an Athenaeum-compiled
corpus. This record adds the arms that read a native store instead.

## 2. What the baseline is

The baseline is Claude Code auto memory as documented at
<https://code.claude.com/docs/en/memory>, verified against the live docs and
Claude Code 2.1.273 on 2026-09-16:

- One directory per project containing `MEMORY.md` and one topic file per
  memory.
- **The first 200 lines or 25KB of `MEMORY.md`, whichever comes first, are
  loaded at the start of every conversation. Nothing past that is loaded.**
- Topic files are never loaded at startup. The model reads them on demand
  with its ordinary file tools.
- The model decides what to write, when, and how to keep the index short.

Not the baseline: `CLAUDE.md` (operator-authored instructions, not memory),
the Anthropic memory tool (a primitive for people building their own agents),
and third-party memory libraries. A third-party system may be added later as
a further arm; see §8.

## 3. What can be established before running anything

The index cap decides the shape of the comparison. One index line per page,
at a realistic name-plus-description length, exhausts 200 lines at roughly
200 pages and 25KB at roughly 150 to 200 pages. A real deployment reaches
tens of thousands of pages. So:

- **Below the cap** both systems have an index and the comparison is about
  quality per token: does compilation plus ranked search beat a flat list
  the model scans itself?
- **Above the cap** native memory has no index for most of the corpus. The
  model must find pages by searching the directory with its file tools. The
  comparison becomes: does the librarian plus FTS5 and vector search beat
  the model grepping a directory of flat files?

The second question is the one that decides shelving, and it is genuinely
open. The synthetic corpus already has the scales to straddle the cap:
`core` (about 130 pages) and `small` (200) sit at or under it, `medium`
(1,000), `large` (10,000), and `xlarge` (25,000) are well past it
(`tests/evals/corpus.py`, `SCALES`). `xlarge` exists because a real
single-operator deployment is already past 20,000 pages (issue
athenaeum#1735) -- `large` alone cannot show whether the answer holds at the
size the project actually runs at. It is opt-in at the grid-dispatch level
(`tests/evals/north_star_cli.py`'s `DEFAULT_CORPUS_SCALES` excludes it, and
the `evals.yml` workflow's `north_star_corpus_scales` dispatch input must
name it explicitly) because a `NATIVE_GREP` cell over 25,000 files is the
most expensive cell in the grid.

It follows that there is almost certainly a corpus size **below which
Athenaeum is not worth using**: while everything fits the native index, a
compiler and a search stack are overhead. Finding that cutoff is an explicit
output of the comparison (§6, crossover scale), and once measured it belongs
in the README's "is this for me" gate as a number, so a reader with a small
corpus is told plainly to use their agent's own memory.

## 4. The arms

Two native arms join the existing grid. Both are genuine tool-use loops. The
**primary implementation is API-backed**: the Anthropic Messages API with a
`recall`/`read_entity` tool pair for the PULL-style arms and a `grep`/`read`
tool pair served by
the harness over the materialised directory for the native arms. That lets
the whole grid run from the evals workflow's manual dispatch with the key it
already loads, and keeps the live run off the operator's plate (operator
preference, 2026-09-16). The existing `claude -p` path stays as an optional
fidelity spot-check: it is the only way to observe Claude Code's own index
load and its own tools, so it is worth one run to confirm the API-mode
numbers, not the path the decision rests on. The report states which mode
produced each cell.

Selected via `run_probe_all_arms(mode=...)` or `tests/evals/north_star_cli.py
--mode` (falls back to the `ATHENAEUM_EVAL_MODE` env var, default `api`) —
`"api"` or `"cli"`, the same two values named wherever mode appears
(issue athenaeum#1733).

| Arm | Store | Index | Model's read path |
|---|---|---|---|
| `NATIVE_INDEX` | corpus pages materialised as topic files in an auto-memory directory | `MEMORY.md` built as one `name — description` line per page, then truncated exactly as Claude Code truncates it | index in context; topic files on demand |
| `NATIVE_GREP` | same files | none | file search and read tools only |

`NATIVE_INDEX` at a scale past the cap is the honest picture of what native
memory becomes at that scale, so it runs at every scale; the truncation is
the finding, not a confound. `NATIVE_GREP` isolates the no-index case so a
result can say whether the index helped at all.

In API mode the harness builds the index and applies the real cap itself
(first 200 lines, then a further cut by STRING LENGTH past 25000 characters
-- `NATIVE_INDEX_MAX_CHARS`, extracted from the 2.1.274 binary as `jW =
25000`, not a `25 * 1024` approximation, and not UTF-8 bytes -- within that
window, matching the real loader), pinned by a test, and injects it as the
first user turn to mirror Claude Code's load, appending a reproduction of
Claude Code 2.1.274's own truncation-notice template when the cap actually
bound: a three-form size clause (lines-only, chars-only, or `"{N} lines and
{X}"` when both are exceeded -- never the literal word "both"), the exact
cut-count/start-line, and an 80-character word-boundary-cut snippet ending in
a single `…`, all filled from the actual truncation performed (recorded as
`truncated_by_harness`, since a harness truncation is a distinct fact from
`truncated_by_claude_code`). In CLI mode
the auto-memory directory is supplied through `autoMemoryDirectory`
in a settings file passed to the subprocess, so Claude Code performs its own
load and truncation.

Each API-mode arm's system prompt mirrors what its real counterpart actually
gives the model: the `PULL`-style arms are told a `recall` tool exists over
the knowledge base and to use it before concluding it does not know, and that
a `read_entity` tool returns one whole page given the `uid` and `type` a
`recall` hit reports — both served in-process by the same functions the
shipped MCP server calls (`recall_search`, `entity_read`), using the MCP
server's own tool descriptions and parameters
(`query`/`top_k`/`with_pii`/`history`/`type`, and
`uid`/`entity_class`/`include_excluded`/`usage_classes`) rather than a
paraphrase. Those two, and nothing else, are what the real server serves a
PULL arm, so API-mode and CLI-mode PULL measure the same tool surface —
which matters in two distinct ways (athenaeum#1756, both measured by
`tests/evals/test_reference_tag_contract.py`). Every `recall` hit's body is
windowed to 400 characters and the corpus puts each page's reference tag on
its last line, so on three probes the tag-bearing page is returned and the tag
still is not (pages of 587/566/434 characters); `read_entity` returns the
whole page. Separately, on nine probes the tag-bearing page does not rank into
`top_k=5` at all — those pages are short, so no window cut them — and
`read_entity` reaches them by uid from a first-hop page instead. The native
arms are told their memory directory's path and that `MEMORY.md` (when
present) is an index whose topic files are opened on demand with `grep`/
`read`, mirroring Claude Code's own auto-memory instructions rather than the
single-shot arms' "answer using only the context supplied" prompt.

The recall relevance floor ships inactive and the shell hook does not apply
it even when set (athenaeum#1665). The first run uses the floor **as
shipped**, because that is what the deployment delivers today; a second pass
with the floor active is reported alongside once the hook honours it.

The harness input that dispatches that second pass is
`north_star_cli.py --relevance-floor-vector` / `--relevance-floor-fts5`
(athenaeum#1761): set, it writes `recall.relevance_floor` (plain and
push-scoped) into an `athenaeum.yaml` under each materialized knowledge
root, so both the breadcrumb hook and the API-mode `recall` tool apply the
same threshold. This is the input mechanism only — it does not pick a
threshold (athenaeum#1492's production number is still open) and does not
change how the floor is applied inside `src/athenaeum/` (athenaeum#1665
already did that). The floor-on pass this produces is reported **alongside,
never in place of,** the as-shipped pass, and — the caveat already on
athenaeum#1736 — a floor-on pass never rescues a fail: it is additional
evidence, not a second chance for a configuration that lost on its own
terms.

**Vector-backend measurement caveat (athenaeum#1792).** Any grid pass that
dispatches the vector backend as its own second measurement (a wave-2
backend-fidelity comparison against the fts5-default grid) is meaningful
only once athenaeum#1792's hybrid rank fusion has landed. Before that issue,
the vector backend missed 49 of 52 (scale, probe) offline coverage cases
outright (`tests/evals/test_recall_covers_grep.py`'s `_VECTOR_XFAIL`) —
a vector-dispatch grid run against that state would not be measuring the
live hook's real retrieval quality, only the unmitigated embedding-model gap
that issue fixed. A vector-dispatch wave run against a pre-athenaeum#1792
checkout should be treated the same way an unset relevance floor is treated
above: read alongside a clear label of which side of the fix it predates,
never as a like-for-like comparison with a post-fusion run.

## 5. Two phases, because the corpus is compiled pages

The synthetic corpus is compiled wiki pages plus probes. It has no raw
observations. That means the arms above test the **read path only**: both
systems are handed the same finished pages and asked the same questions.

That is the right first phase. It is cheap, it reuses every existing arm,
probe and scale, and it is where the index cap bites. But Athenaeum's claimed
invention is the write path (why-athenaeum, "writes are harder than reads"),
and a read-only comparison cannot see it.

**Phase 1 — read path.** Native arms over materialised pages, all scales,
all probe classes. Answers: at each scale, does Athenaeum's delivery beat the
model's own search over the same content?

Every non-abstention probe's `must_not_rank` set is now audited (issue
athenaeum#1777) -- authored where a plausible corpus false positive exists,
or else marked `precision: n/a` with a reason -- as ground truth for the
precision/contamination tables item I (athenaeum#1782) will consume.

**Phase 2 — write path.** A raw-observation generator emits the same stream
of observations to both systems. Native: a sequence of Claude Code sessions
with auto memory enabled, fed the observations, writing whatever the model
chooses to write. Athenaeum: the librarian compiles the same stream, via
`tests.evals.write_path.compile_observation_stream` (issue athenaeum#1775),
the Athenaeum-side Phase 2 driver. Then
Phase 1's probes run against each system's own store. Answers: given the
same inputs, whose memory ends up answering the questions? Phase 2 also
produces the first measurement of the observation filter against a
baseline other than itself.

Phase 2 is also where write cost becomes comparable. Native memory is not
free to build: the model spends tokens deciding what to save and writing
the files, inside the sessions that see the observations. That is its
compile step, paid inline. So the librarian's spend is compared against the
native writer sessions' spend on memory-saving tool calls, not against zero.
In Phase 1 neither side pays a write cost, because both are handed finished
pages, and the cost comparison there is per-turn read cost only.

Both phases stay a synthetic-corpus, synthetic-probe comparison; no live
head-to-head against an operator's real Claude Code sessions and real native
memory is planned (issue athenaeum#1728).

**Probes must include follow-through.** A single-shot probe whose answer sits
in the surfaced page's first lines cannot tell breadcrumb delivery from
full-page delivery, and cannot tell either from a good grep. The realistic
case is a question whose complete answer needs the agent to notice a
breadcrumb, open the page behind it, and follow a link from that page to a
technical detail on a second page that the query never named. The
comparison therefore needs a probe class of that shape — answer tokens
split across two or more pages, at least one reachable only by link from a
surfaced page — run as a genuine multi-turn tool-use loop in every arm that
has tools. The existing `multi_hop` class is the starting point; the new
requirement is that the second hop is a link, not a second query term.

**Every arm is told to cite the page's reference tag.** Correctness is a
deterministic substring match against each probe's planted `answer_tokens`
(§8 forbids an LLM judge), and those tokens are the invented words the corpus
pages carry on their `Internal reference tag:` line. No model repeats an
unasked-for tag, so as first authored the contract was unsatisfiable by a
correct answer: the first live grid graded even `oracle` — handed the
ground-truth page verbatim — at 0 on every class (athenaeum#1753). The fix is
not to weaken grading but to make the contract reachable: every arm's system
prompt, single-shot and tool-using, API mode and `claude -p` CLI mode, carries
one identical instruction (`tests.evals.rollout.REFERENCE_TAG_INSTRUCTION`,
appended via `--append-system-prompt` on the CLI paths) to end the answer with
`[ref: TAG]` for every page the answer is based on — only pages it actually
draws on, not every page opened while searching — or `[ref: none]` when
declining to answer.
Because the text is byte-identical across arms it cannot bias the comparison;
it turns “did you reach the page” into something a substring match can read,
and it makes `oracle` a positive control that must score at or near 1.0 on
every non-abstention class. Abstention grading is unaffected: `[ref: none]`
carries no planted token, and the declining-language rule still applies.

**A page uid citation also counts, under conditions (athenaeum#1793, operator
ruling 2026-09-18, option 1).** A snippet-only cell can legitimately never see
the reference tag (it sits outside `recall`'s 400-character window), yet still
correctly identify and cite the right page by its frontmatter `uid:` instead.
`grade_correctness` therefore also accepts, per planted token, a citation of
the `expected_uids` page that plants that token, when that page's uid was
ACTUALLY DELIVERED in the arm's own tool output for that cell (recall/
`read_entity` for PULL and PUSH_BREADCRUMB_PULL, `expected_uids` itself for
ORACLE, file-read output for `native_grep`) — never merely because the uid is
in `expected_uids`. This is additive to the tag rule above, never a
replacement: it cannot turn a correct answer wrong, and a breadcrumb-only
arm (no tool call, no uid-bearing text at all) still cannot satisfy it by
construction. See `tests/evals/north_star_report.py`'s `grade_correctness`/
`_answer_token_satisfied`/`_delivered_uids` and the re-graded addendum in
[`../measurements/native-memory-baseline-2026-09-17.md`](../measurements/native-memory-baseline-2026-09-17.md).
The write-path sessions of Phase 2 are outside this contract — they produce memory
files, not graded answers.

**The 2026-09-17 15:43Z run (workflow run 35239792240, develop `af0596bb`,
1392 rows, API mode) failed the oracle positive control, and its decision
block is void** (athenaeum#1759). Two deterministic, distinct defects, both
fixed by that issue: (1) `tests/evals/data/corpus/core/10-follow-through.yaml`
wrote `Internal reference code:` on all 12 of its token lines instead of
`Internal reference tag:`, the exact line `REFERENCE_TAG_INSTRUCTION` names —
so `follow_through` had no line matching the instruction to cite and graded
0.000–0.167 at oracle across every scale; (2) in 17 oracle cells outside
`follow_through` the model cited the page's frontmatter `uid:` instead of its
tag (for example `[ref: client-bluewater]` where the grader wanted
`Thornmere`), because the `uid:` line — first in the rendered frontmatter —
was the most identifier-shaped string on the page, which capped
`multi_hop` near 0.333–0.667 even at oracle. Per the ruling on athenaeum#1724,
a void control means every verdict row from this run is unread; the grid must
be re-dispatched after the fix lands (§9 cadence, manual `workflow_dispatch`
only).

**The 2026-09-17 (run 35260484135, develop `0b804c64`, 1392 rows, API mode)
run cleared the oracle positive control on `single_hop`, `temporal`, and
`disambiguation` at every scale, and its decision block is void anyway**
(athenaeum#1766): `follow_through` scored 0/36 and `multi_hop` scored 24/36.
Both are fixture defects, not grader or prompt defects, and distinct from
athenaeum#1759's. `follow_through`: every one of the six queries in
`tests/evals/data/corpus/core/10-follow-through.yaml` was answerable from its
first expected page alone, so a correct, complete answer cited only that
page's tag while `grade_correctness` required both -- the class could not
score above zero regardless of the arm. `multi_hop`: six pages named in some
probe's `expected_uids` (`client-atlas`, `person-ilva-wrenfield`,
`policy-budget-approval`, `project-keelbridge-rollout`,
`project-portal-refresh`, `project-pricing-review`) carried no `Internal
reference tag:` line at all, so a model correctly citing one of them fell
back to its `uid` and failed the reference-tag contract. Per the ruling on
athenaeum#1724, a void control means this run's verdict rows were not read;
the grid is re-dispatched manually after athenaeum#1766 lands (§9 cadence,
manual `workflow_dispatch` only).

The `follow_through` class's actual semantic property -- that a complete
answer requires the second page, not merely that the two are lexically
disjoint -- is not something CI can check: `validate_core` can only pin the
lexical-unreachability half (no content term, including a stemmed prefix,
reaches the second page from the query). Whether the FIRST page alone is
genuinely insufficient is a judgment call about what "a complete answer"
means, and the only instrument that actually exercises it is the live
grid's oracle row -- handed the ground-truth pages verbatim, oracle can only
score below 1.0 on `follow_through` by citing an incomplete answer, so a
below-1.0 oracle cell on this class is read as a probe-authoring regression,
not a retrieval finding.

**The 2026-09-17 run 35266416405 (develop `b28392ef`, 1392 rows) cleared the
oracle positive control on every graded class -- the first of the four
north-star runs to do so -- and its decision block still failed every
scale** (athenaeum#1768). At `medium`, the entire condition-1 loss on the
relationship subset was the three `multi_hop` probes (0/3 against
`native_grep`'s 7/9 pooled with the rest of the subset); every other
relationship row was correct. Two of the three multi_hop probes carried the
same defect athenaeum#1766 fixed for `follow_through`, one class over: the
token-bearing second page restated enough of the query's own vocabulary
(`person-amir-osei` repeated "discretionary spend above 500 GBP" from
`policy-budget-approval`; `person-hana-lindqvist` repeated "client-facing
surfaces" from `project-portal-refresh`) that a correct, complete answer
could be given and cited from the FIRST page alone, so `grade_correctness`
wanting the second page's token made the class ungradable regardless of the
arm. `ratecard_tooling_owner` had no completeness defect but shared literal
vocabulary ("rate", "card", "build") with its own token page anyway. A fifth
run, dispatched after athenaeum#1768 lands, supersedes this one for the
athenaeum#1724 measurement.

Phase 2 needs the generator to emit observations whose compiled ground truth
is known, which is a generator change, not a corpus rewrite: the hand-authored
core pages already carry `source_ref`, so the generator inverts them into
dated observations that would compile back to them.

Phase 2's native write path can now run in api mode too: `run_native_writer_api`
(issue athenaeum#1774) drives the same observation stream through
`run_api_tool_loop` with harness-served `read`/`grep`/`write`/`edit`/`list`
tools over the memory directory instead of a `claude -p` spawn, so the
blocking prerequisite for the rest of Phase 2 (athenaeum#1785/#1786) no
longer needs a logged-in CLI.

**Fidelity caveat on the api-mode writer's system prompt.** Its writing
instructions are a RECONSTRUCTION of the documented auto-memory contract
above (`- <name> — <description>` index lines, the 200-line/25KB load cap),
not Claude Code's own write-side system prompt -- that text is closed-source
and not extractable the way the read-side truncation-notice template was
(§4). It is known to diverge from the real prompt in at least two
directions with OPPOSITE effect on measured native write cost: (1) handing
the model the index-line FORMAT as an instruction, rather than letting it
emerge from unprompted filing judgment, biases toward better, cheaper
filing than reality (understating native write cost); (2) the explicit
`list`-before-writing and check-before-`edit` instructions add tool calls a
real writer is not documented to be told to make, biasing toward more
turns and higher spend than reality (overstating native write cost). These
do not cancel out by assumption. Every api-mode `NativeWriterResult` is
stamped `prompt_fidelity="reconstructed"` (`None` for cli-mode) so a report
can label Phase 2 write-path numbers produced this way as an
APPROXIMATION, pending confirmation from the cli-mode spot-check
(`run_native_writer`), rather than pooling them with a cli-mode row as
equally faithful.

**Caveat on the `push_breadcrumb*` arms: the hook's 3-breadcrumb cap misses
many grep-reachable expected pages, at both corpus scales this offline
check covers** (`tests/evals/test_recall_covers_grep.py`, issue
athenaeum#1770). That module asserts a narrower, one-directional invariant
offline and deterministically -- every expected page a simple grep baseline
reaches must appear in a plain `recall_search` call's own top-5, at both the
`fts5` and `vector` backends -- and separately measures, without asserting
on it, how many of those grep-reachable expected pages fall outside the
shipped hook's top-3 breadcrumbs. Measured 2026-09-17 against `develop`
@ `b9583362` (issues athenaeum#1782/#1790), `core` and `medium`, fts5
backend (the grid's own default). The `hook_reached`/`hook_irrelevant`
columns are resolved through a name→uid mapping that excludes any page
name shared by more than one page (issue athenaeum#1790 -- `medium`'s
generated ballast/distractor tiers repeat templated names; 81 colliding
names / 827 pages excluded there, 0/0 at `core`, matching the athenaeum#1771
review's count exactly):

**`core`:**

| probe_class | probes | expected | grep_reached | recall_reached | hook_reached | grep_irrelevant | recall_irrelevant | hook_irrelevant |
|---|---|---|---|---|---|---|---|---|
| disambiguation | 4 | 8 | 8 | 8 | 7 | 30 | 4 | 2 |
| distractor_robustness | 2 | 4 | 4 | 4 | 3 | 17 | 4 | 3 |
| follow_through | 6 | 12 | 6 | 6 | 6 | 122 | 14 | 7 |
| multi_hop | 3 | 6 | 3 | 3 | 3 | 114 | 10 | 5 |
| redundancy | 1 | 2 | 2 | 2 | 2 | 4 | 2 | 1 |
| single_hop | 4 | 4 | 4 | 3 | 2 | 148 | 17 | 10 |
| temporal | 6 | 6 | 6 | 5 | 5 | 88 | 18 | 12 |

**`medium`:**

| probe_class | probes | expected | grep_reached | recall_reached | hook_reached | grep_irrelevant | recall_irrelevant | hook_irrelevant |
|---|---|---|---|---|---|---|---|---|
| disambiguation | 4 | 8 | 8 | 6 | 6 | 153 | 13 | 5 |
| distractor_robustness | 2 | 4 | 4 | 2 | 1 | 25 | 8 | 2 |
| follow_through | 6 | 12 | 6 | 6 | 5 | 827 | 22 | 9 |
| multi_hop | 3 | 6 | 3 | 3 | 2 | 730 | 12 | 5 |
| redundancy | 1 | 2 | 2 | 2 | 1 | 74 | 3 | 2 |
| single_hop | 4 | 4 | 4 | 3 | 2 | 189 | 17 | 7 |
| temporal | 6 | 6 | 6 | 5 | 4 | 669 | 24 | 14 |

Reading the columns: "reached by hook top-3" is always ≤ "reached by recall
top-5" by construction (the hook is recall's own top-3 subset in the
common case), so every probe class loses SOME grep-reachable expected
pages to the 3-breadcrumb cap even where the full recall call would have
surfaced them -- this is the operator's own §7 decision-rule condition 3
cost/coverage tradeoff made concrete, not a new finding about `recall`
itself. `single_hop` and `multi_hop` lose the largest share proportionally
at both scales.

The SAME module also confirms the disambiguation win recall has over a bare
grep: for probe `person_not_repo`, the `repo-rowanwrenfield` page is
grep-reachable (the query shares terms with it) but is correctly absent from
both recall's top-5 and the hook's top-3; symmetrically for probe
`repo_not_person`, `person-rowan-wrenfield` is grep-reachable but absent from
both. A bare keyword search cannot tell "Rowan Wrenfield the person" from
"rowanwrenfield the repo"; `recall`'s ranking can.

A genuine retrieval-side coverage gap was also found and is tracked as
xfail(strict=True) cases in that test module rather than fixed here (issue
athenaeum#1770 explicitly keeps a fix for `src/athenaeum/search.py` or the
query path out of this module's scope) -- see the module's `_FTS5_XFAIL` /
`_VECTOR_XFAIL` sets and the PR body that introduced them for the full list
and a proposed follow-up issue.

**Precision/recall/contamination tables (issue athenaeum#1782), pooled
("ALL") over every non-abstention probe -- the table athenaeum#1783's cap
ruling reads.** Same module, same measurement date/SHA, three backend
variants: `fts5`, `vector` with the RRF hybrid fusion on (production
default, issue athenaeum#1792), `vector` with the hybrid opt-out
(`recall.hybrid: false`) on. `recall`/`precision`/`contamination` are
micro-averaged (sum hits and denominators across probes, then divide once)
per `R/P/C`; contamination excludes the 3 `follow_through` probes with no
authored `must_not_rank` set (issue athenaeum#1777's own finding -- "n/a",
never a silent 1.0) from its denominator at every row. Full per-probe-class
breakdown: `pytest tests/evals/test_recall_covers_grep.py -k
precision_contamination -s`.

| scale | variant | grep R/P/C | recall@5 R/P/C | hook@3 R/P/C | grep-miss@5 | grep-miss@hook3 |
|---|---|---|---|---|---|---|
| core | fts5 | 0.79/0.06/0.96 | 0.74/0.31/0.54 | 0.67/0.41/0.46 | 2 | 5 |
| core | vector-hybrid-on | 0.79/0.06/0.96 | 0.62/0.20/0.33 | 0.67/0.41/0.46 | 7 | 5 |
| core | vector-hybrid-off | 0.79/0.06/0.96 | 0.10/0.03/0.17 | 0.67/0.41/0.46 | 30 | 5 |
| medium | fts5 | 0.79/0.01/0.96 | 0.64/0.21/0.38 | 0.50/0.32/0.25 | 6 | 12 |
| medium | vector-hybrid-on | 0.79/0.01/0.96 | 0.40/0.13/0.17 | 0.50/0.32/0.25 | 16 | 12 |
| medium | vector-hybrid-off | 0.79/0.01/0.96 | 0.02/0.01/0.08 | 0.50/0.32/0.25 | 32 | 12 |

Reading this: `hook@3`'s R/P/C is identical across all three variants on
each row on purpose -- the hook subprocess resolves its own backend and is
queried once per probe, independent of which `recall_search` backend
variant this table is measuring (see the module docstring's `config.env`
caveat). `vector-hybrid-off`'s collapse relative to `vector-hybrid-on` (for
example core recall@5 precision 0.20 -> 0.03) is the wiring self-check this
module's own test asserts on: the hybrid knob measurably changes vector
ranking, so the two vector rows are not the same measurement twice.

**Cap signal (eval-wave-2-spec.md §5.3), evaluated literally off the pooled
row above (`CAP_SIGNAL_EPS = 0.05`, this issue's own choice -- the epic
fixes no numeric value):**

| scale | variant | recall@hook3 | recall@recall5 | precision@hook3 | precision@recall5 | verdict |
|---|---|---|---|---|---|---|
| core | fts5 | 0.67 | 0.74 | 0.41 | 0.31 | fixed cap is cutting noise |
| core | vector-hybrid-on | 0.67 | 0.62 | 0.41 | 0.20 | fixed cap is cutting noise |
| core | vector-hybrid-off | 0.67 | 0.10 | 0.41 | 0.03 | fixed cap is cutting noise |
| medium | fts5 | 0.50 | 0.64 | 0.32 | 0.21 | fixed cap is cutting noise |
| medium | vector-hybrid-on | 0.50 | 0.40 | 0.32 | 0.13 | fixed cap is cutting noise |
| medium | vector-hybrid-off | 0.50 | 0.02 | 0.32 | 0.01 | fixed cap is cutting noise |

All six cells land on the same verdict: precision@hook3 is sharply above
precision@recall5 at every scale/backend/hybrid combination measured
(`precision@hook3 - precision@recall5 > 0.05` in every row), which is the
epic's own "converse" pattern -- truncating to the hook's top-3 is buying
real precision over the wider recall@5 window, not discarding relevant
pages for free. This holds even on the `core`/`fts5` row where recall@hook3
< recall@recall5 (0.67 < 0.74): the "cutting signal" branch also requires
`precision@recall5 >= precision@hook3`, which does not hold here
(0.31 < 0.41), so cutting-signal never fires. athenaeum#1783's ruling
reads this table; it is not this issue's place to draw a policy conclusion
beyond the verdict the trigger condition itself produces.

The first measurement report under this design is
[`../measurements/native-memory-baseline-2026-09-17.md`](../measurements/native-memory-baseline-2026-09-17.md)
(issue athenaeum#1724, workflow run 35272748886, Phase 1 only, cutoff scale
`none`).

## 6. What is measured

The dimensions already defined for the north-star report
(athenaeum#1523) apply unchanged, plus two:

| Dimension | Why it matters here |
|---|---|
| Correctness (`answer_tokens` match) | the only dimension that can say "worse" |
| Abstention correctness | native memory has no abstention behaviour; a confident wrong answer on an `abstention` probe is a failure direction the north star names |
| Read cost per turn, both arms | native pays for the index (up to 25KB) on every turn plus its grep and read calls; Athenaeum pays for breadcrumbs on every turn plus its recall calls. Phase 1 compares this alone |
| Write cost, both systems (Phase 2) | the librarian's spend versus the native writer sessions' spend on memory-saving tool calls, for the same observation stream. Amortised over the full probe set at that scale (one compile serves one day's questions) and also reported raw, so a reader can re-amortise for their own turn volume |
| Turns and tool calls to answer | grep over 10,000 files is not free in wall-clock or tokens |
| **Crossover scale** | the smallest scale at which Athenaeum's correctness exceeds native's, per probe class. This is the number the decision turns on |
| **Index coverage** (`NATIVE_INDEX` only) | fraction of the corpus the truncated index still names, so a reader can see the cap bite |
| **Cost per correct answer** (per arm, scale, probe class) | total input+output tokens for the cell divided by correct answers in the cell -- read cost in Phase 1; Phase 2 adds write cost amortised over the full probe set at that scale, with the raw write spend printed alongside. Undefined (never infinite or zero) when a cell has zero correct answers |
| **Verdicts and cutoff scale** | the three §7 conditions read per scale (relationship use case won, no other use case lost, cost within 2×/1×/0.5× of the better native arm), and the smallest scale at or above `medium` where all three hold, or "none" with the failing condition named per scale |

Reported per probe class and per scale, never as one aggregate, for the
reason `tests/evals/data/corpus/README.md` gives: a single number cannot tell
"too big" from "too confusable".

## 7. The decision rule

Athenaeum continues if, at the `medium` scale and above:

1. it beats both native arms on correctness for the relationship use case
   (use-cases §2.1: `single_hop`, `multi_hop`, `disambiguation`, `temporal`
   probes that target person and company pages), and
2. it is not worse than the better native arm on any other current use case, and
3. its total cost per correct answer — read cost per turn, plus write cost
   amortised over the full probe set at that scale (Phase 2 only) — is
   within **2×** the better native arm's.

The cost factor was set by the operator on 2026-09-16, before any run, and
has three readings: **2× is the limit** (above it, condition 3 fails);
**1× is the target** (parity: provenance, the decision queue and passive
delivery come at no premium); **0.5× would be the outcome that changes the
positioning** (cheaper *and* better, which is a claim the README could then
make). The report states which reading was reached, per scale.

Winning only below the cap is not a pass: a system that is only better while
the corpus is small enough not to need it has not earned its complexity.
Losing on the relationship use case is a fail regardless of the others.

The report also states the **cutoff**: the smallest scale at which all three
conditions hold. Below it the recommendation is the agent's own memory, and
that number goes into the README.

All three conditions read the SAME Athenaeum arm -- `push_breadcrumb_pull`,
the shipped configuration -- never a different, most-favourable arm picked
per condition; other Athenaeum arms still appear in the report's per-dimension
tables, just never in these verdicts (`--verdict-arm` overrides the default).
"Better native arm" means two different things across these conditions, both
the harshest reading available to Athenaeum: for condition 1, the native arm
with the higher pooled correctness rate; for condition 3, the native arm with
the cheaper *defined* cost per correct -- a native arm that scored zero
correct answers has no defined cost and is never picked as "cheaper" by that
alone. When the better native arm scored zero correct answers on a probe
class while the verdict arm scored at least one, condition 3 reads
`native-zero`, a pass stated in words rather than a fabricated ratio; if both
sides scored zero, it is `undefined` and fails. When a probe class has no
native rows at a scale at all, condition 3 skips that class (named, counted)
rather than failing the scale outright -- the same treatment condition 2
gives a one-sided class; the scale fails condition 3 only when no class at
it has any native cost data to compare.

The result lands as a dated report under `docs/measurements/`, following the
convention `measurements/README.md` documents: the runner ships in-tree and
needs no credential to test; the live run is a separate operator task,
because the PULL-style arms drive a logged-in `claude` CLI.

A probe class new to this scale is `report_only` by default and excluded
from conditions 2 and 3 (issue athenaeum#1776): promoting it into the
decision rule is an explicit operator ruling recorded on athenaeum#1736's
thread, never an automatic consequence of adding rows to `probes.yaml`.

The operator ruled on athenaeum#1793 (2026-09-18, option 1): a delivered-uid
citation now counts toward correctness alongside the reference-tag citation
(§5), which closed one of the two open medium-scale losses named in
`native-memory-baseline-2026-09-17.md` §5/§6 without changing that report's
cutoff scale (still `none`) -- see that report's addendum for the re-graded
numbers.

## 8. Deliberately not done

- **No separate benchmark repository.** The corpus, runner, probes and report
  already live here; a second repo would duplicate them for a neutrality
  nobody has asked for. Revisit only if a third system (a memory library, a
  hosted service) is added as an arm.
- **No LLM judge in the decision rule.** Correctness is a planted-token
  substring match; abstention is a rule. Judged quality is a later, optional
  layer, as athenaeum#1523 already sequences it.
- **No aggregate score.** See §6.

## 9. Cadence

Every arm in this comparison spends tokens or subscription turns. It runs
**only on explicit dispatch**, when a change to a prompt, a model tier, the
compile pipeline, the recall path, or the sidecar could move the result.
Nothing in this record runs on push. Token-free checks over the runner, the
corpus and the report generator run in ordinary CI.

## 10. Related

- [`../use-cases.md`](../use-cases.md) — the questions being graded.
- [`../north-star.md`](../north-star.md) §1 — the failure directions each dimension maps to.
- [`recall-architecture.md`](recall-architecture.md) — why FTS5 and vector are both load-bearing, which is the half of the bet Phase 1 tests.
- `tests/evals/rollout.py`, `tests/evals/corpus.py`, `tests/evals/north_star_report.py` — the machinery the new arms extend.
