# SPDX-License-Identifier: Apache-2.0
"""Merge proposers honour prior human rulings (issue athenaeum#2077).

Covers the qualified-name scan (athenaeum#1577) and the wiki-dedupe pair
loop. Rulings are produced through the real ``write_pending_merge`` ->
``resolve_merge`` -> ``ingest_resolved_merges`` path, so the archive format
under test is the one production writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from athenaeum.name_structure import propose_qualified_name_merges
from athenaeum.pending_merges import (
    ingest_resolved_merges,
    list_pending_merges,
    resolve_merge,
    write_pending_merge,
)
from athenaeum.prior_rulings import load_prior_rulings, match_rulings


def _page(wiki: Path, filename: str, name: str, body: str = "Some facts.") -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / filename
    path.write_text(f"---\nname: {name}\ntype: project\n---\n{body}\n", encoding="utf-8")
    return path


def _rule(
    wiki: Path,
    bare: Path,
    qualified: Path,
    decision: str,
    *,
    answered_at: str,
    base_name: str = "ORCA",
    archive: bool = True,
    auto_applied: bool = False,
) -> None:
    """Queue then resolve a proposal for (bare, qualified), as production does."""
    merges = wiki / "_pending_merges.md"
    write_pending_merge(
        merges,
        merge_target_name=bare.stem,
        display_name=base_name,
        sources=[str(bare), str(qualified)],
        rationale="athenaeum#1577 qualified-name scan: fixture",
        draft_merged_body=bare.read_text(encoding="utf-8"),
        confidence=0.5,
    )
    pm = list_pending_merges(merges)[-1]
    if decision == "approve":
        # Resolve without enacting the fold: the fixture only needs the
        # recorded decision, so rewrite the block rather than run the fold.
        from athenaeum.pending_merges import _rewrite_block_resolved, _split_blocks

        text = merges.read_text(encoding="utf-8")
        blocks = _split_blocks(text)
        out = []
        for b in blocks:
            if f'Merge: "{bare.stem}"' in b:
                b = _rewrite_block_resolved(
                    b, "approve", "ok", auto_applied=auto_applied, answered_at=answered_at
                )
            out.append(b)
        merges.write_text("# Pending Merges\n\n" + "\n\n---\n\n".join(out) + "\n", encoding="utf-8")
    else:
        result = resolve_merge(
            merges,
            pm["id"],
            "reject",
            note="not the same thing",
            wiki_root=wiki,
            answered_at=answered_at,
        )
        assert result["ok"], result
    if archive:
        ingest_resolved_merges(merges)


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    _page(root, "orca.md", "ORCA")
    _page(root, "orca-coaching.md", "ORCA (Coaching Awesomeness)")
    return root


def _open_ids(wiki: Path) -> list[str]:
    return (
        [m["id"] for m in list_pending_merges(wiki / "_pending_merges.md") if not m.get("resolved")]
        if (wiki / "_pending_merges.md").exists()
        else []
    )


def _advance_clock(monkeypatch: pytest.MonkeyPatch, days: float) -> None:
    """Make ``prior_rulings`` see *days* more elapsed than the wall clock."""
    import athenaeum.prior_rulings as pr

    real = pr.datetime

    class _Later(real):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + timedelta(days=days)

    monkeypatch.setattr(pr, "datetime", _Later)


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
RECENT = "2026-10-15T09:00:00Z"
OLD = "2026-01-01T09:00:00Z"


class TestRejectedPairNotReproposed:
    def test_baseline_without_ruling_is_queued(self, wiki: Path) -> None:
        counts = propose_qualified_name_merges(wiki)
        assert counts["queued"] == 1 and counts["ruled_out"] == 0

    def test_archived_human_rejection_suppresses_forever_by_default(self, wiki: Path) -> None:
        _rule(wiki, wiki / "orca.md", wiki / "orca-coaching.md", "reject", answered_at=OLD)
        counts = propose_qualified_name_merges(wiki)
        assert counts == {"splits": 1, "queued": 0, "unfoldable": 0, "ruled_out": 1}
        assert _open_ids(wiki) == []

    def test_unarchived_resolved_block_also_counts(self, wiki: Path) -> None:
        _rule(
            wiki,
            wiki / "orca.md",
            wiki / "orca-coaching.md",
            "reject",
            answered_at=RECENT,
            archive=False,
        )
        assert propose_qualified_name_merges(wiki)["ruled_out"] == 1

    def test_dry_run_writes_nothing_and_leaves_the_ruling_in_force(self, wiki: Path) -> None:
        _rule(wiki, wiki / "orca.md", wiki / "orca-coaching.md", "reject", answered_at=RECENT)
        merges = wiki / "_pending_merges.md"
        before = merges.read_bytes()

        dry = propose_qualified_name_merges(wiki, dry_run=True)

        assert dry["splits"] == 1 and dry["queued"] == 0
        assert merges.read_bytes() == before
        assert _open_ids(wiki) == []
        # A later real run is still governed by the ruling: the dry run neither
        # queued the pair nor consumed anything.
        real = propose_qualified_name_merges(wiki)
        assert real == {"splits": 1, "queued": 0, "unfoldable": 0, "ruled_out": 1}

    def test_dry_run_on_an_unruled_pair_queues_nothing(self, wiki: Path) -> None:
        # No ruling exists, so the only thing standing between this pair and
        # a queued block is the ``dry_run`` guard itself (athenaeum#2101).
        merges = wiki / "_pending_merges.md"
        assert not merges.exists()

        dry = propose_qualified_name_merges(wiki, dry_run=True)

        assert dry == {"splits": 1, "queued": 0, "unfoldable": 0, "ruled_out": 0}
        assert not merges.exists()
        assert _open_ids(wiki) == []
        # The same pair IS queued by a real run, so the dry run was the
        # difference, not an unqueueable fixture.
        assert propose_qualified_name_merges(wiki)["queued"] == 1


class TestCooldown:
    def test_reproposed_after_cooldown_with_earlier_rejection_named(self, wiki: Path) -> None:
        _rule(wiki, wiki / "orca.md", wiki / "orca-coaching.md", "reject", answered_at=OLD)
        # 2026-01-01 is far older than 30 days from the real clock.
        cfg = {"librarian": {"rejected_merge_cooldown_days": 30}}
        counts = propose_qualified_name_merges(wiki, config=cfg)
        assert counts["queued"] == 1 and counts["ruled_out"] == 0
        (block,) = _open_ids(wiki)
        rationale = next(
            m["rationale"]
            for m in list_pending_merges(wiki / "_pending_merges.md")
            if m["id"] == block
        )
        assert "previously rejected by a human on 2026-01-01" in rationale
        assert "not the same thing" in rationale

    def test_inside_cooldown_still_suppressed(self, wiki: Path) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _rule(wiki, wiki / "orca.md", wiki / "orca-coaching.md", "reject", answered_at=today)
        cfg = {"librarian": {"rejected_merge_cooldown_days": 30}}
        assert propose_qualified_name_merges(wiki, config=cfg)["ruled_out"] == 1

    @pytest.mark.parametrize("bad", [-1, True, "7", 1.5, None])
    def test_invalid_knob_means_forever(self, bad: object) -> None:
        from athenaeum.config import resolve_rejected_merge_cooldown_days

        assert (
            resolve_rejected_merge_cooldown_days(
                {"librarian": {"rejected_merge_cooldown_days": bad}}
            )
            == 0
        )

    def test_match_rulings_expiry_uses_now(self, wiki: Path) -> None:
        _rule(wiki, wiki / "orca.md", wiki / "orca-coaching.md", "reject", answered_at=RECENT)
        rulings = load_prior_rulings(wiki)
        srcs = [str(wiki / "orca.md"), str(wiki / "orca-coaching.md")]
        assert match_rulings(rulings, srcs, cooldown_days=7, now=NOW).in_force is not None
        later = datetime(2026, 11, 30, tzinfo=timezone.utc)
        m = match_rulings(rulings, srcs, cooldown_days=7, now=later)
        assert m.in_force is None and m.expired is not None


class TestApprovedPairNotReproposed:
    @pytest.mark.parametrize("auto", [False, True])
    def test_approved_pair_ignores_cooldown(self, wiki: Path, auto: bool) -> None:
        _rule(
            wiki,
            wiki / "orca.md",
            wiki / "orca-coaching.md",
            "approve",
            answered_at=OLD,
            auto_applied=auto,
        )
        cfg = {"librarian": {"rejected_merge_cooldown_days": 1}}
        counts = propose_qualified_name_merges(wiki, config=cfg)
        assert counts["queued"] == 0 and counts["ruled_out"] == 1


class TestBasePageVariant:
    def test_same_qualified_page_against_a_duplicate_base_is_the_same_ruling(
        self, wiki: Path
    ) -> None:
        _page(wiki, "orca-dup.md", "ORCA")  # second page of the same base name
        _rule(wiki, wiki / "orca.md", wiki / "orca-coaching.md", "reject", answered_at=RECENT)
        counts = propose_qualified_name_merges(wiki)
        # (orca, coaching) exact + (orca-dup, coaching) variant: both ruled out.
        assert counts["splits"] == 2
        assert counts["queued"] == 0 and counts["ruled_out"] == 2

    def test_variant_does_not_reach_a_different_base_name(self, wiki: Path) -> None:
        _page(wiki, "reach.md", "REACH")
        _page(wiki, "reach-q.md", "REACH (Scope)")
        _rule(wiki, wiki / "orca.md", wiki / "orca-coaching.md", "reject", answered_at=RECENT)
        counts = propose_qualified_name_merges(wiki)
        assert counts["queued"] == 1 and counts["ruled_out"] == 1

    def test_expired_variant_is_proposed_and_names_the_rejection(self, wiki: Path) -> None:
        _page(wiki, "orca-dup.md", "ORCA")
        _rule(wiki, wiki / "orca.md", wiki / "orca-coaching.md", "reject", answered_at=OLD)
        cfg = {"librarian": {"rejected_merge_cooldown_days": 30}}
        assert propose_qualified_name_merges(wiki, config=cfg)["queued"] == 2
        texts = [
            m["rationale"]
            for m in list_pending_merges(wiki / "_pending_merges.md")
            if not m.get("resolved")
        ]
        assert texts and all("previously rejected by a human" in t for t in texts)


class TestProvenance:
    def test_retired_blocks_are_not_rulings(self, wiki: Path) -> None:
        archive = wiki / "_pending_merges_archive.md"
        write_pending_merge(
            wiki / "_pending_merges.md",
            merge_target_name="orca",
            sources=[str(wiki / "orca.md"), str(wiki / "orca-coaching.md")],
            rationale="r",
            draft_merged_body="b",
            confidence=0.5,
        )
        text = (wiki / "_pending_merges.md").read_text(encoding="utf-8")
        archive.write_text(
            text.replace("# Pending Merges", "# Archived Merges")
            + "\n**Retired**: 2026-10-15T00:00:00Z — stale\n",
            encoding="utf-8",
        )
        (wiki / "_pending_merges.md").unlink()
        assert load_prior_rulings(wiki) == []

    def test_open_proposal_is_not_a_ruling(self, wiki: Path) -> None:
        propose_qualified_name_merges(wiki)
        assert load_prior_rulings(wiki) == []


class TestWikiDedupeHonoursRulings:
    def test_rejected_pair_is_skipped_before_any_comparison(self, tmp_path: Path) -> None:
        from athenaeum.runlock import RunLock
        from athenaeum.wiki_dedupe import propose_wiki_page_merges
        from tests.test_wiki_dedupe import (
            _BODY_A,
            _BODY_B,
            _COMPARATOR_CONFIG,
            _content_payload,
            _fake_embed,
            _fake_llm_client,
            _write_page,
        )

        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        a = _write_page(wiki, "venture-a.md", body=_BODY_A)
        b = _write_page(wiki, "venture-b.md", body=_BODY_B)
        _rule(wiki, a, b, "reject", answered_at=RECENT, base_name="venture-a")

        client = _fake_llm_client(_content_payload("equivalent"))
        lock = RunLock(root)
        with lock:
            results = propose_wiki_page_merges(
                root,
                config=_COMPARATOR_CONFIG,
                threshold=0.8,
                embedding_provider=_fake_embed,
                client=client,
                lock=lock,
            )
        assert results == []
        client.messages.create.assert_not_called()

    def test_pair_is_compared_again_once_cooldown_lapses(self, tmp_path: Path) -> None:
        from athenaeum.runlock import RunLock
        from athenaeum.wiki_dedupe import propose_wiki_page_merges
        from tests.test_wiki_dedupe import (
            _BODY_A,
            _BODY_B,
            _content_payload,
            _fake_embed,
            _fake_llm_client,
            _write_page,
        )

        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        a = _write_page(wiki, "venture-a.md", body=_BODY_A)
        b = _write_page(wiki, "venture-b.md", body=_BODY_B)
        _rule(wiki, a, b, "reject", answered_at=OLD, base_name="venture-a")
        cfg = {"librarian": {"comparator_enabled": True, "rejected_merge_cooldown_days": 30}}

        client = _fake_llm_client(_content_payload("equivalent"))
        lock = RunLock(root)
        with lock:
            results = propose_wiki_page_merges(
                root,
                config=cfg,
                threshold=0.8,
                embedding_provider=_fake_embed,
                client=client,
                lock=lock,
            )
        # Issue athenaeum#2107: the human's rejection also stamped
        # ``merge_rejected_with`` on the pages; once the cooldown lapsed that
        # stamp must not settle the pair at Gate 1, so Gate 2 runs.
        assert "merge_rejected_with" in a.read_text(encoding="utf-8")
        assert [r["verdict"] for r in results] == ["duplicate"]
        client.messages.create.assert_called()

        # Issue athenaeum#2094 AC2/AC3: the lapsed rejection is named in the
        # pair's attribution row (earlier merge id + the human's note), not
        # dropped. Fails if the proposer discards ``.expired``.
        from athenaeum.wiki_dedupe_attribution import read_attribution_report

        details = " ".join(r.detail for r in read_attribution_report(wiki))
        assert "Earlier ruling on this pair (" in details
        assert "not the same thing" in details
        assert "previously rejected by a human" in details
        assert "cooldown has since lapsed" in details


class TestQueuedDuplicateNamesEarlierRuling:
    """Issue athenaeum#2101: what a human adjudicates names the lapsed
    rejection, not only the attribution row -- the ``_pending_merges.md``
    block with auto-apply on, the ``_pending_questions.md`` item with it off
    (the live default).

    The rejection's own ``merge_rejected_with`` stamp stays on the pages
    (athenaeum#2107): the pair loop hides it from Gate 1 once the cooldown
    has lapsed, so these tests exercise the production path unstripped.
    """

    def _run(self, tmp_path: Path, *, auto_apply: bool) -> Path:
        from athenaeum.runlock import RunLock
        from athenaeum.wiki_dedupe import propose_wiki_page_merges
        from tests.test_wiki_dedupe import (
            _BODY_A,
            _BODY_B,
            _content_payload,
            _fake_embed,
            _fake_llm_client,
            _write_page,
        )

        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        a = _write_page(wiki, "venture-a.md", body=_BODY_A)
        b = _write_page(wiki, "venture-b.md", body=_BODY_B)
        _rule(wiki, a, b, "reject", answered_at=OLD, base_name="venture-a")
        # The rejection's own stamp stays on disk (issue athenaeum#2107).
        assert "merge_rejected_with" in a.read_text(encoding="utf-8")
        cfg = {
            "librarian": {
                "comparator_enabled": True,
                "rejected_merge_cooldown_days": 30,
                "verdict_ledger_enabled": True,
                "reversible_verdict_auto_apply_enabled": auto_apply,
            }
        }
        client = _fake_llm_client(_content_payload("equivalent"))
        lock = RunLock(root)
        with lock:
            results = propose_wiki_page_merges(
                root,
                config=cfg,
                threshold=0.8,
                embedding_provider=_fake_embed,
                client=client,
                lock=lock,
            )
        assert [r["verdict"] for r in results] == ["duplicate"]
        return wiki

    @staticmethod
    def _assert_names_ruling(text: str) -> None:
        assert "Earlier ruling on this pair (" in text
        assert "previously rejected by a human on 2026-01-01" in text
        assert "not the same thing" in text
        assert "the rejection cooldown has since lapsed." in text

    def test_auto_apply_on_block_rationale_names_the_lapsed_rejection(self, tmp_path: Path) -> None:
        wiki = self._run(tmp_path, auto_apply=True)
        rationales = [m["rationale"] for m in list_pending_merges(wiki / "_pending_merges.md")]
        assert len(rationales) == 1
        assert "athenaeum#716 reversible-verdict auto-apply" in rationales[0]
        self._assert_names_ruling(rationales[0])

    def test_auto_apply_off_question_item_names_the_lapsed_rejection(self, tmp_path: Path) -> None:
        wiki = self._run(tmp_path, auto_apply=False)
        assert not (wiki / "_pending_merges.md").read_text(encoding="utf-8").count("Rationale")
        questions = (wiki / "_pending_questions.md").read_text(encoding="utf-8")
        assert "Approve folding" in questions
        self._assert_names_ruling(questions)


class TestStampDoesNotOutliveCooldown:
    """Issue athenaeum#2107: the ``merge_rejected_with`` stamp a rejection
    leaves on the pages must follow the cooldown, not outlive it."""

    @staticmethod
    def _run(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        cooldown: int,
        days: float,
        seed=None,
    ):
        from athenaeum.runlock import RunLock
        from athenaeum.wiki_dedupe import propose_wiki_page_merges
        from tests.test_wiki_dedupe import (
            _BODY_A,
            _BODY_B,
            _content_payload,
            _fake_embed,
            _fake_llm_client,
            _write_page,
        )

        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        a = _write_page(wiki, "venture-a.md", body=_BODY_A)
        b = _write_page(wiki, "venture-b.md", body=_BODY_B)
        # Rejected through the real ``resolve_merge`` path, stamped "now".
        _rule(
            wiki,
            a,
            b,
            "reject",
            answered_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            base_name="venture-a",
        )
        if seed is not None:
            seed(root, a, b)
        _advance_clock(monkeypatch, days)
        cfg = {"librarian": {"comparator_enabled": True, "rejected_merge_cooldown_days": cooldown}}
        client = _fake_llm_client(_content_payload("equivalent"))
        lock = RunLock(root)
        with lock:
            results = propose_wiki_page_merges(
                root,
                config=cfg,
                threshold=0.8,
                embedding_provider=_fake_embed,
                client=client,
                lock=lock,
            )
        return results, client

    def test_lapsed_rejection_reaches_gate_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results, client = self._run(tmp_path, monkeypatch, cooldown=30, days=31)
        assert [r["verdict"] for r in results] == ["duplicate"]
        client.messages.create.assert_called()

    def test_memoized_gate_1_distinct_does_not_pin_a_lapsed_pair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``distinct`` the stamp already earned in the verdict ledger
        (e.g. from another comparator caller) is re-decided after the lapse."""
        from athenaeum.comparator import page_from_path, record_comparison
        from athenaeum.runlock import RunLock

        def seed(root: Path, a: Path, b: Path) -> None:
            lock = RunLock(root)
            with lock:
                rec = record_comparison(
                    root / "wiki",
                    page_from_path(a),
                    page_from_path(b),
                    client=None,
                    lock=lock,
                )
            assert rec["verdict"] == "distinct"

        results, client = self._run(tmp_path, monkeypatch, cooldown=30, days=31, seed=seed)
        assert [r["verdict"] for r in results] == ["duplicate"]
        client.messages.create.assert_called()

    def test_rejection_still_in_force_makes_no_llm_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results, client = self._run(tmp_path, monkeypatch, cooldown=30, days=5)
        assert results == []
        client.messages.create.assert_not_called()

    def test_zero_cooldown_never_lapses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results, client = self._run(tmp_path, monkeypatch, cooldown=0, days=3650)
        assert results == []
        client.messages.create.assert_not_called()

    def test_gate_1_still_settles_a_stamped_pair_distinct(self) -> None:
        """Gate 1 is unchanged: handed the stamped pages, no LLM call."""
        from unittest.mock import MagicMock

        from athenaeum.comparator import compare_pages, page_from_text

        def txt(n: str, o: str) -> str:
            return f"---\nname: {n}\ntype: project\nmerge_rejected_with:\n- {o}\n---\nfacts\n"

        client = MagicMock()
        out = compare_pages(
            page_from_text("a", txt("venture-a", "venture-b")),
            page_from_text("b", txt("venture-b", "venture-a")),
            client=client,
        )
        assert out.verdict == "distinct"
        assert out.separator == ["declared-merge-rejection"]
        client.messages.create.assert_not_called()

    def test_lift_only_drops_the_counterpart(self) -> None:
        from athenaeum.comparator import page_from_text
        from athenaeum.wiki_dedupe import _lift_lapsed_rejection

        a = page_from_text(
            "a", "---\nname: Venture A\nmerge_rejected_with:\n- venture-b\n- venture-c\n---\nx\n"
        )
        b = page_from_text("b", "---\nname: Venture B\nmerge_rejected_with:\n- venture-a\n---\ny\n")
        la, lb = _lift_lapsed_rejection(a, b)
        assert la.meta["merge_rejected_with"] == ["venture-c"]
        assert "merge_rejected_with" not in lb.meta
        assert la.text == a.text and lb.text == b.text
