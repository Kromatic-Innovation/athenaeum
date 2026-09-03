#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Import-budget harness for a Python retrieval entry point (athenaeum#1357).

**What this answers.** Can a Python retrieval entry point serve the FTS5-only
recall path inside a **<=127 ms** wall-clock budget, measured `fork/exec ->
exit`? That budget is not an aspiration: it is the cost of the *live shell*
hook path with the LLM extractor disabled, i.e. "do not regress what the
operator already has". The `<50 ms` contract in the example hook's header is
missed by every live configuration today and is being retired with evidence.

**Why it is genuinely open.** The interpreter floor is comfortable (a bare
`python3 -c pass` is tens of milliseconds), but `import athenaeum` transitively
pulls the `anthropic` SDK through the package root, which costs hundreds of
milliseconds on every CLI invocation (athenaeum#1360). So the question is not
"is Python fast enough" -- it is **"can a retrieval module be reached without
that chain, and what does it cost when it is."** This harness measures exactly
that, by timing three entry points that differ *only* in how they reach the
FTS5 query:

``stdlib``
    Zero athenaeum imports. `sqlite3` + `json` + `re` only. The floor a
    purpose-built retrieval entry point could hit.
``direct-load``
    Loads `src/athenaeum/search.py` by file path with
    `importlib.util.spec_from_file_location`, bypassing `athenaeum/__init__.py`
    entirely. This is not hypothetical -- the live recall hook's vector leg
    already does it, with the comment "avoid __init__.py pulling in heavy
    deps".
``package-import``
    The ordinary `from athenaeum.search import query_fts5_index`. Importing any
    submodule executes the package root, so this pays the full eager-import
    chain. Measuring it is what makes the report able to name *which component*
    blew the budget rather than just reporting a negative.

All three do the **same work**: parse a hook-shaped JSON envelope on stdin,
extract search terms with the stopword filter, run the FTS5 `MATCH` against a
real `wiki-index.db`, look the description up per hit, and emit the hook's
`additionalContext` envelope. Only the import path differs.

**Measurement contract.**

* Wall clock is `fork/exec -> exit` of a real subprocess -- never an in-process
  timer around the query. An in-process timer reports ~5 ms while the real
  per-turn cost is ~650 ms, which is the exact way this measurement has been
  got wrong before.
* Reported per candidate: n, mean, p50, p95, min, max.
* Warm and cold are both measured. Cold is not a guess: before each cold run
  the harness drops the entry point's `__pycache__`, then calls
  `posix_fadvise(POSIX_FADV_DONTNEED)` over the index database, the Python
  executable, and the interpreter's stdlib + site-packages trees. `--cold-check`
  reports the achieved eviction effect so a run on a filesystem where
  `posix_fadvise` is inert (a virtiofs/9p bind mount, for instance) is visible
  as such instead of being silently reported as a cold number.
* The index must be **realistic**. `--index` is required and the harness
  refuses a corpus below `--min-pages` (default 1000), because FTS5 query cost
  is a function of corpus size and a small fixture makes any implementation
  look fast.

**This harness is a measurement fixture, not the core.** The candidate entry
points below are string constants written to a temp directory at run time,
deliberately *not* modules under `src/athenaeum/`. Building the real retrieval
core is athenaeum#1357's explicit non-goal and belongs to the next issue.

Usage::

    # Build a realistic index first (any wiki root with >=1000 pages):
    python -c "from athenaeum.search import build_fts5_index; \\
        build_fts5_index('~/knowledge/wiki', '~/.cache/athenaeum')"

    python scripts/measure_retrieval_entry_point.py \\
        --index ~/.cache/athenaeum/wiki-index.db

    # Full run with the reference points and the eviction self-check:
    python scripts/measure_retrieval_entry_point.py \\
        --index ~/.cache/athenaeum/wiki-index.db --cold-check --json out.json

Exit status is 0 when the verdict is GO against the budget, 1 when it is NO-GO,
and 2 on a usage error. Nothing is written to the wiki, the ledger, or the
index; this is a read-only harness.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

# The budget under test, in milliseconds. Sourced from the live shell hook path
# with the LLM extractor disabled (athenaeum#1357) -- "do not regress what the
# operator already has", not an aspiration.
BUDGET_MS = 127.0

# Refuse to report a number taken against a toy corpus.
DEFAULT_MIN_PAGES = 1000

DEFAULT_PROMPT = (
    "What did we decide about the recall architecture and the memory tier "
    "gate for the knowledge corpus?"
)

# Shared by every candidate: the query, render and emit work the live shell
# hook does. Kept byte-identical across candidates so the only difference
# between their timings is the import path taken to reach the FTS5 query.
_COMMON_BODY = '''
import json
import os
import re
import sys

STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
    "was", "one", "our", "out", "has", "from", "with", "this", "that",
    "they", "will", "have", "been", "what", "when", "which", "while",
    "about", "into", "than", "then", "them", "there", "their", "would",
}


def _load_stopwords(cache_dir):
    """Match the shell hook: prefer the cached canonical list, else the baked one."""
    path = os.path.join(cache_dir, "stopwords.txt")
    try:
        with open(path, encoding="utf-8") as handle:
            words = {line.strip().lower() for line in handle if line.strip()}
        return words or STOPWORDS
    except OSError:
        return STOPWORDS


def _terms(prompt, stopwords, limit=8):
    tokens = re.split(r"[^0-9a-z]+", prompt.lower())
    picked = sorted({t for t in tokens if len(t) >= 3 and t not in stopwords})
    return picked[:limit]


def _seen(session_id):
    path = "/tmp/knowledge-seen-%s" % session_id
    try:
        with open(path, encoding="utf-8") as handle:
            return {line.strip() for line in handle if line.strip()}
    except OSError:
        return set()


def _emit(hits):
    if not hits:
        return
    lines = []
    for name, desc in hits:
        lines.append("  - %s - %s" % (name, desc) if desc else "  - %s" % name)
    header = (
        "[Knowledge context] Wiki pages relevant to this message "
        "(use `recall` MCP tool for full details):"
    )
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": header + "\\n" + "\\n".join(lines),
                }
            }
        )
    )


def main():
    payload = json.loads(sys.stdin.read() or "{}")
    prompt = payload.get("prompt") or ""
    if len(prompt) < 8:
        return
    cache_dir = os.path.dirname(sys.argv[1])
    terms = _terms(prompt, _load_stopwords(cache_dir))
    if not terms:
        return
    exclude = _seen(payload.get("session_id") or "unknown")
    _emit(run_query(sys.argv[1], terms, exclude))


main()
'''

# ---------------------------------------------------------------------------
# Candidate entry points. Each defines run_query(db_path, terms, exclude) and
# then executes the shared body above.
# ---------------------------------------------------------------------------

_CANDIDATE_STDLIB = '''
import sqlite3


def run_query(db_path, terms, exclude, n=3):
    """FTS5 query with zero athenaeum imports -- sqlite3 + stdlib only."""
    match = " OR ".join('"%s"' % t for t in terms)
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    try:
        rows = conn.execute(
            "SELECT filename, name, description FROM wiki "
            "WHERE wiki MATCH ? ORDER BY rank LIMIT ?",
            (match, n + len(exclude)),
        ).fetchall()
    finally:
        conn.close()
    hits = []
    for filename, name, description in rows:
        if filename in exclude:
            continue
        hits.append((name, (description or "").strip()[:200]))
        if len(hits) >= n:
            break
    return hits
''' + _COMMON_BODY

_CANDIDATE_DIRECT_LOAD = '''
import importlib.util
import os


def _load_search(src_root):
    """Load athenaeum.search by file path, bypassing athenaeum/__init__.py.

    This mirrors what the live recall hook already does for its vector leg.
    """
    path = os.path.join(src_root, "athenaeum", "search.py")
    spec = importlib.util.spec_from_file_location("athenaeum_search_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_query(db_path, terms, exclude, n=3):
    module = _load_search(os.environ["ATHENAEUM_SRC_ROOT"])
    cache_dir = os.path.dirname(db_path)
    results = module.query_fts5_index(" OR ".join(terms), cache_dir, n=n, exclude=exclude)
    return [(name, "") for _filename, name, _score in results]
''' + _COMMON_BODY

_CANDIDATE_PACKAGE_IMPORT = '''
import os

from athenaeum.search import query_fts5_index


def run_query(db_path, terms, exclude, n=3):
    cache_dir = os.path.dirname(db_path)
    results = query_fts5_index(" OR ".join(terms), cache_dir, n=n, exclude=exclude)
    return [(name, "") for _filename, name, _score in results]
''' + _COMMON_BODY

CANDIDATES = {
    "stdlib": (
        _CANDIDATE_STDLIB,
        "sqlite3 + json + re only; no athenaeum import at all",
    ),
    "direct-load": (
        _CANDIDATE_DIRECT_LOAD,
        "athenaeum/search.py loaded by file path, package root bypassed",
    ),
    "package-import": (
        _CANDIDATE_PACKAGE_IMPORT,
        "from athenaeum.search import ... (executes athenaeum/__init__.py)",
    ),
}

# Reference points, measured alongside the candidates so the report can
# attribute any overage to a component rather than reporting a bare negative.
REFERENCES = {
    "interpreter-floor": ("pass", "bare interpreter start"),
    "stdlib-imports": ("import sqlite3, json, re", "interpreter + the stdlib the query needs"),
    "import-anthropic": ("import anthropic", "the SDK the package root drags in"),
    "import-athenaeum": ("import athenaeum", "the package root"),
    "import-athenaeum-search": (
        "import athenaeum.search",
        "the retrieval module, via the package root",
    ),
}


# ---------------------------------------------------------------------------
# Cold-start machinery
# ---------------------------------------------------------------------------


def _fadvise_dontneed(path: Path) -> bool:
    """Ask the kernel to drop `path` from the page cache. True if the call ran."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        return True
    except (OSError, AttributeError):
        return False
    finally:
        os.close(fd)


def _evictable_paths(index_db: Path, python: Path) -> list[Path]:
    """Everything a cold run should have to fault back in."""
    paths = [index_db, Path(python).resolve()]
    for key in ("stdlib", "purelib", "platlib"):
        root = sysconfig.get_paths().get(key)
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if filename.endswith((".py", ".pyc", ".so")):
                    paths.append(Path(dirpath) / filename)
    return paths


def _go_cold(script: Path, evictable: list[Path]) -> int:
    """Drop bytecode + page cache before a cold run. Returns files evicted."""
    shutil.rmtree(script.parent / "__pycache__", ignore_errors=True)
    return sum(1 for path in evictable if _fadvise_dontneed(path))


def _cold_effect(index_db: Path, evictable: list[Path]) -> dict[str, float]:
    """Self-check: is eviction doing anything on this filesystem?

    Reports warm vs post-evict read time for the index. A ratio near 1.0 means
    `posix_fadvise` is inert here (common on a virtiofs/9p bind mount) and the
    'cold' numbers in this run should be read as a lower bound, not as cold.
    """

    def _read() -> float:
        start = time.perf_counter()
        with open(index_db, "rb") as handle:
            while handle.read(1 << 20):
                pass
        return (time.perf_counter() - start) * 1000

    _read()
    warm = min(_read() for _ in range(3))
    for path in evictable:
        _fadvise_dontneed(path)
    cold = _read()
    return {
        "warm_read_ms": round(warm, 2),
        "cold_read_ms": round(cold, 2),
        "ratio": round(cold / warm, 2) if warm else 0.0,
    }


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def _stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "mean": round(statistics.fmean(ordered), 1),
        "p50": round(statistics.median(ordered), 1),
        # `n=1` is the exclusive-method floor; fall back to max there.
        "p95": round(
            statistics.quantiles(ordered, n=20)[18] if len(ordered) > 1 else ordered[0], 1
        ),
        "min": round(ordered[0], 1),
        "max": round(ordered[-1], 1),
    }


def _time_once(argv: list[str], stdin_payload: str, env: dict[str, str]) -> float:
    """Wall clock for one `fork/exec -> exit`, in milliseconds."""
    start = time.perf_counter()
    subprocess.run(
        argv,
        input=stdin_payload.encode(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        check=False,
    )
    return (time.perf_counter() - start) * 1000


def _measure(
    argv: list[str],
    stdin_payload: str,
    env: dict[str, str],
    *,
    runs: int,
    warmup: int = 3,
    before_each=None,
) -> dict[str, float]:
    for _ in range(warmup):
        if before_each is not None:
            before_each()
        _time_once(argv, stdin_payload, env)
    samples = []
    for _ in range(runs):
        if before_each is not None:
            before_each()
        samples.append(_time_once(argv, stdin_payload, env))
    return _stats(samples)


def _index_pages(index_db: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT count(*) FROM wiki").fetchone()[0])
    finally:
        conn.close()


def _verify_output(argv: list[str], stdin_payload: str, env: dict[str, str]) -> int:
    """A candidate that returns nothing is not a retrieval entry point.

    Returns the number of pages named in the emitted envelope.
    """
    proc = subprocess.run(
        argv, input=stdin_payload.encode(), capture_output=True, env=env, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"candidate exited {proc.returncode}: {proc.stderr.decode()[:800]}"
        )
    out = proc.stdout.decode().strip()
    if not out:
        return 0
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    return sum(1 for line in context.splitlines() if line.startswith("  - "))


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure a retrieval entry point's fork/exec->exit budget "
        "(athenaeum#1357).",
    )
    parser.add_argument(
        "--index",
        required=True,
        type=Path,
        help="Path to a REAL wiki-index.db. FTS5 cost scales with corpus size; "
        "a toy fixture makes any implementation look fast.",
    )
    parser.add_argument(
        "--min-pages",
        type=int,
        default=DEFAULT_MIN_PAGES,
        help=f"Refuse to report against an index smaller than this "
        f"(default {DEFAULT_MIN_PAGES}).",
    )
    parser.add_argument("--warm-runs", type=int, default=50)
    parser.add_argument("--cold-runs", type=int, default=20)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--budget-ms",
        type=float,
        default=BUDGET_MS,
        help=f"Wall-clock budget the verdict is stated against (default {BUDGET_MS}).",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Interpreter to measure (default: the one running this script).",
    )
    parser.add_argument(
        "--cold-check",
        action="store_true",
        help="Also report whether page-cache eviction is effective on this "
        "filesystem, so an inert-fadvise mount is visible.",
    )
    parser.add_argument(
        "--candidates",
        default=",".join(CANDIDATES),
        help="Comma-separated subset of candidates to measure (default: all of "
        f"{', '.join(CANDIDATES)}).",
    )
    parser.add_argument(
        "--references",
        default=",".join(REFERENCES),
        help="Comma-separated subset of reference points to measure, or 'none' to "
        "skip them. They are what lets the report attribute an overage to a "
        f"component (default: {', '.join(REFERENCES)}).",
    )
    parser.add_argument("--json", type=Path, help="Write the full result record here.")
    args = parser.parse_args(argv)

    selected = [name.strip() for name in args.candidates.split(",") if name.strip()]
    unknown = [name for name in selected if name not in CANDIDATES]
    if unknown:
        parser.error(f"unknown candidate(s): {', '.join(unknown)}")
    # An empty selection must be a usage error (exit 2), never fall through to
    # the verdict block: exit 1 is NO-GO, so a mistyped --candidates would read
    # as "the spike failed" rather than "you passed nothing to measure".
    if not selected:
        parser.error(
            "--candidates selected nothing to measure; choose one or more of "
            f"{', '.join(CANDIDATES)}"
        )

    refs = [] if args.references.strip() == "none" else [
        name.strip() for name in args.references.split(",") if name.strip()
    ]
    unknown_refs = [name for name in refs if name not in REFERENCES]
    if unknown_refs:
        parser.error(f"unknown reference(s): {', '.join(unknown_refs)}")

    index_db = args.index.expanduser().resolve()
    if not index_db.is_file():
        parser.error(f"no index at {index_db}")

    pages = _index_pages(index_db)
    if pages < args.min_pages:
        parser.error(
            f"index at {index_db} holds {pages} pages, below --min-pages "
            f"{args.min_pages}. FTS5 query cost is a function of corpus size; "
            f"measuring against a toy index would report a number that means "
            f"nothing. Build an index over a real corpus first."
        )

    repo_src = Path(__file__).resolve().parent.parent / "src"
    stdin_payload = json.dumps(
        {"prompt": args.prompt, "session_id": "measure-retrieval-entry-point"}
    )

    env = dict(os.environ)
    env["ATHENAEUM_SRC_ROOT"] = str(repo_src)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_src)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )

    record: dict = {
        "budget_ms": args.budget_ms,
        "index": {"path": str(index_db), "pages": pages,
                  "size_bytes": index_db.stat().st_size},
        "python": args.python,
        "python_version": subprocess.run(
            [args.python, "-c", "import sys;print(sys.version.split()[0])"],
            capture_output=True, text=True, check=False,
        ).stdout.strip(),
        "platform": f"{os.uname().sysname} {os.uname().machine}",
        "prompt": args.prompt,
        "warm_runs": args.warm_runs,
        "cold_runs": args.cold_runs,
        "candidates": {},
        "references": {},
    }

    evictable = _evictable_paths(index_db, Path(args.python))
    if args.cold_check:
        record["cold_effect"] = _cold_effect(index_db, evictable)

    print(f"index: {pages} pages, {index_db.stat().st_size / 1e6:.1f} MB  ({index_db})")
    print(f"python: {record['python_version']} on {record['platform']}")
    print(f"budget: {args.budget_ms:.0f} ms, fork/exec -> exit\n")

    with tempfile.TemporaryDirectory(prefix="athenaeum-entrypoint-") as tmp:
        tmpdir = Path(tmp)
        for name in selected:
            source, blurb = CANDIDATES[name]
            script = tmpdir / f"entry_{name.replace('-', '_')}.py"
            script.write_text(source, encoding="utf-8")
            argv_run = [args.python, str(script), str(index_db)]

            hits = _verify_output(argv_run, stdin_payload, env)
            warm = _measure(argv_run, stdin_payload, env, runs=args.warm_runs)
            cold = _measure(
                argv_run,
                stdin_payload,
                env,
                runs=args.cold_runs,
                warmup=1,
                before_each=lambda s=script: _go_cold(s, evictable),
            )
            record["candidates"][name] = {
                "description": blurb, "hits": hits, "warm": warm, "cold": cold,
            }
            print(f"{name:16s} {blurb}")
            print(
                f"{'':16s}   warm p50={warm['p50']:7.1f}  p95={warm['p95']:7.1f}   "
                f"cold p50={cold['p50']:7.1f}  p95={cold['p95']:7.1f}   "
                f"(n={warm['n']}/{cold['n']}, {hits} hits)"
            )

    print()
    for name in refs:
        expr, blurb = REFERENCES[name]
        stats = _measure(
            [args.python, "-c", expr], "", env, runs=max(10, args.warm_runs // 5)
        )
        record["references"][name] = {"expr": expr, "description": blurb, "warm": stats}
        print(f"{name:26s} p50={stats['p50']:7.1f}  p95={stats['p95']:7.1f}   {blurb}")

    # ---- verdict -----------------------------------------------------------
    # The verdict is stated on the *best available* entry point's cold p95:
    # cold because it is the worst realistic case, p95 because a per-turn hook
    # is felt at its tail, and best-available because the question is whether
    # such an entry point CAN meet the budget.
    best = min(
        record["candidates"].items(), key=lambda kv: kv[1]["cold"]["p95"]
    )
    best_name, best_stats = best
    worst_measure = best_stats["cold"]["p95"]
    verdict = "GO" if worst_measure <= args.budget_ms else "NO-GO"
    record["verdict"] = {
        "result": verdict,
        "budget_ms": args.budget_ms,
        "best_candidate": best_name,
        "measured_ms": worst_measure,
        "basis": "cold p95 of the cheapest candidate",
        "margin_ms": round(args.budget_ms - worst_measure, 1),
    }

    print()
    print(
        f"VERDICT: {verdict} -- best candidate '{best_name}' at "
        f"{worst_measure:.1f} ms (cold p95) against a {args.budget_ms:.0f} ms budget; "
        f"margin {record['verdict']['margin_ms']:+.1f} ms."
    )
    if verdict == "NO-GO":
        over = {
            name: stats["cold"]["p95"]
            for name, stats in record["candidates"].items()
            if stats["cold"]["p95"] > args.budget_ms
        }
        print("over budget: " + ", ".join(f"{k} {v:.1f} ms" for k, v in over.items()))
        print(
            "attribution: compare the reference rows above -- the gap between "
            "'stdlib-imports' and 'import-athenaeum' is the package-root chain "
            "(athenaeum#1360)."
        )

    if args.json:
        args.json.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0 if verdict == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
