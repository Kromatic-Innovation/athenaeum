# SPDX-License-Identifier: Apache-2.0
"""Tests for bounding the decisions/questions read path (issue athenaeum#1431).

``list_pending_decisions`` and ``list_unanswered`` returned every pending
item in one unbounded array — against the live corpus that was 11,355,998
bytes across 8,632 items, which breaks the MCP stdio transport
(``Connection closed``). This adds offset/limit pagination with a
total-count envelope at the MCP boundary, while keeping every direct
library caller (notably the ``athenaeum decisions`` CLI, whose ``_counts()``
helper must see every item) unbounded by default.

Covers, per athenaeum#1431's acceptance criteria:

- AC2 (bounded default): an unparameterized MCP call returns a single
  bounded page, not the whole backlog.
- AC4 (byte ceiling): that bounded page stays well under the size that broke
  the stdio transport — the mechanical regression guard for "someone removed
  pagination later."
- AC3 (ordering preserved across pages): paging with a small limit and
  concatenating pages reproduces the full oldest-first list exactly, with no
  duplicates or gaps.
- Backward compatibility: the library functions stay unbounded by default.
- Edge cases: offset past the end, negative offset, non-positive limit.
- The ``ATHENAEUM_DECISIONS_PAGE_LIMIT`` / ``librarian.decisions_page_limit``
  config knob.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from athenaeum.answers import list_unanswered, parse_pending_questions
from athenaeum.config import resolve_decisions_page_limit
from athenaeum.decisions import list_pending_decisions, list_pending_decisions_page
from athenaeum.pagination import paginate

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

# Matches the grammar `_split_blocks`/`_parse_block` in athenaeum.answers
# expect: a canonical `## [DATE] Entity: "..." (from ...)` header, a
# checkbox line carrying the question text, then `**Conflict type**:` /
# `**Description**:` meta lines. See the fixture in
# tests/test_mcp_server.py::TestListPendingDecisions for the same grammar
# used at a smaller scale.


def _write_bulk_pending_questions(
    path: Path,
    n: int,
    *,
    dates: list[str] | None = None,
) -> None:
    """Write ``n`` well-formed pending-question blocks to ``path``.

    Entity name and source path are indexed so each block's derived id
    (header + question text) is unique. ``dates`` lets a caller control the
    ``created_at`` (the header's ``[DATE]``) independently of file order —
    used by the ordering test below to prove the post-sort slice actually
    sorts rather than merely truncating file order.
    """
    lines = ["# Pending Questions", ""]
    for i in range(n):
        date_str = dates[i] if dates is not None else f"2026-01-{(i % 28) + 1:02d}"
        lines.append(f'## [{date_str}] Entity: "Bulk Entity {i}" (from sessions/bulk-{i}.md)')
        lines.append(f"- [ ] Is bulk fixture question {i} still true?")
        lines.append("**Conflict type**: principled")
        lines.append(f"**Description**: description for item {i}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_server(tmp_path: Path):
    from athenaeum.mcp_server import create_server

    raw = tmp_path / "raw"
    wiki = tmp_path / "wiki"
    raw.mkdir()
    wiki.mkdir()
    server = create_server(raw_root=raw, wiki_root=wiki)
    return server, raw, wiki


# ---------------------------------------------------------------------------
# AC2: bounded default + AC4: byte-ceiling regression guard
# ---------------------------------------------------------------------------


class TestBoundedDefault:
    def test_list_pending_decisions_tool_default_is_one_bounded_page(
        self, tmp_path: Path
    ) -> None:
        """AC2: an unparameterized MCP call returns a single bounded page."""
        pytest.importorskip("fastmcp")
        server, raw, wiki = _make_server(tmp_path)
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 3000)

        # Sanity: the fixture actually parses into 3000 unanswered items —
        # otherwise a byte-ceiling assertion below would pass vacuously
        # because the fixture failed to parse, not because pagination works.
        assert len(list_unanswered(wiki / "_pending_questions.md")) == 3000

        async def _run() -> dict:
            tool = await server.get_tool("list_pending_decisions")
            return tool.fn()

        result = asyncio.run(_run())
        assert result["total"] == 3000
        assert len(result["items"]) <= 50
        assert result["next_offset"] == 50

    def test_list_pending_decisions_byte_ceiling(self, tmp_path: Path) -> None:
        """AC4: the bounded envelope stays well under the size that broke
        the MCP stdio transport (11,355,998 bytes / 8,632 items,
        ``Connection closed``). This is the mechanical guard that catches a
        future regression that removes pagination or defaults the limit to
        ``None``.
        """
        pytest.importorskip("fastmcp")
        server, raw, wiki = _make_server(tmp_path)
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 3000)
        assert len(list_unanswered(wiki / "_pending_questions.md")) == 3000

        async def _run() -> dict:
            tool = await server.get_tool("list_pending_decisions")
            return tool.fn()

        result = asyncio.run(_run())
        encoded = json.dumps(result).encode("utf-8")
        assert len(encoded) < 1_000_000

    def test_list_pending_questions_tool_default_is_one_bounded_page(
        self, tmp_path: Path
    ) -> None:
        """AC2, same guarantee for the sibling ``list_pending_questions`` tool."""
        pytest.importorskip("fastmcp")
        server, raw, wiki = _make_server(tmp_path)
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 3000)
        assert len(list_unanswered(wiki / "_pending_questions.md")) == 3000

        async def _run() -> dict:
            tool = await server.get_tool("list_pending_questions")
            return tool.fn()

        result = asyncio.run(_run())
        assert result["total"] == 3000
        assert len(result["items"]) <= 50
        assert result["next_offset"] == 50

    def test_list_pending_questions_byte_ceiling(self, tmp_path: Path) -> None:
        """AC4 for ``list_pending_questions``."""
        pytest.importorskip("fastmcp")
        server, raw, wiki = _make_server(tmp_path)
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 3000)
        assert len(list_unanswered(wiki / "_pending_questions.md")) == 3000

        async def _run() -> dict:
            tool = await server.get_tool("list_pending_questions")
            return tool.fn()

        result = asyncio.run(_run())
        encoded = json.dumps(result).encode("utf-8")
        assert len(encoded) < 1_000_000

    def test_list_pending_decisions_non_positive_limit_does_not_unbound(
        self, tmp_path: Path
    ) -> None:
        """A ``limit`` of ``0`` or negative at the MCP boundary must NOT
        reintroduce the unbounded-list failure this issue fixes.

        ``0`` is exactly the value an LLM client is likely to send meaning
        "no limit". Unlike the library functions (where non-positive means
        unbounded, by design — see ``TestEdgeCases`` below), the MCP tools
        are the transport-safety boundary and must always resolve a
        non-positive/omitted ``limit`` to the same positive page-limit
        default.
        """
        pytest.importorskip("fastmcp")
        server, raw, wiki = _make_server(tmp_path)
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 3000)
        assert len(list_unanswered(wiki / "_pending_questions.md")) == 3000

        async def _run(limit: int) -> dict:
            tool = await server.get_tool("list_pending_decisions")
            return tool.fn(limit=limit)

        for degenerate_limit in (0, -1):
            result = asyncio.run(_run(degenerate_limit))
            assert len(result["items"]) == 50
            assert result["limit"] == 50
            assert result["next_offset"] == 50
            # The mechanical byte-ceiling guard belongs here too: this is
            # precisely the path that would otherwise blow the transport.
            encoded = json.dumps(result).encode("utf-8")
            assert len(encoded) < 1_000_000

    def test_list_pending_questions_non_positive_limit_does_not_unbound(
        self, tmp_path: Path
    ) -> None:
        """Same MCP-boundary guarantee for ``list_pending_questions``."""
        pytest.importorskip("fastmcp")
        server, raw, wiki = _make_server(tmp_path)
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 3000)
        assert len(list_unanswered(wiki / "_pending_questions.md")) == 3000

        async def _run(limit: int) -> dict:
            tool = await server.get_tool("list_pending_questions")
            return tool.fn(limit=limit)

        for degenerate_limit in (0, -1):
            result = asyncio.run(_run(degenerate_limit))
            assert len(result["items"]) == 50
            assert result["limit"] == 50
            assert result["next_offset"] == 50
            encoded = json.dumps(result).encode("utf-8")
            assert len(encoded) < 1_000_000


# ---------------------------------------------------------------------------
# AC3: ordering preserved across pages, no duplicates or gaps
# ---------------------------------------------------------------------------


class TestOrderingAcrossPages:
    def test_paging_reproduces_the_full_oldest_first_list(self, tmp_path: Path) -> None:
        """Page through with a small limit and confirm the concatenation
        equals the full unpaginated library-level list exactly — same
        order, same length, no duplicates.

        The fixture's dates are deliberately the REVERSE of file order (item
        0 gets the newest date, the last item gets the oldest), so this only
        passes if the slice is taken after the ``created_at`` sort — a bug
        that paged file order directly would fail this test.
        """
        pytest.importorskip("fastmcp")
        server, raw, wiki = _make_server(tmp_path)
        n = 23
        # Reverse-of-file-order dates: file index 0 -> latest date, file
        # index n-1 -> earliest date.
        dates = [f"2026-03-{(n - i):02d}" for i in range(n)]
        _write_bulk_pending_questions(wiki / "_pending_questions.md", n, dates=dates)
        assert len(list_unanswered(wiki / "_pending_questions.md")) == n

        full = list_pending_decisions(wiki)
        assert len(full) == n
        full_ids = [d["id"] for d in full]

        # The fixture's dates strictly reverse file order, so the oldest-
        # first sorted list must equal file order REVERSED, and must NOT
        # equal file order itself — otherwise this test would pass even if
        # a bug paged raw file order instead of the post-sort list.
        file_order_ids = [
            pq.id for pq in parse_pending_questions(wiki / "_pending_questions.md")
        ]
        assert full_ids == list(reversed(file_order_ids))
        assert full_ids != file_order_ids

        async def _run(offset: int, limit: int) -> dict:
            tool = await server.get_tool("list_pending_decisions")
            return tool.fn(offset=offset, limit=limit)

        collected_ids: list[str] = []
        offset = 0
        pages = 0
        while True:
            result = asyncio.run(_run(offset, 7))
            collected_ids.extend(d["id"] for d in result["items"])
            pages += 1
            assert pages <= n  # guard against an infinite loop on a bug
            if result["next_offset"] is None:
                break
            offset = result["next_offset"]

        assert collected_ids == full_ids
        assert len(collected_ids) == len(set(collected_ids))


# ---------------------------------------------------------------------------
# Backward compatibility: unbounded by default for direct callers
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_list_pending_decisions_unbounded_by_default(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 120)
        result = list_pending_decisions(wiki)
        assert len(result) == 120

    def test_list_unanswered_unbounded_by_default(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        pending_path = wiki / "_pending_questions.md"
        _write_bulk_pending_questions(pending_path, 120)
        result = list_unanswered(pending_path)
        assert len(result) == 120


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_offset_beyond_total_returns_empty_page(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 10)

        page = list_pending_decisions_page(wiki, offset=1000, limit=5)
        assert page["items"] == []
        assert page["total"] == 10
        assert page["next_offset"] is None

    def test_negative_offset_clamps_to_zero(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 10)

        page = list_pending_decisions_page(wiki, offset=-5, limit=3)
        assert page["offset"] == 0
        assert len(page["items"]) == 3

        pending_path = wiki / "_pending_questions.md"
        unanswered = list_unanswered(pending_path, offset=-5, limit=3)
        assert len(unanswered) == 3

    def test_zero_or_negative_limit_is_unbounded(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 10)

        assert len(list_pending_decisions(wiki, limit=0)) == 10
        assert len(list_pending_decisions(wiki, limit=-3)) == 10

        page = list_pending_decisions_page(wiki, offset=0, limit=0)
        assert page["limit"] is None
        assert len(page["items"]) == 10
        assert page["next_offset"] is None

        pending_path = wiki / "_pending_questions.md"
        assert len(list_unanswered(pending_path, limit=0)) == 10
        assert len(list_unanswered(pending_path, limit=-1)) == 10


# ---------------------------------------------------------------------------
# `paginate` itself — the canonical rule, pinned at its own site.
# ---------------------------------------------------------------------------


class TestPaginateHelper:
    """Direct unit tests of :func:`athenaeum.pagination.paginate`.

    This is the single site the offset/limit/next_offset arithmetic lives
    now (issue athenaeum#1431) — every other call site (``answers.list_unanswered``,
    ``decisions.list_pending_decisions``, ``decisions.list_pending_decisions_page``,
    and the MCP tools after resolving a positive default) delegates here, so
    pinning the rule at its own site is what keeps them all in sync.
    """

    def test_basic_page(self) -> None:
        items = [{"n": i} for i in range(10)]
        page = paginate(items, offset=2, limit=3)
        assert page["items"] == [{"n": 2}, {"n": 3}, {"n": 4}]
        assert page["total"] == 10
        assert page["offset"] == 2
        assert page["limit"] == 3
        assert page["next_offset"] == 5

    def test_last_page_has_no_next_offset(self) -> None:
        items = [{"n": i} for i in range(10)]
        page = paginate(items, offset=8, limit=5)
        assert page["items"] == [{"n": 8}, {"n": 9}]
        assert page["next_offset"] is None

    def test_none_limit_is_unbounded(self) -> None:
        items = [{"n": i} for i in range(10)]
        page = paginate(items, offset=3, limit=None)
        assert len(page["items"]) == 7
        assert page["limit"] is None
        assert page["next_offset"] is None

    def test_zero_or_negative_limit_is_unbounded(self) -> None:
        items = [{"n": i} for i in range(10)]
        assert len(paginate(items, limit=0)["items"]) == 10
        assert paginate(items, limit=0)["limit"] is None
        assert len(paginate(items, limit=-7)["items"]) == 10
        assert paginate(items, limit=-7)["limit"] is None

    def test_negative_offset_clamps_to_zero(self) -> None:
        items = [{"n": i} for i in range(5)]
        page = paginate(items, offset=-100, limit=2)
        assert page["offset"] == 0
        assert page["items"] == [{"n": 0}, {"n": 1}]

    def test_offset_beyond_total_returns_empty_with_no_next_offset(self) -> None:
        items = [{"n": i} for i in range(5)]
        page = paginate(items, offset=999, limit=2)
        assert page["items"] == []
        assert page["total"] == 5
        assert page["next_offset"] is None

    def test_empty_items(self) -> None:
        page = paginate([], offset=0, limit=10)
        assert page["items"] == []
        assert page["total"] == 0
        assert page["next_offset"] is None


# ---------------------------------------------------------------------------
# Config knob
# ---------------------------------------------------------------------------


class TestConfigKnob:
    def test_resolve_decisions_page_limit_default(self) -> None:
        assert resolve_decisions_page_limit(None) == 50

    def test_env_var_changes_mcp_tool_default_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("fastmcp")
        server, raw, wiki = _make_server(tmp_path)
        _write_bulk_pending_questions(wiki / "_pending_questions.md", 30)
        assert len(list_unanswered(wiki / "_pending_questions.md")) == 30

        monkeypatch.setenv("ATHENAEUM_DECISIONS_PAGE_LIMIT", "5")

        async def _run() -> dict:
            tool = await server.get_tool("list_pending_decisions")
            return tool.fn()

        result = asyncio.run(_run())
        assert result["limit"] == 5
        assert len(result["items"]) == 5
        assert result["next_offset"] == 5
