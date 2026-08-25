# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum merges recompare`` (issue athenaeum#715).

One class per acceptance criterion, mirroring ``tests/test_comparator.py``'s
convention. Everything is offline: Gate 2 is a mocked client, and no test
touches a real knowledge store.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.comparator import (
    VERDICT_CONTRADICTION,
    VERDICT_DISTINCT,
    VERDICT_DUPLICATE,
    VERDICT_SPECIALIZATION,
    VERDICT_UNDERDETERMINED,
)
from athenaeum.pending_merges import render_block
from athenaeum.recompare import (
    MAX_PAIRS_PER_PROPOSAL,
    ROUTE_HUMAN,
    ROUTE_LEDGER,
    ProposalRecompare,
    aggregate_verdict,
    can_auto_apply,
    identify_pii_hazards,
    recompare_pending_merges,
    resolve_source_path,
)
from athenaeum.runlock import RunLock


def _fake_client(payload_json: str) -> MagicMock:
    """Mirror :func:`tests.test_comparator._fake_client`'s Anthropic SDK shape."""
    client = MagicMock()
    client.messages.create.return_value.content = [MagicMock(text=payload_json)]
    return client


def _content_payload(relation: str) -> str:
    return json.dumps(
        {
            "content_relation": relation,
            "conflicting_passages": ["a says x", "b says y"] if relation == "conflicting" else [],
            "predicate_a": "what a answers",
            "predicate_b": "what b answers",
            "rationale": "test rationale",
        }
    )


def _write_page(
    wiki_root: Path,
    page_id: str,
    *,
    body: str = "some claim text",
    pii: bool | None = None,
    extra: str = "",
) -> Path:
    lines = ["---", f"name: {page_id}", 'recorded_at: "2026-01-01T00:00:00+00:00"']
    if pii is not None:
        lines.append(f"pii: {'true' if pii else 'false'}")
    if extra:
        lines.append(extra)
    lines += ["---", "", body, ""]
    path = wiki_root / f"{page_id}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_queue(wiki_root: Path, blocks: list[str]) -> Path:
    path = wiki_root / "_pending_merges.md"
    path.write_text("# Pending Merges\n\n---\n\n" + "\n".join(blocks), encoding="utf-8")
    return path


def _block(name: str, sources: list[Path], *, confidence: float = 0.80) -> str:
    return render_block(
        merge_target_name=name,
        sources=[str(p) for p in sources],
        rationale="test cluster",
        draft_merged_body="## From `wiki/a.md`\n\nstapled\n",
        confidence=confidence,
        created_at="2026-08-01",
    )


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


