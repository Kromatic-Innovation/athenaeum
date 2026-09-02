# SPDX-License-Identifier: Apache-2.0
"""The intake window is filled fairly and only from WORKABLE files (athenaeum#1322).

Two independent defects let Lane A prose throughput fall to exactly zero on the
reference deployment for six hours while Lane C field corrections applied within
the hour. Both are window-COMPOSITION bugs — nothing about them is visible in a
pending count, which is why the run read as ``files=0 ... reason=completed``.

1. **The caller-scoped pin bypassed round-robin.** athenaeum#900 pins the
   caller's own new files ahead of the backlog; the pin was then HEAD-TRUNCATED
   to ``max_files``. ``discover_raw_files`` groups by source name, so the head
   of that list is one alphabetically-early source — precisely the starvation
   athenaeum#1291 fixed, reached through a different door. It bites because
   ``compile_changed`` derives the caller scope from a raw-tree hash snapshot
   and a never-compiled file stays "new" forever: observed in production as
   ``Caller-scoped compile: 5969 of 5970 raw file(s)``.

2. **Unworkable files consumed slots.** athenaeum#663 (stuck) and
   athenaeum#1185 (in-backoff) skip a file INSIDE the per-file loop — after it
   has already won a slot. Fifty permanently-stuck files therefore filled the
   entire fifty-slot window on every run, forever.

Offline by construction: ``process_one`` is stubbed, so no test here makes a
network call.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.librarian import STUCK_MANIFEST_NAME, run

# ---------------------------------------------------------------------------
# Harness (mirrors tests/test_entity_changed_paths.py's, with multiple sources)
# ---------------------------------------------------------------------------


def _seed(tmp_path: Path, sources: dict[str, int]) -> Path:
    """Knowledge root with ``{source: n_files}`` of raw intake, written post-commit."""
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "wiki").mkdir()
    (root / "raw").mkdir()
    for source in sources:
        (root / "raw" / source).mkdir(parents=True)
        (root / "raw" / source / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    for source, count in sources.items():
        for i in range(count):
            (
                root
                / "raw"
                / source
                / f"2024041{i // 10}T12{i % 10:02d}000Z-aabbcc{i:02d}.md"
            ).write_text(f"Note {i} from {source} about Acme Corp.\n", encoding="utf-8")
    return root


def _recording_process_one(seen: list[str], wiki_root: Path):
    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        seen.append(raw.ref)
        page = wiki_root / f"entity-{len(seen)}.md"
        page.write_text(f"# Entity\nfrom {raw.ref}\n", encoding="utf-8")
        return SimpleNamespace(
            created=[page.name], updated=[], escalated=[], skipped=[]
        )

    return fake_process_one


def _all_raw_paths(root: Path) -> set[Path]:
    return {p.resolve() for p in (root / "raw").rglob("*.md")}


def _sources_of(refs: list[str]) -> set[str]:
    return {ref.split("/")[0] for ref in refs}


# ---------------------------------------------------------------------------
# Defect 1: the caller-scoped pin must not bypass round-robin
# ---------------------------------------------------------------------------


class TestCallerScopedPinIsStillFairlyScheduled:
    def test_a_caller_naming_the_whole_backlog_still_reaches_every_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE regression. `alpha` sorts first and has more files than the whole
        # window; the caller names EVERY file (what `compile_changed` does on a
        # backlogged corpus). Before athenaeum#1322 the window was
        # `raw_files[:max_files]` — all `alpha` — and `omega` waited forever.
        root = _seed(tmp_path, {"alpha": 20, "omega": 20})
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
            max_files=6,
            max_api_calls=100,
            entity_changed_paths=_all_raw_paths(root),
        )

        assert rc == 0
        assert len(seen) == 6
        assert _sources_of(seen) == {"alpha", "omega"}

    def test_every_pending_source_gets_at_least_one_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # athenaeum#1291 AC1, restated for the pinned partition: while the
        # window is at least as wide as the source count, no source is starved
        # — even when the caller has claimed every file.
        sources = {"alpha": 30, "mid": 30, "omega": 30, "zeta": 30}
        root = _seed(tmp_path, sources)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _recording_process_one(seen, root / "wiki"),
        )

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=8,
            max_api_calls=100,
            entity_changed_paths=_all_raw_paths(root),
        )

        assert _sources_of(seen) == set(sources)

    def test_a_genuine_session_scope_still_compiles_first_and_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # athenaeum#900's own acceptance criterion, unchanged. A caller naming
        # FEWER files than the window hits `round_robin_by_source`'s
        # `len(files) <= limit` short-circuit, which returns its input verbatim
        # — so the pin's order is byte-identical to pre-athenaeum#1322.
        root = _seed(tmp_path, {"alpha": 10})
        mine = root / "raw" / "alpha" / "20260815T120000Z-deadbeef.md"
        mine.write_text("Met Alice Zhang about the new thing.\n", encoding="utf-8")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _recording_process_one(seen, root / "wiki"),
        )

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=3,
            max_api_calls=100,
            entity_changed_paths={mine.resolve()},
        )

        assert seen[0] == f"alpha/{mine.name}"


# ---------------------------------------------------------------------------
# Defect 2: a stuck file must not consume a slot
# ---------------------------------------------------------------------------


def _write_stuck_ledger(root: Path, refs: list[str], *, failures: int = 3) -> None:
    """Mark *refs* stuck, keyed on their real content hash (athenaeum#663 shape)."""
    from athenaeum.librarian import _stuck_content_hash
    from athenaeum.models import RawFile

    entries: dict[str, dict[str, object]] = {}
    for ref in refs:
        path = root / "raw" / ref
        raw = RawFile(
            path=path,
            source=ref.split("/")[0],
            timestamp="20240410T120000Z",
            uuid8="aabbccdd",
        )
        entries[ref] = {
            "failures": failures,
            "hash": _stuck_content_hash(raw),
            "escalated": True,
            "last_error": "BadRequestError",
            "last_failed": "2026-09-02T00:00:00Z",
            "first_failed": "2026-09-01T00:00:00Z",
        }
    (root / "wiki" / STUCK_MANIFEST_NAME).write_text(
        json.dumps({"files": entries, "updated": "2026-09-02T00:00:00Z"}),
        encoding="utf-8",
    )


class TestStuckFilesDoNotConsumeWindowSlots:
    def test_a_stuck_head_does_not_freeze_a_sources_own_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE production shape, scaled down: the OLDEST four files of a source
        # are permanently stuck, and the window is exactly four wide. Discovery
        # is oldest-first, so before athenaeum#1322 the window was those four
        # every run — `files=0 calls=0 stuck=4 reason=completed`, forever,
        # while four workable files sat behind them. This is why
        # `mural-board-summary` drained 2,126 -> 1,785 and then stopped dead.
        root = _seed(tmp_path, {"alpha": 8})
        alpha_refs = sorted(
            f"alpha/{p.name}" for p in (root / "raw" / "alpha").glob("*.md")
        )
        _write_stuck_ledger(root, alpha_refs[:4])
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _recording_process_one(seen, root / "wiki")
        )

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=4,
            max_api_calls=100,
        )

        assert seen == alpha_refs[4:], "a stuck head froze the whole source"

    def test_a_stuck_file_is_never_handed_to_process_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # athenaeum#663's guarantee is preserved by the earlier hold-out: the
        # file is still never processed, still on disk, still surfaced.
        root = _seed(tmp_path, {"alpha": 2, "omega": 2})
        alpha_refs = sorted(
            f"alpha/{p.name}" for p in (root / "raw" / "alpha").glob("*.md")
        )
        _write_stuck_ledger(root, alpha_refs)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _recording_process_one(seen, root / "wiki"),
        )

        run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_files=10,
            max_api_calls=100,
        )

        assert not (set(seen) & set(alpha_refs))
        for ref in alpha_refs:
            assert (root / "raw" / ref).exists(), "a stuck file must stay on disk"


