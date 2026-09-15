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

import pytest

from athenaeum.cluster_comparator import (
    ClusterComparatorResult,
    auto_memory_root,
    candidate_pairs,
    page_from_auto_memory_file,
    planned_pair_count,
    run_cluster_comparator,
)
from athenaeum.comparator import ContentRelation
from athenaeum.models import AutoMemoryFile, TokenUsage
from athenaeum.runlock import RunLock
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

        assert page.id == page_id_for_path(am.path, root=auto_memory_root(am))
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

        Root-relative, not bare-stem (athenaeum#1677): the id folds in the
        member's ``origin_scope`` path segment via
        :func:`~athenaeum.cluster_comparator.auto_memory_root`, rather than
        being just the bare filename stem -- the bare-stem shape is exactly
        the collision athenaeum#1677 fixed (see
        ``test_distinct_origin_scopes_get_distinct_ids`` below).
        """
        root = tmp_path / "raw" / "auto-memory"
        am = _write_am(root / "scope-x", "project_widget.md", "some claim")
        page = page_from_auto_memory_file(am)
        assert page.id == "scope-x-project-widget"
        assert page.id == page_id_for_path(am.path, root=root)
        # Not the bare-stem id -- that's the shape this test used to pin.
        assert page.id != page_id_for_path(am.path)

    def test_distinct_origin_scopes_get_distinct_ids(self, tmp_path: Path) -> None:
        """athenaeum#1677: two members with the SAME filename stem but
        DIFFERENT ``origin_scope`` under one corpus root must resolve to
        DIFFERENT page ids. Before the fix, both adapted to the bare stem
        ``"project-widget"`` and collided onto one
        :func:`~athenaeum.verdicts.make_pair_key` pairing -- the exact
        hazard the issue's downstream retire-lane read depends on this
        module NOT reproducing.
        """
        root = tmp_path / "raw" / "auto-memory"
        am_a = _write_am(
            root / "scope-a", "project_widget.md", "claim a", origin_scope="scope-a"
        )
        am_b = _write_am(
            root / "scope-b", "project_widget.md", "claim b", origin_scope="scope-b"
        )

        page_a = page_from_auto_memory_file(am_a)
        page_b = page_from_auto_memory_file(am_b)

        assert page_a.id != page_b.id
        assert page_a.id == page_id_for_path(am_a.path, root=root)
        assert page_b.id == page_id_for_path(am_b.path, root=root)

    def test_same_long_scope_different_stems_get_different_ids(
        self, tmp_path: Path
    ) -> None:
        """athenaeum#1677 follow-up: on the live corpus, ``origin_scope`` is
        frequently a full path-hash identifier 45-60+ characters long --
        long enough on its own to hit :func:`~athenaeum.models.slugify`'s
        60-char cap, truncating away the stem entirely and silently
        re-colliding two DIFFERENT members of the SAME scope onto one id.
        End-to-end via the real adapter (not :func:`page_id_for_path`
        directly): two members sharing one long ``origin_scope`` but
        different filename stems must resolve to different page ids.
        """
        long_scope = "users-tristankromer-code-kromatic-project-good-reads-newslet"
        root = tmp_path / "raw" / "auto-memory"
        am_a = _write_am(
            root / long_scope,
            "hestia_lock_drops_silently.md",
            "claim a",
            origin_scope=long_scope,
        )
        am_b = _write_am(
            root / long_scope, "MEMORY.md", "claim b", origin_scope=long_scope
        )

        page_a = page_from_auto_memory_file(am_a)
        page_b = page_from_auto_memory_file(am_b)

        assert page_a.id != page_b.id

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

        with RunLock(tmp_path) as lock:
            result = run_cluster_comparator(
                members,
                client,
                config=_AUTO_ON,
                cluster_id="c2",
                wiki_root=tmp_path,
                lock=lock,
            )

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

        with RunLock(tmp_path) as lock:
            result = run_cluster_comparator(
                [a, b], client, config=_AUTO_ON, wiki_root=tmp_path, lock=lock
            )

        assert len(result.outcomes) == 1
        id_a, id_b, _outcome = result.outcomes[0]
        assert {id_a, id_b} == {page_id_for_path(a.path), page_id_for_path(b.path)}

    def test_accepts_usage_accumulator_without_error(self, tmp_path: Path) -> None:
        a = _write_am(tmp_path, "alpha.md", "text a")
        b = _write_am(tmp_path, "beta.md", "text b")
        client = _fake_client(ContentRelation.COMPATIBLE)
        usage = TokenUsage()

        with RunLock(tmp_path) as lock:
            run_cluster_comparator(
                [a, b], client, config=_AUTO_ON, usage=usage, wiki_root=tmp_path, lock=lock
            )
        # No exception is the assertion; exact token counts are Gate 2's own
        # contract (tests/test_comparator.py), not this driver's.

    def test_client_none_degrades_without_raising(self, tmp_path: Path) -> None:
        """``compare_pages`` never raises for an unavailable client --
        ``record_comparison`` reports it as ``ok=False`` (Gate 2
        unavailable) rather than a fabricated verdict, and this driver
        surfaces that as an ``unresolved`` entry rather than raising or
        silently dropping the pair (issue athenaeum#1678)."""
        a = _write_am(tmp_path, "alpha.md", "text a")
        b = _write_am(tmp_path, "beta.md", "text b")

        with RunLock(tmp_path) as lock:
            result = run_cluster_comparator(
                [a, b], None, config=_AUTO_ON, wiki_root=tmp_path, lock=lock
            )

        assert result.pair_count == 1
        assert result.outcomes == []
        assert len(result.unresolved) == 1
        id_a, id_b, reason = result.unresolved[0]
        assert {id_a, id_b} == {page_id_for_path(a.path), page_id_for_path(b.path)}
        assert reason


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
            # athenaeum#1257: the T1 screen's drop list, always present and
            # empty unless a ClusterScreenContext armed the screen.
            "screened_out": [],
            # athenaeum#1678: memoized-fresh and no-verdict pairs, always
            # present and empty when the gate never ran.
            "memoised": [],
            "unresolved": [],
        }

    def test_to_row_gate_on_shape(self, tmp_path: Path) -> None:
        a = _write_am(tmp_path, "alpha.md", "text a")
        b = _write_am(tmp_path, "beta.md", "text b")
        client = _fake_client(ContentRelation.COMPATIBLE)

        with RunLock(tmp_path) as lock:
            result = run_cluster_comparator(
                [a, b], client, config=_AUTO_ON, cluster_id="c4", wiki_root=tmp_path, lock=lock
            )
        row = result.to_row()

        assert row["cluster_id"] == "c4"
        assert row["pair_count"] == 1
        assert row["gate_enabled"] is True
        assert len(row["outcomes"]) == 1
        entry = row["outcomes"][0]
        assert set(entry) == {"a", "b", "verdict"}
        assert row["memoised"] == []
        assert row["unresolved"] == []


