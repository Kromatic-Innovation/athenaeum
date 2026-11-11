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
#: 4-vCPU runner, from the per-worker `-v` timelines of TWO full CI runs:
#: run 42511111865 (the develop tree at 0d63b08c, athenaeum#2233's baseline)
#: and run 42965256518 (this PR's first run). Both were parsed with the
#: method the issue states -- per worker, the gap between consecutive results
#: attributed to the module of the later result -- and that parser reproduces
#: every figure in the issue's own table from the baseline run, so the two
#: sides are directly comparable. Units missing here weigh
#: DEFAULT_SECONDS_PER_TEST per test.
#:
#: An entry is set only where the two runs AGREE. Where they disagree by more
#: than about 25% the unit is left unpriced, or carries a noted midpoint: the
#: weights only steer balance, never correctness, so a confidently wrong
#: number is worse than the per-test default. Left unpriced for that reason:
#: `test_layer_boundary.py` (10.1 then 44.0) and
#: `test_import_graph_acyclic.py` (21.1 then 11.9).
#:
#: Several entries were badly stale in a way that actively corrupted the bin
#: packing rather than merely failing to help it: `test_json_utils.py` was
#: priced at 50.0 against a measured 1.1 in BOTH runs,
#: `test_rollout_mode_labelling.py` at 37.0 against 10.3, and
#: `test_hook_byte_equivalence_1912.py` at 6.3 against 1.4. Five genuinely
#: heavy units (29-47s, stable across both runs) had no entry at all.
UNIT_WEIGHT_SECONDS: dict[str, float] = {
    # 202.5 then 190.8, and NOT changed by athenaeum#2233: its cost is real
    # nested MCP round trips (a fake `claude` child plus a
    # `-m athenaeum.cli_tool_bridge` grandchild, each cold-importing the mcp
    # SDK), once per arm across two full 9-arm groups. Fewer round trips
    # would mean less proven, and reworking `provider.run_tool_loop`'s
    # per-call bridge isolation is a production design change rather than a
    # test cost fix. This single unit is the floor of whichever shard holds
    # it: see athenaeum#2233's PR for the arithmetic. Measured 202.5 before
    # this issue's changes, then 190.8 and 150.7 on two runs after -- the
    # module is untouched, so that spread is runner contention, which is why
    # the entry stays at the conservative end: overpricing the heaviest unit
    # is what makes the packer isolate it.
    "tests/evals/test_north_star_preflight.py": 191.0,
    "tests/test_shell_hooks.py": 115.0,
    "tests/test_eval_corpus_generator.py": 103.0,
    "tests/evals/test_attribution_eval_stub.py": 47.0,
    "tests/test_eval_corpus_leakage.py": 46.0,
    # 34.5 then 55.0. One test, so there is nothing to average over within a
    # run; the midpoint beats the per-test default either way.
    "tests/evals/test_rollout_push_breadcrumb_spike.py": 45.0,
    "tests/test_cmd_attribution.py": 43.0,
    "tests/test_batch_mode.py": 39.0,
    "tests/evals/test_recall_covers_grep.py[medium]": 33.0,
    "tests/evals/test_attribution_eval_harness.py": 33.0,
    "tests/test_llm_surface.py": 29.0,
    "tests/evals/test_recall_covers_grep.py[core]": 28.0,
    # 57.8 -> 27.4: one full-tree scan per process, not three. What is left
    # is the gate's own single scan, which is the gate's actual work.
    "tests/test_declared_imports.py": 27.0,
    "tests/evals/test_rollout_mode_labelling.py": 11.0,
    # 107.6 -> 9.4: cases.yaml is parsed once per process, not per test.
    "tests/evals/test_attribution_dataset.py": 9.0,
    # 165.5 -> 5.9: the five tests driving `main()` end to end no longer
    # perform the harness's real stdlib-wide page-cache eviction, which also
    # stops them evicting it out from under the sibling xdist workers.
    "tests/test_measure_retrieval_entry_point.py": 6.0,
    "tests/evals/test_hook_byte_equivalence_1912.py": 1.5,
    "tests/test_json_utils.py": 1.1,
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
