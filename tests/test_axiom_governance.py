# SPDX-License-Identifier: Apache-2.0
"""Tests for axiom promotion/demotion governance + assignment audit (issue #434).

Covers the four acceptance criteria:

1. ``memory_class: axiom`` with NO promotion record is flagged by validation
   (a recoverable ``UserWarning``, not a hard failure) — via
   :func:`athenaeum.axiom_governance.warn_if_unbacked_axiom`.
2. Promotion and demotion round-trip through the append-only ledger, each
   with a recorded reason.
3. The audit listing (:func:`athenaeum.axiom_governance.list_axiom_audit`,
   surfaced via ``athenaeum axiom list`` and the ``list_axiom_audit`` MCP
   tool) shows every axiom + its promotion record.
4. A scoped axiom's ``scope`` round-trips through parse/serialize
   (frontmatter) AND through a promotion record.

Explicitly NOT covered here (out of scope for #434): scope ENFORCEMENT
(deciding whether the current context matches a page's scope) — the issue
is explicit that's a consumer's concern.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from athenaeum.axiom_governance import (
    ACTION_DEMOTE,
    ACTION_PROMOTE,
    build_axiom_record,
    default_axiom_ledger_path,
    is_axiom_promoted,
    list_axiom_audit,
    read_axiom_ledger,
    record_demotion,
    record_promotion,
    warn_if_unbacked_axiom,
)
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.schemas import validate_wiki_meta

# ---------------------------------------------------------------------------
# AC1 — unbacked axiom flagged by validation (recoverable, not a crash)
# ---------------------------------------------------------------------------


class TestUnbackedAxiomFlagged:
    def test_axiom_with_no_ledger_is_flagged(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        meta = {"uid": "abc123", "type": "concept", "name": "X", "memory_class": "axiom"}
        with pytest.warns(UserWarning, match="no active promotion record"):
            flagged = warn_if_unbacked_axiom(meta, wiki_root, slug="abc123")
        assert flagged is True

    def test_axiom_with_promotion_record_is_not_flagged(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="abc123", reason="bedrock fact", by="tristan")
        meta = {"uid": "abc123", "type": "concept", "name": "X", "memory_class": "axiom"}
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            flagged = warn_if_unbacked_axiom(meta, wiki_root, slug="abc123")
        assert flagged is False

    def test_non_axiom_memory_class_never_flagged(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        meta = {"uid": "abc123", "type": "concept", "name": "X", "memory_class": "fact"}
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            flagged = warn_if_unbacked_axiom(meta, wiki_root, slug="abc123")
        assert flagged is False

    def test_absent_memory_class_never_flagged(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        meta = {"uid": "abc123", "type": "concept", "name": "X"}
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            flagged = warn_if_unbacked_axiom(meta, wiki_root, slug="abc123")
        assert flagged is False

    def test_does_not_raise_even_when_flagged(self, tmp_path: Path) -> None:
        """A recoverable warning, never a hard crash — legacy pages must still load."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        meta = {"uid": "abc123", "type": "concept", "name": "X", "memory_class": "axiom"}
        with pytest.warns(UserWarning):
            warn_if_unbacked_axiom(meta, wiki_root, slug="abc123")  # must not raise

    def test_demoted_axiom_is_flagged_again(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="abc123", reason="bedrock", by="tristan")
        record_demotion(wiki_root, slug="abc123", reason="turned out wrong", by="tristan")
        meta = {"uid": "abc123", "type": "concept", "name": "X", "memory_class": "axiom"}
        with pytest.warns(UserWarning, match="no active promotion record"):
            flagged = warn_if_unbacked_axiom(meta, wiki_root, slug="abc123")
        assert flagged is True

    def test_slug_falls_back_to_uid(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="my-uid", reason="bedrock", by="tristan")
        meta = {"uid": "my-uid", "type": "concept", "name": "X", "memory_class": "axiom"}
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            flagged = warn_if_unbacked_axiom(meta, wiki_root)  # no explicit slug
        assert flagged is False

    def test_validate_wiki_meta_itself_is_unchanged_by_434(self) -> None:
        """#424's validator accepts `axiom` same as before — #434's governance
        is layered ON TOP via a separate function, not baked into the
        pydantic field validator (which has no ledger I/O access)."""
        meta = {"uid": "abc12345", "type": "concept", "name": "X", "memory_class": "axiom"}
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            m = validate_wiki_meta(meta)
        assert m.memory_class == "axiom"


