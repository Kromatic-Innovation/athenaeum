<!-- SPDX-License-Identifier: Apache-2.0 -->

# `athenaeum import-memory` — first real-login run, 2026-11-17

Issue athenaeum#2323 (real-login verification of athenaeum#2316, part of
athenaeum#2296). One supervised run of the opt-in memory compile against a real
Claude Code login, in a throwaway container, with invented memory only.

**Verdict.** The command works end to end on a real login: discovery, the
picker, the confirmation, the cap stop, recall of compiled facts from a fresh
Claude Code session, and the "unchosen projects are never read" promise all
held. Three things did not: the cost estimate is about **38 times too low**, a
re-run after a cap stop **does not finish** when files land in a multi-member
cluster (it re-spends tokens and imports nothing), and a **missing login is not
reported** — the run exits 0, says it imported, and marks files done. Details
and the bugs to file are at the end.

## Setup

| Item | Value |
|---|---|
| Athenaeum | 0.26.0, `develop` source installed with `pip install '.[mcp]'`, following the README quick start (`init`, `claude mcp add`, `doctor`) |
| Claude Code | 2.1.326 |
| Container | `python:3.13-slim`, non-root user, nothing mounted from the host |
| Login | A Claude Code OAuth token passed as `CLAUDE_CODE_OAUTH_TOKEN`. `init` reported "LLM provider: claude-cli (Claude Code subscription login)". `doctor` reported the login detected |
| Search backend | `fts5` (the `[vector]` extra was not installed; `reindex` fell back with its documented warning) |
| Memory | Invented. Five projects under the Claude Code projects root, 17 files, 243 to 359 bytes each (mean about 295). All facts are fictional (an orchard ledger, a tide-pool simulator, a pottery kiln, a chat bot, a docs site) |
| Measurement | `spend.jsonl` rows from `athenaeum spend` (token counts come from the `claude -p` JSON envelope per call), plus the `usage` block of the one recall session |

Two knowledge roots were used. Root A took the cap stop, a missing-login
excursion and the resume attempts. Root B repeated the cap stop and resume on a
clean start (with `reindex` run first) so the numbers below do not depend on the
excursion. The two agree.

## Projects chosen, files compiled

