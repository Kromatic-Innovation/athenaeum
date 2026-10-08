# SPDX-License-Identifier: Apache-2.0
"""Tests for shape mining over the verdict ledger (issue athenaeum#719, Plan step 1)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from athenaeum.runlock import RunLock
from athenaeum.signal_mining import (
    ShapeKey,
    mine_underdetermined_shapes,
    shape_key_for_entry,
    triggered_shapes,
)
from athenaeum.verdicts import Basis, append_verdict, build_verdict_entry

_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _basis(**overrides) -> Basis:
    defaults = dict(
        content_hashes=["hash-a", "hash-b"],
        coords=[None, None],
        coord_origins={},
        registry_epoch=1,
        tree_epoch=1,
        authority_basis=None,
        predicate_instrument=[None, None],
        comparator_version="v1.gate2",
    )
    defaults.update(overrides)
    return Basis(**defaults)


def _write_page(
    wiki_root: Path, *, name: str, memory_class: str | None = None, scope: str | None = None
) -> None:
    lines = ["---", f"name: {name}", "type: feedback"]
    if memory_class is not None:
        lines.append(f"memory_class: {memory_class}")
    if scope is not None:
        lines.append(f"claimed_scope: {scope}")
    lines.append("---")
    lines.append("body text\n")
    (wiki_root / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")


def _seed_underdetermined(
    wiki_root: Path,
    *,
    id_a: str,
    id_b: str,
    missing: list[str],
    at: str,
) -> None:
    entry = build_verdict_entry(
        id_a,
        id_b,
        "underdetermined",
        basis=_basis(),
        missing=missing,
        at=at,
        decided_by="comparator",
    )
    with RunLock(wiki_root.parent) as lock:
        append_verdict(wiki_root, entry, lock=lock)


def _config(threshold: int = 2, window_days: int = 30) -> dict:
    return {"librarian": {"signal_mining": {"threshold": threshold, "window_days": window_days}}}


class TestTypedClustering:
    """AC2: shapes cluster on typed structure, not free text."""

    def test_same_missing_dimensions_cluster_together(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        for n in ("alpha", "beta", "gamma", "delta"):
            _write_page(wiki_root, name=n)
        _seed_underdetermined(
            wiki_root, id_a="alpha", id_b="beta", missing=["source-authority"], at="2026-10-01"
        )
        _seed_underdetermined(
            wiki_root, id_a="gamma", id_b="delta", missing=["source-authority"], at="2026-10-02"
        )
        shapes = mine_underdetermined_shapes(wiki_root, config=_config(), now=_NOW)
        assert len(shapes) == 1
        assert shapes[0].count == 2
        assert shapes[0].key.missing_dimensions == ("source-authority",)
        assert shapes[0].triggered

    def test_different_missing_dimensions_do_not_cluster(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        for n in ("alpha", "beta", "gamma", "delta"):
            _write_page(wiki_root, name=n)
        _seed_underdetermined(
            wiki_root, id_a="alpha", id_b="beta", missing=["source-authority"], at="2026-10-01"
        )
        _seed_underdetermined(
            wiki_root, id_a="gamma", id_b="delta", missing=["deal-stage"], at="2026-10-02"
        )
        shapes = mine_underdetermined_shapes(wiki_root, config=_config(), now=_NOW)
        assert len(shapes) == 2
        assert all(not s.triggered for s in shapes)

    def test_memory_class_and_scope_participate_in_the_key(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _write_page(wiki_root, name="alpha", memory_class="fact", scope="team-a")
        _write_page(wiki_root, name="beta", memory_class="fact", scope="team-a")
        _write_page(wiki_root, name="gamma", memory_class="decision", scope="team-b")
        _write_page(wiki_root, name="delta", memory_class="decision", scope="team-b")
        _seed_underdetermined(
            wiki_root, id_a="alpha", id_b="beta", missing=["source-authority"], at="2026-10-01"
        )
        _seed_underdetermined(
            wiki_root, id_a="gamma", id_b="delta", missing=["source-authority"], at="2026-10-02"
        )
        shapes = mine_underdetermined_shapes(wiki_root, config=_config(), now=_NOW)
        # Same missing dimension, different memory_class/scope -> two distinct
        # shapes, neither crossing the threshold of 2 alone.
        assert len(shapes) == 2
        assert all(s.count == 1 for s in shapes)

    def test_sort_tolerates_mixed_none_and_string_coordinates(self, tmp_path: Path) -> None:
        """Sentry finding (PR#2021): un-backfilled pages leave memory_class/
        scope as None on one shape while another shape has string values —
        sorting must not raise TypeError comparing NoneType to str."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _write_page(wiki_root, name="alpha")
        _write_page(wiki_root, name="beta")
        _write_page(wiki_root, name="gamma", memory_class="fact", scope="team-a")
        _write_page(wiki_root, name="delta", memory_class="fact", scope="team-a")
        _seed_underdetermined(
            wiki_root, id_a="alpha", id_b="beta", missing=["source-authority"], at="2026-10-01"
        )
        _seed_underdetermined(
            wiki_root, id_a="gamma", id_b="delta", missing=["source-authority"], at="2026-10-02"
        )
        # Must not raise — this is what reproduced the Sentry-flagged crash.
        shapes = mine_underdetermined_shapes(wiki_root, config=_config(), now=_NOW)
        assert len(shapes) == 2

    def test_side_order_does_not_fragment_a_shape(self, tmp_path: Path) -> None:
        """Comparing (a, b) and (b, a) must produce the SAME shape key."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _write_page(wiki_root, name="alpha", memory_class="fact", scope="team-a")
        _write_page(wiki_root, name="beta", memory_class="decision", scope="team-b")
        _write_page(wiki_root, name="gamma", memory_class="decision", scope="team-b")
        _write_page(wiki_root, name="delta", memory_class="fact", scope="team-a")
        _seed_underdetermined(
            wiki_root, id_a="alpha", id_b="beta", missing=["source-authority"], at="2026-10-01"
        )
        _seed_underdetermined(
            wiki_root, id_a="gamma", id_b="delta", missing=["source-authority"], at="2026-10-02"
        )
        shapes = mine_underdetermined_shapes(wiki_root, config=_config(), now=_NOW)
        assert len(shapes) == 1
        assert shapes[0].count == 2


class TestWindowAndThreshold:
    """AC1: N and window are configurable; entries outside the window are excluded."""

    def test_entries_outside_window_are_excluded(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        for n in ("alpha", "beta", "gamma", "delta"):
            _write_page(wiki_root, name=n)
        _seed_underdetermined(
            wiki_root, id_a="alpha", id_b="beta", missing=["source-authority"], at="2026-10-01"
        )
        _seed_underdetermined(
            wiki_root, id_a="gamma", id_b="delta", missing=["source-authority"], at="2026-01-01"
        )
        shapes = mine_underdetermined_shapes(wiki_root, config=_config(window_days=30), now=_NOW)
        assert len(shapes) == 1
        assert shapes[0].count == 1

    def test_threshold_is_configurable(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        for n in ("alpha", "beta"):
            _write_page(wiki_root, name=n)
        _seed_underdetermined(
            wiki_root, id_a="alpha", id_b="beta", missing=["source-authority"], at="2026-10-01"
        )
        shapes_low = mine_underdetermined_shapes(wiki_root, config=_config(threshold=1), now=_NOW)
        shapes_high = mine_underdetermined_shapes(
            wiki_root, config=_config(threshold=5), now=_NOW
        )
        assert triggered_shapes(shapes_low) == shapes_low
        assert triggered_shapes(shapes_high) == []

    def test_default_config_uses_documented_defaults(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        for n in ("alpha", "beta"):
            _write_page(wiki_root, name=n)
        _seed_underdetermined(
            wiki_root, id_a="alpha", id_b="beta", missing=["source-authority"], at="2026-10-01"
        )
        shapes = mine_underdetermined_shapes(wiki_root, config=None, now=_NOW)
        assert shapes[0].threshold == 5
        assert shapes[0].window_days == 30


class TestDeterminism:
    """AC2: no LLM, no free text -- the same ledger always mines the same result."""

    def test_mining_is_deterministic_across_runs(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        for n in ("alpha", "beta", "gamma", "delta"):
            _write_page(wiki_root, name=n)
        _seed_underdetermined(
            wiki_root, id_a="alpha", id_b="beta", missing=["source-authority"], at="2026-10-01"
        )
        _seed_underdetermined(
            wiki_root, id_a="gamma", id_b="delta", missing=["source-authority"], at="2026-10-02"
        )
        first = mine_underdetermined_shapes(wiki_root, config=_config(), now=_NOW)
        second = mine_underdetermined_shapes(wiki_root, config=_config(), now=_NOW)
        assert first == second

    def test_shape_key_is_a_typed_tuple_not_a_string(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        _write_page(wiki_root, name="alpha")
        _write_page(wiki_root, name="beta")
        entry = build_verdict_entry(
            "alpha",
            "beta",
            "underdetermined",
            basis=_basis(),
            missing=["source-authority"],
            at="2026-10-01",
            decided_by="comparator",
        ).to_dict()
        key = shape_key_for_entry(entry, wiki_root=wiki_root, cache={})
        assert isinstance(key, ShapeKey)
        assert key.verdict_type == "underdetermined"
