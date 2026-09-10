"""Joined-path coverage for extra intake roots (issue athenaeum#1485).

athenaeum#1457 reported recall coverage of ``raw/auto-memory`` at zero rows,
with an unset config key as the suspected cause. It closed on
**observation**, not on a test: 13 test files exercise ``extra_roots`` and
every one of the 76 call sites passes ``extra_roots=[...]`` by injection;
``resolve_extra_intake_roots`` is tested in isolation in ``test_config.py``.
The two were never joined -- no test went config -> ``resolve_extra_intake_roots``
-> index build -> assert the built index contains rows from that root.

These tests build the index through ``athenaeum reindex`` -- the same CLI
entry point a real deployment runs (``cmd_rebuild_index`` in
``_cmd_index.py``) -- starting from a written ``athenaeum.yaml``, and inspect
the resulting on-disk index for rows attributable to the configured root via
the ``<root_name>/<relpath>`` filename convention ``_iter_extra_root_entries``
(search.py) already encodes for every extra-root hit.

Out of scope (per the issue): changing ``resolve_extra_intake_roots``'s
behaviour, and the existing injection-based tests (``test_search.py``'s
``TestFTS5ExtraRoots`` / ``TestVectorExtraRoots``, which are correct for what
they cover). This module only adds the joined path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.config import load_config, resolve_extra_intake_roots
from athenaeum.search import _DB_NAME


def _seed_wiki(knowledge_root: Path) -> None:
    """A minimal wiki/ with exactly one page, so wiki-only page counts are
    a known, fixed baseline (1) across every test in this module."""
    wiki = knowledge_root / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "a.md").write_text(
        "---\nname: A\ntags: [x]\ndescription: d\n---\n\nAlpha body.\n"
    )


def _write_config(knowledge_root: Path, roots: list[str]) -> None:
    """Write an ``athenaeum.yaml`` DECLARING ``recall.extra_intake_roots`` --
    the config-file shape a real deployment edits, never an
    ``extra_roots=`` call-site injection."""
    if roots:
        lines = ["recall:", "  extra_intake_roots:"]
        lines += [f"    - {root}" for root in roots]
    else:
        lines = ["recall:", "  extra_intake_roots: []"]
    (knowledge_root / "athenaeum.yaml").write_text("\n".join(lines) + "\n")


def _run_reindex(knowledge_root: Path, cache: Path) -> int:
    return main(
        [
            "reindex",
            "--path",
            str(knowledge_root),
            "--cache-dir",
            str(cache),
            "--backend",
            "fts5",
        ]
    )


def _rows_for_root(cache_dir: Path, root_name: str) -> int:
    """Count indexed rows attributable to *root_name*, via the same
    ``<root_name>/<relpath>`` filename prefix a recall consumer already
    relies on to separate wiki hits from extra-root hits (search.py's
    ``_iter_extra_root_entries``). Production behaviour, not a
    test-invented signal.
    """
    db_path = cache_dir / _DB_NAME
    if not db_path.is_file():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM wiki WHERE filename LIKE ?",
            (f"{root_name}/%",),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _total_pages(cache_dir: Path) -> int:
    db_path = cache_dir / _DB_NAME
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT COUNT(*) FROM wiki").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


class TestConfigJoinedToIndexBuild:
    """AC1: a config declaring an extra intake root, run through the real
    index-build path, produces a nonzero row count attributable to that
    root."""

    def test_configured_root_contributes_indexed_rows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        _seed_wiki(knowledge_root)
        intake = knowledge_root / "raw" / "custom-intake"
        intake.mkdir(parents=True)
        (intake / "note-one.md").write_text(
            "---\nname: Note One\ntags: [x]\n---\n\nFirst note body.\n"
        )
        (intake / "note-two.md").write_text(
            "---\nname: Note Two\ntags: [x]\n---\n\nSecond note body.\n"
        )
        _write_config(knowledge_root, ["raw/custom-intake"])
        cache = tmp_path / "cache"

        rc = _run_reindex(knowledge_root, cache)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["pages"] == 3  # 1 wiki + 2 custom-intake

        assert _rows_for_root(cache, "custom-intake") == 2


class TestEmptyRootVsNoRootsConfigured:
    """AC2: a configured root that resolves to an existing directory with no
    matching files must be distinguishable from a silent pass. AC3: that
    case must, in turn, be distinguishable from no roots being configured at
    all -- #1457 could tell neither apart.

    Neither ``resolve_extra_intake_roots`` nor the injection-based tests are
    touched (out of scope per the issue); these tests only inspect the
    artifact the real build already produces.
    """

    def test_existing_empty_root_reports_a_real_zero_not_a_silent_pass(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        _seed_wiki(knowledge_root)
        empty_root = knowledge_root / "raw" / "empty-intake"
        empty_root.mkdir(parents=True)
        # A real, EXISTING directory -- the "resolves but has nothing to
        # index" shape, distinct from the "missing/typo'd path" shape
        # test_config.py::TestResolveExtraIntakeRoots::test_drops_missing_roots
        # already covers.
        (empty_root / "notes.txt").write_text("not markdown\n")
        _write_config(knowledge_root, ["raw/empty-intake"])
        cache = tmp_path / "cache"

        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            rc = _run_reindex(knowledge_root, cache)
        assert rc == 0

        # The root resolves (it exists) -- no "not found" warning, unlike a
        # missing root.
        assert not [
            r for r in caplog.records if "extra_intake_root not found" in r.getMessage()
        ]

        # A REAL, computable, attributable zero -- not "we can't tell".
        assert _rows_for_root(cache, "empty-intake") == 0

        cfg = load_config(knowledge_root)
        resolved = resolve_extra_intake_roots(knowledge_root, cfg)
        assert len(resolved) == 1  # configured AND resolved -- just empty

    def test_no_roots_configured_is_a_different_shape_than_empty_root(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        _seed_wiki(knowledge_root)
        _write_config(knowledge_root, [])
        cache = tmp_path / "cache"

        rc = _run_reindex(knowledge_root, cache)
        assert rc == 0

        cfg = load_config(knowledge_root)
        resolved = resolve_extra_intake_roots(knowledge_root, cfg)
        # ZERO configured roots -- not one configured root with zero rows.
        assert resolved == []

        # Gross totals alone cannot tell this apart from the empty-root
        # case above (both are wiki-only) -- that indistinguishability is
        # exactly athenaeum#1457's blind spot. Only the resolved-roots list
        # (empty here, one entry for the empty-but-present-root case)
        # separates "nothing configured" from "configured but empty".
        assert _total_pages(cache) == 1  # wiki-only, same total as the empty-root case
