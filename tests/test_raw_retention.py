# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#1269 — configurable raw-intake retention size
limits, per-file AND per-source aggregate.

Three concerns, class-per-concern (mirrors ``tests/test_reasoning_triggers.py``):

- ``TestResolveRawRetentionMaxFileBytes`` / ``TestResolveRawRetentionMaxSourceBytes``
  — the two config resolvers under ``librarian.raw_retention.*``: DEFAULT-NONE
  (no seed in ``_DEFAULTS``, issue athenaeum#231), env > yaml > default (``None``)
  precedence, ``bool`` rejected despite being an ``int`` subclass, and
  non-numeric/negative values falling through to off.
- ``TestCheckRawRetention`` — the detection sweep itself
  (:func:`athenaeum.intake.check_raw_retention`): both dimensions
  independently, the motivating many-small-files-aggregate case, and the
  explicit negative assertion that crossing a threshold never blocks
  intake, moves a file, or writes an exempt row.
- ``TestRunRawRetentionPhase`` (in ``tests/test_librarian_run_phases.py``,
  not here) covers the ``librarian.run()`` phase wiring
  (``_run_raw_retention_phase`` / ``ctx.raw_retention_summary``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from athenaeum.config import (
    resolve_raw_retention_max_file_bytes,
    resolve_raw_retention_max_source_bytes,
)
from athenaeum.intake import check_raw_retention

# ---------------------------------------------------------------------------
# resolve_raw_retention_max_file_bytes
# ---------------------------------------------------------------------------


class TestResolveRawRetentionMaxFileBytes:
    def test_default_disabled(self) -> None:
        assert resolve_raw_retention_max_file_bytes(None) is None
        assert resolve_raw_retention_max_file_bytes({}) is None

    def test_explicit_yaml_override(self) -> None:
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 10485760}}}
        assert resolve_raw_retention_max_file_bytes(cfg) == 10485760

    def test_bool_rejected(self) -> None:
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": True}}}
        assert resolve_raw_retention_max_file_bytes(cfg) is None

    def test_non_positive_rejected(self) -> None:
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 0}}}
        assert resolve_raw_retention_max_file_bytes(cfg) is None
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": -5}}}
        assert resolve_raw_retention_max_file_bytes(cfg) is None

    def test_non_numeric_rejected(self) -> None:
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": "10MB"}}}
        assert resolve_raw_retention_max_file_bytes(cfg) is None

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RAW_RETENTION_MAX_FILE_BYTES", "2048")
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 10485760}}}
        assert resolve_raw_retention_max_file_bytes(cfg) == 2048

    def test_negative_env_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RAW_RETENTION_MAX_FILE_BYTES", "-1")
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 10485760}}}
        assert resolve_raw_retention_max_file_bytes(cfg) is None

    def test_malformed_env_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RAW_RETENTION_MAX_FILE_BYTES", "not-a-number")
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 10485760}}}
        with caplog.at_level("WARNING"):
            assert resolve_raw_retention_max_file_bytes(cfg) == 10485760
        assert "ATHENAEUM_RAW_RETENTION_MAX_FILE_BYTES" in caplog.text


# ---------------------------------------------------------------------------
# resolve_raw_retention_max_source_bytes
# ---------------------------------------------------------------------------


class TestResolveRawRetentionMaxSourceBytes:
    def test_default_disabled(self) -> None:
        assert resolve_raw_retention_max_source_bytes(None) is None
        assert resolve_raw_retention_max_source_bytes({}) is None

    def test_explicit_yaml_override(self) -> None:
        cfg = {"librarian": {"raw_retention": {"max_source_bytes": 268435456}}}
        assert resolve_raw_retention_max_source_bytes(cfg) == 268435456

    def test_bool_rejected(self) -> None:
        cfg = {"librarian": {"raw_retention": {"max_source_bytes": True}}}
        assert resolve_raw_retention_max_source_bytes(cfg) is None

    def test_non_positive_rejected(self) -> None:
        cfg = {"librarian": {"raw_retention": {"max_source_bytes": 0}}}
        assert resolve_raw_retention_max_source_bytes(cfg) is None
        cfg = {"librarian": {"raw_retention": {"max_source_bytes": -5}}}
        assert resolve_raw_retention_max_source_bytes(cfg) is None

    def test_non_numeric_rejected(self) -> None:
        cfg = {"librarian": {"raw_retention": {"max_source_bytes": "256MB"}}}
        assert resolve_raw_retention_max_source_bytes(cfg) is None

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RAW_RETENTION_MAX_SOURCE_BYTES", "4096")
        cfg = {"librarian": {"raw_retention": {"max_source_bytes": 268435456}}}
        assert resolve_raw_retention_max_source_bytes(cfg) == 4096

    def test_negative_env_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RAW_RETENTION_MAX_SOURCE_BYTES", "-1")
        cfg = {"librarian": {"raw_retention": {"max_source_bytes": 268435456}}}
        assert resolve_raw_retention_max_source_bytes(cfg) is None

    def test_malformed_env_warns_and_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RAW_RETENTION_MAX_SOURCE_BYTES", "nope")
        cfg = {"librarian": {"raw_retention": {"max_source_bytes": 268435456}}}
        with caplog.at_level("WARNING"):
            assert resolve_raw_retention_max_source_bytes(cfg) == 268435456
        assert "ATHENAEUM_RAW_RETENTION_MAX_SOURCE_BYTES" in caplog.text


