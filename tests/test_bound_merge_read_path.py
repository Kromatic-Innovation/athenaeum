# SPDX-License-Identifier: Apache-2.0
"""Tests for bounding the merge read path (issue #431).

Complements the #400 write-path suppression (``resolve_max_merge_sources`` /
``resolve_min_merge_mean_similarity`` keep a degenerate over-cluster from ever
reaching ``_pending_merges.md``). This closes two READ-path gaps:

1. ``list_pending_merges`` returned ``draft_merged_body`` in full, unbounded
   — a single oversized pending merge (the withdrawn runaway that prompted
   this issue had a ~878 KB draft body) blew out the payload.
2. The decisions view (``merge_to_decision`` / ``list_pending_decisions``)
   rendered EVERY source of a merge with no cap.

Both bounds are config-resolvable (env > yaml > default), mirroring the
existing ``librarian.*`` resolver pattern (``resolve_page_warn_bytes`` /
``resolve_page_flag_bytes``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.config import (
    _DEFAULTS,
    resolve_decisions_max_sources_per_merge,
    resolve_merge_body_preview_chars,
)
from athenaeum.decisions import merge_to_decision
from athenaeum.pending_merges import (
    PendingMerge,
    list_pending_merges,
    write_pending_merge,
)


def _write_source(path: Path, *, name: str, body: str = "body\n") -> None:
    path.write_text(
        "---\n" f"name: {name}\n" "type: feedback\n" "---\n" f"{body}",
        encoding="utf-8",
    )


def _make_pending_merge(**overrides: object) -> PendingMerge:
    defaults: dict = {
        "id": "abc123",
        "merge_target_name": "target",
        "sources": ["/tmp/a.md", "/tmp/b.md"],
        "rationale": "similar",
        "draft_merged_body": "small body",
        "confidence": 0.8,
        "created_at": "2026-07-01",
        "resolved": False,
        "raw_block": "",
    }
    defaults.update(overrides)
    return PendingMerge(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC1 — list_pending_merges bounds draft_merged_body, full body on demand.
# ---------------------------------------------------------------------------


class TestListPendingMergesBoundsDraftBody:
    def test_oversized_body_is_truncated_by_default(self, tmp_path: Path) -> None:
        merges = tmp_path / "_pending_merges.md"
        src_a = tmp_path / "feedback_a.md"
        src_b = tmp_path / "feedback_b.md"
        _write_source(src_a, name="a")
        _write_source(src_b, name="b")

        # A multi-hundred-KB draft body (mirrors the withdrawn ~878 KB
        # runaway that prompted this issue).
        oversized_body = "x" * 400_000
        write_pending_merge(
            merges,
            merge_target_name="oversized-merge",
            sources=[str(src_a), str(src_b)],
            rationale="oversized",
            draft_merged_body=oversized_body,
            confidence=0.9,
        )

        listed = list_pending_merges(merges)
        assert len(listed) == 1
        item = listed[0]
        assert item["draft_merged_body_truncated"] is True
        assert item["draft_merged_body_full_length"] == len(oversized_body)
        # Bounded payload — default preview cap is 2000 chars.
        assert len(item["draft_merged_body"]) == 2000
        assert item["draft_merged_body"] == oversized_body[:2000]

    def test_full_body_retrievable_on_demand(self, tmp_path: Path) -> None:
        merges = tmp_path / "_pending_merges.md"
        src_a = tmp_path / "feedback_a.md"
        src_b = tmp_path / "feedback_b.md"
        _write_source(src_a, name="a")
        _write_source(src_b, name="b")

        oversized_body = "y" * 300_000
        write_pending_merge(
            merges,
            merge_target_name="oversized-merge-2",
            sources=[str(src_a), str(src_b)],
            rationale="oversized",
            draft_merged_body=oversized_body,
            confidence=0.9,
        )

        listed_full = list_pending_merges(merges, full_body=True)
        assert len(listed_full) == 1
        item = listed_full[0]
        assert item["draft_merged_body"] == oversized_body
        assert item["draft_merged_body_truncated"] is False
        assert item["draft_merged_body_full_length"] == len(oversized_body)

    def test_normal_sized_body_is_byte_identical(self, tmp_path: Path) -> None:
        """A small body must be unaffected by the cap (preserve existing behavior)."""
        merges = tmp_path / "_pending_merges.md"
        src_a = tmp_path / "feedback_a.md"
        src_b = tmp_path / "feedback_b.md"
        _write_source(src_a, name="a")
        _write_source(src_b, name="b")

        small_body = "A perfectly normal, short draft body.\n\nSecond paragraph."
        write_pending_merge(
            merges,
            merge_target_name="normal-merge",
            sources=[str(src_a), str(src_b)],
            rationale="normal",
            draft_merged_body=small_body,
            confidence=0.9,
        )

        listed = list_pending_merges(merges)
        assert len(listed) == 1
        item = listed[0]
        assert item["draft_merged_body"] == small_body
        assert item["draft_merged_body_truncated"] is False
        assert item["draft_merged_body_full_length"] == len(small_body)

    def test_config_resolvable_preview_length(self, tmp_path: Path) -> None:
        merges = tmp_path / "_pending_merges.md"
        src_a = tmp_path / "feedback_a.md"
        _write_source(src_a, name="a")

        body = "z" * 5000
        write_pending_merge(
            merges,
            merge_target_name="cfg-merge",
            sources=[str(src_a)],
            rationale="cfg",
            draft_merged_body=body,
            confidence=0.9,
        )

        config = {"librarian": {"merge_body_preview_chars": 100}}
        listed = list_pending_merges(merges, config=config)
        assert len(listed[0]["draft_merged_body"]) == 100
        assert listed[0]["draft_merged_body_truncated"] is True

    def test_env_overrides_yaml_preview_length(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        merges = tmp_path / "_pending_merges.md"
        src_a = tmp_path / "feedback_a.md"
        _write_source(src_a, name="a")

        body = "w" * 5000
        write_pending_merge(
            merges,
            merge_target_name="env-merge",
            sources=[str(src_a)],
            rationale="env",
            draft_merged_body=body,
            confidence=0.9,
        )

        monkeypatch.setenv("ATHENAEUM_MERGE_BODY_PREVIEW_CHARS", "50")
        config = {"librarian": {"merge_body_preview_chars": 100}}
        listed = list_pending_merges(merges, config=config)
        assert len(listed[0]["draft_merged_body"]) == 50


# ---------------------------------------------------------------------------
# Resolver unit tests — resolve_merge_body_preview_chars (env > yaml > default)
# ---------------------------------------------------------------------------


class TestResolveMergeBodyPreviewChars:
    def test_default(self) -> None:
        assert resolve_merge_body_preview_chars(None) == 2000
        assert resolve_merge_body_preview_chars({}) == 2000
        assert resolve_merge_body_preview_chars({"librarian": {}}) == 2000

    def test_yaml_value_wins(self) -> None:
        assert (
            resolve_merge_body_preview_chars(
                {"librarian": {"merge_body_preview_chars": 500}}
            )
            == 500
        )

    def test_bool_and_bad_and_nonpositive_fall_through(self) -> None:
        assert (
            resolve_merge_body_preview_chars(
                {"librarian": {"merge_body_preview_chars": True}}
            )
            == 2000
        )
        assert (
            resolve_merge_body_preview_chars(
                {"librarian": {"merge_body_preview_chars": "abc"}}
            )
            == 2000
        )
        assert (
            resolve_merge_body_preview_chars(
                {"librarian": {"merge_body_preview_chars": 0}}
            )
            == 2000
        )
        assert (
            resolve_merge_body_preview_chars(
                {"librarian": {"merge_body_preview_chars": -5}}
            )
            == 2000
        )

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_MERGE_BODY_PREVIEW_CHARS", "123")
        assert (
            resolve_merge_body_preview_chars(
                {"librarian": {"merge_body_preview_chars": 500}}
            )
            == 123
        )

    def test_not_seeded_in_defaults(self) -> None:
        assert "merge_body_preview_chars" not in _DEFAULTS.get("librarian", {})


# ---------------------------------------------------------------------------
# AC2 — decisions view caps rendered sources per merge with accurate remainder.
# ---------------------------------------------------------------------------


class TestMergeToDecisionCapsSources:
    def test_over_cap_sources_are_capped_with_accurate_remainder(self) -> None:
        sources = [f"/tmp/source_{i}.md" for i in range(35)]
        pm = _make_pending_merge(sources=sources)

        decision = merge_to_decision(pm, max_sources=20)
        payload = decision["payload"]
        assert len(payload["sources"]) == 20
        assert payload["sources_omitted"] == 15

    def test_under_cap_sources_are_unaffected(self) -> None:
        sources = [f"/tmp/source_{i}.md" for i in range(3)]
        pm = _make_pending_merge(sources=sources)

        decision = merge_to_decision(pm, max_sources=20)
        payload = decision["payload"]
        assert len(payload["sources"]) == 3
        assert payload["sources_omitted"] == 0

    def test_default_cap_is_20(self) -> None:
        sources = [f"/tmp/source_{i}.md" for i in range(25)]
        pm = _make_pending_merge(sources=sources)

        decision = merge_to_decision(pm)
        payload = decision["payload"]
        assert len(payload["sources"]) == 20
        assert payload["sources_omitted"] == 5

    def test_cap_disabled_when_nonpositive(self) -> None:
        sources = [f"/tmp/source_{i}.md" for i in range(25)]
        pm = _make_pending_merge(sources=sources)

        decision = merge_to_decision(pm, max_sources=0)
        payload = decision["payload"]
        assert len(payload["sources"]) == 25
        assert payload["sources_omitted"] == 0


class TestListPendingDecisionsThreadsSourceCap(object):
    def test_list_pending_decisions_caps_merge_sources(self, tmp_path: Path) -> None:
        from athenaeum.decisions import list_pending_decisions

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        merges_path = wiki_root / "_pending_merges.md"

        sources = []
        for i in range(30):
            src = tmp_path / f"feedback_source_{i}.md"
            _write_source(src, name=f"source-{i}")
            sources.append(str(src))

        write_pending_merge(
            merges_path,
            merge_target_name="big-fan-in",
            sources=sources,
            rationale="many sources",
            draft_merged_body="body",
            confidence=0.7,
        )

        decisions = list_pending_decisions(wiki_root, max_sources_per_merge=20)
        assert len(decisions) == 1
        payload = decisions[0]["payload"]
        assert len(payload["sources"]) == 20
        assert payload["sources_omitted"] == 10

    def test_default_call_still_works_unbounded_arg_omitted(
        self, tmp_path: Path
    ) -> None:
        """Callers that don't pass max_sources_per_merge get the built-in default."""
        from athenaeum.decisions import list_pending_decisions

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        merges_path = wiki_root / "_pending_merges.md"
        src_a = tmp_path / "feedback_a.md"
        src_b = tmp_path / "feedback_b.md"
        _write_source(src_a, name="a")
        _write_source(src_b, name="b")

        write_pending_merge(
            merges_path,
            merge_target_name="small-merge",
            sources=[str(src_a), str(src_b)],
            rationale="small",
            draft_merged_body="body",
            confidence=0.7,
        )

        decisions = list_pending_decisions(wiki_root)
        assert len(decisions) == 1
        payload = decisions[0]["payload"]
        assert len(payload["sources"]) == 2
        assert payload["sources_omitted"] == 0


