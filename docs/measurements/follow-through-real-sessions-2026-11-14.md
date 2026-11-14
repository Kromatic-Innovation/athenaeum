# Follow-through on real sessions, 2026-11-14

Measurement for athenaeum#2306, part of the epic athenaeum#2295. It runs the
follow-through analyzer from athenaeum#2301 over one deployment's real push
ledger and session transcripts. The question: after a page is pushed to a
session, does the session later open it?

This report holds rates and aggregate counts only. It contains no session
identifiers, page or entity names, query text, paths or transcript content.
Cells with fewer than 5 sessions are suppressed or merged, and each such case
is marked.

## Headline

- Across the 2,191 sessions that could be analyzed (35,365 pushed pages),
  **0.40%** of pushed pages were opened after the push, **0.25%** were cited
  without being opened, and **99.36%** were neither.
- **Interactive** sessions (40 sessions, 20,301 pages): 0.69% opened, 0.40%
  cited without opening, 98.91% neither. At session level, 27 of 40 (67.5%)
  opened at least one pushed page after it was pushed.
- **Headless** sessions (2,151 sessions, 15,064 pages): 0.00% opened, 0.04%
  cited without opening, 99.96% neither. Headless sessions made one call to an
  athenaeum recall or read tool in total, so they had almost no way to open a
  page.
- The `unknown` population is empty among analyzable sessions (0 sessions).
  The much larger unanalyzable group is described under "The unknown bucket".

## Window

- Push ledger: 12,003 push records over 6,077 sessions, with push timestamps
  from 2026-08-02 to 2026-11-14.
- Analyzable sessions have pushes from **2026-09-24 to 2026-11-14**. Sessions
  whose transcripts are gone, and any whose transcript could not be located,
  are excluded (see below).
- The analysis ran once, on 2026-11-14, against the ledger as it stood that
  day. The ledger was still growing during the run, so a rerun gives slightly
  larger counts.

## Population rules

- **Pushed page.** A unique (session, page) pair, dated by its earliest push in
  that session. Repeat pushes of one page to one session count once.
- **Opened after push.** A `read_entity` call for the page, a file read whose
  filename has the shape of a compiled entity or raw intake file, or a `recall`
  call whose result lists the page, at least one whole second after the
  page's push. An open before or in the same second as the push does not count.
- **Cited without opening.** Not opened after the push, but the page's id
  appears as a whole token in text the session itself authored (user or
  assistant text, never tool output).
- **Neither.** Pushed, not opened after the push, not cited.
- **Opened without push.** A page opened in a session that was never pushed
  that page. It has its own denominator (opens, not pushes), so it is reported
  beside the three classes and not folded into them.
- **Population (interactive, headless, unknown).** Derived from the
  `entrypoint` field that Claude Code writes into each transcript record.
  `cli` and `claude-desktop` map to interactive, and `sdk-cli` maps to
  headless. A session with no `entrypoint` value, several distinct values, or
  a value outside that list goes to unknown. Observed values over the
  analyzed sessions: `sdk-cli` 2,151, `cli` 35, `claude-desktop` 5, none
  absent, none conflicting. This is a proxy for the launch surface, not a
  record of whether a person was present. It is never inferred from session
  length.
- **Session length.** The analyzer's own buckets, by transcript record count:
  short is under 10 records, medium is 10 to 49, long is 50 or more.
- **Analyzed sessions** are those in the push ledger whose transcript could be
  found and read. The analyzer joins the ledger to transcripts and does not
  scan sessions that never received a push.

No model calls were made, so the run cost nothing. The run was read-only: the
ledger and transcripts were only read, and no deployment file was written.

## The unknown bucket

Two different things could be called unknown here.

1. **Analyzable sessions with an undeterminable population: 0.** Every
   analyzable transcript carried exactly one recognized `entrypoint` value.
2. **Ledger sessions with no readable transcript: 3,886 of 6,077 (63.9%).**
   Their 37,117 unique pushed pages (of 72,482 across the ledger, 51.2%) could
   not be scored at all, so their population is unknowable. 1,027 of these
   sessions have pushes entirely before 2026-09-24, which fits transcript
   expiry. The other 2,859 fall inside the analyzable window and have no
   transcript on this host, which could mean they ran elsewhere or their
   transcripts were removed. They are excluded, not counted as `neither`.

Every rate in this report is therefore a rate over the sessions whose
transcripts survived, not over the whole ledger. If the surviving sessions
differ from the rest, the headline moves with them.

## Four-way table by population

| Population | Sessions | Pushed pages | Opened after push | Cited without opening | Neither | Opened without push |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Interactive | 40 | 20,301 | 141 (0.69%) | 81 (0.40%) | 20,079 (98.91%) | 0 |
| Headless | 2,151 | 15,064 | 0 (0.00%) | 6 (0.04%) | 15,058 (99.96%) | 0 |
| Unknown | 0 | 0 | n/a | n/a | n/a | n/a |
| All analyzed | 2,191 | 35,365 | 141 (0.40%) | 87 (0.25%) | 35,137 (99.36%) | 0 |

Session-level view (share of sessions in which any pushed page landed in the
class):

