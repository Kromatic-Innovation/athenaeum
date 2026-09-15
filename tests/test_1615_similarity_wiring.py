# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1615 end-to-end wiring: the meaning-based resolver, through
:func:`athenaeum.librarian.process_one` (same "end-to-end" precedent set by
``tests/test_create_name_gate_1173.py::TestSyncTransportWiring`` for the
athenaeum#1170 exact-collision disambiguation this issue extends).

**RED-state provenance (AC1):** ``tests/test_1597_ac1_duplicate_person_page.py``
(unmodified by this change, already on ``develop``) has a strict ``xfail``
proving the baseline this issue fixes: a clean mention ("Bill Lennan")
against an existing page whose ``name:`` carries decorative characters
("Bill Lennan \U0001f4ad") mints a SECOND page today — because both the
tier-0 registry consult and tier1 require an exact literal-substring match.
This file's ``TestMeaningBasedFallbackPreventsDuplicate`` proves the fix:
the same shape, through the real ``validate_create_name`` /
``gate_create_name_classifications`` wiring this issue adds, folds into the
one existing page instead.

The embedder (:func:`athenaeum.search.embed_texts`, chromadb-backed) and the
tier-2 confirmation LLM call are both stubbed — see
:func:`athenaeum.search.embed_texts`'s own docstring ("Tests MUST inject a
stub embedder — never rely on real chromadb in the test suite.") — via
``monkeypatch.setattr("athenaeum.entity_resolution.embed_texts", ...)`` (the
module-global lookup :func:`athenaeum.entity_resolution.resolve_same_subject`
does at call time, NOT a default-parameter capture — see that function's
implementation) and the classify client's ``messages.create`` return value,
matching ``tier3_merge`` being monkeypatched wholesale rather than
constructing a real merge-response payload (the same shortcut
``test_create_name_gate_1173.py``'s athenaeum#1170 collision tests take).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.librarian import process_one
from athenaeum.models import ClassifiedEntity, EntityIndex, RawFile

VALID_TYPES = ["person", "company", "concept", "reference"]
VALID_ACCESS = ["open", "internal", "confidential", "personal"]


def _classified(
    name: str,
    *,
    is_new: bool = True,
    observations: str = "",
    entity_type: str = "person",
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


def _stub_embed_identical_vectors(texts: list[str]) -> list[list[float]]:
    """Every normalized name embeds to the same point — deterministic,
    always-above-threshold similarity, without a real embedding model."""
    return [[1.0, 0.0] for _ in texts]


def _confirm_response(text: str) -> MagicMock:
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    return response


class TestMeaningBasedFallbackPreventsDuplicate:
    """AC1: a clean mention of a decoratively-named existing person resolves
    to that ONE page instead of minting a second."""

    def test_decorated_name_mismatch_no_longer_duplicates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        _write_page(
            wiki,
            "bill-lennan.md",
            uid="aaaaaaaa",
            name="Bill Lennan \U0001f4ad",
            type_="person",
            body="Founder of 40 Percent Better.\n",
        )
        raw = _raw(
            knowledge / "raw" / "producer",
            "Caught up with Bill Lennan today -- launching a coaching program.\n",
        )

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [
                _classified(
                    "Bill Lennan",
                    observations="Launching a coaching program.",
                )
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)
        monkeypatch.setattr(
            "athenaeum.entity_resolution.embed_texts", _stub_embed_identical_vectors
        )

        def _fake_tier3_merge(action, existing_body, source_ref, client, **_kwargs):
            return existing_body + "\n\nLaunching a coaching program.\n", None

        monkeypatch.setattr("athenaeum.tiers.tier3_merge", _fake_tier3_merge)

        classify_client = MagicMock()
        classify_client.messages.create.return_value = _confirm_response("MATCH: aaaaaaaa")
        write_client = MagicMock()
        write_client.messages.create.side_effect = AssertionError(
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
            write_client=write_client,
        )

        page_names = sorted(p.stem for p in wiki.glob("*.md") if not p.name.startswith("_"))
        assert page_names == ["bill-lennan"], "expected exactly ONE person page, not a duplicate"
        assert result.created == []
        assert result.updated == ["aaaaaaaa"]
        assert result.escalated == []
        assert "Launching a coaching program." in (wiki / "bill-lennan.md").read_text(
            encoding="utf-8"
        )
        # The confirmation call used the classify client (issue athenaeum#1615's
        # "an LLM call is already paid on this path" — reuse, not a new client).
        classify_client.messages.create.assert_called_once()


class TestCounterFixtureSameNameDifferentPeopleStaysTwo:
    """AC2: two different people who happen to share a name must NOT merge,
    even when the embedding candidate-generation stage surfaces the other
    as a candidate — the tier-2 confirmer is expected to decline."""

    def test_same_name_different_person_confirmer_declines_stays_two_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        _write_page(
            wiki,
            "chris-martin-coldplay.md",
            uid="cccccccc",
            name="Chris Martin \U0001f3a4",  # decorated -- exact lookup must miss
            type_="person",
            body="Lead singer of Coldplay.\n",
        )
        raw = _raw(
            knowledge / "raw" / "producer",
            "Chris Martin (the plumber, not the musician) fixed the sink today.\n",
        )

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [
                _classified(
                    "Chris Martin",
                    observations="Fixed the sink today; works as a plumber.",
                )
            ]

        create_response = MagicMock()
        create_response.content = [
            MagicMock(
                text=(
                    "# Chris Martin\n\n## Notes\n\n"
                    "- 2026-09-15: Fixed the sink today; works as a plumber.\n"
                )
            )
        ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)
        monkeypatch.setattr(
            "athenaeum.entity_resolution.embed_texts", _stub_embed_identical_vectors
        )

        classify_client = MagicMock()
        # Confirmation call declines -- these are different people despite
        # the identical normalized name.
        classify_client.messages.create.return_value = _confirm_response("NO_MATCH")
        write_client = MagicMock()
        write_client.messages.create.return_value = create_response

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            write_client=write_client,
        )

        page_names = sorted(p.stem for p in wiki.glob("*.md") if not p.name.startswith("_"))
        assert len(page_names) == 2, "two different people sharing a name must stay two pages"
        assert result.escalated == []


class TestDegradedEmbedderNeverMerges:
    """AC3: with the embedder stubbed to return None, the librarian creates
    a new page exactly as today. No merge happens, no LLM confirmation call
    is even attempted."""

    def test_embedder_unavailable_creates_new_page_no_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        _write_page(
            wiki,
            "bill-lennan.md",
            uid="aaaaaaaa",
            name="Bill Lennan \U0001f4ad",
            type_="person",
            body="Founder of 40 Percent Better.\n",
        )
        raw = _raw(
            knowledge / "raw" / "producer",
            "Caught up with Bill Lennan today.\n",
        )

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [_classified("Bill Lennan", observations="Launching a program.")]

        create_response = MagicMock()
        create_response.content = [
            MagicMock(text="# Bill Lennan\n\n## Notes\n\n- 2026-09-15: Launching a program.\n")
        ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)
        monkeypatch.setattr("athenaeum.entity_resolution.embed_texts", lambda texts: None)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "embedder degraded to no_match before any confirmation call is possible"
        )
        write_client = MagicMock()
        write_client.messages.create.return_value = create_response

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            write_client=write_client,
        )

        page_names = sorted(p.stem for p in wiki.glob("*.md") if not p.name.startswith("_"))
        assert len(page_names) == 2, "degraded embedder must create, exactly like today"
        assert result.escalated == []
        classify_client.messages.create.assert_not_called()


