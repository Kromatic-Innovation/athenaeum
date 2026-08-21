"""Tests for `athenaeum ingest` and `athenaeum reindex` (issue athenaeum#349).

Covers the reusable incremental-ingest engine in ``librarian.ingest`` (which
the SessionEnd path athenaeum#350 reuses) and the thin CLI wrappers: incremental
compiles only new/changed raw files and is a fast no-op when none, ``--full``
recompiles, ``reindex --incremental`` is a no-op when nothing changed, the
one-line JSON summary shape, exit codes, and single-flight via the runlock.

All LLM/embedder work is stubbed — no real API calls or 21k embeds. The
tier0-passthrough path deliberately exercises the "compiles with NO LLM cost"
guarantee: the mocked Anthropic client's ``messages.create`` is asserted
never-called.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.cli import EXIT_LOCK_HELD, main

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


def _write_tier0_raw(root: Path, uid: str, name: str, ts: str, uuid8: str) -> Path:
    """A pre-structured (tier0-eligible) raw intake file — uid/type/name set."""
    path = root / "raw" / "sessions" / f"{ts}-{uuid8}.md"
    path.write_text(
        "---\n"
        f"uid: {uid}\n"
        "type: person\n"
        f"name: {name}\n"
        "tags: [active]\n"
        "access: internal\n"
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
# ingest engine (librarian.ingest) — real tier0 compile, no LLM
# ---------------------------------------------------------------------------


class TestIngestEngineTier0:
    def test_incremental_compiles_new_tier0_with_no_llm_cost(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import ingest

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        result = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )

        assert result.exit_code == 0
        assert result.noop is False
        assert result.new_or_changed == 1
        assert result.compiled == 1
        # tier0 passthrough must never touch the model.
        mock_anthropic.messages.create.assert_not_called()
        # wiki page written; raw consumed; stamp manifest created.
        assert list((root / "wiki").glob("p-0001-*.md"))
        assert not list((root / "raw" / "sessions").glob("2024*.md"))
        assert (cache / "ingest-manifest.json").is_file()

    def test_incremental_is_noop_when_nothing_new(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import ingest

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        first = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert first.noop is False and first.compiled == 1

        # Nothing new since the last ingest → fast no-op, no compile.
        second = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert second.noop is True
        assert second.new_or_changed == 0
        assert second.compiled == 0
        assert second.exit_code == 0

    def test_full_recompiles_ignoring_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--full always invokes the compile even when the stamp is current."""
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"
        # Pre-seed a stamp so the incremental gate WOULD no-op.
        (cache).mkdir()
        (cache / "ingest-manifest.json").write_text(
            json.dumps({"version": 1, "hashes": {}})
        )

        calls: list[bool] = []

        def _spy_run(*_a: object, **_k: object) -> int:
            calls.append(True)
            return 0

        monkeypatch.setattr(lib, "run", _spy_run)

        # Incremental with an empty-but-present stamp and no raw files → no-op.
        inc = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert inc.noop is True
        assert calls == []

        # --full ignores the stamp and runs the compile.
        full = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=False,
            cache_dir=cache,
        )
        assert full.noop is False
        assert full.mode == "full"
        assert calls == [True]

    def test_failed_compile_leaves_stamp_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        monkeypatch.setattr(lib, "run", lambda *a, **k: 1)  # simulate failure

        result = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert result.exit_code == 1
        # No stamp written on failure → next run retries.
        assert not (cache / "ingest-manifest.json").exists()

    def test_truncated_exit_zero_run_does_not_stamp(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        # Issue athenaeum#530 (H2): a max_files-truncated run still exits 0, but only
        # part of the intake was compiled. The OLD code stamped the pre-compile
        # snapshot of ALL discovered files, so the next ingest took the false
        # no-op fast path and the beyond-window remainder was silently never
        # compiled. The uncompiled remainder must NOT be stamped — and the very
        # next ingest must then drain it.
        #
        # Issue athenaeum#895 keeps that invariant and narrows it from "stamp nothing
        # unless the whole backlog drained" to "stamp exactly what drained": the
        # manifest now exists after a truncated run, carrying the compiled file
        # ONLY. The uncompiled file being absent from it is the athenaeum#530
        # guarantee, stated per file.
        from athenaeum.librarian import ingest

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aaaaaaaa")
        _write_tier0_raw(root, "p-0002", "Bob", "20240410T130000Z", "bbbbbbbb")
        cache = tmp_path / "cache"

        first = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
            max_files=1,  # truncate: one file compiled, one left beyond the window
        )
        assert first.exit_code == 0
        # Exactly one file was consumed; one remains uncompiled in the intake.
        remaining = list((root / "raw" / "sessions").glob("2024*.md"))
        assert len(remaining) == 1
        # H2: the file the run did NOT compile must never be stamped — otherwise
        # the next ingest false-no-ops and the remaining note is lost forever.
        stamped = json.loads(
            (cache / "ingest-manifest.json").read_text(encoding="utf-8")
        )["hashes"]
        left_rel = remaining[0].relative_to(root).as_posix()
        assert left_rel not in stamped
        # ...and the file it DID compile is stamped (athenaeum#895: real progress).
        assert len(stamped) == 1

        # The next ingest must actually pick up the remainder (no false no-op)
        # and compile it, leaving the intake drained and fully stamped.
        second = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert second.noop is False
        assert second.exit_code == 0
        assert not list((root / "raw" / "sessions").glob("2024*.md"))
        assert (cache / "ingest-manifest.json").is_file()

    def test_session_scopes_new_change_detection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"
        (cache).mkdir()
        (cache / "ingest-manifest.json").write_text(
            json.dumps({"version": 1, "hashes": {}})
        )
        # An auto-memory raw file tagged with a session id.
        am = root / "raw" / "auto-memory" / "_unscoped"
        am.mkdir(parents=True)
        (am / "project_foo.md").write_text(
            "---\nname: Foo\noriginSessionId: sess-XYZ\n---\n\nbody\n"
        )

        monkeypatch.setattr(lib, "run", lambda *a, **k: 0)

        # A different session sees nothing new → no-op.
        other = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            session="sess-OTHER",
            cache_dir=cache,
        )
        assert other.noop is True

        # The owning session sees the new file → compiles.
        owner = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            session="sess-XYZ",
            cache_dir=cache,
        )
        assert owner.noop is False
        assert owner.new_or_changed == 1
        assert owner.session == "sess-XYZ"


