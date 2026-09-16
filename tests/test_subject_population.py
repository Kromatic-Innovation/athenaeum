# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1714 — meaning-based ``subject`` population, dry-run by default.

Exercises :mod:`athenaeum.subject_population` directly with injected
``embedder``/``confirm`` stubs, per :mod:`athenaeum.entity_resolution`'s own
"tests must inject a stub, never rely on real chromadb" contract (mirrors
``tests/test_1615_entity_resolution.py``). Each ``TestAC*`` class name below
maps 1:1 to one of the issue's eight acceptance criteria.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.entity_resolution import Ambiguous, Match, NoMatch, SubjectPage
from athenaeum.models import parse_frontmatter
from athenaeum.subject_population import (
    UNDETERMINABLE,
    SubjectRegistry,
    build_subject_population_report,
    build_tier2_confirm,
    insert_subject,
    run_subject_population,
)


def _page(
    root: Path,
    filename: str,
    *,
    uid: str,
    name: str,
    type_: str = "concept",
    body: str = "Body text.\n",
) -> Path:
    frontmatter = f"uid: '{uid}'\ntype: {type_}\nname: {name}"
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


def _stub_embedder(vectors_by_text: dict[str, list[float]]):
    def _embed(texts: list[str]) -> list[list[float]]:
        return [vectors_by_text[t] for t in texts]

    return _embed


class TestAC1SameReferentDifferentNamesGetSameSubject:
    def test_two_different_names_different_uids_one_person_share_a_subject(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "a-bryan.md", uid="u-bryan", name="Bryan Went")
        _page(wiki, "b-bryan.md", uid="u-bryan-emoji", name="Bryan Went \U0001f981")

        embed = _stub_embedder(
            {
                "bryan went": [1.0, 0.0],
            }
        )

        def confirm(cand: SubjectPage, top: list[tuple[SubjectPage, float]]):
            # Second page compared against the first (now-minted) page.
            assert [p.uid for p, _ in top] == ["u-bryan"]
            return Match("u-bryan")

        report = build_subject_population_report(wiki, embedder=embed, confirm=confirm)

        decisions = {d.uid: d for d in report.decisions}
        assert decisions["u-bryan"].reason == "minted"
        assert decisions["u-bryan-emoji"].reason == "matched"
        assert decisions["u-bryan-emoji"].subject == decisions["u-bryan"].subject
        assert decisions["u-bryan-emoji"].matched_uid == "u-bryan"


class TestAC2SameNameDifferentReferentsGetDifferentSubjects:
    def test_two_same_named_pages_two_people_get_different_subjects(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "a-jordan.md", uid="u-jordan-1", name="Jordan Lee")
        _page(wiki, "b-jordan.md", uid="u-jordan-2", name="Jordan Lee")

        # Identical normalized name -> embedder alone would look like a
        # perfect match; the confirmer is the one that must say "no,
        # different people" for this to prove the pipeline never shortcuts
        # through exact-string name equality (AC4).
        embed = _stub_embedder({"jordan lee": [1.0, 0.0]})

        def confirm(cand: SubjectPage, top: list[tuple[SubjectPage, float]]):
            return NoMatch()

        report = build_subject_population_report(wiki, embedder=embed, confirm=confirm)

        decisions = {d.uid: d for d in report.decisions}
        assert decisions["u-jordan-1"].reason == "minted"
        assert decisions["u-jordan-2"].reason == "minted"
        assert decisions["u-jordan-1"].subject != decisions["u-jordan-2"].subject


