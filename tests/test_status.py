"""Tests for athenaeum status command."""

from __future__ import annotations

import logging
import subprocess
import textwrap
from pathlib import Path

import pytest

from athenaeum.status import format_status, status


class TestStatus:
    def _seed_knowledge(self, tmp_path: Path) -> Path:
        """Create a minimal knowledge directory for status tests."""
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        (wiki / "_schema").mkdir(parents=True)
        raw = root / "raw" / "sessions"
        raw.mkdir(parents=True)

        # Entity page
        (wiki / "a1b2c3d4-acme-corp.md").write_text(
            textwrap.dedent(
                """\
            ---
            uid: a1b2c3d4
            type: company
            name: Acme Corp
            access: internal
            ---

            # Acme Corp
        """
            )
        )

        (wiki / "b2c3d4e5-alice.md").write_text(
            textwrap.dedent(
                """\
            ---
            uid: b2c3d4e5
            type: person
            name: Alice Zhang
            access: internal
            ---

            # Alice Zhang
        """
            )
        )

        # Pending questions
        (wiki / "_pending_questions.md").write_text(
            "# Pending Questions\n\n"
            '## [2024-04-06] Entity: "Acme" (from ref)\n\nConflict.\n\n'
            "---\n\n"
            '## [2024-04-07] Entity: "Bob" (from ref2)\n\nAnother.\n'
        )

        # Raw file pending
        (raw / "20240410T120000Z-aabbccdd.md").write_text("Some raw.\n")

        # Init git
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"],
            cwd=root,
            check=True,
        )

        return root

    def test_status_counts(self, tmp_path: Path) -> None:
        root = self._seed_knowledge(tmp_path)
        info = status(root)
        assert info["raw_pending"] == 1
        assert info["entity_count"] == 2
        assert info["entities_by_type"]["company"] == 1
        assert info["entities_by_type"]["person"] == 1
        assert info["pending_questions"] == 2
        assert info["last_commit_date"] != ""

    def test_status_empty_knowledge(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        info = status(root)
        assert info["raw_pending"] == 0
        assert info["entity_count"] == 0
        assert info["pending_questions"] == 0

    def test_status_zero_yield_defaults_to_zero(self, tmp_path: Path) -> None:
        # Issue athenaeum#899: no librarian run has ever finalized against this
        # knowledge base -- no ``zero_yield_state.json`` cache-dir sidecar
        # exists, and the read-side (:func:`athenaeum.zero_yield.load_state`)
        # fails open to ``0`` rather than raising.
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        info = status(root)
        assert info["zero_yield_consecutive"] == 0

    def test_status_surfaces_persisted_zero_yield_count(self, tmp_path: Path) -> None:
        # Issue athenaeum#899 AC 4: the consecutive-zero-yield count the
        # librarian finalize phase persisted is surfaced in ``athenaeum
        # status`` -- exercised here via the SAME sidecar-writing function
        # the finalize phase calls, without driving a full librarian run.
        # Written under the CACHE dir (redirected to a per-test tmp dir by
        # the ``_isolate_cache_dir`` autouse fixture), not the knowledge
        # root -- see ``athenaeum.zero_yield``'s module docstring for why.
        from athenaeum.config import resolve_cache_dir
        from athenaeum.zero_yield import write_state

        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        write_state(resolve_cache_dir(), consecutive=4, deferred_refs=["a.md"])

        info = status(root)
        assert info["zero_yield_consecutive"] == 4

    def test_status_librarian_refusal_defaults_to_zero(self, tmp_path: Path) -> None:
        # Issue athenaeum#1283: no librarian run has ever finalized against this
        # knowledge base -- no ``run_summary.jsonl`` cache-dir ledger exists,
        # and the read-side (:func:`athenaeum.run_summary_log.read_refusal_streak`)
        # fails open to ``(0, None)`` rather than raising.
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        info = status(root)
        assert info["librarian_refusal_consecutive"] == 0
        assert info["librarian_refusal_reason"] is None

    def test_status_surfaces_persisted_librarian_refusal_streak(
        self, tmp_path: Path
    ) -> None:
        # Issue athenaeum#1283: the persisted consecutive-refusal count is
        # surfaced in ``athenaeum status`` -- exercised here via the SAME
        # durable athenaeum#1102 ledger the finalize phase writes
        # (``RunContext.emit_run_summary``), without driving a full
        # librarian run. Written under the CACHE dir (redirected to a
        # per-test tmp dir by the ``_isolate_cache_dir`` autouse fixture),
        # not the knowledge root -- mirrors the zero-yield sidecar's own
        # reasoning (see its module docstring).
        from athenaeum.config import resolve_cache_dir
        from athenaeum.run_summary_log import (
            default_run_summary_ledger_path,
            write_run_summary_record,
        )

        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        ledger_path = default_run_summary_ledger_path(resolve_cache_dir())
        profile = [("entity", 1.0, {"reason": "spend-ceiling", "files": 0})]
        write_run_summary_record(
            profile,
            ledger_path=ledger_path,
            refusal={"tripped": True, "reason": "spend-ceiling", "files": 0},
        )
        write_run_summary_record(
            profile,
            ledger_path=ledger_path,
            refusal={"tripped": True, "reason": "spend-ceiling", "files": 0},
        )

        info = status(root)
        assert info["librarian_refusal_consecutive"] == 2
        assert info["librarian_refusal_reason"] == {
            "tripped": True,
            "reason": "spend-ceiling",
            "files": 0,
        }

    def test_status_verdict_ledger_duty_cycle_defaults_to_none(self, tmp_path: Path) -> None:
        # Issue athenaeum#712: no run has ever materialized wiki/_verdicts/ (the
        # flag-off default) -- status must not report a duty cycle at all.
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        info = status(root)
        assert info["verdict_ledger_duty_cycle"] is None

    def test_status_surfaces_verdict_ledger_duty_cycle(self, tmp_path: Path) -> None:
        # Issue athenaeum#712 AC: wave duty cycle is computed and reported,
        # surfaced on athenaeum status -- exercised here via the same
        # epoch-registry writer the run finalize phase calls, without
        # driving a full librarian run.
        from athenaeum.runlock import RunLock
        from athenaeum.verdicts import note_run_night, open_epoch

        root = tmp_path / "knowledge"
        wiki_root = root / "wiki"
        wiki_root.mkdir(parents=True)
        (root / "raw").mkdir(parents=True)

        lock = RunLock(root)
        with lock:
            open_epoch(wiki_root, "gate2", "v1.gate2", lock=lock)
            note_run_night(wiki_root, lock=lock)

        info = status(root)
        assert info["verdict_ledger_duty_cycle"] == {"gate2": pytest.approx(1.0)}

    def test_status_embedder_provenance_defaults_to_none(self, tmp_path: Path) -> None:
        # Issue athenaeum#1279: no librarian run has ever finalized against this
        # knowledge base -- no ``run_summary.jsonl`` cache-dir ledger exists,
        # and the read side fails open to ``None`` rather than raising or
        # reporting a false "zero fallback".
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        info = status(root)
        assert info["embedder_provenance"] is None

    def test_status_surfaces_latest_run_embedder_provenance(
        self, tmp_path: Path
    ) -> None:
        # Issue athenaeum#1279: the most recent run's raw-intake (C2) cluster-
        # pass embedder counts are surfaced in ``athenaeum status`` --
        # exercised here via the SAME durable athenaeum#1102 ledger the
        # finalize phase writes, without driving a full librarian run.
        # Written under the CACHE dir (redirected to a per-test tmp dir by
        # the ``_isolate_cache_dir`` autouse fixture), same discipline as
        # the librarian-refusal test above.
        from athenaeum.config import resolve_cache_dir
        from athenaeum.run_summary_log import (
            EMBED_CHROMADB_FIELD,
            EMBED_FALLBACK_FIELD,
            default_run_summary_ledger_path,
            write_run_summary_record,
        )

        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        ledger_path = default_run_summary_ledger_path(resolve_cache_dir())
        write_run_summary_record(
            [
                (
                    "auto-memory",
                    1.0,
                    {EMBED_CHROMADB_FIELD: 0, EMBED_FALLBACK_FIELD: 71},
                )
            ],
            ledger_path=ledger_path,
        )

        info = status(root)
        provenance = info["embedder_provenance"]
        assert provenance is not None
        assert provenance[EMBED_CHROMADB_FIELD] == 0
        assert provenance[EMBED_FALLBACK_FIELD] == 71
        assert provenance["fallback_ratio"] == 1.0
        assert provenance["as_of"] is not None

    def test_status_cluster_embedder_snapshot_defaults_to_none(
        self, tmp_path: Path
    ) -> None:
        # Issue athenaeum#1279: no cluster pass has ever run against this
        # knowledge base -- no ``raw/_librarian-clusters.jsonl`` report
        # exists yet.
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        info = status(root)
        assert info["cluster_embedder_snapshot"] is None

    def test_status_surfaces_current_cluster_report_embedder_snapshot(
        self, tmp_path: Path
    ) -> None:
        # Issue athenaeum#1279: a standing tally of the CURRENT cluster
        # report's ``embedder`` column -- exercised here via the same
        # writer the C2 cluster pass calls, without driving a full
        # librarian run. This is the "legible to a lane" fix athenaeum#1005
        # needed and could not get: which embedder produced the vectors
        # behind the corpus's recorded clusters, from a single read.
        from athenaeum.clusters import (
            EMBEDDER_CHROMADB_DEFAULT,
            EMBEDDER_FALLBACK_HASHING,
            Cluster,
            resolve_cluster_output_path,
            write_cluster_report,
        )

        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        clusters = [
            Cluster(
                cluster_id="c-1",
                member_paths=["a.md"],
                embedder=EMBEDDER_CHROMADB_DEFAULT,
            ),
            Cluster(
                cluster_id="c-2",
                member_paths=["b.md"],
                embedder=EMBEDDER_CHROMADB_DEFAULT,
            ),
            Cluster(
                cluster_id="c-3",
                member_paths=["c.md"],
                embedder=EMBEDDER_FALLBACK_HASHING,
            ),
        ]
        output_path = resolve_cluster_output_path(root)
        write_cluster_report(clusters, output_path)

        info = status(root)
        assert info["cluster_embedder_snapshot"] == {
            EMBEDDER_CHROMADB_DEFAULT: 2,
            EMBEDDER_FALLBACK_HASHING: 1,
        }

    def test_format_status(self) -> None:
        info = {
            "raw_pending": 3,
            "entity_count": 10,
            "entities_by_type": {"person": 5, "company": 3, "concept": 2},
            "last_commit_date": "2024-04-06 12:00:00 -0700",
            "last_commit_message": "librarian: processed 5 file(s)",
            "pending_questions": 1,
        }
        output = format_status(info)
        assert "Raw files pending:    3" in output
        assert "Wiki entities:        10" in output
        assert "person: 5" in output
        assert "Pending questions:    1" in output

    def test_format_status_includes_zero_yield_line(self) -> None:
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "zero_yield_consecutive": 5,
        }
        output = format_status(info)
        assert "Zero-yield runs:      5 consecutive" in output

    def test_format_status_omits_zero_yield_line_when_healthy(self) -> None:
        # Issue athenaeum#899: a healthy run (count 0, or the key altogether
        # absent on a pre-athenaeum#899 status dict) must not clutter status
        # output -- mirrors the drain-advisory "only when actionable" rule.
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "zero_yield_consecutive": 0,
        }
        output = format_status(info)
        assert "Zero-yield" not in output

        # And the pre-athenaeum#899 dict (key absent entirely) still formats cleanly.
        del info["zero_yield_consecutive"]
        output = format_status(info)  # type: ignore[arg-type]
        assert "Zero-yield" not in output

    def test_format_status_includes_librarian_refusal_line_at_streak_of_one(
        self,
    ) -> None:
        # Issue athenaeum#1283: unlike the zero-yield line (an unconditional
        # count once non-zero) and the athenaeum#1291 starvation WARNING
        # (streak-of-3 threshold), a SINGLE refusal must already render --
        # this is "status must not read healthy", not a threshold alarm.
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "librarian_refusal_consecutive": 1,
            "librarian_refusal_reason": {
                "tripped": True,
                "reason": "spend-ceiling",
                "files": 0,
            },
        }
        output = format_status(info)
        assert "librarian-run-refusal" in output
        assert "1 consecutive run(s)" in output
        assert "spend-ceiling" in output

    def test_format_status_omits_librarian_refusal_line_when_healthy(self) -> None:
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "librarian_refusal_consecutive": 0,
            "librarian_refusal_reason": None,
        }
        output = format_status(info)
        assert "librarian-run-refusal" not in output

        # And the pre-athenaeum#1283 dict (keys absent entirely) still formats
        # cleanly.
        del info["librarian_refusal_consecutive"]
        del info["librarian_refusal_reason"]
        output = format_status(info)  # type: ignore[arg-type]
        assert "librarian-run-refusal" not in output

    def test_format_status_includes_verdict_ledger_duty_cycle_line(self) -> None:
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "verdict_ledger_duty_cycle": {"gate2": 0.25},
        }
        output = format_status(info)
        assert "Verdict ledger duty cycle:" in output
        assert "gate2: 25%" in output

    def test_format_status_omits_verdict_ledger_line_when_absent(self) -> None:
        # Issue athenaeum#712: the flag-off default (None, or the key absent
        # entirely on a pre-athenaeum#712 status dict) must not clutter status
        # output -- mirrors the zero-yield "only when actionable" rule.
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "verdict_ledger_duty_cycle": None,
        }
        output = format_status(info)  # type: ignore[arg-type]
        assert "Verdict ledger" not in output

        del info["verdict_ledger_duty_cycle"]
        output = format_status(info)  # type: ignore[arg-type]
        assert "Verdict ledger" not in output

    def test_format_status_includes_embedder_provenance_line(self) -> None:
        # Issue athenaeum#1279: shown whenever a run has ever recorded it --
        # NOT gated on "only when alarming", unlike zero-yield/refusal above.
        # A healthy run's ratio must be just as readable as an unhealthy one.
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "embedder_provenance": {
                "embed_chromadb": 0,
                "embed_fallback": 71,
                "fallback_ratio": 1.0,
                "as_of": "2026-08-25T00:00:00Z",
            },
        }
        output = format_status(info)
        assert "Embedder provenance" in output
        assert "chromadb=0" in output
        assert "fallback=71" in output
        assert "fallback_ratio=100%" in output

    def test_format_status_embedder_provenance_ratio_none_reads_as_n_a(self) -> None:
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "embedder_provenance": {
                "embed_chromadb": 0,
                "embed_fallback": 0,
                "fallback_ratio": None,
                "as_of": "2026-08-25T00:00:00Z",
            },
        }
        output = format_status(info)
        assert "fallback_ratio=n/a" in output

    def test_format_status_omits_embedder_provenance_line_when_absent(self) -> None:
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "embedder_provenance": None,
        }
        output = format_status(info)  # type: ignore[arg-type]
        assert "Embedder provenance" not in output

        del info["embedder_provenance"]
        output = format_status(info)  # type: ignore[arg-type]
        assert "Embedder provenance" not in output

    def test_format_status_includes_cluster_embedder_snapshot_line(self) -> None:
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "cluster_embedder_snapshot": {
                "chromadb-default": 2,
                "fallback-hashing": 1,
            },
        }
        output = format_status(info)
        assert "Cluster report embedder distribution:" in output
        assert "chromadb-default=2" in output
        assert "fallback-hashing=1" in output

    def test_format_status_omits_cluster_embedder_snapshot_line_when_absent(
        self,
    ) -> None:
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
            "cluster_embedder_snapshot": None,
        }
        output = format_status(info)  # type: ignore[arg-type]
        assert "Cluster report embedder" not in output

        del info["cluster_embedder_snapshot"]
        output = format_status(info)  # type: ignore[arg-type]
        assert "Cluster report embedder" not in output

    def test_cli_status(self, tmp_path: Path) -> None:
        from athenaeum.cli import main

        root = self._seed_knowledge(tmp_path)
        exit_code = main(["status", "--path", str(root)])
        assert exit_code == 0

    def test_cli_status_missing_dir(self, tmp_path: Path) -> None:
        from athenaeum.cli import main

        exit_code = main(["status", "--path", str(tmp_path / "nope")])
        assert exit_code == 1


