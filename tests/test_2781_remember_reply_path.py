# SPDX-License-Identifier: Apache-2.0
"""The ``remember`` reply names no absolute server path under http (athenaeum#2781)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    (knowledge / "wiki").mkdir(parents=True)
    (knowledge / "raw").mkdir(parents=True)
    return knowledge


def _remember(corpus: Path, transport: str | None, **kwargs: object) -> str:
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    server = create_server(
        raw_root=corpus / "raw",
        wiki_root=corpus / "wiki",
        cache_dir=corpus.parent / "cache",
        config={},
        transport=transport,
    )

    async def _run():
        return (await server.get_tool("remember")).fn

    return asyncio.run(_run())(content="A fact.", **kwargs)


def test_http_reply_is_root_relative(corpus: Path) -> None:
    reply = _remember(corpus, "http")
    assert reply.startswith("Saved to raw/remote-session/")
    assert reply.endswith(".md")
    assert str(corpus) not in reply
    assert not Path(reply[len("Saved to ") :]).is_absolute()
    # The file really is where the relative path says.
    assert (corpus / reply[len("Saved to ") :]).is_file()


def test_http_reply_with_a_caller_supplied_source_is_still_relative(corpus: Path) -> None:
    # A caller-supplied ``source`` is filed under the transport default
    # (athenaeum#2764); the reply stays root-relative either way.
    reply = _remember(corpus, "http", source="manual")
    assert reply.startswith("Saved to raw/remote-session/")
    assert str(corpus) not in reply
    assert not Path(reply[len("Saved to ") :]).is_absolute()


def test_stdio_reply_is_byte_identical_to_remember_write(corpus: Path) -> None:
    """Stdio keeps the absolute path exactly as ``remember_write`` returns it."""
    reply = _remember(corpus, None)
    (written,) = (corpus / "raw" / "claude-session").glob("*.md")
    assert reply == f"Saved to {written.resolve()}"
    assert Path(reply[len("Saved to ") :]).is_absolute()


def test_stdio_explicit_transport_name_is_also_unchanged(corpus: Path) -> None:
    reply = _remember(corpus, "stdio")
    assert str(corpus.resolve()) in reply


def test_http_error_replies_are_untouched(corpus: Path) -> None:
    reply = _remember(corpus, "http", bucket="not-a-bucket")
    assert reply.startswith("Error: invalid `bucket`")
    assert str(corpus) not in reply
