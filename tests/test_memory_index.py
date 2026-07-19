"""Tests for per-scope MEMORY.md index maintenance (issue #388).

Covers the two layers:
  - the pure parsing primitives (:func:`index_line_target`,
    :func:`rewrite_index`) that the retire pass uses inline;
  - the one-shot backfill (:func:`build_dangling_report`,
    :func:`apply_prune_index`) exposed as ``auto-memory prune-index``.

Acceptance:
  - only *dangling sibling* pointers are dropped; cross-tree links, URLs,
    anchors, prose and headings survive verbatim;
  - the backfill removes ONLY dangling pointers in one git commit, scoped by
    pathspec, and is git-recoverable;
  - a scope whose index is fully valid is byte-unaffected.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum.memory_index import (
    apply_prune_index,
    build_dangling_report,
    index_line_target,
    rewrite_index,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Index Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial: seed indexes")


# ---------------------------------------------------------------------------
# index_line_target — which markdown links count as sibling pointers
# ---------------------------------------------------------------------------


class TestIndexLineTarget:
    def test_bare_sibling_pointer(self) -> None:
        assert (
            index_line_target("- [Berlin](user_x_berlin.md) — hook")
            == "user_x_berlin.md"
        )

    def test_no_link_returns_none(self) -> None:
        assert index_line_target("# Memory Index") is None
        assert index_line_target("") is None
        assert index_line_target("- just prose, no link") is None

    def test_cross_tree_link_is_not_a_sibling_pointer(self) -> None:
        # A link into the wiki tree (or any path with a separator) is left
        # alone — retirement only removes SIBLING members.
        assert index_line_target("- [Wiki](../../wiki/auto-x.md) — hook") is None
        assert index_line_target("- [Sub](sub/dir/x.md)") is None

    def test_url_and_anchor_rejected(self) -> None:
        assert index_line_target("- [Ext](https://example.com/x.md)") is None
        assert index_line_target("- [Frag](user_x.md#section)") is None

    def test_first_link_on_line_wins(self) -> None:
        assert index_line_target("- [A](a.md) and [B](b.md)") == "a.md"


# ---------------------------------------------------------------------------
# rewrite_index — line-level drop preserving everything else
# ---------------------------------------------------------------------------


class TestRewriteIndex:
    def test_drops_only_matching_lines(self) -> None:
        text = (
            "# Memory Index\n"
            "- [Gone](gone.md) — dangling\n"
            "- [Keep](keep.md) — live\n"
        )
        new_text, dropped = rewrite_index(text, {"gone.md"}.__contains__)
        assert dropped == ["gone.md"]
        assert new_text == "# Memory Index\n- [Keep](keep.md) — live\n"

    def test_non_pointer_lines_preserved(self) -> None:
        text = "# Heading\n\nprose paragraph\n- [Keep](keep.md)\n"
        new_text, dropped = rewrite_index(text, lambda _t: True)
        assert dropped == ["keep.md"]
        # Heading, blank line and prose all survive; only the pointer went.
        assert new_text == "# Heading\n\nprose paragraph\n"

    def test_preserves_missing_trailing_newline(self) -> None:
        text = "- [Keep](keep.md)\n- [Gone](gone.md)"  # no trailing newline
        new_text, dropped = rewrite_index(text, {"gone.md"}.__contains__)
        assert dropped == ["gone.md"]
        assert new_text == "- [Keep](keep.md)\n"

    def test_nothing_dropped_returns_input_unchanged(self) -> None:
        text = "- [A](a.md)\n- [B](b.md)\n"
        new_text, dropped = rewrite_index(text, {"z.md"}.__contains__)
        assert dropped == []
        assert new_text == text


# ---------------------------------------------------------------------------
# build_dangling_report — dangling = sibling target missing on disk
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge_with_dangling(tmp_path: Path) -> Path:
    kr = tmp_path / "knowledge"
    am = kr / "raw" / "auto-memory"

    # Scope A: one dangling (reference_gone.md absent) + one live pointer.
    a = am / "-Users-x-Code-a"
    a.mkdir(parents=True)
    (a / "MEMORY.md").write_text(
        "# Index\n"
        "- [Gone](reference_gone.md) — dangling\n"
        "- [Live](reference_live.md) — ok\n",
        encoding="utf-8",
    )
    (a / "reference_live.md").write_text("---\nname: l\n---\nlive\n", encoding="utf-8")

    # Scope B: two dangling pointers, both targets absent.
    b = am / "-Users-x-Code-b"
    b.mkdir(parents=True)
    (b / "MEMORY.md").write_text(
        "- [G1](feedback_g1.md)\n- [G2](project_g2.md)\n", encoding="utf-8"
    )

    # Scope C: fully valid index — must be byte-unaffected / omitted.
    c = am / "-Users-x-Code-c"
    c.mkdir(parents=True)
    (c / "MEMORY.md").write_text("- [OK](user_ok.md)\n", encoding="utf-8")
    (c / "user_ok.md").write_text("ok", encoding="utf-8")

    (kr / "wiki").mkdir(parents=True)
    _git_init(kr)
    return kr


def _intake_roots(kr: Path) -> list[Path]:
    return [kr / "raw" / "auto-memory"]


class TestBuildDanglingReport:
    def test_detects_dangling_across_scopes(self, knowledge_with_dangling: Path) -> None:
        report = build_dangling_report(_intake_roots(knowledge_with_dangling))
        assert report.scanned_indexes == 3
        assert report.total_dangling == 3
        by_scope = {s.index_path.parent.name: s for s in report.scopes}
        assert set(by_scope) == {"-Users-x-Code-a", "-Users-x-Code-b"}
        assert by_scope["-Users-x-Code-a"].dangling == ["reference_gone.md"]
        assert by_scope["-Users-x-Code-a"].total_pointers == 2

    def test_valid_scope_is_omitted(self, knowledge_with_dangling: Path) -> None:
        report = build_dangling_report(_intake_roots(knowledge_with_dangling))
        assert all(s.index_path.parent.name != "-Users-x-Code-c" for s in report.scopes)

    def test_missing_root_yields_empty(self, tmp_path: Path) -> None:
        report = build_dangling_report([tmp_path / "nope"])
        assert report.scopes == []
        assert report.total_dangling == 0


# ---------------------------------------------------------------------------
# apply_prune_index — rewrite + commit, scoped and recoverable
# ---------------------------------------------------------------------------


class TestApplyPruneIndex:
    def test_apply_rewrites_and_commits_once(self, knowledge_with_dangling: Path) -> None:
        kr = knowledge_with_dangling
        report = build_dangling_report(_intake_roots(kr))
        report = apply_prune_index(kr, report)

        assert report.committed is True
        a = kr / "raw" / "auto-memory" / "-Users-x-Code-a" / "MEMORY.md"
        assert a.read_text(encoding="utf-8") == "# Index\n- [Live](reference_live.md) — ok\n"
        b = kr / "raw" / "auto-memory" / "-Users-x-Code-b" / "MEMORY.md"
        assert b.read_text(encoding="utf-8") == ""

        # One labeled commit touching only the two rewritten indexes.
        show = _git(kr, "show", "--stat", "--format=%s", "HEAD")
        assert "prune 3 dangling MEMORY.md pointer(s)" in show.stdout
        assert "#388" in show.stdout
        assert _git(kr, "status", "--porcelain").stdout.strip() == ""

    def test_dangling_line_is_git_recoverable(self, knowledge_with_dangling: Path) -> None:
        kr = knowledge_with_dangling
        apply_prune_index(kr, build_dangling_report(_intake_roots(kr)))
        # The dropped pointer text survives in the pre-prune commit.
        prior = _git(
            kr, "show", "HEAD~1:raw/auto-memory/-Users-x-Code-a/MEMORY.md"
        )
        assert "reference_gone.md" in prior.stdout

    def test_refuses_without_git(self, tmp_path: Path) -> None:
        kr = tmp_path / "no-git"
        scope = kr / "raw" / "auto-memory" / "-Users-x-Code-a"
        scope.mkdir(parents=True)
        (scope / "MEMORY.md").write_text("- [Gone](gone.md)\n", encoding="utf-8")
        report = apply_prune_index(kr, build_dangling_report(_intake_roots(kr)))
        assert report.committed is False
        assert any("refusing to prune" in e for e in report.errors)
        # The index is left untouched — no git means no recoverable rewrite.
        assert (scope / "MEMORY.md").read_text(encoding="utf-8") == "- [Gone](gone.md)\n"

    def test_empty_report_is_noop(self, knowledge_with_dangling: Path) -> None:
        kr = knowledge_with_dangling
        # Prune once to clean, then a second pass finds nothing dangling.
        apply_prune_index(kr, build_dangling_report(_intake_roots(kr)))
        head = _git(kr, "rev-parse", "HEAD").stdout.strip()
        report2 = apply_prune_index(kr, build_dangling_report(_intake_roots(kr)))
        assert report2.committed is False
        assert _git(kr, "rev-parse", "HEAD").stdout.strip() == head

    def test_commit_is_scoped_and_ignores_unrelated_staged_change(
        self, knowledge_with_dangling: Path
    ) -> None:
        kr = knowledge_with_dangling
        # A pre-staged unrelated edit must NOT be swept into the prune commit.
        (kr / "wiki" / "unrelated.md").write_text("stray", encoding="utf-8")
        _git(kr, "add", "wiki/unrelated.md")
        apply_prune_index(kr, build_dangling_report(_intake_roots(kr)))
        head_files = _git(kr, "show", "--name-only", "--format=", "HEAD").stdout
        assert "unrelated.md" not in head_files
        # The stray file is still staged, not committed.
        assert "unrelated.md" in _git(kr, "status", "--porcelain").stdout
