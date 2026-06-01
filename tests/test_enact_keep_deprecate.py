# SPDX-License-Identifier: Apache-2.0
"""Issue #191: keep_a/keep_b/deprecate_both enact NON-DESTRUCTIVE markers.

PR #190 wired enactment for the destructive forget_*/correct_* verdicts
(delete the loser). #191 closes the remaining gap: keep_a/keep_b mark the
LOSING member ``superseded_by: <winner name>`` and deprecate_both marks BOTH
members ``deprecated: true``. Nothing is deleted; the markers make a member
inactive so the C3 compile + recall skip it.

These tests cover:

* ``enact_resolution`` (unit) for the three marking actions + the
  ``_mark_member_frontmatter`` helper (idempotence, body/key preservation).
* The threshold loader returns 0.90 for deprecate_both (was None).
* The C3 compile (``merge_cluster_row`` / ``merge_clusters_to_wiki``) excludes
  inactive members.
* Recall (FTS5 + keyword backends) never surfaces an inactive page.
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.merge import merge_cluster_row, merge_clusters_to_wiki
from athenaeum.models import parse_frontmatter
from athenaeum.resolutions import (
    ENACTING_ACTIONS,
    ResolutionProposal,
    _mark_member_frontmatter,
    enact_resolution,
    resolve_auto_apply_threshold_for,
)
from athenaeum.search import FTS5Backend, KeywordBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proposal(action: str, confidence: float = 0.95) -> ResolutionProposal:
    winner = {"keep_a": "a", "keep_b": "b"}.get(action, "neither")
    return ResolutionProposal(
        recommended_winner=winner,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        rationale=f"test-{action}",
        confidence=confidence,
        source_precedence_used=["a:user > b:unsourced"],
    )


def _member(path: Path, name: str, body: str = "the claim") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ntype: feedback\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# _mark_member_frontmatter (unit)
# ---------------------------------------------------------------------------


class TestMarkMemberFrontmatter:
    def test_sets_key_preserving_body_and_existing_keys(self, tmp_path: Path) -> None:
        p = _member(tmp_path / "m.md", "Mem A", body="line one\n\nline two")
        assert _mark_member_frontmatter(p, "superseded_by", "Mem B") is True
        meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        assert meta["name"] == "Mem A"
        assert meta["type"] == "feedback"
        assert meta["superseded_by"] == "Mem B"
        assert "line one" in body
        assert "line two" in body

    def test_idempotent(self, tmp_path: Path) -> None:
        p = _member(tmp_path / "m.md", "Mem A")
        assert _mark_member_frontmatter(p, "deprecated", True) is True
        first = p.read_text(encoding="utf-8")
        assert _mark_member_frontmatter(p, "deprecated", True) is True
        assert p.read_text(encoding="utf-8") == first

    def test_creates_frontmatter_when_absent(self, tmp_path: Path) -> None:
        p = tmp_path / "no_fm.md"
        p.write_text("bare body, no frontmatter\n", encoding="utf-8")
        assert _mark_member_frontmatter(p, "deprecated", True) is True
        meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        assert meta["deprecated"] is True
        assert "bare body" in body

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        assert _mark_member_frontmatter(tmp_path / "gone.md", "x", True) is False


# ---------------------------------------------------------------------------
# enact_resolution — marking branch (unit)
# ---------------------------------------------------------------------------


class TestEnactMarking:
    def test_enacting_actions_includes_marks(self) -> None:
        assert {"keep_a", "keep_b", "deprecate_both"} <= ENACTING_ACTIONS
        assert {"forget_a", "forget_b", "correct_a", "correct_b"} <= ENACTING_ACTIONS

    def test_keep_a_marks_loser_b_superseded_by_a(self, tmp_path: Path) -> None:
        a = _member(tmp_path / "a.md", "Winner A")
        b = _member(tmp_path / "b.md", "Loser B")
        ret = enact_resolution(_proposal("keep_a"), [a, b])
        assert ret == b
        # Nothing deleted.
        assert a.exists() and b.exists()
        meta_a, _ = parse_frontmatter(a.read_text(encoding="utf-8"))
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        # Loser b is marked; winner a is untouched.
        assert meta_b["superseded_by"] == "Winner A"
        assert "superseded_by" not in meta_a

    def test_keep_b_marks_loser_a_superseded_by_b(self, tmp_path: Path) -> None:
        a = _member(tmp_path / "a.md", "Loser A")
        b = _member(tmp_path / "b.md", "Winner B")
        ret = enact_resolution(_proposal("keep_b"), [a, b])
        assert ret == a
        assert a.exists() and b.exists()
        meta_a, _ = parse_frontmatter(a.read_text(encoding="utf-8"))
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        assert meta_a["superseded_by"] == "Winner B"
        assert "superseded_by" not in meta_b

    def test_keep_a_falls_back_to_stem_when_winner_unnamed(
        self, tmp_path: Path
    ) -> None:
        a = tmp_path / "winner_stem.md"
        a.write_text("no frontmatter\n", encoding="utf-8")
        b = _member(tmp_path / "b.md", "Loser B")
        enact_resolution(_proposal("keep_a"), [a, b])
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        assert meta_b["superseded_by"] == "winner_stem"

    def test_deprecate_both_marks_both(self, tmp_path: Path) -> None:
        a = _member(tmp_path / "a.md", "Mem A")
        b = _member(tmp_path / "b.md", "Mem B")
        ret = enact_resolution(_proposal("deprecate_both"), [a, b])
        assert ret == a  # first path returned; both marked as side effect
        assert a.exists() and b.exists()
        meta_a, _ = parse_frontmatter(a.read_text(encoding="utf-8"))
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        assert meta_a["deprecated"] is True
        assert meta_b["deprecated"] is True

    def test_keep_short_member_list_no_crash(self, tmp_path: Path) -> None:
        a = _member(tmp_path / "a.md", "A")
        assert enact_resolution(_proposal("keep_a"), [a]) is None

    def test_deprecate_both_requires_two_paths(self, tmp_path: Path) -> None:
        a = _member(tmp_path / "a.md", "A")
        assert enact_resolution(_proposal("deprecate_both"), [a]) is None
        # The single member is not marked.
        meta_a, _ = parse_frontmatter(a.read_text(encoding="utf-8"))
        assert "deprecated" not in meta_a


# ---------------------------------------------------------------------------
# Threshold loader
# ---------------------------------------------------------------------------


class TestThresholds:
    def test_deprecate_both_default_threshold(self) -> None:
        assert resolve_auto_apply_threshold_for(None, "deprecate_both") == 0.90

    def test_keep_thresholds_unchanged(self) -> None:
        assert resolve_auto_apply_threshold_for(None, "keep_a") == 0.90
        assert resolve_auto_apply_threshold_for(None, "keep_b") == 0.90


# ---------------------------------------------------------------------------
# C3 merge honors markers
# ---------------------------------------------------------------------------


def _knowledge_with_cluster(
    tmp_path: Path, members: list[tuple[str, str]]
) -> tuple[Path, list[str]]:
    """Build a knowledge tree with one cluster row covering ``members``.

    ``members`` is a list of (filename, full-text). Returns the knowledge
    root and the scope-relative member paths used in the cluster JSONL.
    """
    knowledge_root = tmp_path / "knowledge"
    scope = knowledge_root / "raw" / "auto-memory" / "scope-x"
    scope.mkdir(parents=True, exist_ok=True)
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    member_paths: list[str] = []
    for fname, text in members:
        (scope / fname).write_text(text, encoding="utf-8")
        member_paths.append(f"scope-x/{fname}")
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.write_text(
        json.dumps(
            {
                "cluster_id": "scope-x-0001",
                "member_paths": member_paths,
                "centroid_score": 0.95,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return knowledge_root, member_paths


class TestMergeHonorsMarkers:
    def test_superseded_member_excluded_from_compile(self, tmp_path: Path) -> None:
        root, _ = _knowledge_with_cluster(
            tmp_path,
            [
                (
                    "feedback_active.md",
                    "---\nname: Active Rule\ntype: feedback\n---\n\n"
                    "ACTIVELIVECLAIM body.\n",
                ),
                (
                    "feedback_superseded.md",
                    "---\nname: Old Rule\ntype: feedback\n"
                    "superseded_by: Active Rule\n---\n\n"
                    "SUPERSEDEDCLAIM body.\n",
                ),
            ],
        )
        entries = merge_clusters_to_wiki(root)
        assert len(entries) == 1
        body = entries[0].body
        assert "ACTIVELIVECLAIM" in body
        assert "SUPERSEDEDCLAIM" not in body
        # Only the active member survived into resolved_members.
        assert len(entries[0].resolved_members) == 1

    def test_all_inactive_skips_row(self, tmp_path: Path) -> None:
        root, _ = _knowledge_with_cluster(
            tmp_path,
            [
                (
                    "feedback_dep_a.md",
                    "---\nname: A\ntype: feedback\ndeprecated: true\n---\n\nA body.\n",
                ),
                (
                    "feedback_dep_b.md",
                    "---\nname: B\ntype: feedback\ndeprecated: true\n---\n\nB body.\n",
                ),
            ],
        )
        entries = merge_clusters_to_wiki(root)
        # Every member inactive → no live claim → row skipped.
        assert entries == []

    def test_merge_cluster_row_excludes_deprecated(self, tmp_path: Path) -> None:
        scope = tmp_path / "scope-x"
        scope.mkdir(parents=True, exist_ok=True)
        (scope / "active.md").write_text(
            "---\nname: Active\ntype: feedback\n---\n\nKEEPME body.\n",
            encoding="utf-8",
        )
        (scope / "dep.md").write_text(
            "---\nname: Dep\ntype: feedback\ndeprecated: true\n---\n\nDROPME body.\n",
            encoding="utf-8",
        )
        row = {
            "cluster_id": "c1",
            "member_paths": ["scope-x/active.md", "scope-x/dep.md"],
            "centroid_score": 0.95,
        }
        entry = merge_cluster_row(row, extra_roots=[tmp_path], am_by_path={})
        assert entry is not None
        assert "KEEPME" in entry.body
        assert "DROPME" not in entry.body
        assert len(entry.resolved_members) == 1


# ---------------------------------------------------------------------------
# Recall honors markers
# ---------------------------------------------------------------------------


def _recall_wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "active-topic.md").write_text(
        "---\nname: Active Topic\n"
        "description: A live recallable rule about widgets\n---\n\n"
        "Widgets should be assembled clockwise.\n",
        encoding="utf-8",
    )
    (wiki / "superseded-topic.md").write_text(
        "---\nname: Superseded Topic\n"
        "description: An old rule about widgets\n"
        "superseded_by: Active Topic\n---\n\n"
        "Widgets should be assembled counterclockwise.\n",
        encoding="utf-8",
    )
    (wiki / "deprecated-topic.md").write_text(
        "---\nname: Deprecated Topic\n"
        "description: A stale rule about widgets\n"
        "deprecated: true\n---\n\n"
        "Widgets are obsolete.\n",
        encoding="utf-8",
    )
    return wiki


class TestRecallHonorsMarkers:
    def test_fts5_excludes_inactive(self, tmp_path: Path) -> None:
        wiki = _recall_wiki(tmp_path)
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        count = backend.build_index(wiki, cache)
        # Only the active page is indexed.
        assert count == 1
        results = backend.query("widgets rule", cache, n=10)
        filenames = [r[0] for r in results]
        assert "active-topic.md" in filenames
        assert "superseded-topic.md" not in filenames
        assert "deprecated-topic.md" not in filenames

    def test_keyword_excludes_inactive(self, tmp_path: Path) -> None:
        wiki = _recall_wiki(tmp_path)
        cache = tmp_path / "cache"
        backend = KeywordBackend()
        results = backend.query("widgets rule", cache, n=10, wiki_root=wiki)
        filenames = [r[0] for r in results]
        assert any(f.endswith("active-topic.md") for f in filenames)
        assert not any(f.endswith("superseded-topic.md") for f in filenames)
        assert not any(f.endswith("deprecated-topic.md") for f in filenames)
