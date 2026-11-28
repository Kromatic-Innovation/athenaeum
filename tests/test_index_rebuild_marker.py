# SPDX-License-Identifier: Apache-2.0
"""Session end never runs a full index rebuild (issue athenaeum#3320).

The defect: an upgrade rolled the vector index's metadata schema from v3 to
v4, so the existing index could not be reused. Every ``athenaeum session-end``
then started a full rebuild of a ~26,000-page corpus, was killed at the
SessionEnd deadline, and left the index on v3 for the next close to try
again (148 timeouts, about 14 minutes of CPU each).

These tests pin the four acceptance criteria:

1. a v3 index under v4 code: ``session_end`` exits without attempting the
   rebuild and leaves the marker (both compile arms);
2. the nightly path (``refresh_index_after_run``) and ``athenaeum reindex``
   perform the rebuild once and clear the marker, and the next
   ``session_end`` is incremental;
3. consecutive failures, including killed attempts, surface in
   ``athenaeum doctor``;
4. the docs say where full rebuilds happen (checked against the guide).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from athenaeum import doctor, index_rebuild_marker
from athenaeum.cli import main
from tests.test_session_end import (  # noqa: F401 - mock_anthropic is a fixture
    _seed_knowledge_root,
    _write_tier0_raw,
    mock_anthropic,
)

_REPO = Path(__file__).resolve().parents[1]


def _session_end(root: Path, cache: Path, backend: str):  # type: ignore[no-untyped-def]
    from athenaeum.librarian import session_end

    return session_end(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        cache_dir=cache,
        backend=backend,
    )


def _built_store(tmp_path: Path, backend: str) -> tuple[Path, Path]:
    """A knowledge root with one compiled page and a current index."""
    root = _seed_knowledge_root(tmp_path)
    _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
    cache = tmp_path / "cache"
    first = _session_end(root, cache, backend)
    assert first.reindexed is True
    assert index_rebuild_marker.read_marker(cache) is None
    return root, cache


def _roll_vector_manifest_to_v3(cache: Path) -> None:
    """Make the on-disk vector index look like one written by the old code."""
    from athenaeum.search import VectorBackend

    assert VectorBackend._METADATA_SCHEMA_VERSION == 4, (
        "this test reproduces the v3 -> v4 roll; update the literal below if the schema moves again"
    )
    manifest = cache / "vector-manifest.json"
    payload = json.loads(manifest.read_text())
    payload["metadata_schema_version"] = 3
    manifest.write_text(json.dumps(payload))


def _roll_fts5_db_back(cache: Path) -> None:
    """Make the fts5 DB look like one written under an older schema."""
    conn = sqlite3.connect(str(cache / "wiki-index.db"))
    try:
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


def _forbid_reindex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any reindex from session end is the defect: make it fail loudly."""
    import athenaeum.librarian as librarian

    def _boom(*_a: object, **_k: object) -> tuple[str, int]:
        raise AssertionError("session_end attempted a reindex it must defer")

    monkeypatch.setattr(librarian, "reindex", _boom)


# ---------------------------------------------------------------------------
# AC1: a v3 index under v4 code — session end defers, fast, with the marker
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_anthropic")
def test_v3_vector_index_session_end_skips_rebuild_and_leaves_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("chromadb")
    root, cache = _built_store(tmp_path, "vector")
    _roll_vector_manifest_to_v3(cache)
    _forbid_reindex(monkeypatch)

    result = _session_end(root, cache, "vector")

    assert result.exit_code == 0
    assert result.reindexed is False
    assert result.full_rebuild_deferred == "metadata schema version 3, expected 4"
    summary = result.summary()
    assert summary["full_rebuild_required"]["reason"] == result.full_rebuild_deferred
    marker = index_rebuild_marker.read_marker(cache)
    assert marker is not None
    assert marker.reason == "metadata schema version 3, expected 4"
    assert marker.backend == "vector"
    assert marker.deferrals == 1 and marker.attempts == 0
    # The old index is left in place for recall to keep serving from.
    assert json.loads((cache / "vector-manifest.json").read_text())["metadata_schema_version"] == 3