class TestAC3NoSubjectDerivedFromUid:
    def test_minted_and_matched_subjects_never_equal_the_page_uid(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "a.md", uid="uid-aaa-111", name="Alpha")
        _page(wiki, "b.md", uid="uid-bbb-222", name="Alpha Prime")

        embed = _stub_embedder({"alpha": [1.0, 0.0], "alpha prime": [1.0, 0.0]})
        report = build_subject_population_report(
            wiki, embedder=embed, confirm=lambda cand, top: Match(top[0][0].uid)
        )
        for decision in report.decisions:
            assert decision.subject != decision.uid
            assert not decision.subject.startswith("uid")

    def test_module_source_never_assigns_subject_from_uid(self) -> None:
        """Grep-style regression control (per the issue's own suggestion):
        the module must never write a bare ``uid`` value straight into a
        ``subject`` field — every assignment must route through the
        registry (``.mint()`` / matched pool lookup) or the literal
        ``undeterminable``.
        """
        import athenaeum.subject_population as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "subject=uid" not in source.replace(" ", "")
        assert "subject = uid" not in source


class TestAC4NoSubjectDerivedFromExactNameEquality:
    def test_confirm_is_always_consulted_never_bypassed_by_name_match(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "a.md", uid="u1", name="Taylor Kim")
        _page(wiki, "b.md", uid="u2", name="Taylor Kim")

        calls: list[str] = []

        def confirm(cand: SubjectPage, top: list[tuple[SubjectPage, float]]):
            calls.append(cand.uid)
            return NoMatch()

        embed = _stub_embedder({"taylor kim": [1.0, 0.0]})
        build_subject_population_report(wiki, embedder=embed, confirm=confirm)
        # The second (identically-named) page must have gone through the
        # confirmer -- a bare exact-string lookup would never call it.
        assert calls == ["u2"]


class TestAC5DegradedNeverMintsNeverGuesses:
    def _two_pages(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "a.md", uid="u1", name="Existing Page")
        _page(wiki, "b.md", uid="u2", name="Existing Page Two")
        return wiki

    def test_embedder_unavailable_is_undeterminable_not_minted(self, tmp_path: Path) -> None:
        wiki = self._two_pages(tmp_path)
        report = build_subject_population_report(wiki, embedder=lambda texts: None)
        decisions = {d.uid: d for d in report.decisions}
        # u1 has an empty pool (genuine first-of-kind) -> mint is correct.
        assert decisions["u1"].reason == "minted"
        # u2 compares against a non-empty pool with a broken embedder ->
        # must be undeterminable, never minted, never a guess.
        assert decisions["u2"].reason == "undeterminable-degraded"
        assert decisions["u2"].subject == UNDETERMINABLE

    def test_absent_confirmer_with_high_similarity_is_undeterminable(
        self, tmp_path: Path
    ) -> None:
        wiki = self._two_pages(tmp_path)
        embed = _stub_embedder(
            {"existing page": [1.0, 0.0], "existing page two": [1.0, 0.0]}
        )
        report = build_subject_population_report(wiki, embedder=embed, confirm=None)
        decisions = {d.uid: d for d in report.decisions}
        assert decisions["u1"].reason == "minted"
        assert decisions["u2"].reason == "undeterminable-degraded"
        assert decisions["u2"].subject == UNDETERMINABLE

    def test_confirmer_raising_is_undeterminable(self, tmp_path: Path) -> None:
        wiki = self._two_pages(tmp_path)
        embed = _stub_embedder(
            {"existing page": [1.0, 0.0], "existing page two": [1.0, 0.0]}
        )

        def confirm(cand, top):
            raise RuntimeError("boom")

        report = build_subject_population_report(wiki, embedder=embed, confirm=confirm)
        decisions = {d.uid: d for d in report.decisions}
        assert decisions["u2"].reason == "undeterminable-degraded"
        assert decisions["u2"].subject == UNDETERMINABLE

    def test_confirmer_uid_outside_candidates_is_undeterminable(self, tmp_path: Path) -> None:
        wiki = self._two_pages(tmp_path)
        embed = _stub_embedder(
            {"existing page": [1.0, 0.0], "existing page two": [1.0, 0.0]}
        )
        report = build_subject_population_report(
            wiki, embedder=embed, confirm=lambda cand, top: Match("does-not-exist")
        )
        decisions = {d.uid: d for d in report.decisions}
        assert decisions["u2"].reason == "undeterminable-degraded"
        assert decisions["u2"].subject == UNDETERMINABLE


