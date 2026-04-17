"""End-to-end round-trip: init → remember → seed → rebuild → recall.

The unit tests in ``test_mcp_server.py`` and ``test_search.py`` exercise each
layer in isolation. This test pins the happy-path contract a first-adopter
actually cares about: after ``athenaeum init``, writing an observation with
``remember`` and searching compiled wiki pages with ``recall`` both work
against the same on-disk knowledge root without hand-wiring paths.

Deliberately skips the LLM compile step (``athenaeum run``) — that path is
covered by ``test_tiers.py`` / ``test_librarian.py`` and requires an
``ANTHROPIC_API_KEY``. Here we seed a wiki page directly to stand in for a
compiled entity, then verify the full read/write gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.mcp_server import recall_search, remember_write


def test_init_remember_rebuild_recall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Happy path: every piece of the read/write gate wires up correctly.

    Regression guard for the class of bugs where a path default drifts
    (``~/.cache/athenaeum`` vs. an explicit ``--cache-dir``, ``raw/`` vs.
    ``raw/claude-session/``) and the layers still pass their own unit tests
    but no longer talk to each other end-to-end.
    """
    knowledge = tmp_path / "knowledge"
    cache = tmp_path / "cache"

    # 1. init — sets up raw/, wiki/, athenaeum.yaml
    rc = main(["init", "--path", str(knowledge)])
    assert rc == 0
    assert (knowledge / "raw").is_dir()
    assert (knowledge / "wiki").is_dir()

    # 2. remember — write an observation to raw/
    result = remember_write(
        knowledge / "raw",
        "Acme Corp is a fintech client; quarterly review scheduled.",
        source="claude-session",
    )
    assert result.startswith("Saved to")
    raw_files = list((knowledge / "raw" / "claude-session").glob("*.md"))
    assert len(raw_files) == 1

    # 3. Stand in for ``athenaeum run``: seed a compiled wiki entity
    #    directly. The compile pipeline is exercised elsewhere; here we just
    #    need a page the FTS5 index can find.
    (knowledge / "wiki" / "acme-corp.md").write_text(
        "---\n"
        "uid: acme0001\n"
        "type: organization\n"
        "name: Acme Corp\n"
        "tags: [client, fintech]\n"
        "description: Enterprise fintech client\n"
        "---\n\n"
        "Acme Corp is an enterprise fintech client under quarterly review.\n"
    )

    # 4. rebuild-index — build the FTS5 index into the explicit cache dir
    rc = main([
        "rebuild-index",
        "--path", str(knowledge),
        "--cache-dir", str(cache),
        "--backend", "fts5",
    ])
    assert rc == 0
    capsys.readouterr()  # drain CLI output so later capsys users aren't polluted
    assert (cache / "wiki-index.db").is_file()

    # 5. recall — FTS5 backend must find the seeded page with an explicit
    #    cache_dir. The MCP tool defaults to ``~/.cache/athenaeum`` which
    #    would miss this tmp cache — passing it through is the contract
    #    ``athenaeum serve`` also relies on.
    output = recall_search(
        knowledge / "wiki",
        "Acme fintech client",
        top_k=5,
        search_backend="fts5",
        cache_dir=cache,
    )
    assert "Acme Corp" in output
    assert "wiki/acme-corp.md" in output
    assert "score:" in output


def test_remember_raw_is_visible_to_keyword_backend_without_cache(
    tmp_path: Path,
) -> None:
    """Keyword backend is the zero-setup fallback: no cache required.

    The keyword backend is specifically the code path that runs in the
    ``test-mcp`` smoke check and in tiny bootstrap wikis. If it ever starts
    needing a cache directory, the out-of-box experience breaks silently —
    recall returns "No wiki pages matched" on fresh installs.
    """
    knowledge = tmp_path / "knowledge"
    rc = main(["init", "--path", str(knowledge)])
    assert rc == 0

    (knowledge / "wiki" / "lean-startup.md").write_text(
        "---\n"
        "name: Lean Startup\n"
        "tags: [methodology]\n"
        "---\n\n"
        "The Lean Startup methodology.\n"
    )

    output = recall_search(
        knowledge / "wiki",
        "lean startup methodology",
        top_k=5,
        search_backend="keyword",
    )
    assert "Lean Startup" in output
