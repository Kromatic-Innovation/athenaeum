# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum session-end` — the change-gated SessionEnd path (athenaeum#350).

`session_end` composes the incremental `ingest` engine (athenaeum#349) with the
incremental `reindex` (athenaeum#348) as ONE change-gated command: the cwc SessionEnd
hook and the nightly-after-librarian path both invoke it so a memory
`remember`ed by one agent becomes recallable by every other agent after that
session ends — closing the ~24h gap where a raw fact sat uncompiled until the
next nightly librarian run.

Two guarantees under test:

* Composition + cost bound: new raw → ingest + reindex; an idle SessionEnd
  (nothing new) is a fast no-op with zero LLM work AND no reindex; a failed
  compile never indexes a half-built wiki; `--full` forces both steps.
* End-to-end cross-agent recall (the issue's acceptance criterion): session A
  `remember`s a fact, session A's SessionEnd runs `session_end`, and session B
  `recall`s the fully-compiled wiki entry — no waiting for the nightly run.

All LLM/embedder work is stubbed. The `tier0_passthrough` structured path
exercises the "compiles with NO LLM cost" guarantee: the mocked Anthropic
client's `messages.create` is asserted never-called.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.cli import EXIT_LOCK_HELD, main

# Issue athenaeum#896: reuse the deadline suite's FakeClock + writing-`process_one`
# harness verbatim (same convention `test_librarian_heartbeat.py` follows) for
# the derived-inner-deadline graceful-stop test below, rather than inventing a
# new one. Aliased to avoid colliding with this file's own tier0-oriented
# `_seed_knowledge_root` (no wiki/_schema — process_one is stubbed, so schema
# validity is irrelevant here, exactly as in the deadline suite).
from tests.test_librarian_deadline import (
    _FakeClock,
    _last_subject,
    _porcelain,
    _writing_process_one_factory,
)
from tests.test_librarian_deadline import (
    _seed_knowledge_root as _entity_seed_knowledge_root,
)

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _seed_knowledge_root(tmp_path: Path) -> Path:
    """A minimal knowledge/ tree with .git, wiki/_schema, raw/sessions."""
    root = tmp_path / "knowledge"
    (root / "wiki" / "_schema").mkdir(parents=True)
    (root / "wiki" / "_schema" / "types.md").write_text(
        "# Types\n\n| Type |\n|------|\n| person |\n"
    )
    (root / "wiki" / "_schema" / "tags.md").write_text(
        "# Tags\n\n| Tag |\n|-----|\n| active |\n"
    )
    (root / "wiki" / "_schema" / "access-levels.md").write_text(
        "# Access\n\n| Level |\n|-------|\n| internal |\n"
    )
    sessions = root / "raw" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".gitkeep").write_text("")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _write_tier0_raw(
    root: Path,
    uid: str,
    name: str,
    ts: str,
    uuid8: str,
    *,
    session_dir: str = "sessions",
    origin_session: str | None = None,
) -> Path:
    """A pre-structured (tier0-eligible) raw intake file — uid/type/name set."""
    origin = f"originSessionId: {origin_session}\n" if origin_session else ""
    path = root / "raw" / session_dir / f"{ts}-{uuid8}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"uid: {uid}\n"
        "type: person\n"
        f"name: {name}\n"
        "tags: [active]\n"
        "access: internal\n"
        f"{origin}"
        "---\n\n"
        f"Notes about {name}.\n"
    )
    return path


@pytest.fixture
def mock_anthropic(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch anthropic.Anthropic + a fake key so run()'s startup gate passes.

    Returns the mock client so tests can assert ``messages.create`` was never
    called (the tier0 "no LLM cost" guarantee).
    """
    import anthropic as anthropic_mod

    client = MagicMock()
    monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kw: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key-not-real")
    return client


# ---------------------------------------------------------------------------
# session_end composition (librarian.session_end)
# ---------------------------------------------------------------------------


class TestSessionEndComposition:
    def test_new_raw_ingests_then_reindexes_no_llm(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
            backend="fts5",
        )

        assert result.exit_code == 0
        assert result.ingest.noop is False
        assert result.ingest.compiled == 1
        # Compile ran → reindex ran, and the new wiki page is in the index.
        assert result.reindexed is True
        assert result.reindex_pages >= 1
        assert result.backend == "fts5"
        # tier0 passthrough must never touch the model.
        mock_anthropic.messages.create.assert_not_called()
        # wiki page written; ingest stamp + index manifests created.
        assert list((root / "wiki").glob("p-0001-*.md"))
        assert (cache / "ingest-manifest.json").is_file()

    def test_idle_session_end_is_noop_and_skips_reindex(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert first.reindexed is True and first.ingest.compiled == 1

        # Nothing new since the last SessionEnd → ingest no-op → NO reindex.
        second = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert second.ingest.noop is True
        assert second.reindexed is False
        assert second.reindex_pages == 0
        assert second.exit_code == 0

    def test_failed_compile_skips_reindex_and_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        monkeypatch.setattr(lib, "run", lambda *a, **k: 1)  # simulate failure
        # Spy the reindex so we can assert it is never called on a bad compile.
        reindex_calls: list[bool] = []
        real_reindex = lib.reindex
        monkeypatch.setattr(
            lib,
            "reindex",
            lambda *a, **k: (reindex_calls.append(True), real_reindex(*a, **k))[1],
        )

        result = lib.session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert result.exit_code == 1
        assert result.reindexed is False
        assert reindex_calls == []
        # No stamp written on failure → next SessionEnd retries.
        assert not (cache / "ingest-manifest.json").exists()

    def test_dry_run_skips_reindex_and_stamp(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
            dry_run=True,
        )
        assert result.reindexed is False
        # Dry-run never stamps and never consumes the raw file.
        assert not (cache / "ingest-manifest.json").exists()
        assert list((root / "raw" / "sessions").glob("2024*.md"))

    def test_dry_run_previews_without_compile_cluster_or_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dry-run is a pure manifest-diff preview (athenaeum#370): NO compile, NO
        clustering, NO chromadb/ONNX — yet it still reports the deltas."""
        import athenaeum.librarian as lib

        def _boom(*_a: object, **_k: object) -> object:
            raise AssertionError("must not run on a dry-run")

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        # The heavy compile pipeline (run → cluster → chromadb) must never fire.
        monkeypatch.setattr(lib, "run", _boom)
        monkeypatch.setattr(lib, "cluster_auto_memory_files", _boom)

        result = lib.session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
            dry_run=True,
        )
        assert result.dry_run is True
        assert result.reindexed is False
        # The preview still counts the new raw file...
        assert result.ingest.new_or_changed == 1
        assert result.ingest.noop is False
        # ...and reports a cheap wiki-diff reindex preview (no chromadb opened).
        assert result.reindex_would_change is not None
        assert result.reindex_would_change >= 0
        # No compile side effects: no wiki page, no stamp, raw preserved.
        assert not list((root / "wiki").glob("p-0001-*.md"))
        assert not (cache / "ingest-manifest.json").exists()
        assert list((root / "raw" / "sessions").glob("2024*.md"))
        # The JSON summary surfaces the preview count.
        summary = result.summary()
        assert summary["reindex_would_change"] == result.reindex_would_change

    def test_vector_backend_dry_run_never_opens_chromadb_or_onnx(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The PRODUCTION path: ``backend="vector"`` dry-run must stay a pure
        manifest-diff preview — it must NOT construct a chromadb client OR build
        any embedding function (the ONNX model). This pins the athenaeum#370 claim on the
        configured backend, not just fts5.
        """
        pytest.importorskip("chromadb")
        import chromadb

        import athenaeum.librarian as lib
        from athenaeum.search import VectorBackend

        root = _seed_knowledge_root(tmp_path)
        # A real wiki page so the would_change preview has something to diff.
        (root / "wiki" / "lean.md").write_text("---\nname: Lean\n---\n\nbody\n")
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        # Any attempt to open chromadb or build the embedding function on the
        # dry-run path fails the test immediately.
        def _no_chromadb(*_a: object, **_k: object) -> object:
            raise AssertionError(
                "chromadb.PersistentClient must not be opened on a dry-run"
            )

        def _no_ef(*_a: object, **_k: object) -> object:
            raise AssertionError("embedding function must not be built on a dry-run")

        monkeypatch.setattr(chromadb, "PersistentClient", _no_chromadb)
        monkeypatch.setattr(VectorBackend, "_embedding_function", _no_ef)
        # onnxruntime is optional; guard its session construction too when present.
        try:
            import onnxruntime as _ort

            monkeypatch.setattr(_ort, "InferenceSession", _no_ef)
        except ImportError:
            pass

        result = lib.session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
            dry_run=True,
        )
        # Completed without touching chromadb/ONNX (else the guards would raise).
        assert result.dry_run is True
        assert result.reindexed is False
        assert result.backend == "vector"
        assert result.ingest.new_or_changed == 1
        # Cheap wiki-diff preview: an int (would_change) or the null-with-note
        # fallback — either way, no chromadb was opened to compute it.
        summary = result.summary()
        assert "reindex_would_change" in summary
        if result.reindex_would_change is None:
            assert "reindex_would_change_note" in summary
        else:
            assert result.reindex_would_change >= 1  # the lean.md page
        # No compile/index side effects.
        assert not (cache / "ingest-manifest.json").exists()
        assert not (cache / "wiki-vectors").exists()

    def test_full_forces_recompile_and_full_reindex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"
        # Pre-seed an ingest stamp so an incremental run WOULD no-op.
        cache.mkdir()
        (cache / "ingest-manifest.json").write_text(
            json.dumps({"version": 1, "hashes": {}})
        )

        run_calls: list[bool] = []
        reindex_modes: list[bool] = []
        monkeypatch.setattr(lib, "run", lambda *a, **k: (run_calls.append(True), 0)[1])
        monkeypatch.setattr(
            lib,
            "reindex",
            lambda *a, **k: (
                reindex_modes.append(bool(k.get("incremental", True))),
                ("fts5", 0),
            )[1],
        )

        result = lib.session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=False,
            cache_dir=cache,
            backend="fts5",
        )
        assert result.ingest.mode == "full"
        assert run_calls == [True]  # --full ignored the stamp and compiled.
        assert result.reindexed is True
        assert reindex_modes == [False]  # full compile → full reindex.


# ---------------------------------------------------------------------------
# reindex gate keyed on index staleness, not compile activity (athenaeum#1456)
# ---------------------------------------------------------------------------


class TestReindexGateOnStaleness:
    """The reindex gate must consult the INDEX, not the ingest stamp (athenaeum#1456).

    Ingest and the index keep separate manifests. A raw file ingest has already
    recorded is ``new_or_changed: 0`` forever after, so gating the reindex on
    compile activity alone meant a page that missed its indexing window was
    never reconsidered — every later tick was also ``noop`` and also skipped,
    and the index drifted below the corpus and stayed there silently.

    Both halves matter. The positive case is the bug; the negative case is the
    guard against the overcorrection that turns every tick into a rebuild.
    """

    def test_noop_ingest_with_stale_index_still_reindexes(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        """A page in the corpus that never reached the index IS picked up by a
        later tick, even though ingest reports ``noop``."""
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert first.reindexed is True

        # A wiki page that the index has never seen, with NO new raw behind it —
        # the production shape of a page that missed its indexing window.
        (root / "wiki" / "lean.md").write_text("---\nname: Lean\n---\n\nbody\n")

        second = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert second.exit_code == 0
        assert second.ingest.noop is True  # nothing new to compile...
        assert second.reindexed is True  # ...but the index was behind.
        assert second.reindex_pages >= 1
        # The staleness count is surfaced in the JSON summary so rebuild.log
        # shows WHY an otherwise-idle tick reindexed.
        assert second.reindex_would_change == 1
        assert second.summary()["reindex_would_change"] == 1

        # And the page is now actually retrievable — a third tick is idle again.
        third = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert third.ingest.noop is True and third.reindexed is False

    def test_noop_ingest_with_current_index_does_not_reindex(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The guard against the overcorrection: a current index must NOT be
        rebuilt on every tick just because the gate now checks staleness."""
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = lib.session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert first.reindexed is True

        # Spy AFTER the legitimate first reindex: the second tick must not call it.
        reindex_calls: list[bool] = []
        real_reindex = lib.reindex
        monkeypatch.setattr(
            lib,
            "reindex",
            lambda *a, **k: (reindex_calls.append(True), real_reindex(*a, **k))[1],
        )

        second = lib.session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert second.ingest.noop is True
        assert reindex_calls == []  # asserted directly, not inferred.
        assert second.reindexed is False
        assert second.reindex_pages == 0
        assert second.reindex_would_change == 0

    def test_vector_backend_idle_tick_stays_cheap(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The PRODUCTION backend: the staleness check reads the vector manifest
        (a different path from fts5's) and must agree with what ``reindex``
        wrote — otherwise every tick rebuilds. Pinned by forbidding chromadb and
        the embedding model on the idle tick, which is also the athenaeum#370
        cheapness claim carried onto the non-dry-run path.
        """
        pytest.importorskip("chromadb")
        import chromadb

        import athenaeum.librarian as lib
        from athenaeum.search import VectorBackend

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = lib.session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        assert first.reindexed is True and first.backend == "vector"

        def _boom(*_a: object, **_k: object) -> object:
            raise AssertionError("idle tick must not open chromadb / build a model")

        monkeypatch.setattr(chromadb, "PersistentClient", _boom)
        monkeypatch.setattr(VectorBackend, "_embedding_function", _boom)

        second = lib.session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        # Nothing stale → no reindex, and the guards above never fired.
        assert second.ingest.noop is True
        assert second.reindexed is False
        assert second.reindex_would_change == 0

    def test_unembedded_class_does_not_wedge_the_gate_open(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        """Scan parity: the staleness preview must apply the SAME filters as the
        build, or the gate never closes.

        ``config`` gates the athenaeum#532 ``is_embedded`` filter inside
        ``_scan_indexed_records``. The build threads it and therefore writes a
        manifest WITHOUT pages whose class routes to an ``embedded: false``
        surface. If the preview omits it, those pages are counted as ``added``
        against a manifest that will never contain them — a delta that cannot
        converge, so every idle tick reindexes forever. On the vector backend
        that is a chromadb open plus an embedding-model load per tick.

        The other two negative tests run against a corpus with zero filtered
        pages, so they pin the gate arithmetic but cannot catch parity drift.
        This one can: it asserts the count CONVERGES across consecutive idle
        ticks, not merely that it is zero once.
        """
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        # A surface INSIDE wiki/ (so both scans still walk the page) that is
        # nonetheless not embedded — the shape that makes the two scans differ.
        (root / "athenaeum.yaml").write_text(
            "storage:\n"
            "  adapters:\n"
            "    notes-unembedded:\n"
            "      backing_store: markdown\n"
            "      surface_root: wiki\n"
            "      corpus_policy:\n"
            "        embedded: false\n"
            "        recallable: false\n"
            "        merge_eligible: false\n"
            "  mapping:\n"
            "    note: notes-unembedded\n"
        )
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert first.reindexed is True

        # A page the BUILD will refuse to index (unembedded class) but which
        # sits in the scanned tree all the same.
        (root / "wiki" / "scratch-note.md").write_text(
            "---\nname: Scratch\ntype: note\n---\n\nbody\n"
        )

        # Two consecutive idle ticks. The first may legitimately reindex (the
        # page is new to the scan); the second must NOT — by then the build has
        # had its chance and the delta has to have converged to zero.
        session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        third = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )

        assert third.ingest.noop is True
        assert third.reindex_would_change == 0, (
            "staleness preview disagrees with the build about which pages count "
            "— the gate is wedged open and every idle tick will reindex"
        )
        assert third.reindexed is False

    # -- athenaeum#1459: the preview must apply the BUILD's invalidation checks --
    #
    # A manifest hash-diff is a strict SUBSET of what ``build_index`` treats as
    # invalidating. Each test below breaks the index in a way the corpus scan
    # cannot see — the manifest still matches the wiki byte for byte — and
    # asserts the next idle tick rebuilds anyway. Each then asserts a THIRD
    # tick is idle again: a blocker that never clears would be an infinite
    # rebuild loop, which is the athenaeum#1458 failure in the opposite
    # direction.

    def test_deleted_index_under_an_intact_manifest_is_rebuilt(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        """Row 1, probe-confirmed on athenaeum#1458: delete the index file and
        leave the manifest, and the hash-diff reports 0 forever — so nothing
        ever rebuilt the index that recall was querying.

        The build has always refused to reuse an index whose DB is gone; only
        the preview did not know that.
        """
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert first.reindexed is True

        db = cache / "wiki-index.db"
        manifest = cache / "fts5-manifest.json"
        assert db.is_file() and manifest.is_file()
        db.unlink()  # the manifest stays: the corpus scan still matches it.

        second = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert second.ingest.noop is True  # nothing new to compile...
        assert second.reindexed is True  # ...but the index was gone.
        assert db.is_file()

        third = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
        )
        assert third.reindex_would_change == 0 and third.reindexed is False

    def test_non_vector_backend_name_previews_the_index_reindex_builds(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        """``backend="keyword"`` must consult the FTS5 blocker, because that is
        what :func:`reindex` actually builds under any non-``vector`` name.

        ``reindex`` is ``if backend_name == "vector": build_vector_index(...)
        else: build_fts5_index(...)`` — a two-way split, not a three-way one.
        Resolving the preview's backend with ``get_backend(backend_name)``
        instead of the ``"fts5"`` literal hands back ``KeywordBackend``, whose
        blocker is unconditionally ``None`` because that class persists
        nothing. The gate then never fires and athenaeum#1459 survives verbatim
        under this config, while the fts5 twin above still passes — which is
        exactly why that test cannot stand in for this one.
        """
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="keyword",
        )
        assert first.reindexed is True
        # Proof of the premise: a NON-vector name built an FTS5 index on disk.
        db = cache / "wiki-index.db"
        assert db.is_file(), "reindex maps every non-vector name to fts5"

        db.unlink()  # manifest stays, so the hash-diff alone still reports 0.

        second = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="keyword",
        )
        assert second.ingest.noop is True
        assert second.reindexed is True
        assert db.is_file()

        third = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="keyword",
        )
        assert third.reindex_would_change == 0 and third.reindexed is False

    def test_deleted_vector_collection_under_an_intact_manifest_is_rebuilt(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        """Row 1 on the PRODUCTION backend: the fts5 twin above covers the
        ``wiki-index.db`` stat; this covers the vector backend's ``is_dir``
        on the collection dir.

        Worth its own test because it is the only vector blocker that is a
        filesystem check — the model and schema rows are both manifest-field
        comparisons, so neither would catch a missing ``stat``.
        """
        pytest.importorskip("chromadb")
        import shutil

        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        assert first.reindexed is True and first.backend == "vector"

        vector_dir = cache / "wiki-vectors"
        assert vector_dir.is_dir() and (cache / "vector-manifest.json").is_file()
        shutil.rmtree(vector_dir)  # the manifest stays.

        second = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        assert second.ingest.noop is True
        assert second.reindexed is True
        assert vector_dir.is_dir()

        third = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        assert third.reindex_would_change == 0 and third.reindexed is False

    def test_embedding_model_swap_forces_a_reindex(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        """Row 2, the most damaging in practice: the corpus stays embedded under
        the old model while queries embed under the new one. No error, no log
        line — just silently degraded retrieval, because every hash still
        matches.

        The manifest is edited directly rather than pointing the config at a
        real second model: the blocker compares recorded-vs-configured names,
        and this way no second sentence-transformer is ever downloaded.
        """
        pytest.importorskip("chromadb")
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        assert first.reindexed is True and first.backend == "vector"

        manifest = cache / "vector-manifest.json"
        payload = json.loads(manifest.read_text())
        real_model = payload["embedding_model"]
        payload["embedding_model"] = "superseded-model-v0"
        manifest.write_text(json.dumps(payload))

        second = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        assert second.ingest.noop is True
        assert second.reindexed is True
        # The rebuild re-embedded under the configured model and re-stamped it.
        assert json.loads(manifest.read_text())["embedding_model"] == real_model

        third = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        assert third.reindex_would_change == 0 and third.reindexed is False

    def test_metadata_schema_roll_forces_a_reindex(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        """Row 3: a metadata-schema roll (athenaeum#964) must re-embed every page,
        because a stat-matched incremental build never re-reads an unchanged
        page's frontmatter and would leave it on the old metadata shape.

        Same blind spot as the model swap — the contract changed, the bytes
        did not, so a hash-diff sees nothing to do.
        """
        pytest.importorskip("chromadb")
        from athenaeum.librarian import session_end
        from athenaeum.search import VectorBackend

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        assert first.reindexed is True

        manifest = cache / "vector-manifest.json"
        payload = json.loads(manifest.read_text())
        assert payload["metadata_schema_version"] == VectorBackend._METADATA_SCHEMA_VERSION
        payload["metadata_schema_version"] = 1  # a manifest from the old contract
        manifest.write_text(json.dumps(payload))

        second = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        assert second.ingest.noop is True
        assert second.reindexed is True
        assert (
            json.loads(manifest.read_text())["metadata_schema_version"]
            == VectorBackend._METADATA_SCHEMA_VERSION
        )

        third = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="vector",
        )
        assert third.reindex_would_change == 0 and third.reindexed is False


# ---------------------------------------------------------------------------
# session-end CLI wrapper
# ---------------------------------------------------------------------------


class TestSessionEndCLI:
    def test_json_summary_shape_and_exit_zero(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        rc = main(
            [
                "session-end",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["command"] == "session-end"
        assert payload["mode"] == "incremental"
        assert payload["reindexed"] is True
        assert payload["reindex_pages"] >= 1
        assert payload["backend"] == "fts5"
        assert payload["exit_code"] == 0
        assert isinstance(payload["duration_ms"], int)
        # The nested ingest summary round-trips.
        assert payload["ingest"]["command"] == "ingest"
        assert payload["ingest"]["compiled"] == 1

    def test_single_flight_lock_held(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from athenaeum.runlock import RunLock

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")

        with RunLock(root):  # hold the lock so session-end can't acquire it
            rc = main(
                [
                    "session-end",
                    "--path",
                    str(root),
                    "--cache-dir",
                    str(tmp_path / "c"),
                    "--backend",
                    "fts5",
                ]
            )
        assert rc == EXIT_LOCK_HELD
        assert "error" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# End-to-end cross-agent recall — the issue athenaeum#350 acceptance criterion
# ---------------------------------------------------------------------------


class TestCrossAgentRecall:
    """Session A remembers → A's SessionEnd → session B recalls it.

    Demonstrates the ~24h gap is closed WITHOUT waiting for the nightly
    librarian: the fact is invisible to `recall` while it sits in `raw/`, and
    becomes recallable the moment A's `session_end` compiles + indexes it.
    """

    def test_remember_in_session_a_is_recallable_in_session_b(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from athenaeum.librarian import session_end
        from athenaeum.mcp_server import remember_write

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        # --- Session A: an agent remembers a structured (tier0) fact. It lands
        #     in raw/<session>/ only — recall reads wiki/, so it is invisible.
        content = (
            "---\n"
            "uid: p-9350\n"
            "type: person\n"
            "name: Marie Curie\n"
            "tags: [active]\n"
            "access: internal\n"
            "originSessionId: sess-A\n"
            "---\n\n"
            "Marie Curie pioneered research on radioactivity.\n"
        )
        msg = remember_write(
            root / "raw", content, source="sess-A", sources="user-stated:e2e"
        )
        assert msg.startswith("Saved to")
        # Pre-condition (the gap): raw file exists, but NO compiled wiki page.
        assert list((root / "raw" / "sess-A").glob("*.md"))
        assert not list((root / "wiki").glob("p-9350-*.md"))

        # --- Session A's SessionEnd: change-gated ingest + reindex, scoped to
        #     the originating session id (the cwc hook use-case).
        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            session="sess-A",
            cache_dir=cache,
            backend="fts5",
        )
        assert result.exit_code == 0
        assert result.ingest.new_or_changed == 1
        assert result.ingest.compiled == 1
        assert result.reindexed is True
        assert result.session == "sess-A"
        # No LLM: the structured entry compiled via tier0 passthrough.
        mock_anthropic.messages.create.assert_not_called()
        # The compiled, fully-resolved wiki page now exists.
        wiki_pages = list((root / "wiki").glob("p-9350-*.md"))
        assert wiki_pages, "session_end must compile the raw fact into wiki/"

        # --- Session B: a DIFFERENT agent recalls the fact via the shell recall
        #     path (same index the MCP `recall` tool reads). It is now found.
        capsys.readouterr()  # clear
        rc = main(
            [
                "recall",
                "Marie Curie",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert out.strip(), "session B recall returned no hits — gap not closed"
        assert "curie" in out.lower()


# ---------------------------------------------------------------------------
# Reference-determination wiring (issue athenaeum#711)
# ---------------------------------------------------------------------------


class TestSessionEndReferenceDetermination:
    """``session_end`` is the SessionEnd-hook entry point, so it is where
    per-session reference determination (referenced / pushed precision) is
    triggered — see ``athenaeum.push_metrics.run_reference_determination``.
    """

    def test_calls_reference_determination_when_session_given(
        self, tmp_path: Path, mock_anthropic: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import push_metrics
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        calls: list[tuple[str, Path | None]] = []

        def _fake_run_reference_determination(
            session_id: str, *, cache_dir: Path | None = None, config: object = None, **_: object
        ) -> None:
            calls.append((session_id, cache_dir))
            return None

        monkeypatch.setattr(
            push_metrics, "run_reference_determination", _fake_run_reference_determination
        )

        session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            session="sess-ref-1",
            cache_dir=cache,
            backend="fts5",
        )

        assert calls == [("sess-ref-1", cache)]

    def test_skipped_when_no_session_id(
        self, tmp_path: Path, mock_anthropic: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import push_metrics
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        calls: list[str] = []
        monkeypatch.setattr(
            push_metrics,
            "run_reference_determination",
            lambda *a, **kw: calls.append(a[0] if a else kw.get("session_id", "")),
        )

        session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            session=None,
            cache_dir=cache,
            backend="fts5",
        )

        assert calls == []

    def test_skipped_on_dry_run(
        self, tmp_path: Path, mock_anthropic: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import push_metrics
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        calls: list[str] = []
        monkeypatch.setattr(
            push_metrics,
            "run_reference_determination",
            lambda *a, **kw: calls.append("called"),
        )

        session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            session="sess-dry",
            cache_dir=cache,
            backend="fts5",
            dry_run=True,
        )

        assert calls == []

    def test_real_reference_determination_never_breaks_session_end(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        """End-to-end with the REAL (unmocked) push_metrics function: no push
        records and no transcript exist for this session id, so
        ``determine_references`` returns ``None`` internally — but
        ``session_end`` must still complete normally either way.
        """
        from athenaeum.librarian import session_end

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            session="sess-no-transcript",
            cache_dir=cache,
            backend="fts5",
        )

        assert result.exit_code == 0
        assert result.session == "sess-no-transcript"


# ---------------------------------------------------------------------------
# Issue athenaeum#896 — derived inner budgets wired into cmd_session_end
# ---------------------------------------------------------------------------


class TestSessionEndCLIDerivedBudgets:
    """`cmd_session_end` (the CLI wrapper over `session_end`, registered as
    the ``session-end`` subcommand) must resolve its inner `max_runtime` from
    the outer SessionEnd wrapper timeout instead of falling through to the
    nightly-run `DEFAULT_MAX_RUNTIME`, and pass explicit, session-scoped-sized
    `max_files`/`max_api_calls` instead of the nightly-run defaults."""

    def test_cli_passes_derived_runtime_and_explicit_budgets(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import athenaeum.librarian as lib
        from athenaeum.config import load_config

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        # A small, explicit outer value so the derived inner is distinctive
        # (not coincidentally equal to DEFAULT_MAX_RUNTIME).
        monkeypatch.setenv("KNOWLEDGE_REBUILD_TIMEOUT", "300")
        monkeypatch.delenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", raising=False)
        expected_max_runtime = lib.session_end_max_runtime(load_config(root))
        assert expected_max_runtime != lib.DEFAULT_MAX_RUNTIME
        assert expected_max_runtime < 300

        captured: dict[str, object] = {}
        real_session_end = lib.session_end

        def _spy(*args: object, **kwargs: object) -> object:
            captured.update(kwargs)
            return real_session_end(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(lib, "session_end", _spy)

        # No raw intake seeded. With no prior ingest-manifest stamp, `ingest`
        # still calls through to `run()` on this FIRST invocation (the
        # noop-skip only fires once a stamp exists) — `mock_anthropic` covers
        # the resulting startup gate. This test targets the CLI→session_end
        # kwarg wiring, not the compile itself.
        rc = main(
            [
                "session-end",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )

        assert rc == 0
        assert captured["max_runtime"] == expected_max_runtime
        assert captured["max_files"] == lib.SESSION_END_MAX_FILES
        assert captured["max_api_calls"] == lib.SESSION_END_MAX_API_CALLS


# ---------------------------------------------------------------------------
# Issue athenaeum#896 — derived inner deadline trips the graceful-stop path
# ---------------------------------------------------------------------------


class TestSessionEndDerivedDeadlineGracefulStop:
    """A SessionEnd run that trips its DERIVED inner deadline (outer
    `KNOWLEDGE_REBUILD_TIMEOUT` minus margin) must exit through the SAME
    graceful-stop path the nightly run uses (issue athenaeum#396/athenaeum#337): partial
    progress committed, remaining raw left on disk for the next run, and a
    resumable EXIT_GRACEFUL_PARTIAL (75, issue athenaeum#897) exit — instead of
    running unbounded until the wrapper's external `timeout --signal=TERM`
    kills it (that external-kill path keeps exit code 124, unchanged by
    athenaeum#897). Drives `session_end()` end-to-end with the SAME FakeClock +
    writing-`process_one` harness `test_librarian_deadline.py` uses for the
    equivalent `run()`-level test
    (`test_entity_loop_deadline_defers_and_exits_75`)."""

    def test_derived_deadline_trips_graceful_stop_partial_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import (
            EXIT_GRACEFUL_PARTIAL,
            session_end,
            session_end_max_runtime,
        )

        # Same fixture shape (freeform, non-tier0 raw — routes through the
        # real entity loop) as the run()-level deadline test.
        root = _entity_seed_knowledge_root(tmp_path, n_files=3)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        monkeypatch.delenv("ATHENAEUM_MAX_API_CALLS", raising=False)
        monkeypatch.setenv("KNOWLEDGE_REBUILD_TIMEOUT", "1000")
        monkeypatch.delenv("ATHENAEUM_SESSION_END_RUNTIME_MARGIN", raising=False)
        # Issue athenaeum#898: isolate this run-level-deadline test from the new
        # per-file wall-clock bound — see the identical note in
        # test_librarian_deadline.py::test_entity_loop_deadline_defers_and_exits_75.
        monkeypatch.setenv("ATHENAEUM_RAW_FILE_MAX_RUNTIME_SECONDS", "999999")

        derived = session_end_max_runtime({})  # outer=1000, default margin -> 880
        assert derived < 1000

        clock = _FakeClock(start=0.0)
        monkeypatch.setattr("athenaeum.librarian.time.monotonic", clock.monotonic)

        def _bump() -> None:
            clock.now = derived + 5000.0

        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _writing_process_one_factory(root / "wiki", bump_clock=_bump, bump_after=1),
        )

        cache = tmp_path / "cache"
        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            cache_dir=cache,
            backend="fts5",
            max_runtime=derived,
            max_api_calls=100,
        )

        # Resumable graceful-partial exit (issue athenaeum#897), propagated
        # through IngestResult.exit_code -> SessionEndResult.exit_code.
        assert result.exit_code == EXIT_GRACEFUL_PARTIAL
        # A half-compiled wiki is never indexed (session_end's change-gate).
        assert result.reindexed is False
        # Partial progress committed; nothing left uncommitted.
        assert _porcelain(root) == ""
        assert _last_subject(root).startswith("librarian: processed 1 file(s)")
        assert (root / "wiki" / "entity-1.md").exists()
        assert not (root / "wiki" / "entity-2.md").exists()
        remaining = sorted((root / "raw" / "sessions").glob("2024041*.md"))
        assert len(remaining) == 2, "deferred intake must remain on disk for the next run"
