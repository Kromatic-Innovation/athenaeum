# SPDX-License-Identifier: Apache-2.0
"""After ``athenaeum run`` compiles a note, ``recall`` returns the compiled
page and never a ghost hit for the deleted raw note (athenaeum#3090).

Two halves: ``run`` refreshes the search index for what it changed, and the
shared recall seam drops hits whose file is gone.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import athenaeum.librarian as lib
from athenaeum.init import init_knowledge_dir

NOTE = "The launch code for Project Zephyr is ZX-9942."
PAGE = (
    "---\nname: Project Zephyr\ntype: project\n---\n\n"
    "The compiled Zephyr launch code is ZX-9942.\n"
)


def _build_args(argv: list[str]) -> argparse.Namespace:
    from athenaeum._cmd_run import add_run_subparser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    add_run_subparser(subparsers)
    return parser.parse_args(argv)


def _recall(knowledge: Path, cache: Path, backend: str) -> str:
    from athenaeum.config import resolve_extra_intake_roots
    from athenaeum.mcp_server import recall_search

    return recall_search(
        knowledge / "wiki",
        "Zephyr launch code",
        top_k=5,
        search_backend=backend,
        cache_dir=cache,
        extra_roots=resolve_extra_intake_roots(knowledge),
    )


def _seed(tmp_path: Path, backend: str) -> tuple[Path, Path, Path]:
    from athenaeum.mcp_server import remember_write

    knowledge = tmp_path / "knowledge"
    init_knowledge_dir(knowledge)
    (knowledge / "athenaeum.yaml").write_text(f"search_backend: {backend}\n", encoding="utf-8")
    raw_note = remember_write(knowledge / "raw", NOTE, sources="user-stated:test")
    cache = tmp_path / "cache"
    lib.reindex(knowledge_root=knowledge, cache_dir=cache, backend=backend)
    return knowledge, cache, Path(str(raw_note))


class TestRunRefreshesIndex:
    def test_recall_after_run_returns_compiled_page_not_ghost(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum._cmd_run import cmd_run

        knowledge, cache, _ = _seed(tmp_path, "fts5")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
        assert "[uncompiled]" in _recall(knowledge, cache, "fts5")
        raw_files = [p for p in (knowledge / "raw").rglob("*.md") if NOTE in p.read_text()]
        assert raw_files

        def _fake_run(**kwargs: object) -> int:
            # Stand-in for the LLM compile: page written, raw note deleted.
            (knowledge / "wiki" / "project-zephyr.md").write_text(PAGE, encoding="utf-8")
            for p in raw_files:
                p.unlink()
            return 0

        monkeypatch.setattr("athenaeum.librarian.run", _fake_run)
        rc = cmd_run(_build_args(["run", "--knowledge-root", str(knowledge)]))

        assert rc == 0
        out = _recall(knowledge, cache, "fts5")
        assert "project-zephyr.md" in out
        assert "[uncompiled]" not in out
        for p in raw_files:
            assert p.name not in out

    def test_index_refresh_failure_keeps_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum._cmd_run import cmd_run

        knowledge, cache, _ = _seed(tmp_path, "fts5")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
        monkeypatch.setattr("athenaeum.librarian.run", lambda **k: 0)
        monkeypatch.setattr(
            "athenaeum.librarian._reindex_would_change",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert cmd_run(_build_args(["run", "--knowledge-root", str(knowledge)])) == 0
        assert "athenaeum reindex" in capsys.readouterr().err

    @pytest.mark.parametrize("compile_rc", [0, 1, 2])
    @pytest.mark.parametrize("failing", ["_reindex_would_change", "reindex"])
    def test_refresh_failure_never_changes_run_exit_code(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        compile_rc: int,
        failing: str,
    ) -> None:
        # athenaeum#3124: a refresh that fails at either step, after a compile
        # that succeeded or failed, leaves run's exit code to the compile.
        from athenaeum._cmd_run import cmd_run

        knowledge, cache, _ = _seed(tmp_path, "fts5")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
        monkeypatch.setattr("athenaeum.librarian.run", lambda **k: compile_rc)
        monkeypatch.setattr("athenaeum.librarian._reindex_would_change", lambda *a, **k: True)

        def _boom(*a: object, **k: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(f"athenaeum.librarian.{failing}", _boom)
        rc = cmd_run(_build_args(["run", "--knowledge-root", str(knowledge)]))
        assert rc == compile_rc
        err = capsys.readouterr().err
        assert "search index refresh failed (OSError: disk full)" in err
        assert "athenaeum reindex" in err

    def test_refresh_does_not_swallow_keyboard_interrupt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum._cmd_run import cmd_run

        knowledge, cache, _ = _seed(tmp_path, "fts5")
        monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
        monkeypatch.setattr("athenaeum.librarian.run", lambda **k: 0)

        def _interrupt(*a: object, **k: object) -> bool:
            raise KeyboardInterrupt

        monkeypatch.setattr("athenaeum.librarian._reindex_would_change", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            cmd_run(_build_args(["run", "--knowledge-root", str(knowledge)]))


class TestStaleHitFilter:
    def test_drop_vanished_hits_unit(self, tmp_path: Path) -> None:
        from athenaeum.mcp_server import _drop_vanished_hits

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "here.md").write_text("x", encoding="utf-8")
        hits = [("here.md", "here", 1.0), ("gone.md", "gone", 0.9), ("nowhere/x.md", "x", 0.5)]
        kept = _drop_vanished_hits(hits, wiki, [])
        # Existing file kept; deleted file dropped; unresolvable root kept as before.
        assert [h[0] for h in kept] == ["here.md", "nowhere/x.md"]

    @pytest.mark.parametrize("surface", ["mcp", "cli"])
    def test_recall_never_returns_deleted_raw_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        surface: str,
    ) -> None:
        knowledge, cache, _ = _seed(tmp_path, "fts5")
        raw_files = [p for p in (knowledge / "raw").rglob("*.md") if NOTE in p.read_text()]
        for p in raw_files:
            p.unlink()  # index is now stale: it still lists the deleted note

        if surface == "mcp":
            out = _recall(knowledge, cache, "fts5")
            assert "[uncompiled]" not in out
            assert not any(p.name in out for p in raw_files)
        else:
            from athenaeum.cli import main

            monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache))
            main(["recall", "Zephyr launch code", "--path", str(knowledge)])
            out = capsys.readouterr().out
            assert not any(p.name in out for p in raw_files)
            assert "[uncompiled]" not in out
