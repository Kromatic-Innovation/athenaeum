# Reasoning-tier (T1/T2) measurements

Durable record of measurements taken against the live `~/knowledge` store for the
reasoning-tier screening subsystem (`src/athenaeum/reasoning_tiers.py`, issues
athenaeum#518 / athenaeum#602 / athenaeum#432).

Figures here are **measured**, never estimated. Every section records the exact
commands that produced it so a later agent with no session memory can reproduce a
comparable number.

---

## Baseline 0 — pre-enable, 2026-08-19

Recorded for athenaeum#784. `ATHENAEUM_REASONING_TIER_AUDITING_ENABLED` was **unset
(off)** for the entire measurement, and every command below is read-only: nothing
acquired `~/knowledge/.athenaeum.lock`, and no wiki page, raw file, or ledger was
written.

### Provenance

| | |
|---|---|
| Measured | 2026-08-19 (UTC 2026-08-20T02:0x) |
| Store | `~/knowledge` (live) |
| athenaeum version | `0.19.0` |
| Interpreter | `/Users/tristankromer/local-deploys/athenaeum/.venv/bin/python` — the pinned deploy checkout the nightly librarian runs from (`librarian-run.sh` resolves this same path) |
| Deploy checkout git SHA | `ca038f5bfa5856bf12a0a3f9eb58990cdf403a3a` (committed 2026-08-16) |
| Provider at measurement time | `llm.provider: api` (flipped from `claude-cli` on 2026-08-14, athenaeum#774) |

Interpreter choice is load-bearing: this host carries three athenaeum installs and
the bare `athenaeum` on `PATH` is a stale pyenv 0.15.0. Reproduce with the deploy
venv path above, not with `athenaeum`.

### Window and its justification

Two windows are used, and they are not interchangeable:

- **Queue state** (depth, safe-class, sample review) is a **point-in-time snapshot**
  as of 2026-08-19. A queue with no inflow (see next section) has no meaningful
  rate, so a snapshot is the correct instrument.
- **Proposal generation rate** is measured over the **9 nights between 2026-08-05
  and 2026-08-17 on which the nightly sweep log records a completed `wiki-dedupe`
  phase**: 08-05, 08-06, 08-07, 08-11, 08-13, 08-14, 08-15, 08-16, 08-17.
  Nights in that span with no usable `wiki-dedupe` record are excluded rather
  than counted as zero. Separately, **2026-08-18 and 2026-08-19 had no librarian
  run at all** — the wrapper's pre-flight refused with
  `LIBRARIAN-GUARD: OUT OF CREDITS` (exit 69, ~2–3s) — so a naive
  per-calendar-night denominator over the last week would understate any rate.

### AC — human merge-queue depth

**405 unresolved entries, 0 resolved**, in `wiki/_pending_merges.md`.

By `created_at` on the entry:

| created_at | unresolved entries |
|---|---|
| 2026-08-07 | 4 |
| 2026-08-08 | 399 |
| 2026-08-10 | 2 |

By write kind: `fold-into-existing` 350, `create-merged` 55.

By source-page count:

| source pages | entries |
|---|---|
| 2 | 226 |
| 3 | 95 |
| 4 | 58 |
| 5 | 26 |

### AC — merge proposals generated per night

**Zero, on every one of the 9 observed nights.**

Over the same 9 nights the `wiki-dedupe` phase evaluated candidates and
**suppressed all of them pre-proposal** as degenerate over-clusters
(`> max_merge_sources=5`), 127–132 suppressions per night:

| night | suppressed proposals | written proposals |
|---|---|---|
| 2026-08-05 | 127 | 0 |
| 2026-08-06 | 128 | 0 |
| 2026-08-07 | 128 | 0 |
| 2026-08-11 | 129 | 0 |
| 2026-08-13 | 129 | 0 |
| 2026-08-14 | 130 | 0 |
| 2026-08-15 | 132 | 0 |
| 2026-08-16 | 132 | 0 |
| 2026-08-17 | 132 | 0 |

The last night on which the sweep log shows a proposal actually written to
`_pending_merges.md` is **2026-08-04 (3 proposals)**.

**Interpretation that the post-enable comparison must carry:** the 405-entry queue
is a **static backlog, not a flowing stream**. The bulk (399 entries) carries a
`created_at` of 2026-08-08, which does not correspond to any proposal write in the
nightly sweep log — it was produced by an invocation outside the nightly wrapper.
Consequently T1's post-enable value is measurable as **backlog drain**, not as
inflow suppression, and any "T1 rejected N tonight" figure will be ~0 until
proposal generation resumes. If a post-enable measurement is taken while
`max_merge_sources=5` is still suppressing every candidate, it measures nothing.

### AC — T2 safe-class count (auto-apply blast radius)

**321 of 405 unresolved proposals (79.3%) are inside T2's safe class** and would
therefore be eligible for `resolve_merge(auto_applied=True)` — applied without
human review — once T2 is enabled.

Computed by calling `athenaeum.reasoning_tiers.safe_class_violation()` directly on
`build_bounded_source_view()` of every source of every unresolved proposal:

| safe_class_violation | entries |
|---|---|
| `None` (SAFE) | **321** |
| `too_many_pages` | 84 |
| `pii_flagged` | 0 |
| `axiom_member` | 0 |
| `cross_memory_class` | 0 |

**The three semantic guards are inert on this corpus.** Across all **1099** source
page views loaded, **every single one has `memory_class` absent from its
frontmatter** (`memory_class = None`, 1099/1099) and **none is `pii`-flagged**
(0/1099). Live wiki frontmatter carries `access`, `created`, `name`, `refines`,
`source`, `tags`, `type`, `uid`, `updated` — there is no `memory_class` key. So
`pii_flagged`, `axiom_member`, and `cross_memory_class` cannot fire, and the safe
class collapses to the single mechanical condition **`len(sources) <= 3`**.

The count is also an **upper bound in one further respect**: it was computed with
`authority_manifest=None`, so the `live_source_duplicate` check was not applied.
Enabling T2 with a manifest present could only reduce 321, never raise it.

### AC — reviewed sample: how many would a human obviously reject

Without this figure, "T1 rejected N" is uninterpretable. A **deterministic
systematic sample of 25** unresolved proposals was drawn (sort by proposal id,
take every `floor(405/25)`-th entry) and each was classified by reading its
`merge_target_name`, source page slugs, and rationale.

Classification rule used: **obvious-reject** = the sources are not one topic; the
cluster is an artifact of lexical/embedding similarity (e.g. several unrelated
tracker issues clustered on the token "issue", or two unrelated bug pages from
different subsystems). **Plausible** = a human would at least have to read the
bodies to decide.

| | count | share of sample |
|---|---|---|
| obvious-reject | **13** | 52% |
| plausible | 12 | 48% |

Obvious-reject proposal ids (2026-08-19 snapshot):
`09da2f04617d`, `120cea781dcc`, `1befe00e44ca`, `26e68a15208e`, `3272a51c6372`,
`4df854f6cb2c`, `810bcbf844ef`, `a026b41dc109`, `b327040faddb`, `ba04f3d95a30`,
`c6f418053ab2`, `e8f09b326308`, `f3c41d221c3b`.

**Cross-tabulating the two figures is the headline risk number.** Of those 13
obvious-rejects, **9 have ≤3 source pages and therefore sit inside T2's safe
class**: `120cea781dcc`, `1befe00e44ca`, `4df854f6cb2c`, `810bcbf844ef`,
`a026b41dc109`, `b327040faddb`, `ba04f3d95a30`, `e8f09b326308`, `f3c41d221c3b`.

That is **9 of 25 sampled proposals (36%) that a human would obviously reject and
that T2's safe class would nonetheless permit to auto-apply** if T2's own model
verdict came back "approve". The safe class is doing no semantic filtering on this
corpus — only a page-count cap — so the entire protection against auto-applying a
bad merge rests on T2's model judgement, with no structural backstop.

### AC — baseline LLM spend for the merge path, per knob

`athenaeum spend --by-knob`, both windows:

**Last 7 days** — subscription 88k tokens (324 calls, 14 runs); API $26.65 (1587 calls, 223 runs):

| knob | subscription | api |
|---|---|---|
| classify | 88k tok | $26.48 |
| write | 88k tok | $26.48 |
| topic | 0 tok | $0.17 |

**Last 30 days** — subscription 2.5M tokens (2291 calls, 100 runs); API $128.82 (7529 calls, 807 runs):

| knob | subscription | api |
|---|---|---|
| (unattributed) | 1.7M tok | $102.17 |
| classify | 853k tok | $26.48 |
| write | 853k tok | $26.48 |
| resolve | 35k tok | $0.00 |
| topic | 0 tok | $0.17 |

Merge-path baseline, stated precisely:

- `resolve` (the merge resolver): **$0.00 API over 30 days**, 35k subscription
  tokens — all of it predating the 2026-08-14 provider flip.
- `reasoning_t1` / `reasoning_t2`: **absent from the ledger entirely**, i.e. $0.00
  and zero calls. This is the expected reading with the tiers disabled and is the
  correct zero to compare a post-enable figure against.
- By run type over 30 days: `librarian` $128.61, `query-topics` $0.18,
  `answers` $0.03.

Caveat for the post-enable comparison: `(unattributed)` is 79% of 30-day API
spend, so per-knob attribution is incomplete on this ledger. Compare
`reasoning_t1` / `reasoning_t2` (which start at a hard zero) rather than trying to
reconcile against the total.

### Exact commands

Run from any directory; all paths absolute. None takes `.athenaeum.lock`.

```sh
PY=/Users/tristankromer/local-deploys/athenaeum/.venv/bin/python
BIN=/Users/tristankromer/local-deploys/athenaeum/.venv/bin/athenaeum

# provenance
"$PY" -c 'import athenaeum, sys; sys.stdout.write(athenaeum.__file__)'
git -C /Users/tristankromer/local-deploys/athenaeum log -1 --format='%H %ci'
grep -n '^version' /Users/tristankromer/local-deploys/athenaeum/pyproject.toml

# queue depth, write kinds, source-page histogram, safe-class breakdown
"$PY" - <<'EOF'
import collections, json
from pathlib import Path
from athenaeum.pending_merges import parse_pending_merges
from athenaeum.reasoning_tiers import (
    SAFE_CLASS_MAX_PAGES, build_bounded_source_view, safe_class_violation)
W = Path.home() / "knowledge" / "wiki"
pms = parse_pending_merges(W / "_pending_merges.md")
un = [p for p in pms if not p.resolved]
print("unresolved", len(un), "resolved", len(pms) - len(un))
print(collections.Counter(p.created_at[:10] for p in un))
print(collections.Counter(p.write_kind for p in un))
print(collections.Counter(len(p.sources) for p in un))
viol = collections.Counter()
for p in un:
    viol[safe_class_violation([build_bounded_source_view(s) for s in p.sources]) or "SAFE"] += 1
print(viol, "SAFE_CLASS_MAX_PAGES=", SAFE_CLASS_MAX_PAGES)
EOF

# frontmatter reality check (memory_class / pii presence across merge sources)
"$PY" - <<'EOF'
import collections
from pathlib import Path
from athenaeum.pending_merges import parse_pending_merges
from athenaeum.reasoning_tiers import build_bounded_source_view
from athenaeum.pii import is_pii_flagged
W = Path.home() / "knowledge" / "wiki"
mc, pii = collections.Counter(), collections.Counter()
for p in parse_pending_merges(W / "_pending_merges.md"):
    if p.resolved:
        continue
    for s in p.sources:
        v = build_bounded_source_view(s)
        mc[str(v.frontmatter.get("memory_class"))] += 1
        pii[bool(is_pii_flagged(v.frontmatter))] += 1
print(mc, pii)
EOF

# deterministic sample of 25 for the human-reject classification
"$PY" - <<'EOF'
from pathlib import Path
from athenaeum.pending_merges import parse_pending_merges
W = Path.home() / "knowledge" / "wiki"
un = [p for p in parse_pending_merges(W / "_pending_merges.md") if not p.resolved]
un.sort(key=lambda p: p.id)
for p in un[:: max(1, len(un) // 25)][:25]:
    print(p.id, len(p.sources), round(p.confidence, 2), p.write_kind, p.merge_target_name)
    print("   ", ", ".join(Path(s).stem for s in p.sources))
EOF

# spend
"$BIN" spend --by-knob
"$BIN" spend --since 30d --by-knob
"$BIN" spend --since 30d --by-provider

# proposal-generation rate (nightly sweep log)
grep -hE "wiki-page dedup" ~/Library/Logs/pre-dawn-sweep.out.log \
  | grep -v SUPPRESSED | awk '{print substr($1,1,10)}' | sort | uniq -c
grep -hE "wiki-page dedup: SUPPRESSED" ~/Library/Logs/pre-dawn-sweep.out.log \
  | awk '{print substr($1,1,10)}' | sort | uniq -c
```

### Carried forward to the enable-and-measure pass

1. Compare `reasoning_t1` / `reasoning_t2` spend against a hard **$0.00 / 0 calls**.
2. The **321** safe-class figure is the auto-apply exposure to watch, and **it is a
   page-count cap only** — three of the four safe-class conditions cannot fire
   while wiki frontmatter carries no `memory_class` key.
3. A "T1 rejected N" number is only interpretable against the **52% obvious-reject**
   share measured here, and only once proposal generation resumes — inflow was
   **0/night across all 9 observed nights**.