@pytest.mark.usefixtures("mock_anthropic")
def test_repeated_session_ends_only_count_deferrals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop the issue reported: sequential closes each restarted the
    rebuild. Now each one only bumps the deferral count."""
    root, cache = _built_store(tmp_path, "fts5")
    _roll_fts5_db_back(cache)
    _forbid_reindex(monkeypatch)

    for _ in range(3):
        result = _session_end(root, cache, "fts5")
        assert result.reindexed is False
        assert result.full_rebuild_deferred == "db schema version 1, expected 7"

    marker = index_rebuild_marker.read_marker(cache)
    assert marker is not None
    assert marker.deferrals == 3
    assert marker.attempts == 0 and marker.consecutive_failures == 0


@pytest.mark.usefixtures("mock_anthropic")
def test_compile_that_ran_also_defers_the_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compile-ran arm used to reindex unconditionally, which on a
    non-reusable index is the same full rebuild by another door."""
    root, cache = _built_store(tmp_path, "fts5")
    _roll_fts5_db_back(cache)
    _forbid_reindex(monkeypatch)
    _write_tier0_raw(root, "p-0002", "Bob Okafor", "20240411T120000Z", "11223344")

    result = _session_end(root, cache, "fts5")

    assert result.ingest.noop is False  # the compile ran...
    assert result.exit_code == 0
    assert result.reindexed is False  # ...but the full rebuild did not.
    assert result.full_rebuild_deferred
    assert index_rebuild_marker.read_marker(cache) is not None


@pytest.mark.usefixtures("mock_anthropic")
def test_first_build_still_happens_at_session_end(tmp_path: Path) -> None:
    """No index at all is a first build, not a rebuild: unchanged."""
    root = _seed_knowledge_root(tmp_path)
    _write_tier0_raw(root, "p-0001", "Alice Zhang", "20240410T120000Z", "aabbccdd")
    cache = tmp_path / "cache"

    result = _session_end(root, cache, "fts5")

    assert result.reindexed is True
    assert result.full_rebuild_deferred is None
    assert index_rebuild_marker.read_marker(cache) is None


@pytest.mark.usefixtures("mock_anthropic")
def test_dry_run_records_nothing(tmp_path: Path) -> None:
    from athenaeum.librarian import session_end

    root, cache = _built_store(tmp_path, "fts5")
    _roll_fts5_db_back(cache)

    result = session_end(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        cache_dir=cache,
        backend="fts5",
        dry_run=True,
    )

    assert result.reindexed is False
    assert index_rebuild_marker.read_marker(cache) is None