# ---------------------------------------------------------------------------
# check_raw_retention
# ---------------------------------------------------------------------------


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


class TestCheckRawRetention:
    def test_both_thresholds_unset_reports_nothing(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        _write(raw_root / "mural" / "a.json", 1000)
        summary = check_raw_retention(raw_root, config=None)
        assert summary == {
            "raw-oversize-file": 0,
            "raw-oversize-source": 0,
            "oversize_files": [],
            "oversize_sources": [],
        }

    def test_both_thresholds_unset_skips_walk_entirely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh install (no config) pays no filesystem-walk cost."""
        raw_root = tmp_path / "raw"
        raw_root.mkdir()

        def _boom(*args, **kwargs):
            raise AssertionError("os.walk must not be called with both thresholds unset")

        monkeypatch.setattr(os, "walk", _boom)
        summary = check_raw_retention(raw_root, config=None)
        assert summary["raw-oversize-file"] == 0
        assert summary["raw-oversize-source"] == 0

    def test_nonexistent_raw_root_reports_nothing(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "does-not-exist"
        cfg = {
            "librarian": {
                "raw_retention": {"max_file_bytes": 100, "max_source_bytes": 100}
            }
        }
        summary = check_raw_retention(raw_root, config=cfg)
        assert summary["raw-oversize-file"] == 0
        assert summary["raw-oversize-source"] == 0

    def test_per_file_dimension_flags_one_large_file(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        _write(raw_root / "sessions" / "small.md", 100)
        _write(raw_root / "sessions" / "huge.jsonl", 5000)
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 1000}}}

        summary = check_raw_retention(raw_root, config=cfg)

        assert summary["raw-oversize-file"] == 1
        assert summary["oversize_files"] == [
            {"path": "sessions/huge.jsonl", "bytes": 5000}
        ]
        # per-source dimension was never armed -- must stay at zero.
        assert summary["raw-oversize-source"] == 0
        assert summary["oversize_sources"] == []

    def test_per_file_threshold_is_reaches_or_exceeds(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        _write(raw_root / "sessions" / "exact.md", 1000)
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 1000}}}

        summary = check_raw_retention(raw_root, config=cfg)

        assert summary["raw-oversize-file"] == 1

    def test_many_small_files_aggregate_past_source_limit(self, tmp_path: Path) -> None:
        """The motivating case (issue athenaeum#1269): 2,247 ~420 KB files summing
        to 943 MB -- no single file trips any sane per-file threshold, only
        the aggregate. Modelled here at a tiny scale: 50 files x 10 KB
        (500 KB total) against a 100 KB per-file limit (none trip it) and a
        200 KB source limit (the aggregate does).
        """
        raw_root = tmp_path / "raw"
        per_file = 10_000  # 10 KB -- comfortably under the 100 KB per-file cap
        file_count = 50
        for i in range(file_count):
            _write(raw_root / "mural" / f"record-{i:04d}.json", per_file)
        cfg = {
            "librarian": {
                "raw_retention": {
                    "max_file_bytes": 100_000,  # 100 KB -- no single file crosses this
                    "max_source_bytes": 200_000,  # 200 KB -- the 500 KB aggregate does
                }
            }
        }

        summary = check_raw_retention(raw_root, config=cfg)

        # A per-file-only implementation would report NOTHING here -- this
        # is the exact failure mode the issue was filed to prevent.
        assert summary["raw-oversize-file"] == 0
        assert summary["oversize_files"] == []
        assert summary["raw-oversize-source"] == 1
        assert summary["oversize_sources"] == [
            {"source": "mural", "bytes": per_file * file_count}
        ]

    def test_json_extension_is_counted(self, tmp_path: Path) -> None:
        """The real corpus is `.json`, which `discover_raw_files` never globs
        at all (only `.md`/`.jsonl`) -- a check built on top of that
        discovery path would silently never see it. Proves this check does
        not delegate to `discover_raw_files`.
        """
        raw_root = tmp_path / "raw"
        _write(raw_root / "mural" / "export.json", 5000)
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 1000}}}

        summary = check_raw_retention(raw_root, config=cfg)

        assert summary["raw-oversize-file"] == 1
        assert summary["oversize_files"][0]["path"] == "mural/export.json"

    def test_both_dimensions_can_fire_independently_in_one_run(
        self, tmp_path: Path
    ) -> None:
        raw_root = tmp_path / "raw"
        # source A: one individually-oversized file, small aggregate.
        _write(raw_root / "big-single" / "one.md", 5000)
        # source B: many small files, oversized aggregate only.
        for i in range(10):
            _write(raw_root / "many-small" / f"f{i}.json", 1000)
        cfg = {
            "librarian": {
                "raw_retention": {
                    "max_file_bytes": 2000,
                    "max_source_bytes": 8000,
                }
            }
        }

        summary = check_raw_retention(raw_root, config=cfg)

        assert summary["raw-oversize-file"] == 1
        assert summary["oversize_files"] == [{"path": "big-single/one.md", "bytes": 5000}]
        assert summary["raw-oversize-source"] == 1
        assert summary["oversize_sources"] == [
            {"source": "many-small", "bytes": 10000}
        ]

    def test_never_blocks_moves_or_exempts(self, tmp_path: Path) -> None:
        """AC (issue athenaeum#1269): crossing a threshold NEVER blocks intake,
        moves a file, or writes an exempt row. Explicit negative assertion:
        the file stays exactly where it was, with the same bytes, and no
        compiled-exempt manifest is created as a side effect.
        """
        raw_root = tmp_path / "raw"
        knowledge_root = raw_root.parent
        fpath = raw_root / "mural" / "big.json"
        _write(fpath, 5000)
        original_bytes = fpath.read_bytes()
        cfg = {
            "librarian": {
                "raw_retention": {"max_file_bytes": 100, "max_source_bytes": 100}
            }
        }

        summary = check_raw_retention(raw_root, config=cfg)

        assert summary["raw-oversize-file"] == 1
        assert summary["raw-oversize-source"] == 1
        # Still exactly where it was, byte-for-byte.
        assert fpath.exists()
        assert fpath.read_bytes() == original_bytes
        # No exempt-manifest / preserved-area side effect of any kind.
        assert not (knowledge_root / "_compiled_exempt.jsonl").exists()
        assert list((raw_root / "mural").iterdir()) == [fpath]

    def test_subdirectories_within_a_source_are_included(self, tmp_path: Path) -> None:
        """Aggregation is over the WHOLE `raw/<source>/` tree, not just its
        top-level files -- a nested subdirectory's bytes count too.
        """
        raw_root = tmp_path / "raw"
        _write(raw_root / "hestia" / "top.md", 3000)
        _write(raw_root / "hestia" / "lane-1" / "nested.md", 3000)
        cfg = {"librarian": {"raw_retention": {"max_source_bytes": 5000}}}

        summary = check_raw_retention(raw_root, config=cfg)

        assert summary["raw-oversize-source"] == 1
        assert summary["oversize_sources"] == [{"source": "hestia", "bytes": 6000}]

    def test_stray_file_directly_under_raw_root_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """Only directories directly under `raw_root` are treated as source
        trees -- a stray file at that level (never valid layout, but must
        not crash or be mis-treated as an empty source) is skipped.
        """
        raw_root = tmp_path / "raw"
        raw_root.mkdir()
        (raw_root / "stray.txt").write_bytes(b"x" * 10)
        _write(raw_root / "mural" / "a.json", 5000)
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 100}}}

        summary = check_raw_retention(raw_root, config=cfg)

        assert summary["raw-oversize-file"] == 1
        assert summary["oversize_files"] == [{"path": "mural/a.json", "bytes": 5000}]

    def test_vanishing_file_mid_walk_is_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file removed between the directory read and the stat call (a
        race with a concurrent compile/retire pass) is skipped rather than
        raising -- mirrors `discover_raw_backlog_bytes`'s own tolerance.
        """
        raw_root = tmp_path / "raw"
        _write(raw_root / "mural" / "vanishes.json", 100)
        _write(raw_root / "mural" / "stays.json", 5000)
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 1000}}}

        real_stat = Path.stat

        def _flaky_stat(self: Path, *args, **kwargs):
            if self.name == "vanishes.json":
                raise OSError("vanished mid-walk")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _flaky_stat)

        summary = check_raw_retention(raw_root, config=cfg)

        assert summary["raw-oversize-file"] == 1
        assert summary["oversize_files"] == [
            {"path": "mural/stays.json", "bytes": 5000}
        ]

    def test_wrong_root_raises(self, tmp_path: Path) -> None:
        """Mirrors the adjacent-wrong-root guard the other `discover_*`
        helpers in this module share (issue athenaeum#1134) -- passing
        `knowledge_root` where `raw_root` is expected raises rather than
        silently reporting an empty/wrong result.
        """
        knowledge_root = tmp_path / "knowledge"
        (knowledge_root / "wiki").mkdir(parents=True)
        (knowledge_root / "raw").mkdir(parents=True)
        cfg = {"librarian": {"raw_retention": {"max_file_bytes": 1}}}

        with pytest.raises(ValueError, match="raw_root"):
            check_raw_retention(knowledge_root, config=cfg)
