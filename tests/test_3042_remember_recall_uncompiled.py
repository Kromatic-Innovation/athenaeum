# SPDX-License-Identifier: Apache-2.0
"""A note saved with ``remember`` is recallable as ``[uncompiled]`` with no
credential and no manual reindex (athenaeum#3042).

Two gaps, one per test class:

* ``remember`` refreshed the ``vector`` index even on an install without
  chromadb, where ``recall`` answers from the ``fts5`` index. The build raised
  ImportError, was swallowed, and no index existed for ``recall`` to read.
* ``session-end`` skipped its reindex when the compile failed (for example for
  a missing credential), leaving the index older than raw intake.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import athenaeum.librarian as lib
from athenaeum.init import init_knowledge_dir

NOTE = "The launch code for Project Zephyr is ZX-9942."

# Runs in a fresh interpreter. chromadb is blocked, the way a plain
# `athenaeum[mcp]` install lacks it; the server is built the way `serve` does.
_DRIVER = textwrap.dedent(
    """
    import asyncio, sys
    from pathlib import Path

    sys.modules["chromadb"] = None
    from athenaeum.config import load_config, resolve_cache_dir, resolve_search_backend
    from athenaeum.mcp_server import create_server

    knowledge = Path(sys.argv[2])
    cfg = load_config(knowledge)
    server = create_server(
        raw_root=knowledge / "raw",
        wiki_root=knowledge / "wiki",
        search_backend=resolve_search_backend(cfg),
        cache_dir=resolve_cache_dir(None),
        config=cfg,
    )

    async def main():
        if sys.argv[1] == "remember":
            tool = await server.get_tool("remember")
            print(tool.fn(content=sys.argv[3], sources="user-stated:test"))
        else:
            tool = await server.get_tool("recall")
            print(tool.fn(query=sys.argv[3]))

    asyncio.run(main())
    """
)


def _run(mode: str, knowledge: Path, cache: Path, arg: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["ATHENAEUM_CACHE_DIR"] = str(cache)
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, mode, str(knowledge), arg],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


class TestRememberThenRecallNewProcess:
    def test_no_credential_no_chromadb_recall_returns_uncompiled(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        init_knowledge_dir(knowledge)
        cache = tmp_path / "cache"

        assert _run("remember", knowledge, cache, NOTE).startswith("Saved to")
        out = _run("recall", knowledge, cache, "Zephyr launch code")

        assert "ZX-9942" in out
        assert "[uncompiled]" in out


class TestSessionEndIndexesRawIntakeWhenCompileFails:
    def _seed(self, tmp_path: Path) -> tuple[Path, Path]:
        from athenaeum.mcp_server import remember_write

        knowledge = tmp_path / "knowledge"
        init_knowledge_dir(knowledge)
        # Written after init, with no index refresh: raw intake the index has
        # never seen.
        remember_write(knowledge / "raw", NOTE, sources="user-stated:test")
        return knowledge, tmp_path / "cache"

    def test_failed_compile_still_reindexes_and_keeps_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.mcp_server import recall_search

        knowledge, cache = self._seed(tmp_path)
        monkeypatch.setattr(lib, "run", lambda *a, **k: 1)  # compile fails

        result = lib.session_end(
            raw_root=knowledge / "raw",
            wiki_root=knowledge / "wiki",
            knowledge_root=knowledge,
            cache_dir=cache,
            backend="fts5",
        )

        assert result.exit_code == 1
        assert result.reindexed is True
        out = recall_search(
            knowledge / "wiki",
            "Zephyr launch code",
            top_k=5,
            search_backend="fts5",
            cache_dir=cache,
            extra_roots=resolve_extra_intake_roots(knowledge),
        )
        assert "ZX-9942" in out
        assert "[uncompiled]" in out

    def test_counter_example_index_not_older_than_raw_intake(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a failed compile the index manifest agrees with raw intake:
        nothing is left pending for a reindex."""
        knowledge, cache = self._seed(tmp_path)
        wiki = knowledge / "wiki"

        def pending() -> int | None:
            return lib._reindex_would_change(
                knowledge, wiki, cache_dir=cache, config=lib.load_config(knowledge), backend="fts5"
            )

        assert pending()  # the raw note is not indexed yet
        monkeypatch.setattr(lib, "run", lambda *a, **k: 1)
        result = lib.session_end(
            raw_root=knowledge / "raw",
            wiki_root=wiki,
            knowledge_root=knowledge,
            cache_dir=cache,
            backend="fts5",
        )
        assert result.exit_code == 1
        assert pending() == 0