# ---------------------------------------------------------------------------
# Resolver unit tests — resolve_decisions_max_sources_per_merge (env>yaml>default)
# ---------------------------------------------------------------------------


class TestResolveDecisionsMaxSourcesPerMerge:
    def test_default(self) -> None:
        assert resolve_decisions_max_sources_per_merge(None) == 20
        assert resolve_decisions_max_sources_per_merge({}) == 20
        assert resolve_decisions_max_sources_per_merge({"librarian": {}}) == 20

    def test_yaml_value_wins(self) -> None:
        assert (
            resolve_decisions_max_sources_per_merge(
                {"librarian": {"decisions_max_sources_per_merge": 5}}
            )
            == 5
        )

    def test_bool_and_bad_and_nonpositive_fall_through(self) -> None:
        assert (
            resolve_decisions_max_sources_per_merge(
                {"librarian": {"decisions_max_sources_per_merge": True}}
            )
            == 20
        )
        assert (
            resolve_decisions_max_sources_per_merge(
                {"librarian": {"decisions_max_sources_per_merge": "abc"}}
            )
            == 20
        )
        assert (
            resolve_decisions_max_sources_per_merge(
                {"librarian": {"decisions_max_sources_per_merge": 0}}
            )
            == 20
        )
        assert (
            resolve_decisions_max_sources_per_merge(
                {"librarian": {"decisions_max_sources_per_merge": -1}}
            )
            == 20
        )

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_DECISIONS_MAX_SOURCES_PER_MERGE", "7")
        assert (
            resolve_decisions_max_sources_per_merge(
                {"librarian": {"decisions_max_sources_per_merge": 5}}
            )
            == 7
        )

    def test_not_seeded_in_defaults(self) -> None:
        assert "decisions_max_sources_per_merge" not in _DEFAULTS.get("librarian", {})


