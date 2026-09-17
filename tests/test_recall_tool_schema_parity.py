# SPDX-License-Identifier: Apache-2.0
"""Pins that ``tests/evals/rollout.py``'s api-mode ``recall`` tool schema
never silently drifts from the REAL served ``recall`` tool (Quine review,
issue athenaeum#1733, SHOULD item 4): a new parameter added to the real
``recall`` function's signature must fail this test until the api-mode
schema is updated to match, rather than only being caught (or missed) by
eye.

Compares against a live server built via ``create_server().list_tools()`` --
the same introspection API ``tests/test_mcp_server.py`` and
``tests/test_security_posture_tool_parity.py`` already use -- not a
hand-copied expectation.

Unmarked: no network, no ``eval``/``rollout`` marker needed (constructing a
FastMCP server and listing its tools touches no live model and no corpus
scale beyond an empty temp wiki).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest


def _build_server(tmp_path: Path):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    raw = tmp_path / "raw"
    wiki = tmp_path / "wiki"
    raw.mkdir()
    wiki.mkdir()
    return create_server(raw_root=raw, wiki_root=wiki)


def _recall_tool(server):
    async def _list():
        return {t.name: t for t in await server.list_tools()}

    return asyncio.run(_list())["recall"]


def test_api_mode_recall_description_matches_the_real_served_tool(tmp_path: Path) -> None:
    from athenaeum.entity_schema import declared_entity_classes
    from athenaeum.mcp_server import recall_tool_docstring
    from tests.evals.rollout import _recall_tool_schema

    server = _build_server(tmp_path)
    real_tool = _recall_tool(server)

    wiki_root = tmp_path / "wiki"
    entity_classes_str = ", ".join(sorted(declared_entity_classes(wiki_root))) or "(none yet)"
    # FastMCP renders a tool's `.description` via the SAME dedent Python's
    # own `inspect.getdoc`/`cleandoc` apply to a docstring (stripping the
    # function-body indentation `recall_tool_docstring`'s raw triple-quoted
    # string still carries), AND it is only the SUMMARY portion -- everything
    # before "Args:" -- with each parameter's own text decomposed into the
    # schema instead (verified empirically against a live server; the second
    # test below pins that decomposition's property names/required set).
    full_doc = inspect.cleandoc(recall_tool_docstring(entity_classes_str))
    expected_description = full_doc.split("\n\nArgs:")[0].strip()

    assert real_tool.description == expected_description

    api_mode_schema = _recall_tool_schema(wiki_root)
    assert api_mode_schema["description"] == real_tool.description


def test_api_mode_recall_input_schema_property_names_match_the_real_served_tool(
    tmp_path: Path,
) -> None:
    """A new parameter on the real ``recall(...)`` signature changes what
    FastMCP puts in ``inputSchema["properties"]``; this must fail until
    ``RECALL_TOOL_INPUT_SCHEMA`` (and thus the api-mode tool) is updated to
    match -- the whole point of pinning this against the LIVE server rather
    than a second hand-written expectation of what the signature is."""
    from tests.evals.rollout import RECALL_TOOL_INPUT_SCHEMA

    server = _build_server(tmp_path)
    real_tool = _recall_tool(server)

    # FastMCP's own generated JSON schema lives on `.parameters` (a
    # `FunctionTool` attribute), not `.inputSchema`.
    real_properties = set(real_tool.parameters["properties"].keys())
    api_mode_properties = set(RECALL_TOOL_INPUT_SCHEMA["properties"].keys())
    assert api_mode_properties == real_properties

    real_required = set(real_tool.parameters.get("required") or [])
    api_mode_required = set(RECALL_TOOL_INPUT_SCHEMA.get("required") or [])
    assert api_mode_required == real_required
