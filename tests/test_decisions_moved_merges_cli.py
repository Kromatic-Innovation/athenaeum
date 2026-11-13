# SPDX-License-Identifier: Apache-2.0
"""``athenaeum decisions {revalidate,recompare,scrub-pii,propose-fold,provenance}``
(issue athenaeum#2456) — these five verbs moved wholesale from the
since-removed ``athenaeum merges`` (issue athenaeum#2457) to ``athenaeum
decisions`` with the SAME flags, output and exit codes (they delegate to the
exact same handler functions in :mod:`athenaeum._cmd_merges`; see that
module's ``_add_*_subparser`` / ``_cmd_*`` functions and
``_cmd_decisions.cmd_decisions``'s dispatch).

Each class below reuses the fixtures/scenarios an existing ``merges`` test
already established (cited per class) and re-runs them through the
``decisions`` spelling, so a divergence from the behaviour the moved verbs
shipped with fails here rather than only showing up live. ``TestRecompareLockFix`` additionally
covers the lock-held/lock-free split the issue requires: a held run lock
must exit 75 with the standard message and write nothing, never a traceback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.pending_merges import parse_pending_merges, render_block, write_pending_merge
from athenaeum.runlock import RunLock

# Reused fixtures/scenarios from the existing `merges` test suites (issue
# athenaeum#2456 AC: "each moved verb gets a CLI test under decisions, reusing
# the existing merges test cases").
from tests.test_merges_propose_fold_cli import _seed as _propose_fold_seed
from tests.test_pending_merges_pii import EMAIL, PHONE
from tests.test_pending_merges_pii import _queue_proposal as _pii_queue_proposal
from tests.test_pending_merges_pii import _seed_root as _pii_seed_root


class TestDecisionsRevalidate:
    """Mirrors ``tests/test_pending_merges_revalidate.py``'s over-cap scenario
    (7 sources > the default ``max_merge_sources`` of 5) through the CLI, under
    the new ``decisions`` spelling instead of ``merges``."""

    def _seed(self, tmp_path: Path) -> Path:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        block = render_block(
            merge_target_name="over-cap-cluster",
            sources=[f"/k/src-{i}.md" for i in range(7)],
            rationale="chained on a weak bridge",
            draft_merged_body="draft",
            confidence=0.33,
            created_at="2026-06-20",
        )
        merges_path = root / "wiki" / "_pending_merges.md"
        merges_path.write_text("# Pending Merges\n\n---\n\n" + block, encoding="utf-8")
        return root

    def test_dry_run_reports_without_writing(self, tmp_path: Path, capsys) -> None:
        root = self._seed(tmp_path)
        merges_path = root / "wiki" / "_pending_merges.md"
        before = merges_path.read_text(encoding="utf-8")

        rc = main(["decisions", "revalidate", "--path", str(root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Would retire 1 stale proposal(s)" in out
        assert merges_path.read_text(encoding="utf-8") == before
        assert not (root / "wiki" / "_pending_merges_archive.md").exists()

    def test_apply_archives_the_stale_proposal(self, tmp_path: Path, capsys) -> None:
        root = self._seed(tmp_path)

        rc = main(["decisions", "revalidate", "--path", str(root), "--apply"])

        assert rc == 0
        assert "Retired 1 stale proposal(s)" in capsys.readouterr().out
        assert (root / "wiki" / "_pending_merges_archive.md").exists()
        assert parse_pending_merges(root / "wiki" / "_pending_merges.md") == []

    def test_json_output_matches_merges_shape(self, tmp_path: Path, capsys) -> None:
        root = self._seed(tmp_path)

        rc = main(["decisions", "revalidate", "--path", str(root), "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["applied"] is False
        assert len(payload["retired"]) == 1
        assert payload["retired"][0]["merge_target_name"] == "over-cap-cluster"

    def _mixed_seed(self, tmp_path: Path) -> Path:
        """One over-cap (7-source) block plus one legal (2-source) block.

        Carried over from the removed ``tests/test_merges_cli.py`` (issue
        athenaeum#2457) so the only scenario that proves revalidate is
        SELECTIVE — a legal proposal survives the same sweep that archives
        a stale one — still runs under the surviving spelling.
        """
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        wiki.mkdir(parents=True)
        over_cap = render_block(
            merge_target_name="merge-workflow-pattern",
            sources=[f"/k/src-{i}.md" for i in range(7)],
            rationale="chained",
            draft_merged_body="draft",
            confidence=0.33,
            created_at="2026-06-20",
        )
        # The legal block's sources must really exist: athenaeum#2072 made a
        # proposal with a missing source page stale in its own right.
        for stem in ("a", "b"):
            (wiki / f"{stem}.md").write_text(
                "---\nname: acme-corp\ntype: concept\n---\nbody\n", encoding="utf-8"
            )
        legal = render_block(
            merge_target_name="acme-corp",
            sources=[str(wiki / "a.md"), str(wiki / "b.md")],
            rationale="tight",
            draft_merged_body="draft",
            confidence=0.9,
            created_at="2026-07-05",
        )
        (wiki / "_pending_merges.md").write_text(
            "# Pending Merges\n\n" + over_cap + "\n\n---\n\n" + legal + "\n",
            encoding="utf-8",
        )
        return root

    def test_apply_json_archives_only_the_stale_proposal(
        self, tmp_path: Path, capsys
    ) -> None:
        root = self._mixed_seed(tmp_path)

        rc = main(["decisions", "revalidate", "--path", str(root), "--apply", "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["applied"] is True
        assert [r["merge_target_name"] for r in payload["retired"]] == [
            "merge-workflow-pattern"
        ]
        assert payload["retired"][0]["n_sources"] == 7
        assert "over-cluster" in payload["retired"][0]["reason"]
        primary = (root / "wiki" / "_pending_merges.md").read_text(encoding="utf-8")
        assert "acme-corp" in primary
        assert "merge-workflow-pattern" not in primary
        archive = (root / "wiki" / "_pending_merges_archive.md").read_text(encoding="utf-8")
        assert "merge-workflow-pattern" in archive


class TestDecisionsScrubPii:
    """Mirrors ``tests/test_pending_merges_pii.py::TestScrubPiiCli`` through
    the ``decisions`` spelling."""

    def test_dry_run_reports_without_writing(self, tmp_path: Path, capsys) -> None:
        root = _pii_seed_root(tmp_path)
        merges_path = _pii_queue_proposal(root)
        before = merges_path.read_text(encoding="utf-8")

        rc = main(["decisions", "scrub-pii", "--path", str(root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Would redact 2 contact value(s)" in out
        assert EMAIL not in out
        assert merges_path.read_text(encoding="utf-8") == before

    def test_apply_redacts_without_resolving_the_merge(self, tmp_path: Path, capsys) -> None:
        root = _pii_seed_root(tmp_path)
        merges_path = _pii_queue_proposal(root)

        rc = main(["decisions", "scrub-pii", "--path", str(root), "--apply"])

        assert rc == 0
        assert "Redacted 2 contact value(s)" in capsys.readouterr().out
        text = merges_path.read_text(encoding="utf-8")
        assert EMAIL not in text
        assert PHONE not in text
        merges = parse_pending_merges(merges_path)
        assert len(merges) == 1
        assert merges[0].resolved is False


class TestDecisionsProposeFold:
    """Mirrors ``tests/test_merges_propose_fold_cli.py``'s core AC1/AC2 scenario
    through the ``decisions`` spelling."""

    def test_apply_queues_one_proposal_with_expected_sources_and_target(
        self, tmp_path: Path
    ) -> None:
        wiki, canonical, src_b, src_c = _propose_fold_seed(tmp_path)

        rc = main(
            [
                "decisions",
                "propose-fold",
                "--path",
                str(tmp_path),
                "--into",
                "maria-springer",
                "--source",
                "springer-maria",
                "--source",
                "m-springer",
                "--apply",
            ]
        )

        assert rc == 0
        pms = parse_pending_merges(wiki / "_pending_merges.md")
        assert len(pms) == 1
        pm = pms[0]
        assert pm.merge_target_name == "Maria Springer"
        assert pm.sources == [str(canonical), str(src_b), str(src_c)]
        assert pm.write_kind == "fold-into-existing"

    def test_dry_run_writes_nothing(self, tmp_path: Path, capsys) -> None:
        wiki, _canonical, _src_b, _src_c = _propose_fold_seed(tmp_path)

        rc = main(
            [
                "decisions",
                "propose-fold",
                "--path",
                str(tmp_path),
                "--into",
                "maria-springer",
                "--source",
                "springer-maria",
            ]
        )

        assert rc == 0
        assert "No changes written" in capsys.readouterr().out
        assert not (wiki / "_pending_merges.md").exists()


class TestDecisionsProvenance:
    """``decisions provenance`` against a missing/empty
    ``_merge_provenance.jsonl`` — same "never raises" discipline as
    ``merges provenance``."""

    def test_missing_provenance_file_reports_zero(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)

        rc = main(["decisions", "provenance", "--path", str(root)])

        assert rc == 0
        assert "0 recorded" in capsys.readouterr().out

    def test_missing_provenance_file_json(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)

        rc = main(["decisions", "provenance", "--path", str(root), "--json"])

        assert rc == 0
        assert json.loads(capsys.readouterr().out) == []


class TestRecompareLockFix:
    """Issue athenaeum#2456 AC3: ``decisions recompare --apply`` acquires the
    run lock through ``_acquire_or_exit`` and exits 75 (never a traceback)
    when another process already holds it; a free lock still succeeds.

    Before the fix, ``_cmd_merges._cmd_recompare`` used a bare
    ``with RunLock(knowledge_root)`` with no ``LockHeld`` handling, so a
    contended lock propagated as an uncaught exception out of ``main()``
    (an interpreter-level traceback + exit 1 at the shell), rather than the
    standard EXIT_LOCK_HELD contract every other mutating command honors.
    (The legacy ``merges recompare`` spelling shared the same fixed
    implementation; issue athenaeum#2457 removed that spelling in 0.24.0 —
    see ``TestRemovedSurfaces`` in ``tests/test_cli.py``.)
    """

    def _knowledge_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        return root

    def test_apply_exits_75_without_traceback_or_writes_when_lock_held(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_COMPARATOR_ENABLED", "1")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        root = self._knowledge_root(tmp_path)
        merges_path = root / "wiki" / "_pending_merges.md"
        write_pending_merge(
            merges_path,
            merge_target_name="still-unresolved",
            sources=["/k/a.md", "/k/b.md"],
            rationale="untouched by a refused apply",
            draft_merged_body="draft",
            confidence=0.8,
        )
        before = merges_path.read_text(encoding="utf-8")

        holder = RunLock(root)
        holder.acquire()
        try:
            rc = main(["decisions", "recompare", "--path", str(root), "--apply"])
        finally:
            holder.release()

        captured = capsys.readouterr()
        assert rc == 75
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out
        assert "error" in captured.err.lower()
        # Nothing written: the proposal queue is untouched and no verdict
        # ledger directory was ever created.
        assert merges_path.read_text(encoding="utf-8") == before
        assert not (root / "wiki" / "_verdicts").exists()

    def test_apply_succeeds_when_lock_is_free(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_COMPARATOR_ENABLED", "1")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        root = self._knowledge_root(tmp_path)

        rc = main(["decisions", "recompare", "--path", str(root), "--apply"])

        assert rc == 0
        assert "Traceback" not in capsys.readouterr().err
        # The lock was released after use -- a fresh acquire succeeds.
        lock = RunLock(root)
        lock.acquire()
        lock.release()