# ---------------------------------------------------------------------------
# CLI formatting — "and N more" remainder marker.
# ---------------------------------------------------------------------------


class TestCliFormatsRemainderMarker:
    def test_format_block_shows_and_n_more(self) -> None:
        from athenaeum._cmd_decisions import _format_block

        decision = {
            "type": "merge",
            "id": "abc123",
            "created_at": "2026-07-01",
            "summary": "Merge these 25 pages?",
            "confidence": 0.8,
            "payload": {
                "merge_target_name": "target",
                "rationale": "r",
                "sources": [
                    {"path": f"/tmp/s{i}.md", "title": f"s{i}", "gist": ""}
                    for i in range(20)
                ],
                "sources_omitted": 5,
            },
        }
        rendered = _format_block(decision)
        assert "… and 5 more" in rendered

    def test_format_block_omits_marker_when_nothing_omitted(self) -> None:
        from athenaeum._cmd_decisions import _format_block

        decision = {
            "type": "merge",
            "id": "abc123",
            "created_at": "2026-07-01",
            "summary": "Merge these 2 pages?",
            "confidence": 0.8,
            "payload": {
                "merge_target_name": "target",
                "rationale": "r",
                "sources": [
                    {"path": "/tmp/s0.md", "title": "s0", "gist": ""},
                    {"path": "/tmp/s1.md", "title": "s1", "gist": ""},
                ],
                "sources_omitted": 0,
            },
        }
        rendered = _format_block(decision)
        assert "more" not in rendered
