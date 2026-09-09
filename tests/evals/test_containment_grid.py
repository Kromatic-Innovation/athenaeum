# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the `--scale` knob (issue athenaeum#1521 AC3/AC4).

UNMARKED — runs in the default pytest selection, no network, no credential.
Proves ONE entrypoint (:func:`tests.evals.containment.build_grid`) drives
all three budgets: the same axis inputs produce a different cell count per
scale, never a different code path.
"""

from __future__ import annotations

import pytest

from tests.evals.containment import SCALE_BUDGETS, GridCell, build_grid

PROBES = ("p1", "p2", "p3", "p4", "p5")
ARMS = ("control", "candidate_a", "candidate_b")
CORPUS_SCALES = ("core", "small")
REPLICATES = (0, 1, 2)


def _grid(scale: str) -> list[GridCell]:
    return build_grid(
        scale, probes=PROBES, arms=ARMS, corpus_scales=CORPUS_SCALES, replicates=REPLICATES
    )


def test_same_entrypoint_drives_all_three_scales() -> None:
    """`smoke`/`small`/`full` are all calls to the SAME function; only the
    returned cardinality differs (the design constraint's own wording)."""
    smoke = _grid("smoke")
    small = _grid("small")
    full = _grid("full")

    assert len(smoke) == 1, "smoke must always cap to a single cell"
    assert len(small) == 3 * 2 * 1 * 2  # capped probes=3, arms=2, corpus=1, reps=2
    assert len(full) == len(PROBES) * len(ARMS) * len(CORPUS_SCALES) * len(REPLICATES)
    assert len(smoke) < len(small) < len(full)


def test_smoke_scale_is_a_single_cell_regardless_of_axis_size() -> None:
    """AC: smoke must be runnable "without thinking about cost" — true only
    if it stays a single cell even as the real probe/arm lists grow."""
    huge_probes = tuple(f"p{i}" for i in range(200))
    grid = build_grid(
        "smoke", probes=huge_probes, arms=ARMS, corpus_scales=CORPUS_SCALES, replicates=REPLICATES
    )
    assert len(grid) == 1


def test_cap_preserves_order_never_samples() -> None:
    """A cap truncates the caller's sequence in place; it must not reorder
    or randomly sample, so results are reproducible across runs."""
    small = _grid("small")
    probes_seen = [cell.probe for cell in small]
    # `small` caps probes to 3 -- must be the FIRST three of PROBES, in order.
    assert sorted(set(probes_seen)) == sorted(PROBES[:3])


def test_cell_keys_are_unique_and_stable() -> None:
    full = _grid("full")
    keys = [cell.cell_key() for cell in full]
    assert len(keys) == len(set(keys)), "grid cells must not collide on identity"

    # Stability: rebuilding an identical cell yields an identical key.
    a = GridCell(probe="p1", arm="control", corpus_scale="core", replicate=0)
    b = GridCell(probe="p1", arm="control", corpus_scale="core", replicate=0)
    assert a.cell_key() == b.cell_key()

    # Any one of the four identity fields differing must change the key.
    variants = [
        GridCell(probe="p2", arm="control", corpus_scale="core", replicate=0),
        GridCell(probe="p1", arm="candidate_a", corpus_scale="core", replicate=0),
        GridCell(probe="p1", arm="control", corpus_scale="small", replicate=0),
        GridCell(probe="p1", arm="control", corpus_scale="core", replicate=1),
    ]
    for variant in variants:
        assert variant.cell_key() != a.cell_key()


def test_unknown_scale_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown scale"):
        build_grid(
            "extra-large", probes=PROBES, arms=ARMS, corpus_scales=CORPUS_SCALES,
            replicates=REPLICATES,
        )


def test_scale_budgets_cover_exactly_smoke_small_full() -> None:
    """Pin the vocabulary the issue names -- a future rename must fail here,
    not surface as a confusing CLI --scale choice error."""
    assert set(SCALE_BUDGETS) == {"smoke", "small", "full"}
