"""Deterministic test-suite sharding for CI.

ci.yml's `test` job runs the suite as a matrix of N runner shards, and each
shard runs its slice under pytest-xdist (`--dist loadfile`). This module is
the pure half: given every collected nodeid, it decides which shard owns each
one. tests/conftest.py is the pytest half (the `--ci-shard` option, the
collection filter, and the xdist scheduler, which share `unit_key` below).

Three properties keep this safe, and tests/test_ci_sharding.py pins them:

* **Partition.** Every unit lands in exactly one shard. The `coverage` job in
  ci.yml also checks at runtime that the shards' selected counts sum to the
  collected total, so a dropped test fails the gate instead of passing.
* **Determinism.** Every process computes the same assignment: each xdist
  worker in each shard collects independently. There is no `hash()` (it is
  salted per process) and no reliance on set or dict order. Units are sorted
  with a full tie-break.
* **Unit integrity.** The assignment unit is the same as the xdist scheduling
  unit (`unit_key`), so a module's module-scoped fixtures still build once.

The weights only steer balance, never correctness. A stale weight costs some
wall clock and can never drop or duplicate a test.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Modules whose xdist work unit is split by the FIRST param id of each test
#: (e.g. `[core-...]` / `[medium-...]`). Each one parametrizes an expensive
#: module-scoped fixture by that id, so splitting by it still builds each
#: fixture instance exactly once, just on separate workers or shards.
SPLIT_BY_LEADING_PARAM = frozenset({"tests/evals/test_recall_covers_grep.py"})

#: Measured worker-seconds under 4-way xdist contention on a GitHub-hosted
#: 4-vCPU runner (athenaeum#2230's per-worker timelines). Units missing here
#: weigh DEFAULT_SECONDS_PER_TEST per test.
UNIT_WEIGHT_SECONDS: dict[str, float] = {
    "tests/evals/test_recall_covers_grep.py[core]": 450.0,
    "tests/evals/test_recall_covers_grep.py[medium]": 110.0,
    "tests/evals/test_hook_byte_equivalence_1912.py": 250.0,
    "tests/evals/test_north_star_preflight.py": 250.0,
    "tests/test_eval_corpus_generator.py": 180.0,
    "tests/test_shell_hooks.py": 120.0,
    "tests/evals/test_rollout_push_breadcrumb_spike.py": 80.0,
    "tests/evals/test_adapter_overflow_breadcrumb_1905.py": 70.0,
    "tests/test_declared_imports.py": 70.0,
}

DEFAULT_SECONDS_PER_TEST = 0.1


def unit_key(nodeid: str) -> str:
    """The indivisible scheduling unit for *nodeid*: its file, or for a
    `SPLIT_BY_LEADING_PARAM` module, its file plus leading param id."""
    scope = nodeid.split("::", 1)[0]
    if scope in SPLIT_BY_LEADING_PARAM and "[" in nodeid:
        leading = nodeid.split("[", 1)[1].split("-", 1)[0].rstrip("]")
        return f"{scope}[{leading}]"
    return scope


def parse_shard(spec: str) -> tuple[int, int]:
    """`"2/3"` -> `(2, 3)`. Raises ValueError on anything malformed, so a bad
    value fails the run loudly instead of silently running all or nothing."""
    index_text, sep, total_text = spec.partition("/")
    if not sep or not index_text.isdigit() or not total_text.isdigit():
        raise ValueError(f"--ci-shard must look like INDEX/TOTAL (e.g. 2/3), got {spec!r}")
    index, total = int(index_text), int(total_text)
    if total < 1 or not 1 <= index <= total:
        raise ValueError(f"--ci-shard {spec!r}: need 1 <= INDEX <= TOTAL and TOTAL >= 1")
    return index, total


def assign_units(nodeids: Iterable[str], total: int) -> dict[str, int]:
    """Map each unit of *nodeids* to a 1-based shard index.

    Uses greedy longest-processing-time bin packing: heaviest unit first,
    each onto the currently lightest shard, with ties broken by unit name and
    then by shard index, so the result is a pure function of the input."""
    counts: dict[str, int] = {}
    for nodeid in nodeids:
        key = unit_key(nodeid)
        counts[key] = counts.get(key, 0) + 1
    weighted = sorted(
        (
            (UNIT_WEIGHT_SECONDS.get(key, count * DEFAULT_SECONDS_PER_TEST), key)
            for key, count in counts.items()
        ),
        key=lambda pair: (-pair[0], pair[1]),
    )
    load = [0.0] * total
    owner: dict[str, int] = {}
    for weight, key in weighted:
        lightest = min(range(total), key=lambda i: (load[i], i))
        load[lightest] += weight
        owner[key] = lightest + 1
    return owner
