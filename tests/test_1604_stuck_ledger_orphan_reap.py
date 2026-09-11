# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1604 (AC4 finding from athenaeum#1597): the persistent stuck-file
ledger (``_stuck_files.json``) never drops an entry whose raw file has been
deleted from disk. On the reference deployment, 47 of the 50 ``BadRequestError``
entries name a raw file that is already gone under ``~/knowledge/raw/`` —
mostly ``drive``-sourced imports that cycle through and are never
re-materialized. Such an entry can never be retried (there is nothing left to
retry) and never resolves on its own; left in place it is pure ledger bloat
that inflates every entry-count figure an operator reads off the ledger
relative to the CURRENT backlog, and the file grows without bound.

This file pins the fix: :func:`athenaeum.stuck_ledger.reap_orphaned_entries`,
called from :func:`athenaeum.librarian._hold_out_unworkable_raw` (the one
place that already loads the ledger unconditionally on every non-dry-run
call — including the all-stuck case where the entity loop's OWN
``_load_stuck_ledger`` / ``_write_stuck_ledger`` pair never runs at all,
because ``ctx.raw_files`` empties out before reaching that branch).

Scope: read-only structural cleanup of the ledger. Does NOT touch
``PersonNeverLLMRewriteError``, ``type: person`` handling, or
``src/athenaeum/tiers.py`` — that ground belongs to the concurrent
``dijkstra/1597-remove-person-rewrite-guard`` lane.

See the PR body for this file's RED (pre-fix) and GREEN (post-fix) output.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.librarian import STUCK_MANIFEST_NAME, _stuck_content_hash, run
from athenaeum.models import RawFile
from athenaeum.stuck_ledger import load_stuck_ledger, reap_orphaned_entries