# ---------------------------------------------------------------------------
# The pass reports how its window was filled and why it stopped
# ---------------------------------------------------------------------------


class TestWindowCompositionIsReported:
    def _entity_segment(
        self, root: Path, caplog: pytest.LogCaptureFixture
    ) -> dict[str, str]:
        from athenaeum.run_summary_log import parse_run_summary_text

        records = parse_run_summary_text(caplog.text)
        assert records, "no librarian-run-summary line was emitted"
        return records[-1].phases["entity"]

    def test_a_pass_reports_considered_and_window(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # AC1: "considered" and "window" make a CAP distinguishable from a
        # FAILURE without reading the raw log or counting files on disk.
        root = _seed(tmp_path, {"alpha": 10, "omega": 10})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _recording_process_one(seen, root / "wiki"),
        )

        with caplog.at_level("INFO", logger="athenaeum.librarian"):
            run(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                max_files=6,
                max_api_calls=100,
            )

        entity = self._entity_segment(root, caplog)
        assert entity["considered"] == "20"
        assert entity["window"] == "6"

    def test_a_pass_that_skipped_every_slot_does_not_read_as_completed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # AC1's "why it stopped". `reason=completed` on a zero-yield pass is
        # what let a six-hour stall look healthy in the durable ledger.
        root = _seed(tmp_path, {"alpha": 3})
        alpha_refs = sorted(
            f"alpha/{p.name}" for p in (root / "raw" / "alpha").glob("*.md")
        )
        _write_stuck_ledger(root, alpha_refs)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        seen: list[str] = []
        monkeypatch.setattr(
            "athenaeum.librarian.process_one",
            _recording_process_one(seen, root / "wiki"),
        )

        with caplog.at_level("INFO", logger="athenaeum.librarian"):
            run(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                max_files=10,
                max_api_calls=100,
            )

        entity = self._entity_segment(root, caplog)
        assert entity["reason"] == "all-slots-skipped"
        assert entity["held_stuck"] == "3"
