# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum storage prune-dispositions`` (issue athenaeum#1274 AC3/AC4).

Covers the thin CLI wrapper (:mod:`athenaeum._cmd_storage`) over the pure
transform (:func:`athenaeum.rules.prune_shape_rule_dispositions_to_positive`,
tested directly in ``tests/test_rules_dispositions.py``'s
``TestPruneShapeRuleDispositionsToPositive``): dry-run-by-default reporting,
``--apply`` writing, the run-lock guard (issue athenaeum#309) on the mutating
path, and a missing ledger.
"""

from __future__ import annotations

import json
from pathlib import Path

from athenaeum.cli import main
from athenaeum.rules import append_shape_rule_disposition_row, default_shape_rule_dispositions_path
from athenaeum.runlock import RunLock


def _seed(wiki_root: Path, *, source_ref: str, disposition: str) -> None:
    append_shape_rule_disposition_row(
        wiki_root,
        {
            "schema_version": 1,
            "at": "2026-08-01T00:00:00Z",
            "source": "s",
            "source_ref": source_ref,
            "key_fingerprint": "fp",
            "tier": None,
            "rule_id": None,
            "disposition": disposition,
        },
    )


def _seed_mixed(root: Path) -> Path:
    wiki_root = root / "wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        _seed(wiki_root, source_ref=f"s/nomatch-{i}", disposition="no-match")
    _seed(wiki_root, source_ref="s/p1", disposition="preserve")
    _seed(wiki_root, source_ref="s/op1", disposition="observed-preserve")
    return wiki_root


class TestPruneDispositionsCLI:
    def test_dry_run_reports_and_does_not_mutate(self, tmp_path: Path, capsys) -> None:
        wiki_root = _seed_mixed(tmp_path)
        path = default_shape_rule_dispositions_path(wiki_root)
        before = path.read_bytes()

        rc = main(["storage", "prune-dispositions", "--path", str(tmp_path)])

        assert rc == 0
        assert path.read_bytes() == before
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "total records:     7" in out
        assert "no-match:          5" in out
        assert "positive records:  2" in out
        assert "rows dropped:      5" in out
        assert "would drop 5 no-match row(s)" in out

    def test_apply_writes_and_drops_no_match_rows(self, tmp_path: Path, capsys) -> None:
        wiki_root = _seed_mixed(tmp_path)
        path = default_shape_rule_dispositions_path(wiki_root)

        rc = main(["storage", "prune-dispositions", "--path", str(tmp_path), "--apply"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "APPLY" in out
        assert "pruned 5 no-match row(s)" in out
        remaining = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
        assert len(remaining) == 2
        assert {row["disposition"] for row in remaining} == {"preserve", "observed-preserve"}

    def test_apply_is_required_to_mutate(self, tmp_path: Path) -> None:
        wiki_root = _seed_mixed(tmp_path)
        path = default_shape_rule_dispositions_path(wiki_root)
        before = path.read_bytes()

        rc = main(["storage", "prune-dispositions", "--path", str(tmp_path)])

        assert rc == 0
        assert path.read_bytes() == before

    def test_missing_ledger_is_a_clean_no_op(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)

        rc = main(["storage", "prune-dispositions", "--path", str(tmp_path), "--apply"])

        assert rc == 0
        assert "nothing to prune" in capsys.readouterr().out

    def test_apply_refuses_when_run_lock_is_held(self, tmp_path: Path, capsys) -> None:
        wiki_root = _seed_mixed(tmp_path)
        path = default_shape_rule_dispositions_path(wiki_root)
        before = path.read_bytes()

        lock = RunLock(tmp_path, wait=0)
        lock.acquire()
        try:
            rc = main(["storage", "prune-dispositions", "--path", str(tmp_path), "--apply"])
        finally:
            lock.release()

        assert rc == 75  # EXIT_LOCK_HELD
        assert path.read_bytes() == before  # refused before any write
        assert "holds the lock" in capsys.readouterr().err

    def test_dry_run_does_not_require_the_lock(self, tmp_path: Path) -> None:
        # A dry-run is read-only, so it must not be blocked by a concurrent
        # holder the way --apply is.
        _seed_mixed(tmp_path)
        lock = RunLock(tmp_path, wait=0)
        lock.acquire()
        try:
            rc = main(["storage", "prune-dispositions", "--path", str(tmp_path)])
        finally:
            lock.release()
        assert rc == 0
