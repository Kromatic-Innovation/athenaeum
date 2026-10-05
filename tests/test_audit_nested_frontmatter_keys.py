# SPDX-License-Identifier: Apache-2.0
"""Tests for ``scripts/audit_nested_frontmatter_keys.py`` (issue athenaeum#1966 AC3).

The audit turns "some pages have a nested name/tags/aliases/description key
that clobbers the real one" into a number. These tests pin that count against
fixture pages built to both real-world collision shapes named in the issue —
a nested mapping value under a sibling top-level key, and a second key inside
a block-list entry — plus the two properties that make the audit safe to run
against a real corpus at all: it writes nothing, and its output carries no
page content, titles, or filenames.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "audit_nested_frontmatter_keys.py"
)

_spec = importlib.util.spec_from_file_location(
    "audit_nested_frontmatter_keys", _SCRIPT
)
assert _spec and _spec.loader
audit_nested_frontmatter_keys = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_nested_frontmatter_keys)

audit_page = audit_nested_frontmatter_keys.audit_page
audit_tree = audit_nested_frontmatter_keys.audit_tree
main = audit_nested_frontmatter_keys.main

_CLEAN = """---
name: Clean Page
tags: [a, b]
aliases: [x]
description: Nothing nested here.
---

Body.
"""

#: A nested mapping value under a sibling top-level key.
_NESTED_MAPPING = """---
name: Real Name
field_sources:
  name: user:someone
  tags: provenance
---

Body.
"""

#: A second key inside a block-list entry.
_NESTED_BLOCK_LIST = """---
name: Another Real Name
related:
  - kind: x
    description: a nested description
---

Body.
"""

_NO_FRONTMATTER = "Just a body, no frontmatter at all.\n"


class TestAuditPage:
    def test_clean_page_has_no_hits(self) -> None:
        from athenaeum.models import parse_frontmatter

        meta, _body = parse_frontmatter(_CLEAN)
        per_field = audit_page(meta)
        assert not any(per_field.values())

    def test_nested_mapping_value_is_detected_per_field(self) -> None:
        from athenaeum.models import parse_frontmatter

        meta, _body = parse_frontmatter(_NESTED_MAPPING)
        per_field = audit_page(meta)
        assert per_field["name"] is True
        assert per_field["tags"] is True
        assert per_field["aliases"] is False
        assert per_field["description"] is False

    def test_nested_block_list_entry_key_is_detected(self) -> None:
        from athenaeum.models import parse_frontmatter

        meta, _body = parse_frontmatter(_NESTED_BLOCK_LIST)
        per_field = audit_page(meta)
        assert per_field["description"] is True
        assert per_field["name"] is False

    def test_top_level_key_is_never_counted_as_a_collision(self) -> None:
        from athenaeum.models import parse_frontmatter

        meta, _body = parse_frontmatter(_CLEAN)
        per_field = audit_page(meta)
        assert per_field == {
            "name": False,
            "tags": False,
            "aliases": False,
            "description": False,
        }


class TestAuditTree:
    def _tree(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "clean.md").write_text(_CLEAN, encoding="utf-8")
        (wiki / "nested-mapping.md").write_text(_NESTED_MAPPING, encoding="utf-8")
        (wiki / "nested-block-list.md").write_text(
            _NESTED_BLOCK_LIST, encoding="utf-8"
        )
        (wiki / "no-frontmatter.md").write_text(_NO_FRONTMATTER, encoding="utf-8")
        return wiki

    def test_totals_aggregate_across_pages(self, tmp_path: Path) -> None:
        report = audit_tree(self._tree(tmp_path))
        assert report["pages_scanned"] == 4
        assert report["pages_without_frontmatter"] == 1
        assert report["pages_affected"] == 2
        assert report["by_field"]["name"] == 1
        assert report["by_field"]["tags"] == 1
        assert report["by_field"]["description"] == 1
        assert report["by_field"]["aliases"] == 0

    def test_a_clean_only_corpus_scores_zero(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "clean.md").write_text(_CLEAN, encoding="utf-8")
        report = audit_tree(wiki)
        assert report["pages_affected"] == 0
        assert all(count == 0 for count in report["by_field"].values())

    def test_the_scan_mutates_nothing(self, tmp_path: Path) -> None:
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
    def test_default_output_carries_no_filename_or_content(
        self, tmp_path: Path, capsys
    ) -> None:
        wiki = self._make_wiki(tmp_path)
        assert main(["--wiki", str(wiki)]) == 0
        out = capsys.readouterr().out
        assert "nested-mapping" not in out
        assert "Real Name" not in out
        assert "pages affected (any field):    1" in out

    def test_json_output_parses(self, tmp_path: Path, capsys) -> None:
        wiki = self._make_wiki(tmp_path)
        assert main(["--wiki", str(wiki), "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["pages_affected"] == 1
        assert report["by_field"]["name"] == 1

    def test_a_clean_corpus_still_exits_zero(self, tmp_path: Path, capsys) -> None:
        """A measurement, not a gate."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "clean.md").write_text(_CLEAN, encoding="utf-8")
        assert main(["--wiki", str(wiki)]) == 0
        assert "pages affected (any field):    0" in capsys.readouterr().out

    @staticmethod
    def _make_wiki(tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "nested-mapping.md").write_text(_NESTED_MAPPING, encoding="utf-8")
        return wiki