class TestAmbiguousResultEscalatesNeverMerges:
    """AC4: a resolver result of `ambiguous` produces a `name_collision`
    escalation and no merge."""

    def test_ambiguous_confirmation_escalates_no_page_created_no_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        _write_page(wiki, "bill-lennan-a.md", uid="aaaaaaaa", name="Bill Lennan A", type_="person")
        _write_page(wiki, "bill-lennan-b.md", uid="bbbbbbbb", name="Bill Lennan B", type_="person")
        raw = _raw(
            knowledge / "raw" / "producer",
            "Bill Lennan called about the project today.\n",
        )

        def _fake_tier2_classify(*_args: object, **_kwargs: object) -> list[ClassifiedEntity]:
            return [_classified("Bill Lennan", observations="Called about the project.")]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)
        monkeypatch.setattr(
            "athenaeum.entity_resolution.embed_texts", _stub_embed_identical_vectors
        )

        classify_client = MagicMock()
        classify_client.messages.create.return_value = _confirm_response(
            "AMBIGUOUS: aaaaaaaa,bbbbbbbb"
        )
        write_client = MagicMock()
        write_client.messages.create.side_effect = AssertionError(
            "an ambiguous resolution must never reach tier-3 create or merge"
        )

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            write_client=write_client,
        )

        page_names = sorted(p.stem for p in wiki.glob("*.md") if not p.name.startswith("_"))
        assert page_names == ["bill-lennan-a", "bill-lennan-b"], "no page created or merged"
        assert result.created == []
        assert result.updated == []
        assert len(result.escalated) == 1
        assert result.escalated[0].conflict_type == "name_collision"
        pending = (wiki / "_pending_questions.md").read_text(encoding="utf-8")
        assert "Bill Lennan" in pending