class TestAC6AmbiguousRecordsUndeterminableAndPendingQuestion:
    def test_ambiguous_result_is_undeterminable_no_forced_pick(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "a.md", uid="u1", name="Casey Rivera")
        _page(wiki, "b.md", uid="u2", name="Casey Rivera Jr")

        embed = _stub_embedder({"casey rivera": [1.0, 0.0], "casey rivera jr": [1.0, 0.0]})
        report = build_subject_population_report(
            wiki, embedder=embed, confirm=lambda cand, top: Ambiguous(("u1",))
        )
        decisions = {d.uid: d for d in report.decisions}
        assert decisions["u2"].reason == "undeterminable-ambiguous"
        assert decisions["u2"].subject == UNDETERMINABLE

    def test_apply_raises_a_pending_question_for_ambiguous_pages(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "a.md", uid="u1", name="Casey Rivera")
        _page(wiki, "b.md", uid="u2", name="Casey Rivera Jr")
        pending_path = tmp_path / "_pending_questions.md"

        embed = _stub_embedder({"casey rivera": [1.0, 0.0], "casey rivera jr": [1.0, 0.0]})
        report = run_subject_population(
            wiki,
            apply=True,
            embedder=embed,
            confirm=lambda cand, top: Ambiguous(("u1",)),
            pending_path=pending_path,
        )
        assert pending_path.exists()
        text = pending_path.read_text(encoding="utf-8")
        assert "Casey Rivera Jr" in text

        b_text = (wiki / "b.md").read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(b_text)
        assert meta.get("subject") == UNDETERMINABLE
        assert [d.reason for d in report.decisions if d.uid == "u2"] == ["undeterminable-ambiguous"]


