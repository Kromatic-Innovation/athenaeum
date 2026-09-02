# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.cluster_comparator` (athenaeum#1255).

The cluster-domain comparator adapter + dark candidate-pairs driver. Every
LLM "client" here is a ``unittest.mock.MagicMock`` mirroring the Anthropic
SDK's ``messages.create`` response shape -- the same posture
``tests/test_comparator.py`` and ``tests/test_contradictions.py`` already
establish. No network calls; no filesystem outside ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.cluster_comparator import (
    ClusterComparatorResult,
    candidate_pairs,
    page_from_auto_memory_file,
    planned_pair_count,
    run_cluster_comparator,
)
from athenaeum.comparator import ContentRelation
from athenaeum.models import AutoMemoryFile, TokenUsage
from athenaeum.verdicts import page_id_for_path

_AUTO_ON: dict[str, object] = {"librarian": {"comparator_enabled": True}}
_AUTO_OFF: dict[str, object] = {"librarian": {"comparator_enabled": False}}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_am(
    scope_dir: Path,
    filename: str,
    body: str,
    *,
    origin_scope: str = "scope-x",
) -> AutoMemoryFile:
    """Build a real-on-disk :class:`AutoMemoryFile`, mirroring
    ``tests/test_contradictions.py``'s ``_write_am`` helper -- the adapter
    under test reads ``member.content`` off disk, so a real file (not an
    in-memory ``_content=`` stub) exercises the actual read path.
    """
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        f"---\nname: {filename}\ntype: feedback\n---\n" + body + "\n",
        encoding="utf-8",
    )
    return AutoMemoryFile(
        path=path,
        origin_scope=origin_scope,
        memory_type="feedback",
        name=filename,
    )


def _fake_client(relation: str) -> MagicMock:
    """A MagicMock mirroring the Anthropic SDK's ``messages.create`` response
    shape, canned to resolve Gate 2 to *relation* (one of
    :class:`~athenaeum.comparator.ContentRelation`'s values)."""
    payload = json.dumps(
        {
            "content_relation": relation,
            "conflicting_passages": [],
            "predicate_a": "a-predicate",
            "predicate_b": "b-predicate",
            "rationale": "test rationale",
        }
    )
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload)]
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# page_from_auto_memory_file -- the AutoMemoryFile -> ComparatorPage adapter
# ---------------------------------------------------------------------------


class TestPageFromAutoMemoryFile:
    def test_adapts_id_text_meta_body(self, tmp_path: Path) -> None:
        scope_dir = tmp_path / "raw" / "auto-memory" / "scope-x"
        am = _write_am(scope_dir, "feedback_probe.md", "hello world")
        page = page_from_auto_memory_file(am)

        assert page.id == page_id_for_path(am.path)
        assert page.text == am.content
        assert page.meta.get("name") == "feedback_probe.md"
        assert page.meta.get("type") == "feedback"
        assert page.body.strip() == "hello world"

    def test_id_matches_verdict_ledger_slug_space(self, tmp_path: Path) -> None:
        """The adapter must key on the SAME id space
        :func:`athenaeum.verdicts.page_id_for_path` already uses for wiki
        pages -- not a second, cluster-domain-only id space -- so a future
        wiring step's verdict-ledger pair keys line up with wiki-domain
        pairs keyed the same way.
        """
        am = _write_am(tmp_path, "project_widget.md", "some claim")
        page = page_from_auto_memory_file(am)
        assert page.id == "project-widget"
        assert page.id == page_id_for_path(am.path)

    def test_reads_content_only_once(self, tmp_path: Path) -> None:
        """``AutoMemoryFile.content`` caches after first read; adapting twice
        must not re-read the file (would raise if the file were deleted
        between reads)."""
        am = _write_am(tmp_path, "feedback_once.md", "cached body")
        page_from_auto_memory_file(am)
        am.path.unlink()
        # Second adaptation must succeed off the cached _content, not a
        # second disk read.
        page_from_auto_memory_file(am)


# ---------------------------------------------------------------------------
# candidate_pairs / planned_pair_count -- pure combinatorics, no model spend
# ---------------------------------------------------------------------------


class TestCandidatePairsAndPlannedCount:
    def test_empty_and_singleton_yield_no_pairs(self, tmp_path: Path) -> None:
        assert candidate_pairs([]) == []
        assert planned_pair_count([]) == 0

        one = _write_am(tmp_path, "a.md", "x")
        assert candidate_pairs([one]) == []
        assert planned_pair_count([one]) == 0

    def test_pair_count_matches_n_choose_2(self, tmp_path: Path) -> None:
        members = [_write_am(tmp_path, f"m{i}.md", f"body {i}") for i in range(4)]
        pairs = candidate_pairs(members)
        # C(4, 2) == 6, every pair distinct and unordered.
        assert len(pairs) == 6
        assert planned_pair_count(members) == 6
        seen = {frozenset((a.path.name, b.path.name)) for a, b in pairs}
        assert len(seen) == 6

    def test_planned_pair_count_needs_no_client(self, tmp_path: Path) -> None:
        """The whole point of AC4 (sizing without model spend): computing the
        count must not touch a client at all -- there is no ``client``
        parameter on this function."""
        members = [_write_am(tmp_path, f"m{i}.md", f"body {i}") for i in range(5)]
        assert planned_pair_count(members) == 10


# ---------------------------------------------------------------------------
# run_cluster_comparator -- gated driver
# ---------------------------------------------------------------------------


