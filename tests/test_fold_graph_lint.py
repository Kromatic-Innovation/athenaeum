# SPDX-License-Identifier: Apache-2.0
"""Tests for the read-only fold-graph invariant lint (issue athenaeum#716).

Two invariants over the tombstone ``folded_into`` edge set: acyclic, and
exactly one live canonical per connected fold set. See
:mod:`athenaeum.fold_graph_lint`'s module docstring for the full contract.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.fold_graph_lint import (
    VIOLATION_AMBIGUOUS_LIVE_CANONICAL,
    VIOLATION_CYCLE,
    VIOLATION_MISSING_LIVE_CANONICAL,
    _find_canonical_violations,
    scan_fold_graph,
)
from athenaeum.models import render_frontmatter, stamp_tombstone


def _write_live(path: Path, *, name: str, body: str = "body\n") -> None:
    path.write_text(f"---\nname: {name}\n---\n{body}", encoding="utf-8")


def _write_tombstone(path: Path, *, name: str, folded_into: str, body: str = "body\n") -> None:
    meta = stamp_tombstone({"name": name}, folded_into)
    path.write_text(render_frontmatter(meta) + body, encoding="utf-8")


class TestCleanCorpus:
    def test_no_tombstones_is_clean(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_live(wiki / "a.md", name="A")
        _write_live(wiki / "b.md", name="B")
        report = scan_fold_graph(wiki)
        assert report.ok
        assert report.pages_scanned == 2
        assert report.tombstones == 0

    def test_one_clean_fold_set_is_clean(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_live(wiki / "canonical.md", name="Canonical")
        _write_tombstone(wiki / "source.md", name="Source", folded_into="canonical")
        report = scan_fold_graph(wiki)
        assert report.ok
        assert report.tombstones == 1

    def test_chained_fold_through_two_tombstones_is_clean(self, tmp_path: Path) -> None:
        """source -> intermediate -> canonical, where intermediate was
        ITSELF later folded onward. Still exactly one live member."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_live(wiki / "canonical.md", name="Canonical")
        _write_tombstone(wiki / "intermediate.md", name="Intermediate", folded_into="canonical")
        _write_tombstone(wiki / "source.md", name="Source", folded_into="intermediate")
        report = scan_fold_graph(wiki)
        assert report.ok

    def test_underscore_prefixed_sidecars_are_skipped(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_live(wiki / "canonical.md", name="Canonical")
        (wiki / "_pending_questions.md").write_text("not a page\n", encoding="utf-8")
        report = scan_fold_graph(wiki)
        assert report.ok
        assert report.pages_scanned == 1


class TestAcyclicInvariant:
    def test_self_loop_is_a_cycle(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_tombstone(wiki / "a.md", name="A", folded_into="a")
        report = scan_fold_graph(wiki)
        assert not report.ok
        kinds = {v.kind for v in report.violations}
        assert VIOLATION_CYCLE in kinds

    def test_mutual_fold_is_a_cycle(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_tombstone(wiki / "a.md", name="A", folded_into="b")
        _write_tombstone(wiki / "b.md", name="B", folded_into="a")
        report = scan_fold_graph(wiki)
        assert not report.ok
        cycle_violations = [v for v in report.violations if v.kind == VIOLATION_CYCLE]
        assert len(cycle_violations) == 1
        assert set(cycle_violations[0].members) == {"a", "b"}

    def test_three_node_cycle_reported_once(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_tombstone(wiki / "a.md", name="A", folded_into="b")
        _write_tombstone(wiki / "b.md", name="B", folded_into="c")
        _write_tombstone(wiki / "c.md", name="C", folded_into="a")
        report = scan_fold_graph(wiki)
        cycle_violations = [v for v in report.violations if v.kind == VIOLATION_CYCLE]
        assert len(cycle_violations) == 1
        assert set(cycle_violations[0].members) == {"a", "b", "c"}


class TestExactlyOneLiveCanonicalInvariant:
    def test_dangling_target_has_no_live_canonical(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_tombstone(wiki / "a.md", name="A", folded_into="ghost")
        report = scan_fold_graph(wiki)
        assert not report.ok
        missing = [v for v in report.violations if v.kind == VIOLATION_MISSING_LIVE_CANONICAL]
        assert len(missing) == 1
        assert set(missing[0].members) == {"a", "ghost"}

    def test_every_member_tombstoned_has_no_live_canonical(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # a -> b, and b itself also tombstoned (folded into c, which does
        # not exist either) -- the whole chain has no live page anywhere.
        _write_tombstone(wiki / "a.md", name="A", folded_into="b")
        _write_tombstone(wiki / "b.md", name="B", folded_into="c")
        report = scan_fold_graph(wiki)
        missing = [v for v in report.violations if v.kind == VIOLATION_MISSING_LIVE_CANONICAL]
        assert len(missing) == 1
        assert set(missing[0].members) == {"a", "b", "c"}

    def test_two_tombstones_sharing_one_canonical_is_still_one_live_member(
        self, tmp_path: Path
    ) -> None:
        """Baseline: two SEPARATE sources folded into the same canonical is
        a perfectly normal, non-ambiguous fold set (one live member, two
        tombstones) — ambiguity requires TWO live members, not two
        tombstones."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_live(wiki / "canonical.md", name="Canonical")
        _write_tombstone(wiki / "source-a.md", name="Source A", folded_into="canonical")
        _write_tombstone(wiki / "source-b.md", name="Source B", folded_into="canonical")
        report = scan_fold_graph(wiki)
        assert report.ok

    def test_ambiguous_two_live_members_in_one_component(self, tmp_path: Path) -> None:
        """Exercises the component detector directly with a hand-built edge
        set naming two DIFFERENT live slugs in one connected component — the
        shape a hand-edited or corrupted ``folded_into`` chain could produce
        (e.g. a tombstone's target rewritten by hand to a second live page
        that a sibling tombstone in the same chain already resolves to).
        Not constructible from two independent files alone (each tombstone
        has exactly one outgoing edge), so this calls the pure graph helper
        rather than ``scan_fold_graph`` end to end."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        canonical_path = wiki / "canonical.md"
        bridge_path = wiki / "bridge.md"
        a_path = wiki / "a.md"
        _write_live(canonical_path, name="Canonical")
        _write_live(bridge_path, name="Bridge")
        _write_tombstone(a_path, name="A", folded_into="canonical")

        violations = _find_canonical_violations(
            edges={"a": "canonical", "bridge": "canonical"},
            slug_to_path={"a": a_path, "bridge": bridge_path, "canonical": canonical_path},
            slug_is_tombstone={"a": True, "bridge": False, "canonical": False},
        )
        ambiguous = [v for v in violations if v.kind == VIOLATION_AMBIGUOUS_LIVE_CANONICAL]
        assert len(ambiguous) == 1
        assert set(ambiguous[0].members) == {"a", "bridge", "canonical"}


class TestReadOnly:
    def test_scan_never_writes_any_file(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        canonical_path = wiki / "canonical.md"
        source_path = wiki / "source.md"
        _write_live(canonical_path, name="Canonical")
        _write_tombstone(source_path, name="Source", folded_into="ghost")
        before = {p: p.read_bytes() for p in wiki.glob("*.md")}
        scan_fold_graph(wiki)
        after = {p: p.read_bytes() for p in wiki.glob("*.md")}
        assert before == after
        assert set(wiki.iterdir()) == {canonical_path, source_path}


class TestFoldLintCLI:
    def test_cli_exits_zero_on_clean_corpus(self, tmp_path: Path, capsys) -> None:
        from athenaeum._cmd_fold_lint import EXIT_VIOLATIONS_FOUND, cmd_fold_lint

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_live(wiki / "canonical.md", name="Canonical")

        class _Args:
            path = tmp_path
            json = False

        assert cmd_fold_lint(_Args()) == 0
        assert EXIT_VIOLATIONS_FOUND == 2

    def test_cli_exits_nonzero_and_reports_on_violation(self, tmp_path: Path, capsys) -> None:
        from athenaeum._cmd_fold_lint import EXIT_VIOLATIONS_FOUND, cmd_fold_lint

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_tombstone(wiki / "a.md", name="A", folded_into="a")

        class _Args:
            path = tmp_path
            json = False

        assert cmd_fold_lint(_Args()) == EXIT_VIOLATIONS_FOUND
        out = capsys.readouterr().out
        assert "cycle" in out

    def test_cli_json_mode(self, tmp_path: Path, capsys) -> None:
        import json as json_mod

        from athenaeum._cmd_fold_lint import cmd_fold_lint

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_live(wiki / "canonical.md", name="Canonical")

        class _Args:
            path = tmp_path
            json = True

        assert cmd_fold_lint(_Args()) == 0
        out = capsys.readouterr().out
        payload = json_mod.loads(out)
        assert payload["ok"] is True
        assert payload["violations"] == []

    def test_subcommand_registered_on_cli(self) -> None:
        import athenaeum.cli as cli_mod

        assert cli_mod._SUBCOMMAND_LOADERS["fold-lint"] == (
            "athenaeum._cmd_fold_lint",
            "add_fold_lint_subparser",
        )