# ---------------------------------------------------------------------------
# per-file ingest stamping (issue athenaeum#895)
# ---------------------------------------------------------------------------


def _stamp(cache: Path) -> dict[str, str]:
    """The stamp manifest's ``relpath -> hash`` map ({} when never written)."""
    path = cache / "ingest-manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))["hashes"]


def _pending(root: Path) -> set[str]:
    """Raw intake still on disk, as knowledge-root-relative posix paths."""
    return {
        p.relative_to(root).as_posix()
        for p in (root / "raw" / "sessions").glob("2024*.md")
    }


class TestIngestPerFileStamping:
    """Issue athenaeum#895: the stamp advances PER FILE, not all-or-nothing.

    athenaeum#530 withheld the stamp entirely unless a run drained the whole backlog.
    Under a steady backlog above ``max_files`` that condition never holds, so
    the stamp froze and every SessionEnd rediscovered the same work. These
    tests pin the replacement: a run stamps exactly the files it drained, the
    remainder stays unstamped and discoverable, and successive runs make real
    progress — while keeping athenaeum#530's invariant that a file which was not
    compiled is never stamped.
    """

    def test_truncated_run_stamps_only_the_files_it_compiled(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import ingest

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aaaaaaaa")
        _write_tier0_raw(root, "p-0002", "Bob", "20240410T130000Z", "bbbbbbbb")
        _write_tier0_raw(root, "p-0003", "Cleo", "20240410T140000Z", "cccccccc")
        discovered = _pending(root)
        cache = tmp_path / "cache"

        result = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
            max_files=2,  # two compiled, one left beyond the window
        )

        assert result.exit_code == 0
        assert result.compiled == 2
        stamped, left = set(_stamp(cache)), _pending(root)
        # The stamp holds exactly the compiled subset...
        assert len(stamped) == 2
        assert stamped == discovered - left
        # ...and nothing that was left uncompiled (the athenaeum#530 invariant, per file).
        assert not (stamped & left)
        # Nothing discovered was dropped: every file is stamped or still pending.
        assert stamped | left == discovered

    def test_next_run_skips_the_stamped_subset_and_takes_the_remainder(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        from athenaeum.librarian import ingest

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aaaaaaaa")
        _write_tier0_raw(root, "p-0002", "Bob", "20240410T130000Z", "bbbbbbbb")
        cache = tmp_path / "cache"
        kwargs = {
            "raw_root": root / "raw",
            "wiki_root": root / "wiki",
            "knowledge_root": root,
            "incremental": True,
            "cache_dir": cache,
        }

        first = ingest(**kwargs, max_files=1)
        assert first.compiled == 1
        first_stamped = set(_stamp(cache))
        remainder = _pending(root)
        assert len(remainder) == 1

        second = ingest(**kwargs)
        # The remainder is seen as new work (no false no-op) and is compiled...
        assert second.noop is False
        assert second.new_or_changed == 1
        assert second.compiled == 1
        assert not _pending(root)
        # ...and the stamp GREW rather than being rewritten from scratch: the
        # first run's file is still recorded, so it is not recompiled again.
        assert set(_stamp(cache)) == first_stamped | remainder

        third = ingest(**kwargs)
        assert third.noop is True
        assert third.compiled == 0

    def test_successive_truncated_runs_drain_a_backlog(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        """The athenaeum#895 regression: under a backlog the stamp never advanced.

        With ``max_files`` below the pending count on every run, the athenaeum#530 gate
        was never satisfied, so each run rediscovered the same head forever.
        """
        from athenaeum.librarian import ingest

        root = _seed_knowledge_root(tmp_path)
        for n, (uid, name, uuid8) in enumerate(
            [
                ("p-0001", "Alice", "aaaaaaaa"),
                ("p-0002", "Bob", "bbbbbbbb"),
                ("p-0003", "Cleo", "cccccccc"),
            ]
        ):
            _write_tier0_raw(root, uid, name, f"2024041{n}T120000Z", uuid8)
        discovered = _pending(root)
        cache = tmp_path / "cache"

        for expected_stamped in (1, 2, 3):
            result = ingest(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                incremental=True,
                cache_dir=cache,
                max_files=1,
            )
            assert result.exit_code == 0
            assert result.compiled == 1
            stamped, left = set(_stamp(cache)), _pending(root)
            # Monotonic progress, and the athenaeum#530 invariant holds every round.
            assert len(stamped) == expected_stamped
            assert not (stamped & left)
            assert stamped | left == discovered

        assert not _pending(root)

    def test_discovered_but_uncompiled_file_is_never_stamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clean exit-0 run that consumed nothing stamps nothing (athenaeum#530).

        The compile is stubbed to succeed without touching the intake — the
        shape of a run whose files were all deferred, failed, or skipped as
        stuck (athenaeum#663). The file must stay discoverable for the next run.
        """
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aaaaaaaa")
        cache = tmp_path / "cache"
        monkeypatch.setattr(lib, "run", lambda *a, **k: 0)

        result = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert result.exit_code == 0
        assert result.compiled == 0
        assert _stamp(cache) == {}
        # Still on disk → still discoverable, and still new work next run.
        assert len(_pending(root)) == 1

        again = lib.ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )
        assert again.noop is False
        assert again.new_or_changed == 1

    def test_v1_manifest_from_the_old_code_still_loads_and_is_extended(
        self, tmp_path: Path, mock_anthropic: MagicMock
    ) -> None:
        """Read back-compat: a v1 stamp (no ``stats``) loads and is merged into."""
        from athenaeum.librarian import ingest

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aaaaaaaa")
        cache = tmp_path / "cache"
        cache.mkdir()
        # A stamp in the pre-athenaeum#370 shape, naming a file consumed long ago.
        (cache / "ingest-manifest.json").write_text(
            json.dumps(
                {"version": 1, "hashes": {"raw/sessions/20230101T000000Z-old.md": "d0"}}
            )
        )

        result = ingest(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
        )

        assert result.exit_code == 0
        assert result.compiled == 1
        stamped = _stamp(cache)
        # The historical entry survives; the newly compiled file is added.
        assert stamped["raw/sessions/20230101T000000Z-old.md"] == "d0"
        assert len(stamped) == 2
        payload = json.loads(
            (cache / "ingest-manifest.json").read_text(encoding="utf-8")
        )
        # Upgraded to v2 with stats, and no stat row for a name with no hash row.
        assert payload["version"] == 2
        assert set(payload["stats"]) <= set(payload["hashes"])


# ---------------------------------------------------------------------------
# ingest CLI wrapper
# ---------------------------------------------------------------------------


class TestIngestCLI:
    def test_json_summary_shape_and_exit_zero(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        rc = main(["ingest", "--path", str(root), "--cache-dir", str(cache)])
        assert rc == 0
        line = capsys.readouterr().out.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["command"] == "ingest"
        assert payload["mode"] == "incremental"
        assert payload["compiled"] == 1
        assert payload["noop"] is False
        assert payload["exit_code"] == 0
        assert isinstance(payload["duration_ms"], int)

    def test_exit_nonzero_on_compile_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import athenaeum.librarian as lib

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        monkeypatch.setattr(lib, "run", lambda *a, **k: 1)

        rc = main(["ingest", "--path", str(root), "--cache-dir", str(tmp_path / "c")])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["exit_code"] == 1

    def test_single_flight_lock_held(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from athenaeum.runlock import RunLock

        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")

        with RunLock(root):  # hold the lock so ingest can't acquire it
            rc = main(
                ["ingest", "--path", str(root), "--cache-dir", str(tmp_path / "c")]
            )
        assert rc == EXIT_LOCK_HELD
        assert "error" in capsys.readouterr().err.lower()

    def test_dry_run_does_not_stamp(
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
                "ingest",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
                "--dry-run",
            ]
        )
        assert rc == 0
        # Dry-run never writes the stamp and never consumes the raw file.
        assert not (cache / "ingest-manifest.json").exists()
        assert list((root / "raw" / "sessions").glob("2024*.md"))


# ---------------------------------------------------------------------------
# --if-triggered (issue athenaeum#909) — the trigger control-signal surface on
# the SAME ``ingest`` command. Individual trigger LOGIC (each reason, the
# backstop, precedence) is covered purely in
# ``tests/test_reasoning_triggers.py``; these tests cover the CLI wiring:
# evaluate-before-lock, the no-op JSON shape, a firing trigger running the
# normal compile, and the single-flight lock guard (AC8, mirrors
# ``test_single_flight_lock_held`` above).
# ---------------------------------------------------------------------------


class TestIngestIfTriggeredCLI:
    def test_nothing_configured_second_call_is_a_cheap_noop(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        # First call: no reasoning-trigger stamp yet -> "infinitely overdue"
        # -> the always-on nightly backstop fires -> runs and stamps.
        rc1 = main(
            ["ingest", "--if-triggered", "--path", str(root), "--cache-dir", str(cache)]
        )
        assert rc1 == 0
        first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert first["trigger"] == "nightly-backstop"
        assert (cache / "reasoning-trigger-stamp.json").is_file()

        # Second call, immediately after: no trigger configured, the stamp
        # is fresh (well under the 24h backstop) -> a cheap no-op.
        rc2 = main(
            ["ingest", "--if-triggered", "--path", str(root), "--cache-dir", str(cache)]
        )
        assert rc2 == 0
        second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert second == {
            "command": "ingest",
            "mode": "incremental",
            "new_or_changed": 0,
            "compiled": 0,
            "noop": True,
            "duration_ms": 0,
            "exit_code": 0,
            "trigger": "none",
        }

    def test_noop_never_takes_the_run_lock(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from athenaeum.runlock import RunLock

        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"
        # Establish a fresh stamp with nothing else configured, then a
        # held run lock must NOT block a no-op second call.
        assert (
            main(
                [
                    "ingest",
                    "--if-triggered",
                    "--path",
                    str(root),
                    "--cache-dir",
                    str(cache),
                ]
            )
            == 0
        )
        capsys.readouterr()

        with RunLock(root):
            rc = main(
                [
                    "ingest",
                    "--if-triggered",
                    "--path",
                    str(root),
                    "--cache-dir",
                    str(cache),
                ]
            )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["trigger"] == "none"

    def test_backlog_files_trigger_fires_and_runs(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _seed_knowledge_root(tmp_path)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  reasoning_triggers:\n    backlog_files: 1\n"
        )
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        rc = main(
            ["ingest", "--if-triggered", "--path", str(root), "--cache-dir", str(cache)]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["trigger"] == "backlog-files"
        assert payload["compiled"] == 1
        assert (cache / "reasoning-trigger-stamp.json").is_file()

    def test_without_the_flag_behaves_exactly_as_before(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # AC3: on-demand is the EXISTING behaviour (no --if-triggered) —
        # unchanged, no "trigger" key in the summary.
        root = _seed_knowledge_root(tmp_path)
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        rc = main(["ingest", "--path", str(root), "--cache-dir", str(cache)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "trigger" not in payload
        assert payload["compiled"] == 1

    def test_single_flight_lock_held_blocks_a_firing_trigger(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # AC8 / D5: a trigger MUST go through the CLI's existing lock guard
        # — mirrors TestIngestCLI.test_single_flight_lock_held above, but
        # via --if-triggered with a trigger CONFIGURED TO FIRE.
        from athenaeum.runlock import RunLock

        root = _seed_knowledge_root(tmp_path)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  reasoning_triggers:\n    backlog_files: 1\n"
        )
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")

        with RunLock(root):  # hold the lock so the fired trigger can't acquire it
            rc = main(
                [
                    "ingest",
                    "--if-triggered",
                    "--path",
                    str(root),
                    "--cache-dir",
                    str(tmp_path / "c"),
                ]
            )
        assert rc == EXIT_LOCK_HELD
        assert "error" in capsys.readouterr().err.lower()

    def test_full_combo_is_rejected(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # D7/AC4: a triggered run is ALWAYS incremental — --if-triggered
        # combined with --full must be a loud, rejected error, not a
        # silent downgrade of either flag.
        root = _seed_knowledge_root(tmp_path)

        rc = main(
            ["ingest", "--if-triggered", "--full", "--path", str(root)]
        )
        assert rc == 1
        assert "--if-triggered" in capsys.readouterr().err

    def test_firing_trigger_never_passes_incremental_false(
        self,
        tmp_path: Path,
        mock_anthropic: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # D7/AC4 (positive half): capture the EXACT kwargs the CLI hands to
        # the reusable ingest engine when a trigger fires, and assert
        # ``incremental`` is True and no ``full_compile`` kwarg is smuggled
        # through — never a full recompile.
        import athenaeum.librarian as librarian_mod
        from athenaeum.librarian import IngestResult

        root = _seed_knowledge_root(tmp_path)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  reasoning_triggers:\n    backlog_files: 1\n"
        )
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")

        captured: dict = {}

        def fake_ingest(**kwargs):
            captured.update(kwargs)
            return IngestResult(
                mode="incremental",
                new_or_changed=1,
                compiled=1,
                noop=False,
                exit_code=0,
                duration_ms=1,
            )

        monkeypatch.setattr(librarian_mod, "ingest", fake_ingest)

        rc = main(
            [
                "ingest",
                "--if-triggered",
                "--path",
                str(root),
                "--cache-dir",
                str(tmp_path / "c"),
            ]
        )
        assert rc == 0
        assert captured["incremental"] is True
        assert "full_compile" not in captured


# ---------------------------------------------------------------------------
# --evaluate-only (issue athenaeum#1001) — the lock-free PUBLIC
# trigger-evaluation mode. Unlike --if-triggered, this never calls
# ``ingest()`` (the only thing in this module that touches git), so its
# fixture skips ``_seed_knowledge_root``'s ``git init``/commit entirely —
# this also sidesteps an unrelated, pre-existing sandbox restriction here
# that refuses commits to a fixture repo whose initial branch is `main`.
# ---------------------------------------------------------------------------


def _seed_bare_knowledge_root(tmp_path: Path) -> Path:
    """A knowledge root with just a `raw/sessions` dir — no git required.

    Valid for --evaluate-only because that mode never calls
    :func:`athenaeum.librarian.ingest` (the only git-touching call in this
    module), regardless of whether a trigger fires.
    """
    root = tmp_path / "knowledge"
    (root / "raw" / "sessions").mkdir(parents=True)
    return root


class TestIngestEvaluateOnlyCLI:
    def test_never_takes_the_run_lock_even_when_a_trigger_fires(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC1: holds the real run lock, then shows --evaluate-only still
        succeeds and reports a FIRED verdict — the strongest form of "does
        not take .athenaeum.lock" (mirrors
        ``TestIngestIfTriggeredCLI.test_noop_never_takes_the_run_lock``, but
        for a firing trigger rather than a no-op one, since --evaluate-only
        must never contend for the lock regardless of the verdict)."""
        from athenaeum.runlock import RunLock

        root = _seed_bare_knowledge_root(tmp_path)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  reasoning_triggers:\n    backlog_files: 1\n"
        )
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        with RunLock(root):  # held for the entire evaluate-only call
            rc = main(
                [
                    "ingest",
                    "--evaluate-only",
                    "--path",
                    str(root),
                    "--cache-dir",
                    str(cache),
                ]
            )
        assert rc == 2
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload == {
            "command": "ingest",
            "mode": "evaluate-only",
            "fired": True,
            "trigger": "backlog-files",
            "exit_code": 2,
        }

    def test_never_compiles_even_when_a_trigger_fires(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC1 (compile half): monkeypatch the underlying compile engine to
        raise if called at all, then show a FIRING --evaluate-only call
        never reaches it — unlike --if-triggered, a fire never compiles
        here."""
        import athenaeum.librarian as librarian_mod

        def _boom(**kwargs: object) -> None:
            raise AssertionError("--evaluate-only must never call ingest()")

        monkeypatch.setattr(librarian_mod, "ingest", _boom)

        root = _seed_bare_knowledge_root(tmp_path)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  reasoning_triggers:\n    backlog_files: 1\n"
        )
        _write_tier0_raw(root, "p-0001", "Alice", "20240410T120000Z", "aabbccdd")
        cache = tmp_path / "cache"

        rc = main(
            [
                "ingest",
                "--evaluate-only",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
            ]
        )
        assert rc == 2
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["fired"] is True

    def test_reads_the_same_stamp_path_if_triggered_completion_writes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC2: writes the reasoning-trigger stamp via the EXACT function
        --if-triggered's completion path calls
        (:func:`athenaeum._cmd_index._record_ingest_trigger_completion`),
        then shows --evaluate-only sees it as fresh (no trigger fires). If
        --evaluate-only read from a different path/source, it would see NO
        stamp, treat ``since_last_run`` as ``None`` ("infinitely overdue"),
        and the nightly backstop would fire unconditionally — so this test
        fails loudly on a path mismatch rather than passing by coincidence.
        """
        from athenaeum._cmd_index import _record_ingest_trigger_completion

        root = _seed_bare_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        _record_ingest_trigger_completion(cache)  # same writer --if-triggered uses

        rc = main(
            [
                "ingest",
                "--evaluate-only",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload == {
            "command": "ingest",
            "mode": "evaluate-only",
            "fired": False,
            "trigger": "none",
            "exit_code": 0,
        }

    def test_evaluate_only_never_writes_the_stamp_itself(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--evaluate-only is read-only: it must not advance the
        reasoning-trigger stamp it reads (only a real completed run does)."""
        root = _seed_bare_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        rc = main(
            [
                "ingest",
                "--evaluate-only",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
            ]
        )
        assert rc == 2  # first-ever call: nightly backstop fires
        capsys.readouterr()
        assert not (cache / "reasoning-trigger-stamp.json").exists()

    def test_error_path_exits_1_and_prints_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The error exit code (1) is distinct from both fired (2) and
        not-fired (0)."""
        import athenaeum._cmd_index as cmd_index_mod

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(cmd_index_mod, "_evaluate_ingest_trigger", _boom)

        root = _seed_bare_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        rc = main(
            [
                "ingest",
                "--evaluate-only",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
            ]
        )
        assert rc == 1
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["command"] == "ingest"
        assert payload["mode"] == "evaluate-only"
        assert "boom" in payload["error"]
        assert payload["exit_code"] == 1

    def test_combined_with_if_triggered_is_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _seed_bare_knowledge_root(tmp_path)

        rc = main(["ingest", "--evaluate-only", "--if-triggered", "--path", str(root)])
        assert rc == 1
        assert "--evaluate-only" in capsys.readouterr().err

    def test_public_mode_matches_what_the_private_symbol_read_would_show(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC3: the deployed wrapper's private-symbol imports become
        unnecessary. Reproduces exactly what the wrapper's private read does
        today (:data:`athenaeum.librarian.REASONING_TRIGGER_STAMP_NAME` +
        :func:`athenaeum.librarian._load_timestamp_stamp` to check "has a
        triggered run ever completed") and shows the public
        ``ingest --evaluate-only`` JSON verdict carries the same
        fired/not-fired answer for the same state — so the wrapper's job
        (decide whether to invoke ``ingest --if-triggered`` next) is fully
        expressible through the public CLI mode alone, with no private
        import at the call site.
        """
        from athenaeum.config import resolve_cache_dir
        from athenaeum.librarian import (
            REASONING_TRIGGER_STAMP_NAME,
            _load_timestamp_stamp,
        )

        root = _seed_bare_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        # What the wrapper's private-symbol read sees today: no stamp yet.
        stamp_path = resolve_cache_dir(cache) / REASONING_TRIGGER_STAMP_NAME
        assert _load_timestamp_stamp(stamp_path) is None

        rc = main(
            [
                "ingest",
                "--evaluate-only",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
            ]
        )
        payload = json.loads(capsys.readouterr().out.strip())
        # The public mode's "run now" answer (exit 2, fired) agrees with what
        # the private read above implies (no completed run ever -> overdue).
        assert rc == 2
        assert payload["fired"] is True

    def test_renaming_the_private_symbols_does_not_change_behavior(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC4: renaming ``REASONING_TRIGGER_STAMP_NAME`` /
        ``_load_timestamp_stamp`` must not change --evaluate-only's
        observable behavior. Simulates the rename by aliasing the stamp
        filename constant under a new name in ``athenaeum.librarian`` —
        ``_evaluate_ingest_trigger`` re-imports the module attribute fresh
        on every call, so this exercises the real lookup, not a stale
        reference — and shows the public mode's verdict is unaffected.
        """
        import athenaeum.librarian as librarian_mod

        monkeypatch.setattr(
            librarian_mod, "REASONING_TRIGGER_STAMP_NAME", "renamed-stamp-name.json"
        )

        root = _seed_bare_knowledge_root(tmp_path)
        cache = tmp_path / "cache"

        rc = main(
            [
                "ingest",
                "--evaluate-only",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
            ]
        )
        assert rc == 2  # first-ever call still fires the nightly backstop
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["fired"] is True
        assert payload["trigger"] == "nightly-backstop"


# ---------------------------------------------------------------------------
# reindex CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge_with_wiki(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "a.md").write_text(
        "---\nname: A\ntags: [x]\ndescription: d\n---\n\nAlpha body.\n"
    )
    (wiki / "b.md").write_text(
        "---\nname: B\ntags: [x]\ndescription: d\n---\n\nBeta body.\n"
    )
    return knowledge


class TestReindexCLI:
    def test_incremental_json_summary(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cache = tmp_path / "cache"
        rc = main(
            [
                "reindex",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["command"] == "reindex"
        assert payload["backend"] == "fts5"
        assert payload["mode"] == "incremental"
        assert payload["pages"] == 2
        assert payload["exit_code"] == 0
        assert isinstance(payload["duration_ms"], int)

    def test_incremental_noop_when_nothing_changed(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cache = tmp_path / "cache"
        args = [
            "reindex",
            "--path",
            str(knowledge_with_wiki),
            "--cache-dir",
            str(cache),
            "--backend",
            "fts5",
        ]
        assert main(args) == 0
        capsys.readouterr()
        # Second incremental pass with no file changes: still succeeds and is
        # sub-second (the athenaeum#348 hash-diff finds an empty delta).
        assert main(args) == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["mode"] == "incremental"
        assert payload["exit_code"] == 0
        assert payload["duration_ms"] < 1000

    def test_full_mode_reported(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(
            [
                "reindex",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--backend",
                "fts5",
                "--full",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["mode"] == "full"

    def test_rebuild_index_alias_still_works(
        self,
        knowledge_with_wiki: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(
            [
                "rebuild-index",
                "--path",
                str(knowledge_with_wiki),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "FTS5 index rebuilt" in out  # legacy human line preserved
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["command"] == "rebuild-index"
