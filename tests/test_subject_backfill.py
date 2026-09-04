# SPDX-License-Identifier: Apache-2.0
"""subject: coordinate derivation + backfill command (issue athenaeum#1244).

Covers: deterministic derivation (`subject := uid`), the mechanical
backfill's idempotence/never-overwrite/skip behavior, byte-level
idempotence outside the frontmatter block, and the zero-movement
regression this issue's central finding rests on — that Gate 1
(`athenaeum.comparator.gate1_separator_relations`) can never emit DISJOINT
on the `subject` dimension under the default `ratified=False`, so this
backfill (even if applied) cannot move the settle-rate measured in AC4.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.comparator import gate1_separator_relations
from athenaeum.dimensions import DEFAULT_REGISTRY, Relation
from athenaeum.models import parse_frontmatter
from athenaeum.subject_backfill import (
    apply_subject_backfill,
    build_subject_report,
    derive_subject_for_page,
    discover_wiki_pages,
    insert_subject,
)


def _page(root: Path, name: str, frontmatter: str, body: str = "Body text.\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "wiki"
    root.mkdir(parents=True)
    return root


# --- derive_subject_for_page -------------------------------------------------


class TestDeriveSubject:
    def test_derives_from_uid(self) -> None:
        assert derive_subject_for_page({"uid": "abc123", "type": "concept"}) == "abc123"

    def test_strips_whitespace(self) -> None:
        assert derive_subject_for_page({"uid": "  abc123  "}) == "abc123"

    def test_no_uid_returns_none(self) -> None:
        assert derive_subject_for_page({"type": "concept"}) is None

    def test_empty_uid_returns_none(self) -> None:
        assert derive_subject_for_page({"uid": ""}) is None

    def test_non_string_scalar_uid_is_coerced(self) -> None:
        # YAML may load a bare-numeric uid as an int; parse_frontmatter
        # normally coerces this at the boundary, but this function is
        # defensive on its own input too.
        assert derive_subject_for_page({"uid": 12345}) == "12345"

    def test_list_or_dict_uid_returns_none(self) -> None:
        # Malformed frontmatter must never crash the sweep.
        assert derive_subject_for_page({"uid": ["a", "b"]}) is None
        assert derive_subject_for_page({"uid": {"x": 1}}) is None


# --- discover_wiki_pages: flat, non-recursive, underscore-skip -------------


class TestDiscoverWikiPages:
    def test_flat_scan_skips_underscore_prefixed(self, wiki: Path) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        _page(wiki, "_pending_merges.md", "uid: '2'")
        found = discover_wiki_pages(wiki)
        assert [p.name for p in found] == ["a.md"]

    def test_does_not_recurse_into_underscore_subdirectories(self, wiki: Path) -> None:
        """Regression: an rglob walk with a filename-only underscore check
        would reach wiki/_quarantine/*.md — this must not.
        """
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        _page(wiki / "_quarantine", "b.md", "uid: '2'\ntype: concept\nname: B")
        found = discover_wiki_pages(wiki)
        assert [p.name for p in found] == ["a.md"]

    def test_missing_root_returns_empty(self, tmp_path: Path) -> None:
        assert discover_wiki_pages(tmp_path / "no-such-wiki") == []


# --- build_subject_report ----------------------------------------------------


class TestBuildReport:
    def test_assigns_by_uid_and_reports_counts(self, wiki: Path) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        _page(wiki, "b.md", "uid: '2'\ntype: reference\nname: B")

        report = build_subject_report(wiki)

        assert report.scanned == 2
        assert report.counts_by_reason() == {"derivable": 2}
        assert {o.subject for o in report.assignments} == {"1", "2"}

    def test_already_set_is_skipped(self, wiki: Path) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A\nsubject: existing-value")
        report = build_subject_report(wiki)
        assert report.counts_by_reason() == {"already-set": 1}

    def test_no_uid_is_skipped(self, wiki: Path) -> None:
        _page(wiki, "a.md", "type: concept\nname: A")
        report = build_subject_report(wiki)
        assert report.counts_by_reason() == {"no-uid": 1}

    def test_no_frontmatter_is_skipped(self, wiki: Path) -> None:
        path = wiki / "a.md"
        path.write_text("Just a body, no frontmatter block.\n", encoding="utf-8")
        report = build_subject_report(wiki)
        assert report.scanned == 1
        assert report.counts_by_reason() == {"no-frontmatter": 1}

    def test_empty_frontmatter_is_skipped(self, wiki: Path) -> None:
        path = wiki / "a.md"
        path.write_text("---\n\n---\nBody.\n", encoding="utf-8")
        report = build_subject_report(wiki)
        assert report.counts_by_reason() == {"empty-frontmatter": 1}

    def test_dry_run_writes_nothing(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        before = path.read_text(encoding="utf-8")
        build_subject_report(wiki)
        assert path.read_text(encoding="utf-8") == before


# --- insert_subject: byte-level idempotence ---------------------------------


class TestInsertSubject:
    def test_appends_within_frontmatter_block(self) -> None:
        text = "---\nuid: '1'\ntype: concept\nname: A\n---\nBody.\n"
        updated = insert_subject(text, "1")
        meta, body = parse_frontmatter(updated)
        assert meta["subject"] == "1"
        assert body == "Body.\n"

    def test_touches_no_other_byte(self) -> None:
        """The insertion must not reflow unrelated frontmatter or the body —
        this is what makes a second run a byte-level no-op on pages this
        pass never meant to touch."""
        text = (
            "---\nuid: '1'\ntype: concept\nname: A\ntags:\n- x\n- y\n"
            "---\nBody with\nmultiple lines.\n"
        )
        updated = insert_subject(text, "1")
        # Everything before the closing '---' of frontmatter, and the body,
        # are byte-identical; only one new line was inserted.
        assert updated.startswith("---\nuid: '1'\ntype: concept\nname: A\ntags:\n- x\n- y\n")
        assert updated.endswith("---\nBody with\nmultiple lines.\n")
        assert "\nsubject: '1'\n" in updated

    def test_no_frontmatter_returns_none(self) -> None:
        assert insert_subject("no frontmatter here", "1") is None

    def test_crlf_preserved(self) -> None:
        text = "---\r\nuid: '1'\r\n---\r\nBody.\r\n"
        updated = insert_subject(text, "1")
        assert updated is not None
        assert "\r\nsubject: '1'\r\n" in updated


# --- apply_subject_backfill --------------------------------------------------


class TestApplyBackfill:
    def test_writes_derived_subject(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        report = build_subject_report(wiki)
        changed = apply_subject_backfill(report)
        assert changed == 1
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["subject"] == "1"

    def test_never_overwrites_existing_value(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A\nsubject: keep-me")
        report = build_subject_report(wiki)
        assert report.assignments == []
        changed = apply_subject_backfill(report)
        assert changed == 0
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["subject"] == "keep-me"

    def test_idempotent_across_two_runs(self, wiki: Path) -> None:
        path = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        report1 = build_subject_report(wiki)
        apply_subject_backfill(report1)
        after_first = path.read_text(encoding="utf-8")

        report2 = build_subject_report(wiki)
        assert report2.assignments == []  # already-set now
        changed2 = apply_subject_backfill(report2)
        assert changed2 == 0
        assert path.read_text(encoding="utf-8") == after_first

    def test_re_checks_at_write_time(self, wiki: Path) -> None:
        """A page that gained a subject: between scan and apply must not be
        overwritten (mirrors memory_class_backfill's own invariant)."""
        path = _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        report = build_subject_report(wiki)
        # Simulate a concurrent write landing a value first.
        path.write_text(
            "---\nuid: '1'\ntype: concept\nname: A\nsubject: raced-in\n---\nBody.\n",
            encoding="utf-8",
        )
        changed = apply_subject_backfill(report)
        assert changed == 0
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["subject"] == "raced-in"


# --- CLI ----------------------------------------------------------------------


class TestCLI:
    def test_dry_run_default(self, wiki: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        rc = main(["subject", "backfill", "--path", str(wiki.parent)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "dry run: nothing written" in out
        meta, _ = parse_frontmatter((wiki / "a.md").read_text(encoding="utf-8"))
        assert "subject" not in meta

    def test_apply_writes(self, wiki: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        rc = main(["subject", "backfill", "--path", str(wiki.parent), "--apply"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "applied: 1 file(s) written" in out
        meta, _ = parse_frontmatter((wiki / "a.md").read_text(encoding="utf-8"))
        assert meta["subject"] == "1"

    def test_dry_run_overrides_apply(self, wiki: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        rc = main(["subject", "backfill", "--path", str(wiki.parent), "--apply", "--dry-run"])
        assert rc == 0
        meta, _ = parse_frontmatter((wiki / "a.md").read_text(encoding="utf-8"))
        assert "subject" not in meta

    def test_json_output(self, wiki: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _page(wiki, "a.md", "uid: '1'\ntype: concept\nname: A")
        rc = main(["subject", "backfill", "--path", str(wiki.parent), "--json"])
        assert rc == 0
        import json

        payload = json.loads(capsys.readouterr().out)
        assert payload["scanned"] == 1
        assert payload["assignable"] == 1
        assert payload["applied"] is False


# --- AC4 zero-movement regression --------------------------------------------


class TestGate1ZeroMovement:
    """The load-bearing claim this issue's PR reports: even a fully-applied
    subject := uid backfill cannot move Gate 1's settle rate under today's
    code, because the subject comparator only emits DISJOINT under
    ratified=True, and no live call site sets it. Pins the property
    directly rather than leaving it as PR-body prose.
    """

    def test_distinct_subjects_never_disjoint_under_default_ratified(self) -> None:
        meta_a = {"uid": "aaa111", "type": "concept"}
        meta_b = {"uid": "bbb222", "type": "concept"}  # a different page entirely
        # Simulate the post-backfill state directly, without touching disk.
        meta_a["subject"] = derive_subject_for_page(meta_a)
        meta_b["subject"] = derive_subject_for_page(meta_b)
        assert meta_a["subject"] != meta_b["subject"]

        rels = gate1_separator_relations(DEFAULT_REGISTRY, meta_a, meta_b)
        # subject_ratified defaults to False (the function's own default,
        # and wiki_dedupe.record_comparison's call site never sets it) —
        # so distinct subjects settle as UNKNOWN, never DISJOINT.
        assert rels.get("subject") == Relation.UNKNOWN
        assert Relation.DISJOINT not in rels.values()

    def test_both_pages_fully_uncoordinated_settles_nothing(self) -> None:
        """The corpus's actual current state (issue athenaeum#1244's baseline):
        no valid-time/scope/subject coordinates anywhere -> every separator
        dimension is UNKNOWN (both-null) -> Gate 1 settles nothing."""
        meta_a: dict[str, object] = {"uid": "aaa111", "type": "concept", "name": "A"}
        meta_b: dict[str, object] = {"uid": "bbb222", "type": "concept", "name": "B"}
        rels = gate1_separator_relations(DEFAULT_REGISTRY, meta_a, meta_b)
        assert Relation.DISJOINT not in rels.values()
        assert all(rel == Relation.UNKNOWN for rel in rels.values())
