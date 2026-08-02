# SPDX-License-Identifier: Apache-2.0
"""Audience scoping across the whole MCP surface (issue athenaeum#538).

Before athenaeum#538 only ``recall`` applied ``caller_audience``. A restricted caller
could route around it by asking a page-content-bearing LIST tool for the same
bytes, and could mutate the operator's human-decision queue unchecked. These
tests pin the closed hole on both axes:

- Read path: ``list_pending_questions`` / ``list_pending_merges`` /
  ``list_pending_decisions`` apply the SAME fail-closed predicate ``recall``
  applies — a restricted caller sees only items whose source pages authorize.
- Write path: ``resolve_question`` / ``resolve_merge`` / ``review_audit_item``
  fail closed for any restricted caller; ``remember`` stays open.

Owner (``caller_audience=None``) behavior is unchanged throughout — every
restricted assertion has an owner-sees-it counterpart.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athenaeum.answers import list_unanswered
from athenaeum.decisions import list_pending_decisions
from athenaeum.models import (
    all_sources_authorized,
    is_page_authorized_at,
)
from athenaeum.pending_merges import list_pending_merges, render_block

RESTRICTED = {"secondary"}


# --------------------------------------------------------------------------
# models: the path-based fail-closed authz primitives
# --------------------------------------------------------------------------


class TestIsPageAuthorizedAt:
    def _page(self, tmp_path: Path, name: str, frontmatter: str) -> Path:
        p = tmp_path / name
        p.write_text(f"---\nname: {name}\n{frontmatter}---\nbody.\n", encoding="utf-8")
        return p

    def test_owner_authorized_without_even_reading(self, tmp_path: Path) -> None:
        # A path that does not exist is still authorized for the owner — the
        # file is never read when caller_audience is None.
        assert is_page_authorized_at(tmp_path / "nope.md", None) is True

    def test_restricted_public_page_authorized(self, tmp_path: Path) -> None:
        page = self._page(tmp_path, "open.md", "access: open\n")
        assert is_page_authorized_at(page, RESTRICTED) is True

    def test_restricted_untagged_page_withheld(self, tmp_path: Path) -> None:
        page = self._page(tmp_path, "plain.md", "")
        assert is_page_authorized_at(page, RESTRICTED) is False

    def test_restricted_matching_role_authorized(self, tmp_path: Path) -> None:
        page = self._page(tmp_path, "role.md", "audience:\n  - secondary\n")
        assert is_page_authorized_at(page, RESTRICTED) is True

    def test_restricted_nonmatching_role_withheld(self, tmp_path: Path) -> None:
        page = self._page(tmp_path, "role.md", "audience:\n  - other\n")
        assert is_page_authorized_at(page, RESTRICTED) is False

    def test_restricted_missing_file_fails_closed(self, tmp_path: Path) -> None:
        # The core fail-closed guarantee: an unreadable path is NEVER authorized
        # for a restricted caller — no routing around scope via a bad path.
        assert is_page_authorized_at(tmp_path / "ghost.md", RESTRICTED) is False

    def test_relative_source_resolved_against_base(self, tmp_path: Path) -> None:
        self._page(tmp_path, "open.md", "access: open\n")
        assert is_page_authorized_at("open.md", RESTRICTED, base=tmp_path) is True
        assert is_page_authorized_at("open.md", RESTRICTED, base=tmp_path / "x") is False


class TestAllSourcesAuthorized:
    def test_owner_always(self, tmp_path: Path) -> None:
        assert all_sources_authorized([], None) is True
        assert all_sources_authorized([tmp_path / "ghost.md"], None) is True

    def test_restricted_empty_fails_closed(self, tmp_path: Path) -> None:
        # Nothing to authorize against => withheld, not a vacuous all([])==True.
        assert all_sources_authorized([], RESTRICTED) is False

    def test_restricted_all_public_authorized(self, tmp_path: Path) -> None:
        for n in ("a.md", "b.md"):
            (tmp_path / n).write_text("---\naccess: open\n---\nx.\n", encoding="utf-8")
        srcs = [tmp_path / "a.md", tmp_path / "b.md"]
        assert all_sources_authorized(srcs, RESTRICTED) is True

    def test_restricted_one_unauthorized_withholds_all(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("---\naccess: open\n---\nx.\n", encoding="utf-8")
        (tmp_path / "b.md").write_text("---\nname: b\n---\nx.\n", encoding="utf-8")
        srcs = [tmp_path / "a.md", tmp_path / "b.md"]
        assert all_sources_authorized(srcs, RESTRICTED) is False


# --------------------------------------------------------------------------
# read path: the module-level list functions filter by source authorization
# --------------------------------------------------------------------------


def _make_page(root: Path, name: str, frontmatter: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text(f"---\nname: {name}\n{frontmatter}---\nsecret body about acme.\n",
                 encoding="utf-8")
    return p


def _seed_merge(wiki: Path, sources: list[Path]) -> None:
    block = render_block(
        merge_target_name="acme",
        sources=[str(s) for s in sources],
        rationale="dupes",
        draft_merged_body="CONFIDENTIAL merged body about acme.",
        confidence=0.9,
    )
    (wiki / "_pending_merges.md").write_text(block + "\n", encoding="utf-8")


class TestListPendingMergesScoping:
    def test_owner_sees_merge_over_untagged_sources(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        src = _make_page(wiki, "acme_a.md", "")
        _seed_merge(wiki, [src])
        owner = list_pending_merges(wiki / "_pending_merges.md")
        assert len(owner) == 1
        assert "CONFIDENTIAL" in owner[0]["draft_merged_body"]

    def test_restricted_withheld_from_untagged_sources(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        src = _make_page(wiki, "acme_a.md", "")
        _seed_merge(wiki, [src])
        restricted = list_pending_merges(
            wiki / "_pending_merges.md",
            caller_audience=RESTRICTED,
            knowledge_root=tmp_path,
        )
        assert restricted == []

    def test_restricted_sees_merge_over_public_sources(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        src = _make_page(wiki, "acme_a.md", "access: open\n")
        _seed_merge(wiki, [src])
        restricted = list_pending_merges(
            wiki / "_pending_merges.md",
            caller_audience=RESTRICTED,
            knowledge_root=tmp_path,
        )
        assert len(restricted) == 1

    def test_restricted_withheld_if_any_source_unauthorized(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        ok = _make_page(wiki, "acme_a.md", "access: open\n")
        bad = _make_page(wiki, "acme_b.md", "")  # untagged -> withheld
        _seed_merge(wiki, [ok, bad])
        restricted = list_pending_merges(
            wiki / "_pending_merges.md",
            caller_audience=RESTRICTED,
            knowledge_root=tmp_path,
        )
        assert restricted == []


class TestListUnansweredScoping:
    def _seed_question(self, wiki: Path, source: Path) -> None:
        block = (
            f'## [2026-07-30] Entity: "Acme" (from {source})\n'
            f"- [ ] Which revenue figure is correct?\n"
            f"**Conflict type**: value-mismatch\n"
            f"**Description**: two figures disagree.\n"
        )
        (wiki / "_pending_questions.md").write_text(block + "\n", encoding="utf-8")

    def test_owner_sees_question(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        src = _make_page(wiki, "acme_a.md", "")
        self._seed_question(wiki, src)
        owner = list_unanswered(wiki / "_pending_questions.md")
        assert len(owner) == 1

    def test_restricted_withheld_untagged_source(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        src = _make_page(wiki, "acme_a.md", "")
        self._seed_question(wiki, src)
        restricted = list_unanswered(
            wiki / "_pending_questions.md",
            caller_audience=RESTRICTED,
            knowledge_root=tmp_path,
        )
        assert restricted == []

    def test_restricted_sees_public_source(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        src = _make_page(wiki, "acme_a.md", "access: open\n")
        self._seed_question(wiki, src)
        restricted = list_unanswered(
            wiki / "_pending_questions.md",
            caller_audience=RESTRICTED,
            knowledge_root=tmp_path,
        )
        assert len(restricted) == 1


class TestListPendingDecisionsScoping:
    def test_restricted_filters_merges_and_questions(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        public = _make_page(wiki, "acme_a.md", "access: open\n")
        secret = _make_page(wiki, "acme_b.md", "")
        # One merge over a secret source (withheld) — owner sees it.
        _seed_merge(wiki, [secret])
        owner = list_pending_decisions(wiki)
        assert any(d["type"] == "merge" for d in owner)
        restricted = list_pending_decisions(wiki, caller_audience=RESTRICTED)
        assert all(d["type"] != "merge" for d in restricted)
        # A public-sourced merge IS visible to the restricted caller.
        _seed_merge(wiki, [public])
        restricted2 = list_pending_decisions(wiki, caller_audience=RESTRICTED)
        assert any(d["type"] == "merge" for d in restricted2)


# --------------------------------------------------------------------------
# write path + read wiring: the live MCP server surface
# --------------------------------------------------------------------------


def _server(tmp_path: Path, *, caller_audience: set[str] | None):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    raw = tmp_path / "raw"
    wiki = tmp_path / "wiki"
    raw.mkdir(exist_ok=True)
    wiki.mkdir(exist_ok=True)
    return create_server(
        raw_root=raw, wiki_root=wiki, caller_audience=caller_audience
    )


def _call(server, name: str, caller):
    async def _run():
        tool = await server.get_tool(name)
        return caller(tool.fn)

    return asyncio.run(_run())


_WRITE_TOOLS = {
    "resolve_question": lambda fn: fn("no-such-id", "an answer"),
    "resolve_merge": lambda fn: fn("no-such-id", "reject"),
    "review_audit_item": lambda fn: fn("no-such-id", "confirm"),
}


class TestWriteGuards:
    @pytest.mark.parametrize("name", sorted(_WRITE_TOOLS))
    def test_restricted_caller_fails_closed(self, tmp_path: Path, name: str) -> None:
        server = _server(tmp_path, caller_audience=RESTRICTED)
        result = _call(server, name, _WRITE_TOOLS[name])
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result.get("error_code") == "forbidden"

    @pytest.mark.parametrize("name", sorted(_WRITE_TOOLS))
    def test_owner_reaches_normal_path(self, tmp_path: Path, name: str) -> None:
        # Owner is NOT forbidden — it reaches the real handler, which returns a
        # not-found/normal error for the bogus id (never error_code forbidden).
        server = _server(tmp_path, caller_audience=None)
        result = _call(server, name, _WRITE_TOOLS[name])
        assert isinstance(result, dict)
        assert result.get("error_code") != "forbidden"

    def test_remember_stays_open_for_restricted_caller(self, tmp_path: Path) -> None:
        # remember is deliberately NOT audience-guarded (intake is screened
        # downstream) — a restricted caller can still write.
        server = _server(tmp_path, caller_audience=RESTRICTED)
        result = _call(
            server, "remember", lambda fn: fn("a note", source="s")
        )
        assert isinstance(result, str)
        assert result.startswith("Saved to")


class TestReadWiringThroughServer:
    def test_list_pending_merges_tool_filters_for_restricted(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        src = _make_page(wiki, "acme_a.md", "")  # untagged -> withheld
        _seed_merge(wiki, [src])
        (tmp_path / "raw").mkdir(exist_ok=True)

        owner_srv = _server(tmp_path, caller_audience=None)
        assert len(_call(owner_srv, "list_pending_merges", lambda fn: fn())) == 1

        restricted_srv = _server(tmp_path, caller_audience=RESTRICTED)
        assert _call(restricted_srv, "list_pending_merges", lambda fn: fn()) == []