Picker output (nothing selected by default; a blank answer first printed "No
projects selected; nothing was read." and left no state file):

| # | Project | Files | Chosen |
|---|---|---|---|
| 1 | kiln-notes | 3 | yes |
| 2 | lantern-docs | 3 | no |
| 3 | orchard-ledger | 4 | yes |
| 4 | quarry-bot | 4 | no |
| 5 | tidepool-sim | 3 | yes |

Chosen: 3 projects, 10 files, 2,945 bytes. Unchosen: 2 projects, 7 files.

## The estimate shown versus the usage measured

The prompt showed, before anything was read: "10 file(s) pending", "Estimated
cost: ~736 tokens on your Claude Code login — rough, from file size only; not
yet measured against a real compile", and "Run cap: 4 files, 100 API calls this
run". It listed project names and file counts, not file names.

Root B, clean start. Every compile call used `claude-haiku-4-5` for the
classify step; cache token counts were 0.

| Run | Files staged | Estimate shown | API calls | Input tokens | Output tokens | Measured total | Files marked imported |
|---|---|---|---|---|---|---|---|
| 1 (cap 4) | 4 | ~736 (priced all 10 pending; the 4-file batch is ~282) | 8 | 6,216 | 3,942 | 10,158 | 4 |
| 2 (resume, default cap) | 6 | ~453 | 12 | 9,318 | 8,610 | 17,928 | 4 |
| 3 (retry) | 2 | ~157 | 4 | 3,106 | 5,002 | 8,108 | 0 |

Runs 1 and 2 are the whole of the work that completed: **28,086 tokens for 10
files, against 736 estimated, a factor of 38.** Per run the factor was 36 for
the first four files and 40 for run 2. Run 3 is pure retry cost (see the bug
below) and is excluded from that figure. Wall time was 48 s for run 1 and 98 s
for run 2.

Root A, for comparison: run 1 (cap 4) 10,842 tokens over 8 calls, a later
resume 8,641 over 4 calls, and a second retry 9,815 over 4 calls. Ledger total
reported by `athenaeum spend`: 29k tokens, 16 calls.

**Does the estimate need correcting? Yes.** The estimate is bytes divided by
4. The measured cost does not follow bytes: it is two classify calls per file
(claim kind and decay bucket), each carrying a roughly 780-token fixed prompt
and producing about 500 to 700 output tokens. That is about 2,800 tokens per file
(28,086 / 10) whether the file is 243 or 359 bytes. For files this small the
estimate should be per-file, roughly 2,500 to 3,000 tokens per file on the
Haiku-class classify path, not per-byte. This run could not measure how the
cost scales for large files; the invented files were all small. A 20-file run
at this rate is about 56k tokens and 40 calls, well inside the 100-call cap.

The other plan draw in this run, not part of the import: one trivial
`claude -p` call to confirm the login (about 17k input and cache tokens) and
one recall session (below).

## Recall from a new session

A fresh `claude -p` session, with only the compiled root B attached as an MCP
server and `recall` as the only allowed tool, was asked four questions. It used
the `recall` tool and answered:

| Question (about) | Answer given | Correct |
|---|---|---|
| Rounding direction of the ledger reconciler | toward the vendor, never toward the buyer | yes |
| Fixed timestep of the simulator | 40 milliseconds | yes |
| Part code for replacement kiln elements | FC-3320 | yes |
| Token rotation of the chat bot (an unchosen project) | NOT FOUND | yes (control) |

The session used 8 input, 455 output, 11,225 cache-creation and 49,349
cache-read tokens over 6 turns (list-price equivalent about $0.054). Plain
`athenaeum recall` with the `fts5` backend returned the same three compiled
pages and nothing for the unchosen project.

## Counter-example: cap stop, then resume

Run 1 stopped at the cap and printed "Imported 4 file(s). 6 file(s) left for
the next run (cap reached)." The wiki had 4 pages and the state file 4 entries.
Run 2, with no flag changes, picked up the remaining 6 and printed
"6 file(s) pending (4 already imported)".

- **No duplicate pages.** After run 2 the wiki held 9 pages for 10 source
  files: one pair of files was merged into a single page by the cluster step.
  Re-running a third time left it at 9. The store was valid after the cap stop.
- **The resume did not finish.** Run 2 imported 4 of 6. The other 2 (the merged
  pair) stayed staged under the raw intake directory, were reported as "staged
  but not compiled", and were **never marked imported**. Every further re-run
  stages them again, makes 4 more calls (about 8k tokens), and imports 0. This
  reproduced on both roots (root B twice, root A three times). The cause is in
  the existing librarian: `retire.py` holds every multi-member cluster until the
  comparator lane has a live caller (athenaeum#1946), so the raw files are
  kept. The page itself is written and recallable; only import-memory's
  bookkeeping never converges.

So the resume half of the criterion is met for "no duplication" and **not met
for "re-running finishes the work"** when any file is held.

## Counter-example: projects that were not chosen

Evidence that the two unchosen projects (7 files, each carrying a unique marker
string) were neither read nor compiled:

- The marker strings appear nowhere in either knowledge root, in either cache
  directory, or in the full git history of either root (`git log -S`).
- No raw or wiki file, and no commit path, names either project.
- Neither state file has an entry for either project.
- File access times were set 30 days before the modification time before the
  run. After it, the 7 unchosen files kept their original access time, while the
  chosen files' access times moved to the run time. That is a positive control:
  the filesystem did record reads, and none were recorded for the unchosen
  files.
- Recall for the unchosen project returned nothing (above).

## Counter-example: a missing login

Root A, run with the login variable unset, same selection, 6 files left.
Compile calls failed (`claude CLI exited 1: (no stderr)`, 6 `claim_kind` and 6
`decay_bucket` failures logged) and the run still exited 0, printed "Imported 4
file(s)", and marked those 4 imported. The 4 pages were written without
`claim_kind` or `bucket` front matter, and no resume path will classify them.
The spend ledger recorded no row for that run (0 tokens). The message never
says the login is missing. This does not meet athenaeum#2316's "a missing login
reports what happened and can be retried".

## Bugs found (not fixed here)

1. **Estimate is about 38 times too low.** Per-byte sizing against a per-file
   cost. Suggest a per-file figure with the call count, and re-measure with
   larger files.
2. **Resume never converges for held files.** Held files are re-staged and
   re-compiled every run, spending about 8k tokens each time, and are never
   marked imported. The run reports "Imported 0 file(s)" with exit 0. The third
   consecutive zero-yield run also fires `librarian-zero-yield-alert`.
3. **A missing login is silent.** Exit 0, "Imported N file(s)", files marked
   done, pages left unclassified, no login message. Needs a login probe, or a
   non-zero exit when every classify call fails.
4. **Misleading warnings on a good run.** "auto-memory: recovery yield 0.000 is
   below threshold 0.500 (recovered 0 of 4)" and "librarian-zero-yield: ...
   committed 0 file(s)" print when 4 files were moved and pages written; the
   ledger row shows `files_processed: 0`. "extra_intake_root not found" is
   printed after the staging directory is emptied. These make a healthy run
   look failed.
5. **The cost line prices every pending file, not the capped batch.** With
   `--max-files 4` the prompt showed the estimate for all 10.
6. **The prompt lists counts, not file names**, where athenaeum#2316's
   criterion says it shows which files it would read.
7. **Minor.** On an `[mcp]`-only install each import prints "changed file ...
   is not in the chromadb index (D3) — full compile". A held raw file also
   shows up in recall as an `[uncompiled]` hit beside its compiled page.

## Cleanup

Nothing was fixed. The container, its image, the login variable and the
invented memory were deleted after the run. The credential was never printed,
logged or written to a file.
