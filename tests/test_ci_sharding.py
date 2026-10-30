"""tests/ci_sharding.py: the CI shard partition must be complete, disjoint,
deterministic, and must keep every scheduling unit whole.

A dropped test would leave CI green while testing less, so these properties
are pinned here offline, and again at runtime by ci.yml's `coverage` job
(the shards' selected counts must sum to the collected total).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests import ci_sharding

REPO_ROOT = Path(__file__).resolve().parent.parent

NODEIDS = [
    "tests/test_a.py::test_one",
    "tests/test_a.py::TestK::test_two",
    "tests/test_b.py::test_three[x]",
    "tests/evals/test_recall_covers_grep.py::test_reach[core-pto]",
    "tests/evals/test_recall_covers_grep.py::test_reach[medium-pto]",
    "tests/evals/test_recall_covers_grep.py::test_print[core]",
    "tests/evals/test_recall_covers_grep.py::test_unparametrized",
    "tests/evals/test_hook_byte_equivalence_1912.py::test_sweep",
    "tests/evals/test_north_star_preflight.py::test_case_a",
    "tests/test_c.py::test_four",
]


def test_unit_key_is_the_file_except_for_split_modules() -> None:
    assert ci_sharding.unit_key("tests/test_a.py::TestK::test_two") == "tests/test_a.py"
    assert ci_sharding.unit_key("tests/test_b.py::test_three[x-y]") == "tests/test_b.py"
    grep = "tests/evals/test_recall_covers_grep.py"
    assert ci_sharding.unit_key(f"{grep}::test_reach[core-pto]") == f"{grep}[core]"
    assert ci_sharding.unit_key(f"{grep}::test_print[medium]") == f"{grep}[medium]"
    assert ci_sharding.unit_key(f"{grep}::test_unparametrized") == grep


@pytest.mark.parametrize("total", [1, 2, 3, 4, 7, 20])
def test_every_test_lands_in_exactly_one_shard(total: int) -> None:
    owner = ci_sharding.assign_units(NODEIDS, total)
    per_shard = {
        index: [n for n in NODEIDS if owner[ci_sharding.unit_key(n)] == index]
        for index in range(1, total + 1)
    }
    flattened = [n for shard in per_shard.values() for n in shard]
    assert sorted(flattened) == sorted(NODEIDS)
    assert all(1 <= shard <= total for shard in owner.values())


def test_assignment_is_independent_of_input_order() -> None:
    forward = ci_sharding.assign_units(NODEIDS, 3)
    backward = ci_sharding.assign_units(list(reversed(NODEIDS)), 3)
    assert forward == backward


def test_heavy_units_are_spread_across_shards() -> None:
    owner = ci_sharding.assign_units(NODEIDS, 2)
    core = owner["tests/evals/test_recall_covers_grep.py[core]"]
    # The two heaviest units must never share a shard.
    assert owner["tests/evals/test_north_star_preflight.py"] != core


@pytest.mark.parametrize("spec", ["", "3", "0/3", "4/3", "1/0", "a/b", "1/3/5", "-1/3"])
def test_malformed_shard_spec_raises(spec: str) -> None:
    with pytest.raises(ValueError):
        ci_sharding.parse_shard(spec)


def test_assignment_is_identical_across_processes() -> None:
    """`hash()` is salted per process, so this guards against it creeping
    into the assignment: two fresh interpreters must agree."""
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]);"
        "from tests import ci_sharding;"
        f"print(json.dumps(ci_sharding.assign_units({NODEIDS!r}, 3), sort_keys=True))"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code, str(REPO_ROOT)],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("1", "2")
    }
    assert len(outputs) == 1
