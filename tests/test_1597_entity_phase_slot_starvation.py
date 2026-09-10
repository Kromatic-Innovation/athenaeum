# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1597: the entity phase has compiled nothing for five days because a
by-design person-page refusal (``PersonNeverLLMRewriteError``, athenaeum#1183 AC4)
sits in the stuck ledger unnoticed, and the resulting `all-slots-skipped`
condition is invisible on any surface the operator reads.

Scope of THIS file (AC1 and AC5 are out — see the PR body):

* **AC2 IS ALREADY SATISFIED ON ``develop``, BY ATHENAEUM#1322 — this PR
  ships NO AC2 behavior change**, and
  ``TestAthenaeum1322RegressionPinNotAC2Evidence`` below is NOT
  failing-then-passing evidence of a fix: every test in that class PASSES
  against unmodified ``develop`` (verified directly against the
  pre-this-PR checkout). Do not read it as satisfying the standing
  "no librarian behaviour change ships without a failing test first" rule
  for AC2 — there is no AC2 behaviour change in this PR for that rule to
  apply to. It is committed anyway, clearly labelled, as a regression pin:
  the hold-out mechanism athenaeum#1322 added
  (:func:`athenaeum.librarian._hold_out_unworkable_raw`, defined at
  ``librarian.py:3853`` and applied at ``librarian.py:6519`` —
  immediately after ``total_intake = len(ctx.raw_files)`` is snapshotted,
  comment: "Hold those files out of the candidate set HERE so an
  unworkable file cannot consume a slot a workable one needed") already
  excludes escalated/held files from the intake window BEFORE it fills,
  through both doors into the window (backlog order and the athenaeum#900
  caller-scoped pin — the second door athenaeum#1322's own test file flags
  as the one place a similar-looking regression could hide). Live corpus
  per-run entity-phase figures confirm the mechanism is doing exactly this:
  ``{"considered": 308, "held_stuck": 308, "window": 0, "stuck": 308,
  "reason": "all-slots-skipped"}`` on essentially every recent run — ALL
  308 currently-discoverable raw files are in the stuck ledger, 305 of them
  permanently (``PersonNeverLLMRewriteError``), so the window is correctly
  empty. (The issue body's "14,485 considered" style daily figures are a
  SUM across ~47 runs/day of this same ~308-file count, not 14,485 distinct
  files — there is no growing set of starved NEW work; the same 308 files
  are re-counted, correctly held out, every run.) The scaled fixture here
  (30 ``PersonNeverLLMRewriteError`` + 5 ``BadRequestError`` stuck entries,
  the live ledger's ~86/14 split) demonstrates the ALREADY-SHIPPED
  behaviour at production-like proportions: new, workable intake is not
  starved by a dominant stuck set, through both doors into the window.

* **AC3** — THE failing-then-passing behavior change in this file.
  ``all-slots-skipped`` with ``held_stuck ≈ considered`` produced no alert on
  any surface the operator reads for five days; before this issue's fix,
  ``athenaeum status`` printed only ``Raw files pending: N`` with no
  indication that N is a dead stuck set, not a backlog awaiting capacity.
  ``TestStuckBacklogWarningOnStatus`` pins the new WARNING line
  (:func:`athenaeum.status.status` / :func:`athenaeum.status.format_status`,
  reading the shared :mod:`athenaeum.stuck_ledger` leaf) naming the dominant
  ``last_error``.

See the PR body for the recorded RED (pre-fix) run of
``TestStuckBacklogWarningOnStatus`` and the GREEN (post-fix) run.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from athenaeum.librarian import STUCK_MANIFEST_NAME, run
from athenaeum.status import format_status, status

FIXTURES = Path(__file__).parent / "fixtures" / "athenaeum_1597"


# ---------------------------------------------------------------------------
# Shared harness (mirrors tests/test_1322_lane_a_window_composition.py's)
# ---------------------------------------------------------------------------


def _seed(tmp_path: Path, sources: dict[str, int]) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "wiki").mkdir()
    (root / "raw").mkdir()
    for source in sources:
        (root / "raw" / source).mkdir(parents=True)
        (root / "raw" / source / ".gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
    for source, count in sources.items():
        for i in range(count):
            (
                root / "raw" / source / f"2024041{i // 10}T12{i % 10:02d}000Z-aabbcc{i:02d}.md"
            ).write_text(f"Note {i} from {source} about Acme Corp.\n", encoding="utf-8")
    return root


def _recording_process_one(seen: list[str], wiki_root: Path):
    def fake_process_one(raw, index, wiki_root_arg, client, *args, **kwargs):
        seen.append(raw.ref)
        page = wiki_root / f"entity-{len(seen)}.md"
        page.write_text(f"# Entity\nfrom {raw.ref}\n", encoding="utf-8")
        return SimpleNamespace(created=[page.name], updated=[], escalated=[], skipped=[])

    return fake_process_one


def _all_raw_paths(root: Path) -> set[Path]:
    return {p.resolve() for p in (root / "raw").rglob("*.md")}


def _write_scaled_stuck_ledger(
    root: Path, refs: list[str], *, error: str, escalated: bool = True, failures: int = 3
) -> None:
    """Mark *refs* stuck with *error*, keyed on their real content hash — the
    production shape (athenaeum#1597's live ledger), scaled down. Reuses the
    same 30 ``PersonNeverLLMRewriteError`` / 5 ``BadRequestError`` proportion
    as ``tests/fixtures/athenaeum_1597/stuck_files_shape.json``."""
    from athenaeum.librarian import _stuck_content_hash
    from athenaeum.models import RawFile

    ledger_path = root / "wiki" / STUCK_MANIFEST_NAME
    existing: dict[str, dict[str, object]] = {}
    if ledger_path.exists():
        existing = json.loads(ledger_path.read_text(encoding="utf-8"))["files"]
    for ref in refs:
        path = root / "raw" / ref
        raw = RawFile(
            path=path,
            source=ref.split("/")[0],
            timestamp="20240410T120000Z",
            uuid8="aabbccdd",
        )
        existing[ref] = {
            "failures": failures,
            "hash": _stuck_content_hash(raw),
            "escalated": escalated,
            "last_error": error,
            "last_failed": "2026-09-02T00:00:00Z",
            "first_failed": "2026-09-01T00:00:00Z",
        }
    ledger_path.write_text(
        json.dumps({"files": existing, "updated": "2026-09-10T16:46:14Z"}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# NOT AC2 EVIDENCE. Every test below PASSES against unmodified `develop` —
# confirmed directly before this PR was written. AC2 ("escalated files must
# not consume the entity phase's intake-window slots") was already shipped
# by athenaeum#1322 (`librarian.py:3853`, applied at `librarian.py:6519`).
# This class is committed anyway as an explicit, labelled regression pin for
# that already-shipped behaviour at athenaeum#1597's larger, production-like
# stuck/workable ratio — it is not a failing-then-passing test and must not
# be cited as satisfying the "no behaviour change without a failing test
# first" rule, because this PR makes no AC2 behaviour change.
# ---------------------------------------------------------------------------


class TestAthenaeum1322RegressionPinNotAC2Evidence:
    def test_a_dominant_stuck_set_does_not_starve_workable_backlog_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 30 person-error + 5 bad-request stuck (matching the live ledger's
        # ~86/14 split) sit ahead of 4 perfectly workable files in the SAME
        # source (discovery is oldest-first) — exactly the shape that starved
        # `mural-board-summary` in athenaeum#1322's own reference incident, at
        # athenaeum#1597's larger ratio.
        root = _seed(tmp_path, {"relationship-stub": 39})
        all_refs = sorted(
            f"relationship-stub/{p.name}" for p in (root / "raw" / "relationship-stub").glob("*.md")
        )
        stuck_refs, workable_refs = all_refs[:35], all_refs[35:]
        _write_scaled_stuck_ledger(root, stuck_refs[:30], error="PersonNeverLLMRewriteError")
        _write_scaled_stuck_ledger(root, stuck_refs[30:35], error="BadRequestError")
        assert len(workable_refs) == 4

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
        assert (
            seen == workable_refs
        ), "the dominant stuck set consumed slots the workable files needed"

    def test_a_dominant_stuck_set_does_not_starve_a_caller_scoped_compile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The SECOND door into the window (athenaeum#1322's own docstring):
        # the athenaeum#900 caller-scoped pin. `compile_changed` names the
        # WHOLE backlog on a session-scoped compile of a never-fully-drained
        # tree, so the pin itself must also never re-admit an escalated file
        # -- it operates on `ctx.raw_files`, which is already stuck-filtered
        # by the time the pin runs (librarian.py:6519 precedes the pin at
        # librarian.py's caller-scoped-prioritize call).
        root = _seed(tmp_path, {"relationship-stub": 39})
        all_refs = sorted(
            f"relationship-stub/{p.name}" for p in (root / "raw" / "relationship-stub").glob("*.md")
        )
        stuck_refs, workable_refs = all_refs[:35], all_refs[35:]
        _write_scaled_stuck_ledger(root, stuck_refs[:30], error="PersonNeverLLMRewriteError")
        _write_scaled_stuck_ledger(root, stuck_refs[30:35], error="BadRequestError")

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
            # The caller names the ENTIRE tree, mirroring
            # `compile_changed`'s "never-compiled stays new forever" shape.
            entity_changed_paths=_all_raw_paths(root),
        )

        assert seen == workable_refs

    def test_run_summary_matches_the_live_shape(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Sanity-checks this file's fixtures against the CODE's own reporting:
        # a fully-stuck backlog (no workable files at all) reads
        # `considered=N window=0 held_stuck=N reason=all-slots-skipped` --
        # BYTE-IDENTICAL in shape to
        # tests/fixtures/athenaeum_1597/run_summary_shape.jsonl (the scrubbed
        # copy of the real ~/.cache/athenaeum/run_summary.jsonl), just at a
        # smaller N.
        from athenaeum.run_summary_log import parse_run_summary_text

        root = _seed(tmp_path, {"relationship-stub": 30})
        all_refs = sorted(
            f"relationship-stub/{p.name}" for p in (root / "raw" / "relationship-stub").glob("*.md")
        )
        _write_scaled_stuck_ledger(root, all_refs, error="PersonNeverLLMRewriteError")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")

        with caplog.at_level("INFO", logger="athenaeum.librarian"):
            run(
                raw_root=root / "raw",
                wiki_root=root / "wiki",
                knowledge_root=root,
                max_files=50,
                max_api_calls=100,
            )

        records = parse_run_summary_text(caplog.text)
        entity = records[-1].phases["entity"]
        assert entity["reason"] == "all-slots-skipped"
        assert entity["considered"] == entity["held_stuck"] == "30"
        assert entity["window"] == "0"

        fixture_line = json.loads(
            (FIXTURES / "run_summary_shape.jsonl").read_text().splitlines()[0]
        )
        fixture_entity = fixture_line["phases"]["entity"]
        assert fixture_entity["reason"] == "all-slots-skipped"
        assert fixture_entity["considered"] == fixture_entity["held_stuck"]
        assert fixture_entity["window"] == 0


# ---------------------------------------------------------------------------
# AC3 — the failing-then-passing behavior change.
#
# Before athenaeum#1597: `stuck_backlog_warning` did not exist on StatusInfo,
# and `format_status` had no WARNING line — a stuck-dominated backlog read
# identically to a healthy one awaiting capacity. See the PR body for this
# class's RED output against the pre-fix `status.py`.
# ---------------------------------------------------------------------------


class TestStuckBacklogWarningOnStatus:
    def _seed_status_knowledge(
        self, tmp_path: Path, *, n_stuck: int, n_workable: int, error: str
    ) -> Path:
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        wiki.mkdir(parents=True)
        raw = root / "raw" / "relationship-stub"
        raw.mkdir(parents=True)
        for i in range(n_stuck + n_workable):
            (raw / f"2024041{i // 10}T12{i % 10:02d}000Z-aabbcc{i:02d}.md").write_text(
                f"Note {i} about Acme Corp.\n", encoding="utf-8"
            )
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

        all_refs = sorted(f"relationship-stub/{p.name}" for p in raw.glob("*.md"))
        stuck_refs = all_refs[:n_stuck]
        if stuck_refs:
            _write_scaled_stuck_ledger(root, stuck_refs, error=error)
        return root

    def test_a_backlog_dominated_by_a_permanent_refusal_is_surfaced(self, tmp_path: Path) -> None:
        # 30 of 31 pending raw files are permanently held (97%, above the 90%
        # alert ratio) — the live shape (355/358 = 99%), scaled down.
        root = self._seed_status_knowledge(
            tmp_path, n_stuck=30, n_workable=1, error="PersonNeverLLMRewriteError"
        )

        info = status(root)

        assert info["stuck_backlog_warning"] is not None
        warning = info["stuck_backlog_warning"]
        assert warning["considered"] == 31
        assert warning["held"] == 30
        assert warning["dominant_error"] == "PersonNeverLLMRewriteError"
        assert warning["dominant_error_count"] == 30

        rendered = format_status(info)
        assert "WARNING" in rendered
        assert "entity phase starved" in rendered
        assert "PersonNeverLLMRewriteError" in rendered
        assert "30/31" in rendered

    def test_a_backlog_with_healthy_headroom_is_not_alarmed(self, tmp_path: Path) -> None:
        # Only 3 of 10 pending files are stuck (30%, below the 90% ratio) --
        # a genuine backlog awaiting capacity, not a starved one.
        root = self._seed_status_knowledge(
            tmp_path, n_stuck=3, n_workable=7, error="PersonNeverLLMRewriteError"
        )

        info = status(root)

        assert info["stuck_backlog_warning"] is None
        rendered = format_status(info)
        assert "entity phase starved" not in rendered

    def test_dominant_error_is_correctly_identified_among_two(self, tmp_path: Path) -> None:
        # AC4: BadRequestError and PersonNeverLLMRewriteError coexist in the
        # live ledger. The WARNING must name whichever dominates, not
        # whichever sorts first / was written last.
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        wiki.mkdir(parents=True)
        raw = root / "raw" / "relationship-stub"
        raw.mkdir(parents=True)
        for i in range(36):
            (raw / f"2024041{i // 10}T12{i % 10:02d}000Z-aabbcc{i:02d}.md").write_text(
                f"Note {i} about Acme Corp.\n", encoding="utf-8"
            )
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)
        all_refs = sorted(f"relationship-stub/{p.name}" for p in raw.glob("*.md"))
        # 30 person-error, 5 bad-request, 1 not-yet-escalated transient — the
        # exact proportions in tests/fixtures/athenaeum_1597/stuck_files_shape.json.
        _write_scaled_stuck_ledger(root, all_refs[:30], error="PersonNeverLLMRewriteError")
        _write_scaled_stuck_ledger(root, all_refs[30:35], error="BadRequestError")
        _write_scaled_stuck_ledger(
            root,
            all_refs[35:36],
            error="TransientAPIError:TimeoutExpired",
            escalated=False,
            failures=1,
        )

        info = status(root)

        warning = info["stuck_backlog_warning"]
        assert warning is not None
        assert warning["dominant_error"] == "PersonNeverLLMRewriteError"
        assert warning["dominant_error_count"] == 30
        # The not-yet-escalated transient failure must NOT count as held —
        # it is still retryable, unlike the two escalated errors.
        assert warning["held"] == 35

    def test_fixture_ledger_shape_reproduces_the_live_ratio(self, tmp_path: Path) -> None:
        # Drops the committed fixture (a scrubbed, proportionally-scaled copy
        # of the live wiki/_stuck_files.json) directly into a knowledge root
        # and confirms `status` reads it exactly as it would in production.
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        wiki.mkdir(parents=True)
        raw = root / "raw" / "relationship-stub"
        raw.mkdir(parents=True)
        fixture = json.loads((FIXTURES / "stuck_files_shape.json").read_text(encoding="utf-8"))
        refs = list(fixture["files"])
        for i, ref in enumerate(refs):
            source, name = ref.split("/", 1)
            source_dir = root / "raw" / source
            source_dir.mkdir(parents=True, exist_ok=True)
            (source_dir / name).write_text(f"Note {i}.\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

        # Re-key the fixture's ledger entries to the real per-file content
        # hash (the fixture's own `hash` values are placeholders — a fresh
        # git checkout's file bytes won't match them), preserving every other
        # field (error, escalated, failures) unchanged.
        from athenaeum.librarian import _stuck_content_hash
        from athenaeum.models import RawFile

        rekeyed = {}
        for ref, entry in fixture["files"].items():
            path = root / "raw" / ref
            raw_obj = RawFile(
                path=path,
                source=ref.split("/")[0],
                timestamp="20240410T120000Z",
                uuid8="aabbccdd",
            )
            rekeyed[ref] = {**entry, "hash": _stuck_content_hash(raw_obj)}
        (wiki / STUCK_MANIFEST_NAME).write_text(
            json.dumps({"files": rekeyed, "updated": fixture["updated"]}),
            encoding="utf-8",
        )

        info = status(root)

        warning = info["stuck_backlog_warning"]
        assert warning is not None
        assert warning["dominant_error"] == "PersonNeverLLMRewriteError"
        # 35 of 36 are escalated (the fixture's 1 TransientAPIError is not).
        assert warning["held"] == 35
        assert warning["considered"] == 36