# ---------------------------------------------------------------------------
# AC2: the nightly / explicit path rebuilds once and clears the marker
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_anthropic")
def test_nightly_refresh_rebuilds_once_clears_marker_then_session_end_is_incremental(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.search as search
    from athenaeum._cli_shared import refresh_index_after_run

    root, cache = _built_store(tmp_path, "fts5")
    _roll_fts5_db_back(cache)
    deferred = _session_end(root, cache, "fts5")
    assert deferred.full_rebuild_deferred

    builds: list[bool] = []
    real_build = search.build_fts5_index

    def _counting_build(*args: object, **kwargs: object) -> int:
        builds.append(bool(kwargs.get("incremental")))
        return real_build(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(search, "build_fts5_index", _counting_build)
    # The nightly path reads the backend from config, as a deployment does.
    (root / "athenaeum.yaml").write_text("search_backend: fts5\n")

    # The nightly `athenaeum run` path.
    refresh_index_after_run(root, root / "wiki", cache)
    assert len(builds) == 1, "the nightly path must perform the rebuild exactly once"
    assert index_rebuild_marker.read_marker(cache) is None
    conn = sqlite3.connect(str(cache / "wiki-index.db"))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        conn.close()

    # A second nightly run has nothing to rebuild.
    refresh_index_after_run(root, root / "wiki", cache)
    assert len(builds) == 1

    # The next session end is incremental: idle, no deferral, no marker.
    after = _session_end(root, cache, "fts5")
    assert after.full_rebuild_deferred is None
    assert after.reindex_would_change == 0 and after.reindexed is False

    # And a session that compiles something reindexes incrementally again.
    _write_tier0_raw(root, "p-0002", "Bob Okafor", "20240411T120000Z", "11223344")
    compiled = _session_end(root, cache, "fts5")
    assert compiled.reindexed is True and compiled.full_rebuild_deferred is None
    assert builds[-1] is True  # incremental
    assert index_rebuild_marker.read_marker(cache) is None


@pytest.mark.usefixtures("mock_anthropic")
def test_reindex_command_clears_the_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, cache = _built_store(tmp_path, "fts5")
    _roll_fts5_db_back(cache)
    assert _session_end(root, cache, "fts5").full_rebuild_deferred

    rc = main(["reindex", "--path", str(root), "--cache-dir", str(cache), "--backend", "fts5"])

    assert rc == 0
    assert index_rebuild_marker.read_marker(cache) is None
    assert _session_end(root, cache, "fts5").full_rebuild_deferred is None


@pytest.mark.usefixtures("mock_anthropic")
def test_reindex_as_of_snapshot_leaves_the_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A historical as-of build is not the current index's rebuild."""
    root, cache = _built_store(tmp_path, "fts5")
    _roll_fts5_db_back(cache)
    assert _session_end(root, cache, "fts5").full_rebuild_deferred

    main(
        [
            "reindex",
            "--path",
            str(root),
            "--cache-dir",
            str(cache),
            "--backend",
            "fts5",
            "--as-of",
            "2024-01-01",
        ]
    )

    assert index_rebuild_marker.read_marker(cache) is not None


# ---------------------------------------------------------------------------
# AC3: consecutive failures surface in doctor
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_anthropic")
def test_failing_rebuilds_are_counted_and_surface_in_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.search as search
    from athenaeum.librarian import reindex

    root, cache = _built_store(tmp_path, "fts5")
    _roll_fts5_db_back(cache)
    assert _session_end(root, cache, "fts5").full_rebuild_deferred

    def _fail(*_a: object, **_k: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(search, "build_fts5_index", _fail)

    for attempt in range(1, index_rebuild_marker.FAILURE_WARN_THRESHOLD + 1):
        check = doctor.check_index_rebuild(cache)
        assert check.status == "warn"
        assert "failed" not in check.detail.split(";")[0]
        with pytest.raises(OSError, match="disk full"):
            reindex(knowledge_root=root, cache_dir=cache, backend="fts5")
        marker = index_rebuild_marker.read_marker(cache)
        assert marker is not None
        assert marker.attempts == attempt
        assert marker.consecutive_failures == attempt
        assert marker.attempt_started_at is None
        assert marker.last_failure == "OSError: disk full"

    check = doctor.check_index_rebuild(cache)
    assert check.status == "warn"
    assert check.detail.startswith(
        f"full index rebuild has failed {index_rebuild_marker.FAILURE_WARN_THRESHOLD} "
        "times in a row"
    )
    assert "reindex --full" in check.fix
    report = doctor.format_report([check])
    assert "[WARN] index_rebuild: full index rebuild has failed" in report

    # A rebuild that finally completes clears the marker and the warning.
    monkeypatch.undo()
    reindex(knowledge_root=root, cache_dir=cache, backend="fts5")
    assert index_rebuild_marker.read_marker(cache) is None
    assert doctor.check_index_rebuild(cache).status == "ok"


def test_killed_attempt_is_counted_by_the_next_attempt(tmp_path: Path) -> None:
    """A timeout kill never reaches the failure handler; the next attempt
    finds the earlier one still marked in progress and counts it."""
    cache = tmp_path / "cache"
    index_rebuild_marker.record_deferred(cache, reason="r", backend="vector")
    for _ in range(index_rebuild_marker.FAILURE_WARN_THRESHOLD + 1):
        # Each attempt "starts" and is then killed: no outcome is recorded.
        index_rebuild_marker.record_attempt_started(cache, reason="r", backend="vector")

    marker = index_rebuild_marker.read_marker(cache)
    assert marker is not None
    assert marker.attempts == index_rebuild_marker.FAILURE_WARN_THRESHOLD + 1
    assert marker.consecutive_failures == index_rebuild_marker.FAILURE_WARN_THRESHOLD
    assert marker.failing
    assert "never finished" in (marker.last_failure or "")
    assert doctor.check_index_rebuild(cache).detail.startswith("full index rebuild has failed")


def test_doctor_reports_no_pending_rebuild(tmp_path: Path) -> None:
    check = doctor.check_index_rebuild(tmp_path)
    assert check.status == "ok"
    assert check.name == "index_rebuild"


@pytest.mark.usefixtures("mock_anthropic")
def test_nightly_rebuild_without_a_prior_session_end_is_still_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the nightly run meets the non-reusable index first, its attempt is
    recorded too, so its failures are not silent either."""
    import athenaeum.search as search
    from athenaeum.librarian import reindex

    root, cache = _built_store(tmp_path, "fts5")
    _roll_fts5_db_back(cache)

    def _fail(*_a: object, **_k: object) -> int:
        raise RuntimeError("embedder crashed")

    monkeypatch.setattr(search, "build_fts5_index", _fail)
    with pytest.raises(RuntimeError):
        reindex(knowledge_root=root, cache_dir=cache, backend="fts5")

    marker = index_rebuild_marker.read_marker(cache)
    assert marker is not None
    assert marker.reason == "db schema version 1, expected 7"
    assert marker.consecutive_failures == 1 and marker.deferrals == 0


@pytest.mark.usefixtures("mock_anthropic")
def test_incremental_reindex_of_a_healthy_index_records_nothing(tmp_path: Path) -> None:
    from athenaeum.librarian import reindex

    root, cache = _built_store(tmp_path, "fts5")
    reindex(knowledge_root=root, cache_dir=cache, backend="fts5")
    assert index_rebuild_marker.read_marker(cache) is None


# ---------------------------------------------------------------------------
# The marker file itself
# ---------------------------------------------------------------------------


def test_corrupt_marker_reads_as_absent(tmp_path: Path) -> None:
    index_rebuild_marker.marker_path(tmp_path).write_text("{not json")
    assert index_rebuild_marker.read_marker(tmp_path) is None
    index_rebuild_marker.marker_path(tmp_path).write_text('{"reason": "x"}')
    assert index_rebuild_marker.read_marker(tmp_path) is None


def test_clear_reports_whether_there_was_a_marker(tmp_path: Path) -> None:
    assert index_rebuild_marker.clear(tmp_path) is False
    index_rebuild_marker.record_deferred(tmp_path, reason="r", backend="fts5")
    assert index_rebuild_marker.clear(tmp_path) is True
    assert index_rebuild_marker.read_marker(tmp_path) is None


def test_failure_without_a_marker_is_a_no_op(tmp_path: Path) -> None:
    assert index_rebuild_marker.record_attempt_failed(tmp_path, "x") is None
    assert not index_rebuild_marker.marker_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# AC4: the docs say where full rebuilds happen
# ---------------------------------------------------------------------------


def test_daily_operation_guide_says_where_full_rebuilds_happen() -> None:
    guide = (_REPO / "docs" / "guides" / "daily-operation.md").read_text()
    assert "session end never runs one" in guide
    assert "full-rebuild-required.json" in guide
    assert "the nightly `athenaeum run`" in guide
    assert "`athenaeum reindex`" in guide
    assert "index_rebuild" in guide


@pytest.mark.usefixtures("mock_anthropic")
def test_killed_rebuild_that_left_a_reusable_looking_index_is_rebuilt_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuild killed part-way can leave a partial index whose manifest
    reads as reusable (the vector build recreates its collection dir first
    and writes the manifest last). Session end must not take that as done,
    and the next owner build must run full, not an empty incremental diff."""
    import athenaeum.search as search
    from athenaeum.librarian import reindex

    root, cache = _built_store(tmp_path, "fts5")
    _roll_fts5_db_back(cache)
    assert _session_end(root, cache, "fts5").full_rebuild_deferred

    def _killed(*_a: object, **_k: object) -> int:
        raise KeyboardInterrupt  # a BaseException, like the kill: no outcome recorded

    monkeypatch.setattr(search, "build_fts5_index", _killed)
    with pytest.raises(KeyboardInterrupt):
        reindex(knowledge_root=root, cache_dir=cache, backend="fts5")
    monkeypatch.undo()
    marker = index_rebuild_marker.read_marker(cache)
    assert marker is not None and marker.attempt_started_at is not None

    # The killed attempt happened to leave a DB stamped with the current
    # schema, so the blocker alone now reads "reusable".
    conn = sqlite3.connect(str(cache / "wiki-index.db"))
    try:
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
    finally:
        conn.close()

    _forbid_reindex(monkeypatch)
    result = _session_end(root, cache, "fts5")
    assert result.reindexed is False
    assert "did not finish" in (result.full_rebuild_deferred or "")
    assert index_rebuild_marker.read_marker(cache) is not None
    monkeypatch.undo()

    builds: list[object] = []
    real_build = search.build_fts5_index

    def _recording(*args: object, **kwargs: object) -> int:
        builds.append(kwargs.get("incremental"))
        return real_build(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(search, "build_fts5_index", _recording)
    reindex(knowledge_root=root, cache_dir=cache, backend="fts5")
    assert builds == [False], "the build after a killed attempt must run full"
    assert index_rebuild_marker.read_marker(cache) is None