# ---------------------------------------------------------------------------
# Memoization -- record_comparison wiring (issue athenaeum#1678)
# ---------------------------------------------------------------------------


class TestClusterComparatorMemoization:
    def test_same_pair_compared_twice_in_one_run_is_memoized(self, tmp_path: Path) -> None:
        """AC4: a cluster-domain pair compared twice in the SAME run (same
        ids, same content) must hit the ``skipped="fresh"`` path the
        second time -- i.e. actually memoized via the verdict ledger, not
        merely routed through ``record_comparison`` once and forgotten.

        Two ``run_cluster_comparator`` calls sharing one caller-acquired
        ``RunLock`` and the same ``wiki_root`` count as "the same run" for
        memoization purposes (see the function's own docstring): the
        ledger lives on disk under ``wiki_root``, not in the lock object,
        so what makes the second call see the first call's verdict is the
        SAME ``wiki_root`` -- the shared lock only proves the single-
        appender contract is satisfiable across repeated calls.
        """
        a = _write_am(tmp_path, "alpha.md", "text a")
        b = _write_am(tmp_path, "beta.md", "text b")
        client = _fake_client(ContentRelation.COMPATIBLE)

        with RunLock(tmp_path) as lock:
            first = run_cluster_comparator(
                [a, b], client, config=_AUTO_ON, wiki_root=tmp_path, lock=lock
            )
            assert len(first.outcomes) == 1
            assert first.memoised == []
            first_calls = client.messages.create.call_count
            assert first_calls == 1
            first_verdict = first.outcomes[0][2].verdict
            assert first_verdict is not None

            second = run_cluster_comparator(
                [a, b], client, config=_AUTO_ON, wiki_root=tmp_path, lock=lock
            )

        # Memoized: no fresh CompareOutcome, no second LLM dispatch, and the
        # reused verdict matches what the first call actually decided.
        assert second.outcomes == []
        assert second.unresolved == []
        assert len(second.memoised) == 1
        id_a, id_b, memoised_verdict = second.memoised[0]
        assert {id_a, id_b} == {page_id_for_path(a.path), page_id_for_path(b.path)}
        assert memoised_verdict == first_verdict
        assert client.messages.create.call_count == first_calls  # no new dispatch

    def test_wiki_root_required_once_a_pair_reaches_record_comparison(
        self, tmp_path: Path
    ) -> None:
        """Without wiki_root/lock, a pair that survives to the comparison
        step raises rather than silently falling back to an unrecorded
        ``compare_pages`` call (issue athenaeum#1678's single-appender
        contract -- see run_cluster_comparator's docstring)."""
        a = _write_am(tmp_path, "alpha.md", "text a")
        b = _write_am(tmp_path, "beta.md", "text b")
        client = _fake_client(ContentRelation.COMPATIBLE)

        with pytest.raises(ValueError, match="wiki_root"):
            run_cluster_comparator([a, b], client, config=_AUTO_ON)
