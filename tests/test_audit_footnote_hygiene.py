# SPDX-License-Identifier: Apache-2.0
"""Tests for ``scripts/audit_footnote_hygiene.py`` (issue athenaeum#1942 AC3).

The audit is the measurement that turns "the merge applier corrupted some
pages" into a number. Its two headline counts are the ones the acceptance
criterion names — list items carrying more than one footnote reference, and
footnote labels defined more than once — so this module pins them against
fixture pages built to the corrupted shape, plus the two properties the audit
must have to be safe to run on a real corpus at all: it writes nothing, and
its default output carries no page content, titles, or filenames.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "audit_footnote_hygiene.py"
)

_spec = importlib.util.spec_from_file_location("audit_footnote_hygiene", _SCRIPT)
assert _spec and _spec.loader
audit_footnote_hygiene = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_footnote_hygiene)

audit_body = audit_footnote_hygiene.audit_body
audit_tree = audit_footnote_hygiene.audit_tree
iter_list_items = audit_footnote_hygiene.iter_list_items
main = audit_footnote_hygiene.main

#: A page in the corrupted shape: one run-on item holding three cited clauses,
#: one clean item, and a label defined twice.
_CORRUPTED = """---
uid: 383920da
type: tool
---

# A tool

## Observations

- Piloted with two partners.[^1] Priced per seat.[^1] Renews in March.[^2]
- Onboarding takes two weeks.[^3]

[^1]: raw/sessions/a.md

[^1]: raw/sessions/b.md

[^2]: raw/sessions/c.md

[^3]: raw/sessions/d.md
"""

#: The same facts in the shape the fixed applier produces.
_CLEAN = """---
uid: 11112222
type: tool
---

# A tool

## Observations

- Piloted with two partners.[^1]
- Priced per seat.[^2]
- Renews in March.[^3]

[^1]: raw/sessions/a.md

[^2]: raw/sessions/b.md

