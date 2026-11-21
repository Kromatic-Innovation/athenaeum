<!-- SPDX-License-Identifier: Apache-2.0 -->

# Why real sessions do not pull: diagnosis, 2026-11-21

Issue athenaeum#3032, under the conditional GO recorded on athenaeum#2805. This
note covers the issue's first step: diagnose why real sessions rarely call
`recall`. It also adds the native-memory comparison the operator asked for on
2026-11-21.

**Status: complete.** Sections 1 to 4 and 6 were written from the repository
and the published measurements. Section 5 holds the counts from the reader
(`athenaeum push-metrics pull-diagnosis`, section 4), run on the deployment on
2026-11-21 (athenaeum#3038), and names the dominant reason.

This note holds counts, rates and method only. It contains no session
identifiers, page names, paths, prompts or transcript text.

## 1. The gap being explained

| Reading | Source | Pull rate |
| --- | --- | --- |
| Grid, arm `push_breadcrumb_pull` | `native-memory-baseline-2026-11-20-regrade.md` | pull-arm no-call rate **0.000** on every enrolled class: the agent called `recall` on every task |
| Real sessions, interactive | `recall-metrics-real-sessions-2026-11-15.md` | recall-through **0.036** (14/385); of pushed relevant pages **0.049** (10/203) |
| Real sessions, headless | same | recall-through **0.000** (0/129); no `recall` call in 120 windows |
| Real sessions, all pushed pages | `follow-through-real-sessions-2026-11-14.md` | **0.40%** of 35,365 pushed pages opened after the push; headless sessions made **1** recall or read call across 2,151 sessions |

The grid and real sessions use the same shipped breadcrumb and the same `recall`
tool, yet they differ by more than an order of magnitude.

## 2. The four candidates, against what is already known

The issue names four candidate reasons. For each one, this section lists the
evidence already on record and what is still missing.

### 2.1 Breadcrumb wording does not signal there is more to read

The shipped preamble (`athenaeum.context.PREAMBLE`) is one line:

> [Knowledge context] Wiki pages relevant to this message (use `recall` MCP tool
> for full details):

Each bullet is `name — description` (`Candidate.bullet`). A bullet carries no
handle: no uid and no suggested query. To pull, the agent has to write its own
`recall` query from the bullet text. A pointer to more content appears only when
the relevance cap withheld candidates (the athenaeum#1905 overflow line).

**Against this as the dominant reason:** the grid's `push_breadcrumb_pull` arm
injects these same words (`tests/evals/rollout.py`, `run_push_breadcrumb_pull`,
which runs the shipped hook against the grid corpus), and there the agent pulls
every time.
The wording therefore does not prevent a pull once the other conditions hold.
It may still be a weak lever where they do not hold, so it stays a candidate
lever, but not the dominant reason.

### 2.2 Tool description salience

In the grid, `recall` is hard to miss. `build_pull_argv` runs `claude -p` with
`--strict-mcp-config` over a config that holds **one** server, athenaeum's, and
pre-approves `recall` and `read_entity` with `--allowedTools`. Those two are
the only MCP tools in the session.

A real deployment typically configures athenaeum next to several other MCP
servers. Claude Code can then defer MCP tools behind tool search. A deferred
tool shows up as a bare name, and its description and schema are not in
context. To call it, the agent first has to call `ToolSearch` to load it. The
breadcrumb says "use `recall` MCP tool", but in that case nothing called
`recall` is loaded. The callable name is the namespaced `mcp__<server>__recall`,
and it needs that extra step.

Nobody has measured how often real sessions had `recall` deferred. The
transcript records it: a `deferred_tools_delta` attachment lists deferred tool
names, along with failed, pending and needs-auth MCP servers. The reader added
here counts it (section 4).

### 2.3 Host-agent behaviour in headless windows

Headless (`sdk-cli`) sessions get one small push and almost never pull: 1 call
in 2,151 sessions. A headless run gets its task as a command or script prompt,
with no human to follow up, so it has little reason to look anything up. The
2026-11-15 report found that headless recall-through and use are zero "by
construction of their tool use". In its sample, no headless page was labelled
`required`, and 47 of 120 headless tasks had no relevant page at all.

Headless therefore cannot be where a pull-inducing lever pays off first. Even a
perfect lever has almost nothing relevant to pull there. Whether native memory
does any better in headless windows is part of the comparison in section 5.

### 2.4 Breadcrumbs not relevant enough to be worth a pull

This candidate has the strongest evidence on record. By the 2026-11-15 labels,
**91.7%** of interactive pushed pages (1,978 of 2,156) and **96.3%** of headless
ones (772 of 802) were irrelevant to the task. With a median of 14 pushed pages
per interactive task, a typical breadcrumb block holds about one relevant page,
next to a dozen or so that are not. An agent that has learned the block is
usually noise is acting rationally when it skips the pull.

**It is not the whole story, though.** Restricted to pushed pages the labeller
judged relevant, recall-through is still only **0.049** (10/203). Relevant
breadcrumbs go unpulled too. So relevance explains part of the gap, and
something in how the pull is offered (2.1 or 2.2) explains the rest.

## 3. What this leaves open

On the published counts alone:

- Low relevance (2.4) is real and large. It caps what any pull lever can earn.
- The grid rules out wording (2.1) as the sole cause, because the same words
  produce a pull there.
- Headless (2.3) is a population where pulls cannot pay off, not a population
  to fix first.
- The deciding unknowns are whether the agent pulls from **native** memory any
  more often than from Athenaeum, and whether `recall` was even **loaded** when
  the breadcrumb named it.

The operator's question on athenaeum#3032 decides the lever's direction:

- **If native reads are just as rare,** the gap is how the host agent behaves,
  and Athenaeum did not cause it. The comparison then comes down to the push
  channel: native's full loaded index against Athenaeum's small set of
  breadcrumbs. The lever should make the push carry the answer.
- **If native reads are clearly more frequent,** the lever should address why
  Athenaeum's breadcrumbs and tool fail to invite a pull. If `recall` turns out
  to be deferred in most pushed windows, that is the first thing to fix.

## 4. The reader

```text
athenaeum push-metrics pull-diagnosis --before 2026-11-15T00:00:00Z --json
```

It reads the push ledger and the transcripts, and it writes nothing. It makes no
model calls and outputs counts only. Its module docstring
(`athenaeum.pull_diagnosis`) gives the definitions. In short:

- **Unit.** A turn window: one human-authored prompt up to the next. A window
  counts only if a proactive push (`sidecar` or `hook` ledger row) falls inside
  it. Headless sessions contribute their first pushed window only. This is the
  2026-11-15 report's task definition.
- **Athenaeum pull.** A `recall` or `read_entity` call in the window.
- **Native pull.** A `Read` of a topic file under an auto-memory directory
  (`projects/<scope>/memory/`), or a `Grep` or `Glob` under one. Since
  athenaeum#3056 it also counts a call to any other tool (a plugin's file
  tool, say) whose input names a path inside such a directory, in a string
  field or a list such as a glob list; a write-capable tool (an edit or write
  tool) is never counted. The JSON splits the total into
  `native_topic_read_builtin` and `native_topic_read_other`. A read of the
  `MEMORY.md` index is counted apart, because Claude Code loads the index at
  session start anyway. A `Bash` command naming the directory is also counted
  apart, as a weaker signal.
- **Deferral.** A `ToolSearch` call naming `recall` in the window, and, per
  session, whether `recall` was deferred, whether its server failed, and
  whether it was called.

**Comparability with the 2026-11-15 report.** The reader covers the whole
eligible universe that report sampled from (701 interactive turns across 39
sessions, 2,150 headless sessions), not its 225-task sample. The label files
and sample manifest live outside the repository. That means:

- These counts are **not conditioned on relevance**. They are raw pull rates,
  computed the same way for both sources, so the Athenaeum and native columns
  compare with each other directly. The issue's phrase "when one is relevant"
  needs labels this run does not have.
- **Positive control.** With `--before 2026-11-15T00:00:00Z`, the window counts
  should come out near 701 interactive and 2,150 headless. They will run lower
  wherever transcripts have since been pruned. A large gap means the reader's
  window rule differs from the report's, and the comparison should not be read
  until that is settled.

## 5. Counts (host run, 2026-11-21)

Command: `athenaeum push-metrics pull-diagnosis --before 2026-11-15T00:00:00Z
--json`, from `develop` after the athenaeum#3032 reader merged, against the
deployment's push ledger and transcript root. Headless sessions contribute
their first pushed window only (the default).

These counts predate athenaeum#3056: the native topic column counts only the
built-in `Read`, `Grep` and `Glob` tools, so it is an undercount where a
plugin file tool was used. A re-run with the corrected reader is pending.

**Positive control: passes.** The reader found 679 interactive windows across
40 sessions, against the 2026-11-15 report's 701 across 39, and 1,846 headless
windows against 2,150. The headless shortfall (86% coverage) fits transcripts
pruned since. The ledger holds 5,989 sessions, and 4,103 of them have no
transcript left to read.

| Population | Pushed windows | Windows with `recall` | Windows with any Athenaeum pull | Windows with a native topic read | Windows with a native index read | Windows with `ToolSearch` for `recall` | Sessions with `recall` deferred | Sessions with server failed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Interactive | 679 | 42 (6.2%) | 42 (6.2%) | 0 (0.0%) | 0 (0.0%) | 42 (6.2%) | 40 of 40 | 0 |
| Headless | 1,846 | 0 | 0 | 0 | 0 | 1 (0.05%) | 950 of 1,846 | 7 |

Supporting counts:

- **Interactive calls:** 53 `recall` calls, 1 `read_entity` call, 45
  `ToolSearch` calls naming `recall`. 31 of 40 sessions called `recall` at
  least once.
- **Weaker native signal:** a `Bash` command naming an auto-memory directory
  appears in 44 interactive windows (6.5%, 63 commands). A shell command that
  names the directory may list it, write to it or search it, so this is not a
  read of a topic file. It is reported apart, as section 4 specifies.
- **Headless deferral:** for 889 of 1,846 headless sessions, the transcript did
  not show whether `recall` was loaded.

**Population caveat.** Outside the population above, the deployment's
transcripts do hold native topic reads: 227 `Read` calls on auto-memory
topic files, across 74 sessions between 2026-10-22 and 2026-11-14. None of those
74 sessions has a row in this deployment's push ledger, so none had a pushed
window to count. They ran in a separate container environment whose pushes, if
any, were not recorded here. Native topic reads therefore do happen, but not in
the sessions where Athenaeum pushed. The comparison below covers the pushed
population only.

**Dominant reason: the host agent rarely pulls from any memory store in pushed
windows. The breadcrumb does not cause it.** By section 3's rule:

- Native topic reads are not more frequent than Athenaeum pulls. They are
  rarer: 0 of 679 interactive windows against 42. Even the weaker `Bash`
  signal (6.5%) only matches the Athenaeum pull rate (6.2%); it does not
  clearly exceed it.
- So the comparison comes down to the push channel, and section 3 points the
  lever at making the push carry the answer (section 6, push-carrying).
- `recall` was deferred behind tool search in every interactive session, and
  every interactive window with a `recall` call also held a `ToolSearch` for
  it (42 of 42). Deferral is universal, so these counts cannot separate its
  effect from low relevance (2.4). The grid, where `recall` is loaded and
  pulled every time, is the only contrast, and relevance confounds it. Under
  section 3, deferral is not the first fix, because native reads are not more
  frequent. It remains a cheap secondary lever: name the callable tool in the
  preamble and say it may need loading.

## 6. Lever targets, set before the counts are seen

The issue asks for a lever with a falsifiable recall-through target. Each
candidate's target is stated here, before the section 5 counts exist, so the
counts cannot shape the target. Choosing the lever, and filing its build slices,
waits for section 5. These are proposals for the operator to rule on.

- **Pull-inducing** (bullets carry a handle the agent can recall by, and the
  preamble names the tool by its callable name and says it may need loading).
  Target: interactive recall-through of pushed relevant pages rises from
  **0.049** to **at least 0.15** on a real-session re-measure. Its lower
  interval bound must exceed 0.093, the upper bound of the 2026-11-15 interval.
- **Push-carrying** (a richer section-level push within the token budget,
  compare grid arm `push_sections_pull`). Recall-through is the wrong yardstick
  here, because the point is not needing to pull. Target: interactive session
  use rate under the LLM rule rises from **0.114** to **at least 0.20**, with
  recall-through not falling below **0.036**.
- **Either lever** also re-runs the native-memory grid, per the re-run rule in
  `docs/use-cases.md` section 1. Delivery changes, so the grid must show the
  lever does not cost the relationship win at `medium`.

## Method

Section 5 was added on 2026-11-21 from one read-only run of the reader on the
deployment, plus a raw count of `Read` calls on auto-memory topic files across
all retained transcripts, used for the population caveat. Neither step called a
model.

The repository was read at the tip of `develop` on 2026-11-21. Sections 1 and
2.4 quote only figures already published in the three reports cited. Section 2
reads the shipped breadcrumb renderer (`athenaeum.context`) and the grid harness
(`tests/evals/rollout.py`). For those sections no deployment data was read,
and no model was called.
