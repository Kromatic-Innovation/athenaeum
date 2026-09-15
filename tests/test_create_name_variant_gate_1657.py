# SPDX-License-Identifier: Apache-2.0
"""Create-name gate: fold-or-mint a tier-1-matched-page name variant (issue athenaeum#1657).

Suppose tier 2 proposes a ``create`` whose name is a variant of a page tier 1
already matched for the same raw file — ``Name (qualifier)`` next to a
matched ``Name``. Before this issue, :func:`athenaeum.tiers.
gate_create_name_classifications` folds only an EXACT index hit
(``validate_create_name``'s uniqueness check); a variant name passes through
as a create unconditionally. This issue adds a second, independent path: a
deterministic name-structure pre-filter
(:func:`athenaeum.name_structure.is_create_name_variant_of_matched_page`)
that puts a genuine candidate in front of a model fold/mint decision.

One test class per acceptance criterion (issue athenaeum#1657's own numbering):

- ``TestIsCreateNameVariantOfMatchedPage`` — the deterministic pre-filter in
  isolation (both accepting shapes, and a negative control).
- ``TestPromptCarriesEvidence`` — AC1: the model prompt carries each
  candidate's uid, name, body size, and fold-size verdict.
- ``TestFoldMintUnparseable`` — AC2: fold/mint/unparseable, one test each.
- ``TestNoModelCallWithoutAVariant`` — AC3: zero client calls when there is
  no tier-1 match, or no create is a variant.
- (AC4 — the exact-hit collision behaviour of ``validate_create_name`` is
  unchanged — is proven by ``tests/test_create_name_gate_1173.py`` passing
  UNMODIFIED, not by a new test here.)
- ``TestMergedBodyThresholdHasAProductionCaller`` — AC5: the size verdict
  fed to the model is the REAL
  :func:`athenaeum.name_structure.merged_body_within_page_size_threshold`,
  not a stub, proven by actually flipping it via the real threshold config.
- ``TestSyncTransportWiring`` / ``TestBatchTransportWiring`` — both call
  sites (``librarian.py:2283`` / ``batch.py:1477``) end to end, proving the
  fold decision lands as a real page update with no new page minted, on
  both transports.

The embedder (:func:`athenaeum.search.embed_texts`) is stubbed to ``None`` in
every test that supplies both an ``index`` and an ``entity_type`` on a create
whose name does NOT exactly collide — the SAME "tests MUST inject a stub
embedder" convention ``tests/test_1615_similarity_wiring.py`` established —
so ``validate_create_name``'s unrelated issue athenaeum#1615 meaning-based
fallback degrades to ``no_match`` immediately rather than either reaching
real chromadb or, via a coincidental embedding hit, consuming the classify
client this file's tests reserve for the athenaeum#1657 fold/mint decision.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.models import ClassifiedEntity, EntityIndex, RawFile, TokenUsage
from athenaeum.name_structure import (
    collect_create_name_variant_candidates,
    is_create_name_variant_of_matched_page,
)
from athenaeum.tiers import gate_create_name_classifications

VALID_TYPES = ["person", "company", "concept", "reference", "project"]
VALID_ACCESS = ["open", "internal", "confidential", "personal"]


def _classified(
    name: str,
    *,
    is_new: bool = True,
    observations: str = "",
    entity_type: str = "project",
) -> ClassifiedEntity:
    return ClassifiedEntity(
        name=name,
        entity_type=entity_type,
        tags=[],
        access="internal",
        is_new=is_new,
        existing_uid=None if is_new else "existing-uid",
        observations=observations,
    )


def _raw(raw_dir: Path, content: str, filename: str = "note.md") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=raw_dir.name, timestamp="", uuid8="")


def _write_page(
    wiki: Path,
    filename: str,
    *,
    uid: str,
    name: str,
    type_: str | None = None,
    body: str = "Some content.\n",
) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"uid: {uid}", f"name: {name}"]
    if type_ is not None:
        lines.append(f"type: {type_}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path = wiki / filename
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _decision_response(text: str) -> MagicMock:
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    return response


def _fold_client(uid: str, reason: str = "same subject, later phase") -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = _decision_response(
        f'{{"decision": "fold", "uid": "{uid}", "reason": "{reason}"}}'
    )
    return client


def _mint_client(reason: str = "genuinely different scope") -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = _decision_response(
        f'{{"decision": "mint", "reason": "{reason}"}}'
    )
    return client


def _never_called_client(message: str) -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = AssertionError(message)
    return client


# ---------------------------------------------------------------------------
# The deterministic pre-filter, in isolation
# ---------------------------------------------------------------------------


class TestIsCreateNameVariantOfMatchedPage:
    def test_qualifier_base_match_is_a_variant(self) -> None:
        assert is_create_name_variant_of_matched_page(
            "Bracklemoor Transit Study (phase two)", "Bracklemoor Transit Study"
        )

    def test_qualifier_base_match_is_case_folded(self) -> None:
        assert is_create_name_variant_of_matched_page(
            "bracklemoor transit study (phase two)", "Bracklemoor Transit Study"
        )

    def test_whole_token_prefix_is_a_variant(self) -> None:
        assert is_create_name_variant_of_matched_page("Keelbridge", "Keelbridge Rollout")

    def test_whole_token_prefix_is_symmetric(self) -> None:
        assert is_create_name_variant_of_matched_page("Keelbridge Rollout", "Keelbridge")

    def test_partial_token_is_not_a_prefix_match(self) -> None:
        """`Keelbridgeish` shares no TOKEN boundary with `Keelbridge` -- not
        a variant by the prefix rule, and it carries no qualifier either."""
        assert not is_create_name_variant_of_matched_page("Keelbridgeish", "Keelbridge")

    def test_unrelated_names_are_not_variants(self) -> None:
        assert not is_create_name_variant_of_matched_page("Acme Corp", "Widget Co")

    def test_group_qualifier_still_matches_via_token_prefix(self) -> None:
        """``split_qualifier``'s own negative control (a GROUP qualifier,
        e.g. "(Team)", is not a facet) means the QUALIFIER-BASE path does
        not fire for "Heart (Team)" vs "Heart" -- but this function has a
        second, independent path: "Heart" is still a whole-token prefix of
        "Heart (Team)", so the pre-filter correctly still proposes it as a
        candidate (it only PROPOSES; the model decides fold vs mint, and a
        genuine staffing-arrangement page is exactly the shape that should
        come back "mint")."""
        assert is_create_name_variant_of_matched_page("Heart (Team)", "Heart")


# ---------------------------------------------------------------------------
# AC1 -- the prompt carries uid/name/body-size/verdict evidence
# ---------------------------------------------------------------------------


class TestPromptCarriesEvidence:
    def test_prompt_carries_candidate_evidence_fields(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(
            wiki,
            "bracklemoor.md",
            uid="bt-uid-1",
            name="Bracklemoor Transit Study",
            type_="project",
            body="A short study of transit options.\n",
        )
        index = EntityIndex(wiki)
        matched = [("bracklemoor transit study", "bt-uid-1", wiki / "bracklemoor.md")]
        c = _classified(
            "Bracklemoor Transit Study (phase two)",
            observations="Phase two extends the survey.",
        )
        client = _fold_client("bt-uid-1")

        outcome = gate_create_name_classifications(
            [c],
            "raw/ref.md",
            "Phase two extends the survey.",
            index=index,
            client=client,
            tier1_matched_entities=matched,
            variant_candidate_builder=collect_create_name_variant_candidates,
        )

        assert outcome.folded == ("Bracklemoor Transit Study (phase two)",)
        client.messages.create.assert_called_once()
        _args, kwargs = client.messages.create.call_args
        prompt = kwargs["messages"][0]["content"]
        assert "bt-uid-1" in prompt
        assert "Bracklemoor Transit Study" in prompt
        assert "project" in prompt
        assert str(len("A short study of transit options.\n")) in prompt
        assert "within_page_size_threshold_if_folded: True" in prompt


# ---------------------------------------------------------------------------
# AC2 -- fold / mint / unparseable, one test each
# ---------------------------------------------------------------------------


class TestFoldMintUnparseable:
    def _matched_and_index(self, tmp_path: Path) -> tuple[EntityIndex, list[tuple[str, str, Path]]]:
        wiki = tmp_path / "wiki"
        path = _write_page(
            wiki,
            "keelbridge.md",
            uid="kb-uid-1",
            name="Keelbridge",
            type_="project",
            body="Keelbridge is a rollout programme.\n",
        )
        return EntityIndex(wiki), [("keelbridge", "kb-uid-1", path)]

    def test_fold_rewrites_to_update_with_existing_uid(self, tmp_path: Path) -> None:
        index, matched = self._matched_and_index(tmp_path)
        c = _classified("Keelbridge Rollout", observations="More rollout detail.")
        client = _fold_client("kb-uid-1")

        outcome = gate_create_name_classifications(
            [c],
            "raw/ref.md",
            "More rollout detail.",
            index=index,
            client=client,
            tier1_matched_entities=matched,
            variant_candidate_builder=collect_create_name_variant_candidates,
        )

        assert outcome.folded == ("Keelbridge Rollout",)
        assert len(outcome.kept) == 1
        kept = outcome.kept[0]
        assert kept.is_new is False
        assert kept.existing_uid == "kb-uid-1"
        assert kept.name == "Keelbridge Rollout"
        assert outcome.rejected == ()
        assert outcome.escalations == ()

    def test_mint_keeps_the_create_unchanged(self, tmp_path: Path) -> None:
        index, matched = self._matched_and_index(tmp_path)
        c = _classified("Keelbridge Rollout", observations="A narrower, separate thing.")
        client = _mint_client()

        outcome = gate_create_name_classifications(
            [c],
            "raw/ref.md",
            "A narrower, separate thing.",
            index=index,
            client=client,
            tier1_matched_entities=matched,
            variant_candidate_builder=collect_create_name_variant_candidates,
        )

        assert outcome.folded == ()
        assert outcome.kept == [c]

    def test_unparseable_reply_keeps_the_create_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        index, matched = self._matched_and_index(tmp_path)
        c = _classified("Keelbridge Rollout", observations="Some detail.")
        client = MagicMock()
        client.messages.create.return_value = _decision_response("not json at all")

        with caplog.at_level(logging.WARNING, logger="athenaeum.tiers"):
            outcome = gate_create_name_classifications(
                [c],
                "raw/ref.md",
                "Some detail.",
                index=index,
                client=client,
                tier1_matched_entities=matched,
                variant_candidate_builder=collect_create_name_variant_candidates,
            )

        assert outcome.folded == ()
        assert outcome.kept == [c]
        assert any("tier3-create-name-variant" in rec.message for rec in caplog.records)

    def test_no_client_available_keeps_the_create_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        index, matched = self._matched_and_index(tmp_path)
        c = _classified("Keelbridge Rollout", observations="Some detail.")

        with caplog.at_level(logging.WARNING, logger="athenaeum.tiers"):
            outcome = gate_create_name_classifications(
                [c],
                "raw/ref.md",
                "Some detail.",
                index=index,
                client=None,
                tier1_matched_entities=matched,
                variant_candidate_builder=collect_create_name_variant_candidates,
            )

        assert outcome.folded == ()
        assert outcome.kept == [c]
        assert any("tier3-create-name-variant" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# AC3 -- no model call at all without a variant
# ---------------------------------------------------------------------------


class TestNoModelCallWithoutAVariant:
    def test_no_tier1_match_at_all(self) -> None:
        c = _classified("Anything Goes", observations="text")
        client = _never_called_client("no tier1_matched_entities -> zero model calls")

        outcome = gate_create_name_classifications(
            [c], "raw/ref.md", "text", client=client, tier1_matched_entities=None
        )

        assert outcome.kept == [c]
        client.messages.create.assert_not_called()

    def test_empty_tier1_match_list(self) -> None:
        c = _classified("Anything Goes", observations="text")
        client = _never_called_client("empty tier1_matched_entities -> zero model calls")

        outcome = gate_create_name_classifications(
            [c], "raw/ref.md", "text", client=client, tier1_matched_entities=[]
        )

        assert outcome.kept == [c]
        client.messages.create.assert_not_called()

    def test_tier1_match_present_but_not_a_variant(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        path = _write_page(wiki, "acme.md", uid="a1", name="Acme", type_="company")
        c = _classified("Totally Unrelated Widget Co", observations="text", entity_type="company")
        client = _never_called_client("no candidate variant -> zero model calls")

        outcome = gate_create_name_classifications(
            [c],
            "raw/ref.md",
            "text",
            index=EntityIndex(wiki),
            client=client,
            tier1_matched_entities=[("acme", "a1", path)],
            variant_candidate_builder=collect_create_name_variant_candidates,
        )

        assert outcome.kept == [c]
        client.messages.create.assert_not_called()

    def test_no_builder_supplied_even_with_a_real_variant(self, tmp_path: Path) -> None:
        """``tier1_matched_entities`` alone is not enough -- the caller must
        ALSO inject ``variant_candidate_builder`` (issue athenaeum#1657's
        dependency-injection seam, required because ``tiers.py`` must never
        import ``athenaeum.name_structure`` -- see
        ``gate_create_name_classifications``'s own docstring). Omitting the
        builder is exactly what a byte-identical pre-athenaeum#1657 caller
        does, so it must stay a full no-op even when a real variant is
        present in ``tier1_matched_entities``."""
        wiki = tmp_path / "wiki"
        path = _write_page(wiki, "keelbridge.md", uid="kb-uid-1", name="Keelbridge")
        c = _classified("Keelbridge Rollout", observations="text")
        client = _never_called_client("no builder injected -> zero model calls")

        outcome = gate_create_name_classifications(
            [c],
            "raw/ref.md",
            "text",
            index=EntityIndex(wiki),
            client=client,
            tier1_matched_entities=[("keelbridge", "kb-uid-1", path)],
        )

        assert outcome.kept == [c]
        client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# AC5 -- merged_body_within_page_size_threshold gets a REAL production caller
# ---------------------------------------------------------------------------


class TestMergedBodyThresholdHasAProductionCaller:
    def test_oversize_candidate_reports_false_verdict_in_the_prompt(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        path = _write_page(
            wiki,
            "keelbridge.md",
            uid="kb-uid-1",
            name="Keelbridge",
            type_="project",
            body="x" * 500,
        )
        index = EntityIndex(wiki)
        c = _classified("Keelbridge Rollout", observations="y" * 500)
        client = _mint_client()
        # A tiny configured threshold forces the REAL
        # merged_body_within_page_size_threshold to report False -- proving
        # this isn't a stubbed/hardcoded verdict.
        config = {"librarian": {"page_size_threshold_chars": 10}}

        gate_create_name_classifications(
            [c],
            "raw/ref.md",
            "y" * 500,
            config,
            index=index,
            client=client,
            tier1_matched_entities=[("keelbridge", "kb-uid-1", path)],
            variant_candidate_builder=collect_create_name_variant_candidates,
        )

        _args, kwargs = client.messages.create.call_args
        prompt = kwargs["messages"][0]["content"]
        assert "within_page_size_threshold_if_folded: False" in prompt


# ---------------------------------------------------------------------------
# End-to-end wiring -- both transports
# ---------------------------------------------------------------------------


class TestSyncTransportWiring:
    def test_variant_create_folds_into_matched_page_no_new_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import process_one

        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        _write_page(
            wiki,
            "bracklemoor.md",
            uid="bt-uid-1",
            name="Bracklemoor Transit Study",
            type_="project",
            body="A study of transit options.\n",
        )
        raw = _raw(
            knowledge / "raw" / "producer",
            "Notes from the Bracklemoor transit study (phase two) kickoff.\n",
        )

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [
                _classified(
                    "Bracklemoor Transit Study (phase two)",
                    observations="Phase two extends the survey.",
                )
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)
        monkeypatch.setattr("athenaeum.entity_resolution.embed_texts", lambda *_a, **_k: None)

        def _fake_tier3_merge(action, existing_body, source_ref, client, **_kwargs):
            return existing_body + "\n\nPhase two extends the survey.\n", None

        monkeypatch.setattr("athenaeum.tiers.tier3_merge", _fake_tier3_merge)

        classify_client = _fold_client("bt-uid-1")
        write_client = _never_called_client(
            "tier3_merge is monkeypatched — the real write client must never be called"
        )

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=TokenUsage(),
            write_client=write_client,
        )

        page_names = sorted(p.stem for p in wiki.glob("*.md") if not p.name.startswith("_"))
        assert page_names == ["bracklemoor"]
        assert result.created == []
        # 2, not 1: the multi-token key "Bracklemoor Transit Study" is
        # ALSO tier-1 mention-matched directly in the raw content (that
        # mechanism is unconditional and predates this issue), so this one
        # raw file drives two separate update actions against the SAME
        # uid -- the pre-existing mention-match, and this issue's new
        # fold. Both touch one page; no second page is minted, which is
        # the invariant this test exists to prove.
        assert sorted(result.updated) == ["bt-uid-1", "bt-uid-1"]
        assert "Phase two extends the survey." in (wiki / "bracklemoor.md").read_text(
            encoding="utf-8"
        )


class TestBatchTransportWiring:
    def test_variant_create_folds_into_matched_page_no_new_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.batch import process_batch_run

        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        _write_page(
            wiki,
            "bracklemoor.md",
            uid="bt-uid-1",
            name="Bracklemoor Transit Study",
            type_="project",
            body="A study of transit options.\n",
        )
        raw = _raw(
            knowledge / "raw" / "producer",
            "Notes from the Bracklemoor transit study (phase two) kickoff.\n",
        )

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [
                _classified(
                    "Bracklemoor Transit Study (phase two)",
                    observations="Phase two extends the survey.",
                )
            ]

        monkeypatch.setattr("athenaeum.batch.tier2_classify", _fake_tier2_classify)
        monkeypatch.setattr("athenaeum.entity_resolution.embed_texts", lambda *_a, **_k: None)

        def _fake_tier3_merge(action, existing_body, source_ref, client, **_kwargs):
            return existing_body + "\n\nPhase two extends the survey.\n", None

        monkeypatch.setattr("athenaeum.batch.tier3_merge", _fake_tier3_merge)

        classify_client = _fold_client("bt-uid-1")
        write_client = _never_called_client(
            "tier3_merge is monkeypatched — the real write client must never be called"
        )

        result = process_batch_run(
            [raw],
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            usage=TokenUsage(),
            config=None,
            max_api_calls=100,
            write_client=write_client,
            batch_classify=False,
            batch_write=False,
        )

        page_names = sorted(p.stem for p in wiki.glob("*.md") if not p.name.startswith("_"))
        assert page_names == ["bracklemoor"]
        assert result.created == 0
        # 2, not 1 -- see the sync-transport test's identical comment: the
        # multi-token mention match and this issue's fold both touch the
        # SAME one page.
        assert result.updated == 2
        assert result.escalated == 0
        assert "Phase two extends the survey." in (wiki / "bracklemoor.md").read_text(
            encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Parity -- batch.py's private duplicate vs. name_structure.py's canonical
# ---------------------------------------------------------------------------


class TestBatchDuplicateParity:
    """``batch.py`` cannot import :mod:`athenaeum.name_structure` (see
    ``gate_create_name_classifications``'s docstring for the import-cycle
    reason), so it carries its own small, deliberately-duplicated
    equivalents of :func:`~athenaeum.name_structure.
    is_create_name_variant_of_matched_page` and
    :func:`~athenaeum.name_structure.collect_create_name_variant_candidates`.
    This class is what keeps the two implementations from silently drifting
    apart -- update both in the same commit if either changes, and this
    parity check will catch a mismatch.
    """

    @pytest.mark.parametrize(
        ("create_name", "matched_name"),
        [
            ("Bracklemoor Transit Study (phase two)", "Bracklemoor Transit Study"),
            ("bracklemoor transit study (phase two)", "Bracklemoor Transit Study"),
            ("Keelbridge", "Keelbridge Rollout"),
            ("Keelbridge Rollout", "Keelbridge"),
            ("Keelbridgeish", "Keelbridge"),
            ("Acme Corp", "Widget Co"),
            ("Heart (Team)", "Heart"),
            ("Fujitsu (2nd Contract)", "Fujitsu"),
        ],
    )
    def test_variant_predicate_matches_the_canonical_implementation(
        self, create_name: str, matched_name: str
    ) -> None:
        from athenaeum.batch import _batch_is_create_name_variant

        assert _batch_is_create_name_variant(create_name, matched_name) == (
            is_create_name_variant_of_matched_page(create_name, matched_name)
        )

    def test_candidate_collection_matches_the_canonical_implementation(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.batch import _batch_collect_create_name_variant_candidates

        wiki = tmp_path / "wiki"
        path = _write_page(
            wiki,
            "keelbridge.md",
            uid="kb-uid-1",
            name="Keelbridge",
            type_="project",
            body="Keelbridge is a rollout programme.\n",
        )
        matched = [("keelbridge", "kb-uid-1", path)]
        observation = "Some new detail about the rollout."

        real = collect_create_name_variant_candidates(
            "Keelbridge Rollout", matched, observation, None
        )
        duplicate = _batch_collect_create_name_variant_candidates(
            "Keelbridge Rollout", matched, observation, None
        )

        assert [tuple(c.__dict__.values()) for c in real] == [
            tuple(c.__dict__.values()) for c in duplicate
        ]

    def test_candidate_collection_threshold_parity_when_oversize(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.batch import _batch_collect_create_name_variant_candidates

        wiki = tmp_path / "wiki"
        path = _write_page(
            wiki,
            "keelbridge.md",
            uid="kb-uid-1",
            name="Keelbridge",
            type_="project",
            body="x" * 500,
        )
        matched = [("keelbridge", "kb-uid-1", path)]
        observation = "y" * 500
        config = {"librarian": {"page_size_threshold_chars": 10}}

        real = collect_create_name_variant_candidates(
            "Keelbridge Rollout", matched, observation, config
        )
        duplicate = _batch_collect_create_name_variant_candidates(
            "Keelbridge Rollout", matched, observation, config
        )

        assert len(real) == 1 and len(duplicate) == 1
        assert real[0].within_threshold is False
        assert duplicate[0].within_threshold is False