class TestPiiHazardGuard:
    """athenaeum#715: the PII-hazard proposals must never be approved — not by
    this re-run, not by auto-apply, not by agent triage."""

    def test_frontmatter_pii_flag_is_identified_before_comparison(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a", pii=True)
        b = _write_page(wiki_root, "b")
        _write_queue(wiki_root, [_block("hazard", [a, b])])
        client = _fake_client(_content_payload("equivalent"))

        result = recompare_pending_merges(wiki_root, client=client)

        assert result.pii_hazard_ids == [result.proposals[0].proposal_id]
        assert result.proposals[0].route == ROUTE_HUMAN
        # Identified BEFORE the comparison: no LLM call was spent on it.
        assert client.messages.create.call_count == 0

    def test_inline_email_in_body_is_a_hazard(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a", body="reach them at person@example.com")
        b = _write_page(wiki_root, "b")
        reasons = identify_pii_hazards([a, b])
        assert any("inline email" in r for r in reasons)

    def test_inline_phone_in_body_is_a_hazard(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a", body="call +1 415 555 0199 to confirm")
        b = _write_page(wiki_root, "b")
        reasons = identify_pii_hazards([a, b])
        assert any("inline phone" in r for r in reasons)

    def test_clean_pair_is_not_a_hazard(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        b = _write_page(wiki_root, "b")
        assert identify_pii_hazards([a, b]) == []

    def test_a_duplicate_verdict_on_a_hazard_still_cannot_auto_apply(self) -> None:
        # The literal athenaeum#715 sentence: "If the re-run's verdict for either
        # is `duplicate`, it still routes to a human."
        hazard = ProposalRecompare(
            proposal_id="p1",
            merge_target_name="hazard",
            sources=["a.md", "b.md"],
            pii_hazard=True,
            pii_hazard_reasons=["a.md: pii: frontmatter flag"],
            pair_verdicts={"a|b": VERDICT_DUPLICATE},
            aggregate=VERDICT_DUPLICATE,
            route=ROUTE_HUMAN,
            stored_confidence=0.92,
        )
        assert can_auto_apply(hazard) is False

    def test_no_proposal_of_any_kind_can_auto_apply(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        b = _write_page(wiki_root, "b")
        _write_queue(wiki_root, [_block("clean", [a, b])])
        result = recompare_pending_merges(
            wiki_root, client=_fake_client(_content_payload("equivalent"))
        )
        assert all(not can_auto_apply(p) for p in result.proposals)


class TestNeverMutatesTheQueue:
    """athenaeum#715: "do not approve, reject, or archive any of them by hand."
    This command's --apply writes to the LEDGER only."""

    def test_dry_run_leaves_the_sidecar_byte_identical(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        b = _write_page(wiki_root, "b")
        path = _write_queue(wiki_root, [_block("clean", [a, b])])
        before = path.read_bytes()
        recompare_pending_merges(wiki_root, client=_fake_client(_content_payload("equivalent")))
        assert path.read_bytes() == before

    def test_apply_leaves_the_sidecar_byte_identical(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        b = _write_page(wiki_root, "b")
        path = _write_queue(wiki_root, [_block("clean", [a, b])])
        before = path.read_bytes()
        lock = RunLock(wiki_root)
        with lock:
            recompare_pending_merges(
                wiki_root,
                client=_fake_client(_content_payload("equivalent")),
                apply=True,
                lock=lock,
            )
        assert path.read_bytes() == before

    def test_dry_run_writes_no_ledger(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        b = _write_page(wiki_root, "b")
        _write_queue(wiki_root, [_block("clean", [a, b])])
        recompare_pending_merges(wiki_root, client=_fake_client(_content_payload("equivalent")))
        assert not (wiki_root / "_verdicts").exists()

    def test_apply_without_a_lock_raises_rather_than_half_running(self, wiki_root: Path) -> None:
        _write_queue(wiki_root, [])
        with pytest.raises(ValueError, match="RunLock"):
            recompare_pending_merges(wiki_root, client=None, apply=True)


class TestApplyWritesTheLedger:
    def test_apply_records_a_verdict_per_pair(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        b = _write_page(wiki_root, "b")
        _write_queue(wiki_root, [_block("clean", [a, b])])
        lock = RunLock(wiki_root)
        with lock:
            result = recompare_pending_merges(
                wiki_root,
                client=_fake_client(_content_payload("equivalent")),
                apply=True,
                lock=lock,
            )
        assert result.applied is True
        assert result.compared == 1
        assert (wiki_root / "_verdicts").is_dir()
        assert list(result.proposals[0].pair_verdicts.values()) == [VERDICT_DUPLICATE]

    def test_a_fresh_pair_is_not_recompared(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        b = _write_page(wiki_root, "b")
        _write_queue(wiki_root, [_block("clean", [a, b])])
        lock = RunLock(wiki_root)
        with lock:
            recompare_pending_merges(
                wiki_root,
                client=_fake_client(_content_payload("equivalent")),
                apply=True,
                lock=lock,
            )
            second = recompare_pending_merges(
                wiki_root,
                client=_fake_client(_content_payload("equivalent")),
                apply=True,
                lock=lock,
            )
        assert second.skipped_fresh == 1
        assert second.compared == 0


class TestAggregateVerdict:
    """The documented, deterministic cluster reduction."""

    def test_any_contradiction_wins(self) -> None:
        assert (
            aggregate_verdict({"1": VERDICT_DUPLICATE, "2": VERDICT_CONTRADICTION})
            == VERDICT_CONTRADICTION
        )

    def test_underdetermined_beats_duplicate(self) -> None:
        assert (
            aggregate_verdict({"1": VERDICT_DUPLICATE, "2": VERDICT_UNDERDETERMINED})
            == VERDICT_UNDERDETERMINED
        )

    def test_all_duplicate_is_duplicate(self) -> None:
        assert (
            aggregate_verdict({"1": VERDICT_DUPLICATE, "2": VERDICT_DUPLICATE}) == VERDICT_DUPLICATE
        )

    def test_specialization_is_reported_not_folded(self) -> None:
        assert (
            aggregate_verdict({"1": VERDICT_DISTINCT, "2": VERDICT_SPECIALIZATION})
            == VERDICT_SPECIALIZATION
        )

    def test_all_distinct_is_distinct(self) -> None:
        assert aggregate_verdict({"1": VERDICT_DISTINCT, "2": VERDICT_DISTINCT}) == VERDICT_DISTINCT

    def test_no_decided_pair_is_none(self) -> None:
        assert aggregate_verdict({"1": None, "2": None}) is None

    def test_a_mixed_cluster_is_underdetermined_not_flattened(self) -> None:
        # athenaeum#658 D1's over-clustering failure: partly duplicative needs a
        # human to split it, not a verdict that flattens it.
        assert (
            aggregate_verdict({"1": VERDICT_DUPLICATE, "2": VERDICT_DISTINCT})
            == VERDICT_UNDERDETERMINED
        )

    def test_undecided_pairs_do_not_veto_a_decided_cluster(self) -> None:
        assert aggregate_verdict({"1": VERDICT_DUPLICATE, "2": None}) == VERDICT_DUPLICATE


class TestSourceResolution:
    def test_an_absolute_path_from_another_machine_falls_back_to_basename(
        self, wiki_root: Path
    ) -> None:
        _write_page(wiki_root, "a")
        resolved = resolve_source_path("/Users/someone/knowledge/wiki/a.md", wiki_root)
        assert resolved == wiki_root / "a.md"

    def test_a_missing_source_resolves_to_none(self, wiki_root: Path) -> None:
        assert resolve_source_path("/nowhere/gone.md", wiki_root) is None

    def test_a_missing_source_is_counted_not_guessed(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        _write_queue(wiki_root, [_block("half", [a, wiki_root / "gone.md"])])
        result = recompare_pending_merges(wiki_root, client=None)
        assert result.skipped_missing_source == 1
        assert any("source not found" in n for n in result.proposals[0].notes)
        assert result.proposals[0].aggregate is None


class TestNoSilentTruncation:
    def test_pairs_past_the_cap_are_counted_in_notes(self, wiki_root: Path) -> None:
        pages = [_write_page(wiki_root, f"p{i}") for i in range(12)]
        _write_queue(wiki_root, [_block("big", pages)])
        result = recompare_pending_merges(
            wiki_root, client=_fake_client(_content_payload("compatible"))
        )
        total_pairs = 12 * 11 // 2
        assert total_pairs > MAX_PAIRS_PER_PROPOSAL
        assert len(result.proposals[0].pair_verdicts) == MAX_PAIRS_PER_PROPOSAL
        assert any("skipped over the" in n for n in result.proposals[0].notes)


class TestConfidenceIsReportedNeverUsed:
    """athenaeum#715: "Similarity is never a verdict input and never a confidence.\""""

    def test_stored_confidence_is_reported_alongside_the_verdict(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        b = _write_page(wiki_root, "b")
        _write_queue(wiki_root, [_block("clean", [a, b], confidence=0.23)])
        result = recompare_pending_merges(
            wiki_root, client=_fake_client(_content_payload("equivalent"))
        )
        assert result.proposals[0].stored_confidence == pytest.approx(0.23)

    def test_a_low_stored_confidence_does_not_change_the_verdict(self, wiki_root: Path) -> None:
        verdicts = []
        for idx, confidence in enumerate((0.01, 0.99)):
            root = wiki_root.parent / f"run{idx}" / "wiki"
            root.mkdir(parents=True)
            a = _write_page(root, "a")
            b = _write_page(root, "b")
            _write_queue(root, [_block("clean", [a, b], confidence=confidence)])
            result = recompare_pending_merges(
                root, client=_fake_client(_content_payload("equivalent"))
            )
            verdicts.append(result.proposals[0].aggregate)
        assert verdicts[0] == verdicts[1] == VERDICT_DUPLICATE


class TestLimitAndRouting:
    def test_limit_truncates_the_run(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        b = _write_page(wiki_root, "b")
        c = _write_page(wiki_root, "c")
        _write_queue(wiki_root, [_block("one", [a, b]), _block("two", [b, c])])
        result = recompare_pending_merges(
            wiki_root, client=_fake_client(_content_payload("compatible")), limit=1
        )
        assert result.total == 1

    def test_a_clean_proposal_routes_to_the_ledger(self, wiki_root: Path) -> None:
        a = _write_page(wiki_root, "a")
        b = _write_page(wiki_root, "b")
        _write_queue(wiki_root, [_block("clean", [a, b])])
        result = recompare_pending_merges(
            wiki_root, client=_fake_client(_content_payload("compatible"))
        )
        assert result.proposals[0].route == ROUTE_LEDGER


class TestCli:
    def test_recompare_refuses_when_the_comparator_is_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import argparse

        from athenaeum._cmd_merges import cmd_merges

        monkeypatch.delenv("ATHENAEUM_COMPARATOR_ENABLED", raising=False)
        (tmp_path / "wiki").mkdir()
        args = argparse.Namespace(
            merges_target="recompare", path=tmp_path, json=False, apply=False, limit=0
        )
        assert cmd_merges(args) == 2
        assert "comparator is disabled" in capsys.readouterr().err