# ---------------------------------------------------------------------------
# AC2 — promotion and demotion round-trip with recorded reasons
# ---------------------------------------------------------------------------


class TestPromotionDemotionRoundTrip:
    def test_build_axiom_record_promote_shape(self) -> None:
        record = build_axiom_record(
            slug="my-page", action=ACTION_PROMOTE, reason="bedrock", by="tristan"
        )
        assert record["slug"] == "my-page"
        assert record["action"] == "promote"
        assert record["reason"] == "bedrock"
        assert record["by"] == "tristan"
        assert "at" in record
        assert "scope" not in record

    def test_build_axiom_record_demote_shape(self) -> None:
        record = build_axiom_record(
            slug="my-page", action=ACTION_DEMOTE, reason="no longer true", by="tristan"
        )
        assert record["action"] == "demote"
        assert record["reason"] == "no longer true"

    def test_invalid_action_raises(self) -> None:
        with pytest.raises(ValueError, match="action must be one of"):
            build_axiom_record(slug="x", action="bogus", reason="r", by="b")

    def test_empty_reason_raises(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            build_axiom_record(slug="x", action=ACTION_PROMOTE, reason="", by="b")

    def test_empty_by_raises(self) -> None:
        with pytest.raises(ValueError, match="by"):
            build_axiom_record(slug="x", action=ACTION_PROMOTE, reason="r", by="")

    def test_empty_slug_raises(self) -> None:
        with pytest.raises(ValueError, match="slug"):
            build_axiom_record(slug="", action=ACTION_PROMOTE, reason="r", by="b")

    def test_record_promotion_appends_to_ledger(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="my-page", reason="bedrock", by="tristan")
        ledger_path = default_axiom_ledger_path(wiki_root)
        assert ledger_path.exists()
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["slug"] == "my-page"
        assert record["action"] == "promote"

    def test_record_demotion_appends_to_ledger(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="my-page", reason="bedrock", by="tristan")
        record_demotion(wiki_root, slug="my-page", reason="reconsidered", by="tristan")
        records = read_axiom_ledger(wiki_root, slug="my-page")
        assert len(records) == 2
        assert records[0]["action"] == "promote"
        assert records[1]["action"] == "demote"
        assert records[1]["reason"] == "reconsidered"

    def test_is_axiom_promoted_true_after_promote(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="my-page", reason="bedrock", by="tristan")
        assert is_axiom_promoted(wiki_root, "my-page") is True

    def test_is_axiom_promoted_false_after_demote(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="my-page", reason="bedrock", by="tristan")
        record_demotion(wiki_root, slug="my-page", reason="reconsidered", by="tristan")
        assert is_axiom_promoted(wiki_root, "my-page") is False

    def test_is_axiom_promoted_true_after_re_promote(self, tmp_path: Path) -> None:
        """Full history, not a single flag: promote -> demote -> re-promote is active."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="my-page", reason="bedrock", by="tristan")
        record_demotion(wiki_root, slug="my-page", reason="reconsidered", by="tristan")
        record_promotion(wiki_root, slug="my-page", reason="actually still true", by="tristan")
        assert is_axiom_promoted(wiki_root, "my-page") is True
        records = read_axiom_ledger(wiki_root, slug="my-page")
        assert len(records) == 3

    def test_is_axiom_promoted_false_when_never_recorded(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        assert is_axiom_promoted(wiki_root, "never-seen") is False

    def test_read_axiom_ledger_empty_when_missing(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        assert read_axiom_ledger(wiki_root) == []

    def test_read_axiom_ledger_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="my-page", reason="bedrock", by="tristan")
        ledger_path = default_axiom_ledger_path(wiki_root)
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write('{"slug": "torn", "action": "promo')  # no trailing newline, truncated
        records = read_axiom_ledger(wiki_root)
        assert len(records) == 1
        assert records[0]["slug"] == "my-page"

    def test_ledger_records_are_isolated_per_wiki_root(self, tmp_path: Path) -> None:
        wiki_a = tmp_path / "wiki_a"
        wiki_b = tmp_path / "wiki_b"
        wiki_a.mkdir()
        wiki_b.mkdir()
        record_promotion(wiki_a, slug="page", reason="r", by="b")
        assert is_axiom_promoted(wiki_a, "page") is True
        assert is_axiom_promoted(wiki_b, "page") is False

    def test_scope_recorded_on_promotion(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record = record_promotion(
            wiki_root,
            slug="resume-style",
            reason="applies while drafting resumes",
            by="tristan",
            scope="applies to resume work",
        )
        assert record["scope"] == "applies to resume work"
        stored = read_axiom_ledger(wiki_root, slug="resume-style")
        assert stored[0]["scope"] == "applies to resume work"

    def test_demotion_never_carries_scope(self, tmp_path: Path) -> None:
        record = build_axiom_record(
            slug="x", action=ACTION_DEMOTE, reason="r", by="b"
        )
        assert "scope" not in record

    def test_record_promotion_without_scope_omits_key(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record = record_promotion(wiki_root, slug="page", reason="r", by="b")
        assert "scope" not in record


# ---------------------------------------------------------------------------
# AC3 — audit listing surfaces every axiom + its promotion record
# ---------------------------------------------------------------------------


class TestAuditListing:
    def test_list_axiom_audit_empty_when_no_ledger(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        assert list_axiom_audit(wiki_root) == []

    def test_list_axiom_audit_shows_active_promotion(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="page-a", reason="bedrock", by="tristan")
        audit = list_axiom_audit(wiki_root)
        assert len(audit) == 1
        assert audit[0]["slug"] == "page-a"
        assert audit[0]["active"] is True
        assert len(audit[0]["history"]) == 1
        assert audit[0]["history"][0]["reason"] == "bedrock"
        assert audit[0]["history"][0]["by"] == "tristan"

    def test_list_axiom_audit_shows_inactive_after_demotion(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="page-a", reason="bedrock", by="tristan")
        record_demotion(wiki_root, slug="page-a", reason="reconsidered", by="tristan")
        audit = list_axiom_audit(wiki_root)
        assert audit[0]["active"] is False
        assert len(audit[0]["history"]) == 2

    def test_list_axiom_audit_multiple_slugs(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="page-a", reason="r1", by="b1")
        record_promotion(wiki_root, slug="page-b", reason="r2", by="b2")
        audit = list_axiom_audit(wiki_root)
        slugs = {entry["slug"] for entry in audit}
        assert slugs == {"page-a", "page-b"}

    def test_cli_axiom_list_json(self, tmp_path: Path) -> None:
        from athenaeum._cmd_axiom import cmd_axiom

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="page-a", reason="bedrock", by="tristan")

        import argparse

        args = argparse.Namespace(
            axiom_target="list", path=tmp_path, json=True
        )
        rc = cmd_axiom(args)
        assert rc == 0

    def test_cli_axiom_promote_writes_ledger(self, tmp_path: Path) -> None:
        import argparse

        from athenaeum._cmd_axiom import cmd_axiom

        args = argparse.Namespace(
            axiom_target="promote",
            path=tmp_path,
            json=True,
            slug="page-a",
            reason="bedrock",
            by="tristan",
            scope=None,
        )
        rc = cmd_axiom(args)
        assert rc == 0
        wiki_root = tmp_path / "wiki"
        assert is_axiom_promoted(wiki_root, "page-a") is True

    def test_cli_axiom_demote_writes_ledger(self, tmp_path: Path) -> None:
        import argparse

        from athenaeum._cmd_axiom import cmd_axiom

        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(wiki_root, slug="page-a", reason="bedrock", by="tristan")

        args = argparse.Namespace(
            axiom_target="demote",
            path=tmp_path,
            json=True,
            slug="page-a",
            reason="reconsidered",
            by="tristan",
        )
        rc = cmd_axiom(args)
        assert rc == 0
        assert is_axiom_promoted(wiki_root, "page-a") is False

    def test_mcp_list_axiom_audit_tool(self, tmp_path: Path) -> None:
        pytest.importorskip("fastmcp")
        import asyncio

        from athenaeum.mcp_server import create_server

        raw = tmp_path / "raw"
        wiki = tmp_path / "wiki"
        raw.mkdir()
        wiki.mkdir()
        record_promotion(wiki, slug="page-a", reason="bedrock", by="tristan")

        server = create_server(raw_root=raw, wiki_root=wiki)

        async def _run() -> list[dict]:
            tool = await server.get_tool("list_axiom_audit")
            return tool.fn()

        result = asyncio.run(_run())
        assert len(result) == 1
        assert result[0]["slug"] == "page-a"
        assert result[0]["active"] is True
        assert result[0]["history"][0]["by"] == "tristan"


# ---------------------------------------------------------------------------
# AC4 — scoped axioms round-trip their scope through parse/serialize
# ---------------------------------------------------------------------------


class TestScopeRoundTrip:
    def test_scope_field_accepted_on_wikibase(self) -> None:
        from athenaeum.schemas import WikiBase

        m = WikiBase(uid="a1", type="concept", name="X", scope="applies to resume work")
        assert m.scope == "applies to resume work"

    def test_scope_absent_is_none(self) -> None:
        from athenaeum.schemas import WikiBase

        m = WikiBase(uid="a1", type="concept", name="X")
        assert m.scope is None

    def test_empty_string_scope_normalizes_to_none(self) -> None:
        from athenaeum.schemas import WikiBase

        m = WikiBase(uid="a1", type="concept", name="X", scope="")
        assert m.scope is None

    def test_validate_wiki_meta_surfaces_scope(self) -> None:
        # validate_wiki_meta (the #424 schema boundary) has no ledger access,
        # so it cannot know whether this axiom is backed by a promotion
        # record -- that check is #434's separate warn_if_unbacked_axiom,
        # exercised in TestUnbackedAxiomFlagged above. Here we only assert
        # the scope field itself surfaces through validation.
        meta = {
            "uid": "abc12345",
            "type": "concept",
            "name": "X",
            "memory_class": "axiom",
            "scope": "applies to resume work",
        }
        m = validate_wiki_meta(meta)
        assert m.scope == "applies to resume work"

    def test_round_trips_through_parse_and_render_frontmatter(self) -> None:
        original = (
            "---\n"
            "uid: axiom001\n"
            "type: concept\n"
            "name: Resume Voice\n"
            "memory_class: axiom\n"
            "scope: applies to resume work\n"
            "---\n"
            "Always write in first person.\n"
        )
        meta, body = parse_frontmatter(original)
        assert meta["scope"] == "applies to resume work"

        validated = validate_wiki_meta(meta)
        assert validated.scope == "applies to resume work"

        rendered = render_frontmatter(meta) + body
        reparsed_meta, reparsed_body = parse_frontmatter(rendered)
        assert reparsed_meta["scope"] == "applies to resume work"
        assert reparsed_body == body

    def test_round_trips_via_model_dump(self) -> None:
        meta = {
            "uid": "abc12345",
            "type": "concept",
            "name": "X",
            "memory_class": "axiom",
            "scope": "applies to resume work",
        }
        model = validate_wiki_meta(meta)
        dumped = model.model_dump(exclude_none=True)
        rendered = render_frontmatter(dumped)
        reparsed, _ = parse_frontmatter(rendered)
        assert reparsed["scope"] == "applies to resume work"

    def test_unscoped_axiom_has_no_scope_key_after_render(self) -> None:
        meta = {
            "uid": "abc12345",
            "type": "concept",
            "name": "X",
            "memory_class": "axiom",
        }
        model = validate_wiki_meta(meta)
        dumped = model.model_dump(exclude_none=True)
        assert "scope" not in dumped

    def test_scope_stored_on_promotion_record_is_distinct_from_frontmatter_scope(
        self, tmp_path: Path
    ) -> None:
        """The promotion record's scope is the human-approved scope AT THAT
        TIME; the frontmatter scope is the page's own current declaration.
        Both round-trip independently."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_promotion(
            wiki_root,
            slug="resume-style",
            reason="applies while drafting resumes",
            by="tristan",
            scope="applies to resume work",
        )
        ledger_records = read_axiom_ledger(wiki_root, slug="resume-style")
        assert ledger_records[0]["scope"] == "applies to resume work"

        meta = {
            "uid": "resume-style",
            "type": "concept",
            "name": "Resume Voice",
            "memory_class": "axiom",
            "scope": "applies to resume work",
        }
        model = validate_wiki_meta(meta)
        assert model.scope == "applies to resume work"
