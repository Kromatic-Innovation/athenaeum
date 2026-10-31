#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Before/after frontmatter-parse-cost benchmark (issue athenaeum#2178).

**What this answers.** ``models.parse_frontmatter`` already prefers
``CSafeLoader`` (athenaeum#1194), but only the scanner+parser stage is C --
turning the parsed node graph into Python objects (implicit-tag resolution,
mapping/sequence/timestamp construction) still runs PyYAML's pure-Python
constructor for EVERY key of EVERY page. This harness measures what a
field-selective parse (:func:`athenaeum.models.parse_frontmatter_selective`,
which constructs only the top-level keys a caller names, skipping the long
tail -- claim lists, provenance blocks, nested ``sources`` -- entirely) saves
over the full parse, on a full-wiki scan shaped like
:class:`athenaeum.models.EntityIndex`'s real ``_load`` loop: glob ``*.md``,
read each file, parse its frontmatter.

**Corpus.** This script GENERATES its own synthetic fixture corpus (default
~26,000 pages, matching the issue's "about 26k pages") under a scratch
directory -- ``tempfile.mkdtemp()`` by default, or ``--scratch-dir`` if given.
It never reads, and takes no option to read, any operator knowledge tree --
there is no ``--wiki`` flag pointing at a live store, unlike
``measure_corpus_shape.py``. Each page carries a realistic mix of identity
fields (``uid``/``name``/``type``/``aliases``/``metadata``) plus a long tail
(``sources``, ``claims``, ``tags``) sized to approximate the real corpus's
~117 constructed sub-objects per page the issue's cProfile citation implies
(2.7M ``construct_object`` calls / ~23k pages).

**Measurement contract.** Wall clock wraps the SAME per-page loop shape for
both candidates (``sorted(wiki_root.glob("*.md"))`` then ``read_text`` then
parse) so the only thing that differs is which parse function runs -- never
an in-process micro-timer around the parse call alone, which would miss I/O
amortization effects a real scan has too. Candidates:

``full``
    :func:`athenaeum.models.parse_frontmatter` -- today's behavior,
    unoptimized by this issue, still the only correct choice for a caller
    that needs arbitrary frontmatter keys (e.g. ``entity_schema``'s
    class-field-union scan).
``selective``
    :func:`athenaeum.models.parse_frontmatter_selective` scoped to
    :data:`~athenaeum.models.EntityIndex._INDEX_KEYS` (``uid``, ``name``,
    ``type``, ``metadata``, ``aliases``) -- what ``EntityIndex.read_page_record``
    actually opted into.

Usage::

    python scripts/measure_frontmatter_parse_cost.py --pages 26000
    python scripts/measure_frontmatter_parse_cost.py --pages 2000 --keep-scratch
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from athenaeum.models import (
    _C_SAFE_LOADER,
    EntityIndex,
    parse_frontmatter,
    parse_frontmatter_selective,
)

_TYPES = ("person", "company", "project", "note", "decision")
_VOCAB = (
    "athenaeum librarian recall memory wiki entity fintech startup series "
    "lean customer discovery validation pivot runway burn traction cohort "
    "revenue margin churn retention funnel acquisition activation referral "
    "onboarding engagement conversion experiment hypothesis assumption risk"
).split()


def _page_text(rng: random.Random, idx: int) -> str:
    """One synthetic wiki page: realistic identity fields + a long tail.

    The long tail (``sources`` with a nested ``provenance`` block, and
    ``claims``) is sized so the per-page constructed-node count approximates
    the issue's own cProfile citation: ~2.7M ``construct_object`` calls over
    ~23,111 real pages, i.e. roughly 115-120 per page.
    """
    etype = _TYPES[idx % len(_TYPES)]
    name = f"{rng.choice(_VOCAB).title()} {idx:06d}"
    aliases = rng.sample(_VOCAB, rng.randint(0, 3))
    n_sources = rng.randint(4, 10)
    n_claims = rng.randint(5, 15)
    created = _dt.date(2024, 1, 1) + _dt.timedelta(days=idx % 700)
    updated = _dt.datetime(2026, 1, 1) + _dt.timedelta(minutes=idx)

    lines = [
        "---",
        f'uid: "{idx:08d}"',
        f"name: {name}",
        f"type: {etype}",
        f"aliases: [{', '.join(aliases)}]" if aliases else "aliases: []",
        "metadata:",
        f"  type: {etype}",
        f"  region: {'us' if idx % 2 else 'eu'}",
        f"created: {created.isoformat()}",
        f"updated_at: {updated.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "sources:",
    ]
    for s in range(n_sources):
        seen = updated - _dt.timedelta(hours=s)
        lines += [
            "  - type: email",
            f'    id: "src-{idx:06d}-{s}"',
            f"    seen_at: {seen.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "    provenance:",
            f"      ingested_by: hook-{s % 3}",
            f"      confidence: 0.{(idx + s) % 10}{s}",
        ]
    lines.append("claims:")
    for c in range(n_claims):
        asserted = created + _dt.timedelta(days=c)
        lines += [
            f"    - text: {rng.choice(_VOCAB)} {rng.choice(_VOCAB)} {rng.choice(_VOCAB)}",
            "      kind: fact",
            f"      asserted_on: {asserted.isoformat()}",
        ]
    lines.append(f"tags: [{', '.join(rng.sample(_VOCAB, 4))}]")
    lines.append("---")
    lines.append("")
    lines.append(f"Body for page {idx}. " + " ".join(rng.choices(_VOCAB, k=40)))
    lines.append("")
    return "\n".join(lines)


def generate_corpus(root: Path, n_pages: int, seed: int = 0xA7E2) -> None:
    """Write *n_pages* synthetic pages under *root* (must be a scratch dir)."""
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    for idx in range(n_pages):
        text = _page_text(rng, idx)
        (root / f"bench_page_{idx:06d}.md").write_text(text, encoding="utf-8")


def _time_full_scan(wiki_root: Path, parse_fn) -> float:
    """Time one `glob -> read_text -> parse_fn` pass, EntityIndex._load shape."""
    start = time.perf_counter()
    for fpath in sorted(wiki_root.glob("*.md")):
        text = fpath.read_text(encoding="utf-8")
        parse_fn(text)
    return time.perf_counter() - start


_INDEX_KEYS = frozenset({"uid", "name", "type", "metadata", "aliases"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", type=int, default=26_000, help="synthetic page count (default: 26000)")
    ap.add_argument("--repeats", type=int, default=3, help="timed repeats per candidate (default: 3)")
    ap.add_argument(
        "--scratch-dir",
        type=Path,
        default=None,
        help="where to generate the fixture corpus (default: a fresh tempfile.mkdtemp())",
    )
    ap.add_argument("--keep-scratch", action="store_true", help="do not delete the generated corpus on exit")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="markdown report path (default: docs/measurements/frontmatter-parse-cost-<date>.md)",
    )
    args = ap.parse_args()

    scratch_is_tmp = args.scratch_dir is None
    scratch = Path(args.scratch_dir) if args.scratch_dir else Path(tempfile.mkdtemp(prefix="athenaeum-fm-bench-"))
    print(f"generating {args.pages} synthetic pages under {scratch} ...")
    gen_start = time.perf_counter()
    generate_corpus(scratch, args.pages)
    gen_elapsed = time.perf_counter() - gen_start
    print(f"generated in {gen_elapsed:.2f}s")

    try:
        full_times = []
        selective_times = []
        # Interleaved (full, selective, full, selective, ...) rather than
        # all-of-one-candidate-then-all-of-the-other: on a shared/contended
        # machine, ambient load drifts over the run's wall-clock span, and
        # interleaving spreads that drift evenly across both candidates
        # instead of letting it bias whichever candidate runs second. MIN
        # across repeats (reported alongside median) is the standard
        # noise-robust statistic for a wall-clock micro-benchmark: it is the
        # closest any repeat got to running with no contention at all.
        for r in range(args.repeats):
            t = _time_full_scan(scratch, parse_frontmatter)
            full_times.append(t)
            print(f"  [full]      rep {r + 1}/{args.repeats}: {t:.3f}s")
            t = _time_full_scan(
                scratch, lambda text: parse_frontmatter_selective(text, _INDEX_KEYS)
            )
            selective_times.append(t)
            print(f"  [selective] rep {r + 1}/{args.repeats}: {t:.3f}s")

        # Also time the REAL caller end to end (EntityIndex._load, which
        # pays read_text + selective parse + index bookkeeping) once, for a
        # sanity check that the function-level numbers above generalize to
        # the actual opted-in caller.
        idx_start = time.perf_counter()
        EntityIndex(scratch)
        idx_elapsed = time.perf_counter() - idx_start
    finally:
        if scratch_is_tmp and not args.keep_scratch:
            shutil.rmtree(scratch, ignore_errors=True)
        elif args.keep_scratch:
            print(f"kept scratch corpus at {scratch}")

    full_median = statistics.median(full_times)
    sel_median = statistics.median(selective_times)
    full_min = min(full_times)
    sel_min = min(selective_times)
    speedup_median = full_median / sel_median if sel_median else float("inf")
    speedup_min = full_min / sel_min if sel_min else float("inf")

    cpu_count = os.cpu_count() or 0

    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        git_sha = "unknown"

    report = f"""# Frontmatter parse cost: full vs. selective (athenaeum#2178)

Durable record of the benchmark issue athenaeum#2178 asks for: a before/after
timing of the full-wiki frontmatter parse, on a synthetic fixture corpus this
script generates (never the operator's live store). Harness committed at
[`scripts/measure_frontmatter_parse_cost.py`](../../scripts/measure_frontmatter_parse_cost.py).

## Result

This container runs several concurrent, unrelated CPU-heavy lane processes
(sibling full-suite `pytest` runs) sharing the same {cpu_count} CPUs this
benchmark ran on -- wall-clock numbers on a single repeat can swing widely
with ambient load. Repeats below are INTERLEAVED (`full`, `selective`,
`full`, `selective`, ...) so load drift is spread evenly across both
candidates rather than biasing whichever one happened to run second, and
**min** (the least-contended repeat observed) is reported as the primary
statistic alongside median.

| Candidate | min wall time | median wall time | pages/s (at min) |
|---|---|---|---|
| `full` (`parse_frontmatter`, today's behavior) | {full_min:.3f}s | {full_median:.3f}s | {args.pages / full_min:,.0f} |
| `selective` (`parse_frontmatter_selective`, `EntityIndex`'s opted-in keys) | {sel_min:.3f}s | {sel_median:.3f}s | {args.pages / sel_min:,.0f} |

**Speedup: {speedup_min:.2f}x (min/min), {speedup_median:.2f}x (median/median).**

Sanity check against the real opted-in caller end to end
(`EntityIndex(scratch_corpus)`, read + parse + index bookkeeping, one run,
same ambient-load caveat): **{idx_elapsed:.3f}s** for {args.pages} pages
({args.pages / idx_elapsed:,.0f} pages/s).

Raw per-repeat timings ({args.repeats} interleaved reps) -- `full`:
{[f"{t:.3f}" for t in full_times]}s; `selective`:
{[f"{t:.3f}" for t in selective_times]}s.

## Provenance

| | |
|---|---|
| Measured | {_dt.date.today().isoformat()} |
| Git SHA | {git_sha} |
| Corpus | {args.pages} synthetic pages, generated by this script (seed 0xA7E2), under a scratch dir -- never `~/knowledge` |
| Corpus generation time | {gen_elapsed:.2f}s |
| Machine | {platform.machine()}, {platform.system()} {platform.release()}, container (shared -- see ambient-load note above) |
| CPUs | {cpu_count} |
| PyYAML | `_C_SAFE_LOADER` = `{_C_SAFE_LOADER}` |
| Command | `python scripts/measure_frontmatter_parse_cost.py --pages {args.pages} --repeats {args.repeats}` |

## What this does and does not show

- This is a parse-cost-only measurement: file read + YAML parse, the same
  shape `EntityIndex._load` and `librarian.rebuild_index` use. It is not an
  end-to-end `athenaeum status` or compile timing.
- The speedup is specific to the key set requested (`uid`, `name`, `type`,
  `metadata`, `aliases`) and this corpus's long-tail shape (4-10 `sources`
  with a nested `provenance` block, 5-15 `claims` entries per page, sized to
  approximate the issue's own ~117-constructed-objects-per-page citation). A
  caller requesting more keys, or a corpus with a thinner long tail, would
  show a smaller ratio.
- Per-call recall latency is explicitly out of scope for athenaeum#2178 and is
  not measured here.
- On a SHARED machine, re-run this a few times and compare the `min`
  figures across runs before trusting a single result -- an outlier
  sibling-process repeat (visible in the raw per-repeat timings above) can
  inflate `full` or `selective` independently, which is exactly why `min`,
  not `mean`, is this script's headline statistic.
"""

    out_path = args.out or (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "measurements"
        / f"frontmatter-parse-cost-{_dt.date.today().isoformat()}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"\nwrote {out_path}")
    print(
        f"full min/median: {full_min:.3f}/{full_median:.3f}s  "
        f"selective min/median: {sel_min:.3f}/{sel_median:.3f}s  "
        f"speedup min/median: {speedup_min:.2f}x/{speedup_median:.2f}x"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