| Population | Sessions with at least one page opened after push | Sessions with at least one page cited without opening |
| --- | ---: | ---: |
| Interactive | 27 of 40 (67.5%) | 11 of 40 (27.5%) |
| Headless | 0 of 2,151 | 6 of 2,151 (0.3%) |

Pushed pages per session differ sharply. The headless median and 90th
percentile are both 7, so a headless session gets one small push. The interactive
median is about 174 and the 90th percentile is 1,472, so interactive sessions
keep receiving pushes as they run. Page-level interactive rates are
therefore weighted toward the longest sessions: the single session with the
most opens accounts for 11% of all interactive opens.

Tool use over the analyzed sessions, as counts of calls: interactive made 126
`recall` calls and 10 `read_entity` calls, and headless made 1 `recall` call
and no `read_entity` calls.

## Four-way table by session length, per population

The analyzer's length buckets do not separate these populations well. The
shortest transcript has 28 records, so **no analyzed session is `short`**.
Transcripts include non-message records, which push every session past the
10-record cutoff.

| Population | Length bucket | Sessions | Pushed pages | Opened after push | Cited without opening | Neither | Opened without push |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Interactive | short, medium, long (merged) | 40 | 20,301 | 141 (0.69%) | 81 (0.40%) | 20,079 (98.91%) | 0 |
| Headless | short | 0 | 0 | n/a | n/a | n/a | n/a |
| Headless | medium | 1,466 | 10,262 | 0 (0.00%) | 6 (0.06%) | 10,256 (99.94%) | 0 |
| Headless | long | 685 | 4,802 | 0 (0.00%) | 0 (0.00%) | 4,802 (100.00%) | 0 |
| Unknown | any | 0 | 0 | n/a | n/a | n/a | n/a |

Interactive short, medium and long are merged because the interactive medium
cell has only 1 session (below the 5-session floor) and the short cell is
empty. Nearly all interactive sessions are `long` by the stock rule, so the
merged row is effectively the interactive long row.

By length across both populations (stock buckets): short 0 sessions, medium
1,467 sessions with 10,269 pages (0 opened, 6 cited, 10,263 neither), long 724
sessions with 25,096 pages (141 opened, 81 cited, 24,874 neither).

Because the stock buckets leave interactive sessions undivided, the table
below splits interactive sessions by transcript record count. These bands are
chosen for this report and are not analyzer buckets.

| Interactive record-count band | Sessions | Pushed pages | Opened after push | Cited without opening | Neither |
| --- | ---: | ---: | ---: | ---: | ---: |
| under 500 | 10 | 280 | 11 (3.93%) | 0 (0.00%) | 269 (96.07%) |
| 500 to 1,999 | 12 | 1,454 | 26 (1.79%) | 0 (0.00%) | 1,428 (98.21%) |
| 2,000 to 4,999 | 8 | 2,562 | 42 (1.64%) | 9 (0.35%) | 2,511 (98.01%) |
| 5,000 or more | 10 | 16,005 | 62 (0.39%) | 72 (0.45%) | 15,871 (99.17%) |

Longer interactive sessions receive many more pushes and open a smaller share
of them. In the first band, the number of sessions with any open is below 5
and is suppressed. The other bands have 8, 7 and 9 sessions with an open,
for 24 of the 27 interactive sessions that opened anything.

## What this does and does not say

- Page-level follow-through is under 1% in both populations. For headless
  sessions the reason is access: they almost never call the recall or read
  tools, so a pushed pointer has no way to be followed.
- For interactive sessions, two thirds of sessions did open at least one
  pushed page, while the page-level rate is low because long sessions accumulate
  hundreds of pushes each. The two readings do not contradict each other.
- These figures separate "opened" from "cited", which the existing `used`
  column cannot do. They do not separate an irrelevant push from a relevant one
  that was ignored. That needs the labelling in athenaeum#2311.
- "Opened" is an upper bound on reading. A `recall` result that merely lists a
  pushed page counts. In the interactive sessions, 475 pushed pages were
  opened or listed by a call at some time, and 141 of those came after the
  push.
- `opened_without_push` is 0 in every cell. A `recall` result is matched only
  against pages already pushed to that session, so the analyzer cannot see a
  `recall` open of a page that was never pushed. Only `read_entity` calls and
  entity-shaped file reads could produce one, and the sessions made only 10
  `read_entity` calls. Treat 0 as "not measurable here", not as a measured zero.
- Citation is checked only against ids in session-authored text. The
  content-reproduction signal behind `used` is not part of this analyzer.
- Single deployment, single window, with 63.9% of ledger sessions
  unanalyzable. The decision rule on athenaeum#2295 consumes this report;
  this report makes no decision.

## Method

The stock analyzer (`push-metrics follow-through`) was run against the real
ledger and transcripts. As shipped, it assigns every session the `unknown`
population, because its source states that no transcript field carries the
interactive-versus-headless distinction. Real transcripts do carry
`entrypoint`, so for this run the analyzer's population function was replaced
at run time with the `entrypoint` mapping above. All class logic, the length
rule and the join are the stock code. The substitution was not committed. The
session-level, tool-call and record-count-band figures came from the same
analyzer functions, run per session.