def _seed(tmp_path: Path, *, workable_names: list[str]) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "wiki").mkdir()
    raw = root / "raw" / "relationship-stub"
    raw.mkdir(parents=True)
    for name in workable_names:
        (raw / name).write_text(f"Note about {name}.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    return root


def _ledger_entry_for(root: Path, ref: str, *, error: str, failures: int = 3) -> dict:
    """Build a real, hash-keyed ledger entry for a file that EXISTS on disk."""
    path = root / "raw" / ref
    raw = RawFile(path=path, source=ref.split("/")[0], timestamp="x", uuid8="x")
    return {
        "failures": failures,
        "hash": _stuck_content_hash(raw),
        "escalated": True,
        "last_error": error,
        "last_failed": "2026-09-02T00:00:00Z",
        "first_failed": "2026-08-26T00:00:00Z",
    }


def _recording_process_one(seen: list[str], wiki_root: Path):
    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        seen.append(raw.ref)
        page = wiki_root / f"entity-{len(seen)}.md"
        page.write_text(f"# Entity\nfrom {raw.ref}\n", encoding="utf-8")
        return SimpleNamespace(created=[page.name], updated=[], escalated=[], skipped=[])

    return fake_process_one


class TestReapOrphanedEntriesUnit:
    """Direct unit coverage of the leaf function."""

    def test_drops_entry_whose_file_is_gone(self, tmp_path: Path) -> None:
        raw_root = tmp_path / "raw"
        (raw_root / "drive").mkdir(parents=True)
        (raw_root / "drive" / "still-here.md").write_text("x", encoding="utf-8")
        ledger = {
            "drive/gone.md": {"failures": 3, "escalated": True, "last_error": "BadRequestError"},
            "drive/still-here.md": {
                "failures": 3,
                "escalated": True,
                "last_error": "PersonNeverLLMRewriteError",
            },
        }

        reaped, n_dropped = reap_orphaned_entries(ledger, raw_root)

        assert n_dropped == 1
        assert "drive/gone.md" not in reaped
        assert "drive/still-here.md" in reaped
        assert reaped["drive/still-here.md"] == ledger["drive/still-here.md"]

    def test_keeps_a_ref_that_does_not_parse_cleanly(self, tmp_path: Path) -> None:
        # Fail CLOSED on anything ambiguous -- never guess-drop.
        raw_root = tmp_path / "raw"
        raw_root.mkdir()
        ledger = {
            "no-slash": {"failures": 3, "escalated": True},
            "../escape/x.md": {"failures": 3, "escalated": True},
        }

        reaped, n_dropped = reap_orphaned_entries(ledger, raw_root)

        assert n_dropped == 0
        assert reaped == ledger


class TestOrphanReapedOnLibrarianRun:
    """Integration: a real ``run()`` reaps orphans via ``_hold_out_unworkable_raw``.

    THE failing-then-passing behavior change. Before this fix, an orphaned
    ledger entry (raw file already deleted from disk) survives every run
    forever -- nothing in the librarian's ledger read/write cycle ever
    checks whether the named file still exists.
    """

    def test_orphan_dropped_live_entry_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _seed(tmp_path, workable_names=["held.md", "workable.md"])

        ledger_path = root / "wiki" / STUCK_MANIFEST_NAME
        live_entry = _ledger_entry_for(root, "relationship-stub/held.md", error="BadRequestError")
        orphan_ref = "relationship-stub/deleted-upstream.md"
        ledger_path.write_text(
            json.dumps(
                {
                    "updated": "2026-09-10T00:00:00Z",
                    "files": {
                        "relationship-stub/held.md": live_entry,
                        orphan_ref: {
                            "failures": 3,
                            "hash": "deadbeefcafef00d",
                            "escalated": True,
                            "last_error": "BadRequestError",
                            "last_failed": "2026-08-26T00:00:00Z",
                            "first_failed": "2026-08-26T00:00:00Z",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        assert not (root / "raw" / orphan_ref).exists()  # never existed

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _recording_process_one(seen, root / "wiki"),
        )

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=10,
            max_api_calls=100,
        )
        assert rc == 0

        # Only the workable file was processed -- the live stuck entry still
        # held its file out exactly as before this fix.
        assert seen == ["relationship-stub/workable.md"]

        reloaded = load_stuck_ledger(root / "wiki")
        assert orphan_ref not in reloaded, "orphaned entry must not survive a run"
        assert "relationship-stub/held.md" in reloaded, (
            "a live, still-on-disk stuck entry must be preserved, not just "
            "have the ledger cleared wholesale"
        )
        assert reloaded["relationship-stub/held.md"]["failures"] == 3
        assert reloaded["relationship-stub/held.md"]["escalated"] is True
        assert reloaded["relationship-stub/held.md"]["last_error"] == "BadRequestError"

    def test_orphan_reaped_even_when_the_entire_backlog_is_stuck(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The live-corpus shape (athenaeum#1597): ALL discoverable raw files
        # are held out, so `ctx.raw_files` empties out and the entity loop's
        # OWN stuck-ledger load/write pair (inside the `if ctx.raw_files:
        # ... else:` split) never runs this call. The reap must still fire --
        # it lives in `_hold_out_unworkable_raw`, which always runs.
        root = _seed(tmp_path, workable_names=["held.md"])

        ledger_path = root / "wiki" / STUCK_MANIFEST_NAME
        live_entry = _ledger_entry_for(
            root, "relationship-stub/held.md", error="PersonNeverLLMRewriteError"
        )
        orphan_ref = "drive/8cd64194-gone.md"
        ledger_path.write_text(
            json.dumps(
                {
                    "updated": "2026-09-10T00:00:00Z",
                    "files": {
                        "relationship-stub/held.md": live_entry,
                        orphan_ref: {
                            "failures": 3,
                            "hash": "deadbeefcafef00d",
                            "escalated": True,
                            "last_error": "BadRequestError",
                            "last_failed": "2026-08-26T00:00:00Z",
                            "first_failed": "2026-08-26T00:00:00Z",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _recording_process_one(seen, root / "wiki"),
        )

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=10,
            max_api_calls=100,
        )
        assert rc == 0
        assert seen == []  # the only discoverable file was (correctly) held out

        reloaded = load_stuck_ledger(root / "wiki")
        assert orphan_ref not in reloaded
        assert "relationship-stub/held.md" in reloaded
