"""Pin test: docs/design/security-posture.md section 2.1 lists every MCP tool
``create_server`` registers (issue athenaeum#1384).

Two reusable pure functions do the derivation:

- :func:`registered_tool_names` reads the registered-tool set off a live
  FastMCP server via its own ``list_tools()`` introspection API — not a
  hand-written list in this file — so it can never drift from what
  ``create_server`` actually registers, whether via the ``@mcp.tool()``
  decorator or an explicit ``mcp.tool()(fn)`` call (``recall`` and
  ``enumerate_entities`` use the latter form; see ``src/athenaeum/mcp_server.py``).
- :func:`documented_tool_names` parses the section 2.1 "Tool group / Tools /
  Restricted behavior" table out of a markdown string and returns every
  backtick-quoted name in its "Tools" column.

Both the live-doc comparison and the synthetic-negative fixture below run
THROUGH THE SAME :func:`documented_tool_names` function, so a parser bug that
happens to return an empty (or a total) set on both sides cannot make the
live comparison pass for the wrong reason.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

SECURITY_POSTURE_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "design" / "security-posture.md"
)

_TOOL_NAME_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def registered_tool_names(server) -> set[str]:
    """Every tool name ``create_server`` registered on *server*.

    Uses FastMCP's own ``list_tools()`` (the same public API
    ``tests/test_mcp_server.py::TestEntitySchemaToolAndConfigDerivedSchema``
    already relies on for tool-presence assertions) rather than introspecting
    private attributes or re-deriving names from source.
    """

    async def _list() -> set[str]:
        return {t.name for t in await server.list_tools()}

    return asyncio.run(_list())


def _extract_section(doc_text: str, heading_prefix: str) -> str:
    """Lines of *doc_text* from the line starting with *heading_prefix* up to
    (not including) the next ``### `` heading, or to end-of-text."""
    lines = doc_text.splitlines()
    start: int | None = None
    end = len(lines)
    for i, line in enumerate(lines):
        if start is None and line.startswith(heading_prefix):
            start = i
            continue
        if start is not None and i > start and line.startswith("### "):
            end = i
            break
    if start is None:
        return ""
    return "\n".join(lines[start:end])


def documented_tool_names(doc_text: str) -> set[str]:
    """Every tool name listed in section 2.1's "Tool group / Tools /
    Restricted behavior" markdown table, read out of its "Tools" column.

    Pure function over an arbitrary markdown string (live doc text or a
    synthetic fixture) — see module docstring for why that matters.
    """
    section = _extract_section(doc_text, "### 2.1")
    names: set[str] = set()
    in_table = False
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table:
                # Table block ended (blank line / prose resumed).
                break
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if cells[0].lower() == "tool group":
            in_table = True
            continue
        if not in_table:
            continue
        if set(cells[0]) <= {"-", ":", ""}:
            # Header/body separator row, e.g. "---|---|---".
            continue
        names.update(_TOOL_NAME_RE.findall(cells[1]))
    return names


def _build_server(tmp_path: Path):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    raw = tmp_path / "raw"
    wiki = tmp_path / "wiki"
    raw.mkdir()
    wiki.mkdir()
    return create_server(raw_root=raw, wiki_root=wiki)


class TestSecurityPostureToolParity:
    def test_every_registered_tool_is_documented_in_2_1(self, tmp_path: Path) -> None:
        server = _build_server(tmp_path)
        registered = registered_tool_names(server)
        documented = documented_tool_names(
            SECURITY_POSTURE_PATH.read_text(encoding="utf-8")
        )

        # Sanity: neither derivation degenerated to empty (the exact failure
        # mode a live-vs-live-only test cannot catch — see
        # test_synthetic_doc_missing_tool_is_reported_missing below).
        assert registered, "sanity: create_server registered zero tools"
        assert documented, (
            "sanity: parsed zero tool names out of docs/design/security-posture.md "
            "section 2.1 -- table format may have changed"
        )
        assert registered == documented, (
            f"registered but not documented in section 2.1: "
            f"{sorted(registered - documented)}; "
            f"documented in section 2.1 but not registered: "
            f"{sorted(documented - registered)}"
        )

    def test_synthetic_doc_missing_tool_is_reported_missing(self) -> None:
        # Take the REAL section 2.1 text and remove one known tool's
        # backtick-quoted name from it, then confirm documented_tool_names
        # -- the exact function the live comparison above uses -- reports it
        # missing. This is the AC's hard requirement: a test that only
        # compared live-server to live-doc would pass even if the extraction
        # returned an empty set on both sides; this fixture proves the
        # parser is actually finding names, and actually notices when one is
        # gone.
        real_text = SECURITY_POSTURE_PATH.read_text(encoding="utf-8")
        omitted_tool = "entity_schema"
        needle = f"`{omitted_tool}`"
        assert needle in real_text, (
            f"fixture precondition failed: {needle} not found verbatim in "
            "docs/design/security-posture.md -- update this fixture if the doc's "
            "wording changed"
        )
        synthetic_text = real_text.replace(needle, "")

        before = documented_tool_names(real_text)
        after = documented_tool_names(synthetic_text)

        assert omitted_tool in before
        assert omitted_tool not in after
        assert omitted_tool in (before - after)