[^3]: raw/sessions/c.md
"""


class TestListItemSegmentation:
    def test_continuation_lines_belong_to_their_item(self) -> None:
        items = iter_list_items("- Alpha.[^1]\n  still alpha.[^2]\n- Beta.[^3]\n")
        assert items == ["- Alpha.[^1]\n  still alpha.[^2]", "- Beta.[^3]"]

    def test_a_wrapped_clause_is_not_counted_as_a_separate_item(self) -> None:
        """Folding continuation lines in is what makes the count honest.

        Counting marker lines alone would score a wrapped run-on item as one
        citation per line and miss the shape entirely.
        """
        counts = audit_body("- Alpha.[^1]\n  Beta.[^2]\n")
        assert counts["list_items"] == 1
        assert counts["multi_cited_items"] == 1
        assert counts["max_refs_in_one_item"] == 2

    def test_prose_and_headings_are_not_list_items(self) -> None:
        assert iter_list_items("# H\n\nSome prose.[^1]\n\n[^1]: a.md\n") == []

    def test_ordered_and_starred_markers_both_count(self) -> None:
        assert len(iter_list_items("1. Alpha\n2. Beta\n")) == 2
        assert len(iter_list_items("* Alpha\n+ Beta\n")) == 2


class TestAuditBody:
    def test_the_corrupted_shape_is_counted(self) -> None:
        body = _CORRUPTED.split("---", 2)[2]
        counts = audit_body(body)
        assert counts["list_items"] == 2
        assert counts["multi_cited_items"] == 1
        assert counts["max_refs_in_one_item"] == 3
        assert counts["reused_labels"] == 1

    def test_the_clean_shape_scores_zero_on_both_headline_counts(self) -> None:
        body = _CLEAN.split("---", 2)[2]
        counts = audit_body(body)
        assert counts["multi_cited_items"] == 0
        assert counts["reused_labels"] == 0
        assert counts["dangling_refs"] == 0
        assert counts["orphan_defs"] == 0

    def test_a_definition_is_never_counted_as_a_reference(self) -> None:
        """``[^1]`` and ``[^1]:`` differ by one character and nothing else.

        Conflating them would make every well-formed page look as though its
        footnote block were a run of extra citations.
        """
        counts = audit_body("Alpha.[^1]\n\n[^1]: a.md\n")
        assert counts["dangling_refs"] == 0
        assert counts["orphan_defs"] == 0

    def test_dangling_and_orphan_are_reported_separately(self) -> None:
        counts = audit_body("- Alpha.[^1]\n\n[^9]: nine.md\n")
        assert counts["dangling_refs"] == 1
        assert counts["orphan_defs"] == 1

    def test_an_empty_page_counts_nothing(self) -> None:
        assert audit_body("") == {
            "list_items": 0,
            "multi_cited_items": 0,
            "max_refs_in_one_item": 0,
            "reused_labels": 0,
            "dangling_refs": 0,
            "orphan_defs": 0,
        }


class TestAuditTree:
    def _tree(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "383920da-a-tool.md").write_text(_CORRUPTED, encoding="utf-8")
        (wiki / "11112222-another.md").write_text(_CLEAN, encoding="utf-8")
        return wiki

    def test_totals_aggregate_across_pages(self, tmp_path: Path) -> None:
        report = audit_tree(self._tree(tmp_path))
        assert report["pages_scanned"] == 2
        assert report["multi_cited_items"] == 1
        assert report["reused_labels"] == 1
        assert report["pages_with_multi_cited_items"] == 1
        assert report["pages_with_reused_labels"] == 1
        assert report["max_refs_in_one_item"] == 3

    def test_per_page_rows_cover_only_affected_pages_and_name_them_by_uid(
        self, tmp_path: Path
    ) -> None:
        report = audit_tree(self._tree(tmp_path))
        assert [page["uid"] for page in report["per_page"]] == ["383920da"]

    def test_frontmatter_is_never_counted_as_body(self, tmp_path: Path) -> None:
        """A bracketed YAML value is not a footnote."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "p.md").write_text(
            '---\nuid: abcd1234\naliases: ["x"]\n---\n\nProse.\n', encoding="utf-8"
        )
        report = audit_tree(wiki)
        assert report["dangling_refs"] == 0
        assert report["orphan_defs"] == 0

    def test_the_scan_mutates_nothing(self, tmp_path: Path) -> None:
        """The whole point of a read-only audit.

        Compares every file's bytes and mtime across the scan, so a write
        that happened to reproduce the same content would still be caught.
        """
        wiki = self._tree(tmp_path)
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(wiki.rglob("*"))
        }
        audit_tree(wiki)
        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(wiki.rglob("*"))
        }
        assert after == before

    def test_a_missing_tree_is_refused_rather_than_reported_as_clean(
        self, tmp_path: Path
    ) -> None:
        assert main(["--wiki", str(tmp_path / "nope")]) == 2


class TestCli:
    def test_default_output_carries_no_filename_title_or_uid(
        self, tmp_path: Path, capsys
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "383920da-a-tool.md").write_text(_CORRUPTED, encoding="utf-8")
        assert main(["--wiki", str(wiki)]) == 0
        out = capsys.readouterr().out
        assert "383920da" not in out
        assert "a-tool" not in out
        assert "A tool" not in out
        assert "Piloted" not in out
        assert "list items with >1 citation:   1" in out

    def test_per_page_adds_uid_rows_but_still_no_content(
        self, tmp_path: Path, capsys
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "383920da-a-tool.md").write_text(_CORRUPTED, encoding="utf-8")
        assert main(["--wiki", str(wiki), "--per-page"]) == 0
        out = capsys.readouterr().out
        assert "383920da" in out
        assert "a-tool" not in out
        assert "Piloted" not in out

    def test_json_output_parses_and_omits_per_page_by_default(
        self, tmp_path: Path, capsys
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "383920da-a-tool.md").write_text(_CORRUPTED, encoding="utf-8")
        assert main(["--wiki", str(wiki), "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["multi_cited_items"] == 1
        assert report["reused_labels"] == 1
        assert "per_page" not in report

    def test_a_clean_corpus_still_exits_zero(self, tmp_path: Path, capsys) -> None:
        """A measurement, not a gate."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "clean.md").write_text(_CLEAN, encoding="utf-8")
        assert main(["--wiki", str(wiki)]) == 0
        assert "reused footnote labels:        0" in capsys.readouterr().out