def _entity_page(name: str, uid: str, target_bytes: int) -> str:
    """Build an entity page whose UTF-8 body is ~``target_bytes`` long."""
    header = textwrap.dedent(
        f"""\
        ---
        uid: {uid}
        type: concept
        name: {name}
        access: internal
        ---

        # {name}

    """
    )
    pad = "x" * max(0, target_bytes - len(header.encode("utf-8")))
    return header + pad


class TestPageSizeGuardrails:
    """Issue athenaeum#310 — warn-only oversized wiki-page reporting."""

    def _seed(self, tmp_path: Path) -> Path:
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        wiki.mkdir(parents=True)
        (root / "raw").mkdir()

        # Under warn (default 8192).
        (wiki / "small.md").write_text(_entity_page("Small", "u1", 500))
        # Over warn, at/under flag (default 16384).
        (wiki / "warnpage.md").write_text(_entity_page("Warn Page", "u2", 10000))
        # Over flag.
        (wiki / "flagpage.md").write_text(_entity_page("Flag Page", "u3", 20000))
        # Big but NOT an entity (no frontmatter name) — must be skipped.
        (wiki / "notes.md").write_text("y" * 30000)
        # Big but _-prefixed — must be skipped.
        (wiki / "_scratch.md").write_text("z" * 30000)
        return root

    def test_scan_buckets_by_threshold(self, tmp_path: Path) -> None:
        from athenaeum.status import scan_page_sizes

        root = self._seed(tmp_path)
        warn, flag = scan_page_sizes(root / "wiki", 8192, 16384)
        warn_names = {n for n, _ in warn}
        flag_names = {n for n, _ in flag}
        assert warn_names == {"warnpage.md"}
        assert flag_names == {"flagpage.md"}
        # Disjoint: a flagged page is not double-counted as a warn.
        assert not (warn_names & flag_names)

    def test_scan_skips_nonentity_and_underscore(self, tmp_path: Path) -> None:
        from athenaeum.status import scan_page_sizes

        root = self._seed(tmp_path)
        warn, flag = scan_page_sizes(root / "wiki", 8192, 16384)
        seen = {n for n, _ in warn} | {n for n, _ in flag}
        assert "notes.md" not in seen  # non-entity (no name) skipped
        assert "_scratch.md" not in seen  # underscore-prefixed skipped
        assert "small.md" not in seen  # under warn threshold

    def test_status_reports_pages(self, tmp_path: Path) -> None:
        root = self._seed(tmp_path)
        info = status(root)
        assert [n for n, _ in info["pages_warn"]] == ["warnpage.md"]
        assert [n for n, _ in info["pages_flag"]] == ["flagpage.md"]

    def test_format_status_includes_oversized_summary(self, tmp_path: Path) -> None:
        root = self._seed(tmp_path)
        out = format_status(status(root))
        assert "Oversized pages (warn/flag): 1/1" in out
        assert "[flag] flagpage.md" in out
        assert "[warn] warnpage.md" in out

    def test_format_status_backward_compatible(self) -> None:
        # A pre-athenaeum#310 status dict (no pages_* keys) must still format.
        info = {
            "raw_pending": 0,
            "entity_count": 0,
            "entities_by_type": {},
            "last_commit_date": "",
            "last_commit_message": "",
            "pending_questions": 0,
        }
        out = format_status(info)  # type: ignore[arg-type]
        assert "Oversized pages (warn/flag): 0/0" in out

    def test_boundary_exact_warn_not_warned(self, tmp_path: Path) -> None:
        # Bucketing is strict `>`; a page of EXACTLY warn_bytes must NOT warn.
        from athenaeum.status import scan_page_sizes

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        page = _entity_page("Exact Warn", "e1", 8192)
        assert len(page.encode("utf-8")) == 8192
        (wiki / "exactwarn.md").write_text(page)
        warn, flag = scan_page_sizes(wiki, 8192, 16384)
        assert not warn
        assert not flag

    def test_boundary_exact_flag_warned_not_flagged(self, tmp_path: Path) -> None:
        # A page of EXACTLY flag_bytes is over warn (warned) but NOT over flag.
        from athenaeum.status import scan_page_sizes

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        page = _entity_page("Exact Flag", "e2", 16384)
        assert len(page.encode("utf-8")) == 16384
        (wiki / "exactflag.md").write_text(page)
        warn, flag = scan_page_sizes(wiki, 8192, 16384)
        assert [n for n, _ in warn] == ["exactflag.md"]
        assert not flag

    def test_inverted_thresholds_clamped_and_warned(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # flag <= warn is a misconfig: clamp flag up to warn AND warn once.
        from athenaeum.status import scan_page_sizes

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "big.md").write_text(_entity_page("Big", "e3", 20000))
        with caplog.at_level(logging.WARNING, logger="athenaeum"):
            warn, flag = scan_page_sizes(wiki, 16384, 8192)  # flag < warn
        # A 20000B page exceeds the clamped flag (== warn == 16384) => flagged.
        assert [n for n, _ in flag] == ["big.md"]
        assert not warn
        assert any(
            "page_flag_bytes" in rec.message and "clamped" in rec.message
            for rec in caplog.records
        )

    def test_thresholds_honor_config(self, tmp_path: Path) -> None:
        # A tiny yaml warn threshold pulls the small page into the warn bucket.
        root = self._seed(tmp_path)
        (root / "athenaeum.yaml").write_text(
            "librarian:\n  page_warn_bytes: 100\n  page_flag_bytes: 15000\n"
        )
        info = status(root)
        warn_names = {n for n, _ in info["pages_warn"]}
        flag_names = {n for n, _ in info["pages_flag"]}
        # small (500B) and warn (10000B) now exceed the 100B warn floor but
        # stay under the 15000B flag; only flagpage (20000B) is flagged.
        assert warn_names == {"small.md", "warnpage.md"}
        assert flag_names == {"flagpage.md"}
