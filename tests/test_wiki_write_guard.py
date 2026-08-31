# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.wiki_write_guard — the write-boundary type guard
(issue athenaeum#1196, AC3).

``schemas.validate_wiki_meta`` already flags a type outside ``KNOWN_TYPES``
with a :class:`UserWarning`, but nothing in the write path reads or acts on
it. This module adds a hard refuse-and-surface guard at the actual disk
write, independent of any upstream clamp. These tests cover:

- A declared type (from ``_schema/types.md``) is admitted.
- A ``KNOWN_TYPES`` fallback type not listed in ``types.md`` is admitted
  (the union, not either set alone).
- ``auto-memory`` is admitted even though it is deliberately absent from
  ``types.md`` (the issue's explicit "do not add a types.md row" guard).
- A genuinely foreign type is REFUSED: no file lands under the plain
  ``wiki_root`` name, the rendered content is parked under
  ``_type_rejected/`` instead, and an audit record is appended to the
  ledger.
- The guard never touches an existing page (non-destructive).
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.wiki_write_guard import (
    TYPE_REJECTED_DIR_NAME,
    TYPE_REJECTED_LEDGER_NAME,
    guard_entity_write_type,
    list_type_rejected,
    resolve_admitted_wiki_types,
)


def _wiki_root(tmp_path: Path, *, declared_types: list[str] | None = None) -> Path:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "_schema").mkdir(parents=True)
    if declared_types is not None:
        rows = "\n".join(f"| {t} |" for t in declared_types)
        (wiki_root / "_schema" / "types.md").write_text(
            f"# Types\n\n| Type |\n|------|\n{rows}\n"
        )
    return wiki_root


class TestResolveAdmittedWikiTypes:
    def test_declared_types_admitted(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path, declared_types=["person", "project"])
        admitted = resolve_admitted_wiki_types(wiki_root)
        assert "person" in admitted
        assert "project" in admitted

    def test_known_types_fallback_admitted_even_when_types_md_present(
        self, tmp_path: Path
    ) -> None:
        """A KNOWN_TYPES fallback type (e.g. 'principle') absent from
        types.md is still admitted — the guard set is a UNION, not just
        the declared list (unlike the narrower valid_types the tier0/tier2
        clamps use)."""
        wiki_root = _wiki_root(tmp_path, declared_types=["person"])
        admitted = resolve_admitted_wiki_types(wiki_root)
        assert "principle" in admitted  # KNOWN_TYPES FALLBACK_TYPES member

    def test_auto_memory_admitted_without_a_types_md_row(self, tmp_path: Path) -> None:
        """athenaeum#1196 explicitly forbids adding an auto-memory row to
        types.md; the guard must still admit it via KNOWN_TYPES."""
        wiki_root = _wiki_root(tmp_path, declared_types=["person", "project"])
        admitted = resolve_admitted_wiki_types(wiki_root)
        assert "auto-memory" in admitted
        # And confirm it really is absent from the declared half.
        types_md = (wiki_root / "_schema" / "types.md").read_text()
        assert "auto-memory" not in types_md

    def test_foreign_type_not_admitted(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path, declared_types=["person", "project"])
        admitted = resolve_admitted_wiki_types(wiki_root)
        assert "issue" not in admitted
        assert "feedback" not in admitted  # removed from KNOWN_TYPES per athenaeum#970


class TestGuardEntityWriteType:
    def test_admitted_type_passes(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path, declared_types=["person"])
        wiki_root.mkdir(exist_ok=True)
        meta = {"uid": "abc123", "type": "person", "name": "Bob"}
        ok = guard_entity_write_type(
            wiki_root, "abc123-bob.md", "---\n...\n---\nbody", meta
        )
        assert ok is True
        # No rejection artifacts written on the admit path.
        assert not (wiki_root / TYPE_REJECTED_DIR_NAME).exists()
        assert not (wiki_root / TYPE_REJECTED_LEDGER_NAME).exists()

    def test_auto_memory_type_passes(self, tmp_path: Path) -> None:
        """AC3's explicit non-regression: auto-memory must not be caught by
        this guard even though it carries no types.md row."""
        wiki_root = _wiki_root(tmp_path, declared_types=["person"])
        meta = {"uid": "xyz789", "type": "auto-memory", "name": "cluster-1"}
        ok = guard_entity_write_type(
            wiki_root, "xyz789-cluster-1.md", "---\n...\n---\nbody", meta
        )
        assert ok is True

    def test_foreign_type_refused_and_parked(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path, declared_types=["person", "project"])
        rendered = (
            "---\nuid: dead1234\ntype: issue\nname: Some GitHub issue\n---\n\nbody\n"
        )
        meta = {"uid": "dead1234", "type": "issue", "name": "Some GitHub issue"}
        ok = guard_entity_write_type(
            wiki_root,
            "dead1234-some-github-issue.md",
            rendered,
            meta,
            source="tier3-create",
        )
        assert ok is False
        # Never landed as a live wiki page.
        assert not (wiki_root / "dead1234-some-github-issue.md").exists()
        # Parked, byte-for-byte, under _type_rejected/.
        parked = wiki_root / TYPE_REJECTED_DIR_NAME / "dead1234-some-github-issue.md"
        assert parked.exists()
        assert parked.read_text() == rendered
        # Ledgered.
        records = list_type_rejected(wiki_root)
        assert len(records) == 1
        assert records[0]["type"] == "issue"
        assert records[0]["uid"] == "dead1234"
        assert records[0]["name"] == "Some GitHub issue"
        assert records[0]["source"] == "tier3-create"
        assert records[0]["filename"] == "dead1234-some-github-issue.md"

    def test_ledger_is_jsonl_and_appends(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path, declared_types=["person"])
        for i in range(2):
            guard_entity_write_type(
                wiki_root,
                f"uid{i}-x.md",
                "---\n...\n---\nbody",
                {"uid": f"uid{i}", "type": "issue", "name": f"x{i}"},
            )
        ledger_path = wiki_root / TYPE_REJECTED_LEDGER_NAME
        lines = ledger_path.read_text().splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # each line is independently valid JSON
        assert [r["uid"] for r in list_type_rejected(wiki_root)] == ["uid0", "uid1"]

    def test_corrupt_trailing_line_tolerated(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path, declared_types=["person"])
        guard_entity_write_type(
            wiki_root,
            "uid0-x.md",
            "---\n...\n---\nbody",
            {"uid": "uid0", "type": "issue", "name": "x0"},
        )
        ledger_path = wiki_root / TYPE_REJECTED_LEDGER_NAME
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write("{ torn line, not valid json")
        records = list_type_rejected(wiki_root)
        assert len(records) == 1
        assert records[0]["uid"] == "uid0"

    def test_never_touches_an_existing_page(self, tmp_path: Path) -> None:
        """Non-destructive: an existing, valid page at the SAME filename the
        guard would have refused is left completely alone — the guard only
        ever runs at a NEW-entity write call site, never against an
        existing on-disk page."""
        wiki_root = _wiki_root(tmp_path, declared_types=["person"])
        existing = wiki_root / "abc123-bob.md"
        original = "---\nuid: abc123\ntype: person\nname: Bob\n---\n\noriginal body\n"
        existing.write_text(original, encoding="utf-8")

        # Simulate a caller that (incorrectly) tried to write a foreign type
        # at the SAME filename -- the guard must not touch `existing` at all
        # since it only ever parks REJECTED content under _type_rejected/.
        ok = guard_entity_write_type(
            wiki_root,
            "abc123-bob.md",
            "---\nuid: abc123\ntype: issue\nname: Bob\n---\n\nforeign body\n",
            {"uid": "abc123", "type": "issue", "name": "Bob"},
        )
        assert ok is False
        assert existing.read_text() == original


class TestListTypeRejectedEmpty:
    def test_no_ledger_returns_empty(self, tmp_path: Path) -> None:
        wiki_root = _wiki_root(tmp_path, declared_types=["person"])
        assert list_type_rejected(wiki_root) == []