class TestRunClusterComparatorGateOff:
    def test_gate_off_by_default_records_pair_count_and_makes_no_call(
        self, tmp_path: Path
    ) -> None:
        members = [_write_am(tmp_path, f"m{i}.md", f"body {i}") for i in range(3)]
        client = MagicMock()

        result = run_cluster_comparator(members, client, cluster_id="c1")

        assert isinstance(result, ClusterComparatorResult)
        assert result.cluster_id == "c1"
        assert result.pair_count == 3  # C(3, 2)
        assert result.gate_enabled is False
        assert result.outcomes == []
        client.messages.create.assert_not_called()

    def test_gate_explicitly_off_makes_no_call(self, tmp_path: Path) -> None:
        members = [_write_am(tmp_path, f"m{i}.md", f"body {i}") for i in range(2)]
        client = MagicMock()

        result = run_cluster_comparator(members, client, config=_AUTO_OFF)

        assert result.gate_enabled is False
        assert result.pair_count == 1
        assert result.outcomes == []
        client.messages.create.assert_not_called()

    def test_gate_off_never_touches_member_content(self, tmp_path: Path) -> None:
        """Proves the "no adapter call at all" half of AC4/AC3 -- not just
        "no LLM call". A member whose file is missing would raise on
        ``.content`` if the driver touched it; the gate-off path must never
        reach that far."""
        missing = AutoMemoryFile(
            path=tmp_path / "does-not-exist.md",
            origin_scope="scope-x",
            memory_type="feedback",
            name="does-not-exist.md",
        )
        other = _write_am(tmp_path, "present.md", "hi")

        result = run_cluster_comparator([missing, other], MagicMock())

        assert result.gate_enabled is False
        assert result.pair_count == 1
        assert result.outcomes == []

    def test_fewer_than_two_members_with_gate_on_makes_no_call(self, tmp_path: Path) -> None:
        one = _write_am(tmp_path, "solo.md", "just one")
        client = MagicMock()

        result = run_cluster_comparator([one], client, config=_AUTO_ON)

        assert result.gate_enabled is True
        assert result.pair_count == 0
        assert result.outcomes == []
        client.messages.create.assert_not_called()


class TestRunClusterComparatorGateOn:
    def test_gate_on_runs_compare_pages_over_every_pair(self, tmp_path: Path) -> None:
        members = [_write_am(tmp_path, f"m{i}.md", f"distinct body {i}") for i in range(3)]
        client = _fake_client(ContentRelation.COMPATIBLE)

        result = run_cluster_comparator(members, client, config=_AUTO_ON, cluster_id="c2")

        assert result.gate_enabled is True
        assert result.pair_count == 3
        assert len(result.outcomes) == 3
        assert client.messages.create.call_count == 3
        expected_ids = {page_id_for_path(m.path) for m in members}
        seen_ids: set[str] = set()
        for id_a, id_b, outcome in result.outcomes:
            seen_ids.update((id_a, id_b))
            assert outcome.verdict is not None
        assert seen_ids == expected_ids

    def test_outcomes_carry_the_adapter_ids_not_paths(self, tmp_path: Path) -> None:
        a = _write_am(tmp_path, "alpha.md", "text a")
        b = _write_am(tmp_path, "beta.md", "text b")
        client = _fake_client(ContentRelation.COMPATIBLE)

        result = run_cluster_comparator([a, b], client, config=_AUTO_ON)

        assert len(result.outcomes) == 1
        id_a, id_b, _outcome = result.outcomes[0]
        assert {id_a, id_b} == {page_id_for_path(a.path), page_id_for_path(b.path)}

    def test_accepts_usage_accumulator_without_error(self, tmp_path: Path) -> None:
        a = _write_am(tmp_path, "alpha.md", "text a")
        b = _write_am(tmp_path, "beta.md", "text b")
        client = _fake_client(ContentRelation.COMPATIBLE)
        usage = TokenUsage()

        run_cluster_comparator([a, b], client, config=_AUTO_ON, usage=usage)
        # No exception is the assertion; exact token counts are Gate 2's own
        # contract (tests/test_comparator.py), not this driver's.

    def test_client_none_degrades_without_raising(self, tmp_path: Path) -> None:
        """``compare_pages`` never raises for an unavailable client -- the
        driver must pass that posture through unchanged."""
        a = _write_am(tmp_path, "alpha.md", "text a")
        b = _write_am(tmp_path, "beta.md", "text b")

        result = run_cluster_comparator([a, b], None, config=_AUTO_ON)

        assert result.pair_count == 1
        assert len(result.outcomes) == 1
        _id_a, _id_b, outcome = result.outcomes[0]
        assert outcome.verdict is None


# ---------------------------------------------------------------------------
# ClusterComparatorResult.to_row -- observability shape
# ---------------------------------------------------------------------------


class TestClusterComparatorResultToRow:
    def test_to_row_gate_off_shape(self) -> None:
        result = ClusterComparatorResult(cluster_id="c3", pair_count=6, gate_enabled=False)
        row = result.to_row()
        assert row == {
            "cluster_id": "c3",
            "pair_count": 6,
            "gate_enabled": False,
            "outcomes": [],
        }

    def test_to_row_gate_on_shape(self, tmp_path: Path) -> None:
        a = _write_am(tmp_path, "alpha.md", "text a")
        b = _write_am(tmp_path, "beta.md", "text b")
        client = _fake_client(ContentRelation.COMPATIBLE)

        result = run_cluster_comparator([a, b], client, config=_AUTO_ON, cluster_id="c4")
        row = result.to_row()

        assert row["cluster_id"] == "c4"
        assert row["pair_count"] == 1
        assert row["gate_enabled"] is True
        assert len(row["outcomes"]) == 1
        entry = row["outcomes"][0]
        assert set(entry) == {"a", "b", "verdict"}