class TestAC7DryRunReportsCountsWithoutWriting:
    def test_dry_run_reports_counts_and_writes_nothing(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        a = _page(wiki, "a.md", uid="u1", name="Alpha")
        b = _page(wiki, "b.md", uid="u2", name="Alpha Twin")
        before_a, before_b = a.read_bytes(), b.read_bytes()

        embed = _stub_embedder({"alpha": [1.0, 0.0], "alpha twin": [1.0, 0.0]})
        report = run_subject_population(
            wiki, embedder=embed, confirm=lambda cand, top: Match(top[0][0].uid)
        )

        counts = report.counts()
        assert counts["scanned"] == 2
        assert counts["minted_new_subject"] == 1
        assert counts["matched_existing_subject"] == 1
        assert counts["undeterminable"] == 0

        assert a.read_bytes() == before_a
        assert b.read_bytes() == before_b
        assert not (wiki / "_subject_registry.json").exists()

    def test_apply_true_writes_pages_and_registry(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        a = _page(wiki, "a.md", uid="u1", name="Alpha")
        b = _page(wiki, "b.md", uid="u2", name="Alpha Twin")

        embed = _stub_embedder({"alpha": [1.0, 0.0], "alpha twin": [1.0, 0.0]})
        run_subject_population(
            wiki, apply=True, embedder=embed, confirm=lambda cand, top: Match(top[0][0].uid)
        )

        meta_a, _ = parse_frontmatter(a.read_text(encoding="utf-8"))
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        assert meta_a["subject"] == meta_b["subject"]
        assert meta_a["subject"] != "u1"

        registry_path = wiki / "_subject_registry.json"
        assert registry_path.exists()
        loaded = SubjectRegistry.load(registry_path)
        assert sorted(loaded.subjects[meta_a["subject"]]) == ["u1", "u2"]


class TestAC8ScopedToComparatorEligibleOnly:
    def test_ineligible_type_is_never_scanned(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "eligible.md", uid="u1", name="Eligible Page", type_="concept")
        # "person" is not in DEDUPE_CANDIDATE_TYPES -- must be invisible to
        # this pass entirely, not merely left undeterminable.
        _page(wiki, "ineligible.md", uid="u2", name="Ineligible Page", type_="person")

        report = build_subject_population_report(wiki, embedder=lambda texts: None)
        touched_uids = {d.uid for d in report.decisions}
        assert touched_uids == {"u1"}
        assert report.scanned == 1

    def test_archived_page_is_excluded_even_if_type_matches(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "eligible.md", uid="u1", name="Eligible Page")
        archived = wiki / "archived.md"
        archived.write_text(
            "---\nuid: 'u2'\ntype: concept\nname: Archived Page\ntags: [archived]\n---\nBody.\n",
            encoding="utf-8",
        )

        report = build_subject_population_report(wiki, embedder=lambda texts: None)
        touched_uids = {d.uid for d in report.decisions}
        assert touched_uids == {"u1"}


class TestTypeScoping:
    def test_same_name_different_types_never_compared(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _page(wiki, "a.md", uid="u1", name="Ambiguous Name", type_="concept")
        _page(wiki, "b.md", uid="u2", name="Ambiguous Name", type_="reference")

        def confirm(cand, top):
            raise AssertionError("cross-type candidates must never reach the confirmer")

        embed = _stub_embedder({"ambiguous name": [1.0, 0.0]})
        report = build_subject_population_report(wiki, embedder=embed, confirm=confirm)
        decisions = {d.uid: d for d in report.decisions}
        assert decisions["u1"].reason == "minted"
        assert decisions["u2"].reason == "minted"
        assert decisions["u1"].subject != decisions["u2"].subject


class TestSubjectRegistry:
    def test_mint_is_monotonic_and_round_trips_through_save_load(self, tmp_path: Path) -> None:
        registry = SubjectRegistry()
        first = registry.mint("u1")
        second = registry.mint("u2")
        assert first != second

        path = tmp_path / "_subject_registry.json"
        registry.save(path)
        loaded = SubjectRegistry.load(path)
        assert loaded.subjects == registry.subjects
        assert loaded.next_id == registry.next_id

    def test_load_missing_file_returns_fresh_registry(self, tmp_path: Path) -> None:
        registry = SubjectRegistry.load(tmp_path / "does-not-exist.json")
        assert registry.subjects == {}
        assert registry.next_id == 1


class TestInsertSubject:
    def test_no_frontmatter_returns_none(self) -> None:
        assert insert_subject("no frontmatter here\n", "subject-000001") is None

    def test_inserts_without_disturbing_other_bytes(self) -> None:
        text = "---\nuid: 'u1'\ntype: concept\nname: Alpha\n---\nBody.\n"
        updated = insert_subject(text, "subject-000001")
        assert updated is not None
        assert "subject: subject-000001" in updated
        meta, body = parse_frontmatter(updated)
        assert meta["subject"] == "subject-000001"
        assert meta["uid"] == "u1"
        assert body == "Body.\n"


class TestBuildTier2Confirm:
    def test_delegates_to_tiers_private_confirmer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import athenaeum.tiers as tiers_mod

        calls: list[tuple] = []

        def fake_confirm(candidate, top, *, client, config=None, usage=None):
            calls.append((candidate, top, client, config, usage))
            return Match("u1")

        monkeypatch.setattr(tiers_mod, "_tier2_confirm_same_subject", fake_confirm)

        sentinel_client = object()
        confirm = build_tier2_confirm(sentinel_client, config={"x": 1})
        cand = SubjectPage(name="Alpha")
        top = ((SubjectPage(uid="u1", name="Alpha"), 0.99),)
        result = confirm(cand, top)

        assert result == Match("u1")
        assert calls == [(cand, top, sentinel_client, {"x": 1}, None)]
