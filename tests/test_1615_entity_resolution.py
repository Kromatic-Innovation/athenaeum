# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1615 — pure unit tests for the meaning-based name resolver.

:mod:`athenaeum.entity_resolution` has no knowledge of LLMs/clients; these
tests exercise it directly with injected ``embedder``/``confirm`` stubs, per
the module's own contract (and ``athenaeum.search.embed_texts``'s own
docstring: "Tests MUST inject a stub embedder — never rely on real chromadb
in the test suite."). The full librarian-pipeline wiring test lives in
``test_1615_ac1_duplicate_person_page_similarity.py``.
"""

from __future__ import annotations

import pytest

from athenaeum.entity_resolution import (
    Ambiguous,
    Match,
    NoMatch,
    SubjectPage,
    normalize_name,
    resolve_same_subject,
)


class TestNormalizeName:
    def test_strips_emoji_and_casefolds(self) -> None:
        assert normalize_name("Bryan Went \U0001f981") == normalize_name("bryan went")

    def test_collapses_whitespace(self) -> None:
        assert normalize_name("Bryan   Went") == "bryan went"

    def test_strips_decorative_symbols(self) -> None:
        assert normalize_name("☆ Bryan Went ☆") == "bryan went"


def _stub_embedder(vectors_by_text: dict[str, list[float]]):
    def _embed(texts: list[str]) -> list[list[float]]:
        return [vectors_by_text[t] for t in texts]

    return _embed


class TestResolveSameSubjectNoMatch:
    def test_no_existing_pages_short_circuits_without_calling_embedder(self) -> None:
        def _boom(_texts: list[str]) -> None:
            raise AssertionError("embedder must not be called with no candidates")

        result = resolve_same_subject("Bryan Went", [], embedder=_boom)
        assert isinstance(result, NoMatch)

    def test_embedder_returning_none_degrades_to_no_match(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("WARNING")
        existing = [SubjectPage(uid="u1", name="Bryan Went", type="person")]
        result = resolve_same_subject("Bryan Went", existing, embedder=lambda texts: None)
        assert isinstance(result, NoMatch)
        assert "embedder-unavailable" in caplog.text

    def test_low_similarity_never_reaches_confirmer(self) -> None:
        def _confirm(_cand, _top):
            raise AssertionError("confirm must not be called below threshold")

        embed = _stub_embedder(
            {
                "bryan went": [1.0, 0.0],
                "someone else entirely": [0.0, 1.0],
            }
        )
        existing = [SubjectPage(uid="u1", name="Someone Else Entirely", type="person")]
        result = resolve_same_subject("Bryan Went", existing, embedder=embed, confirm=_confirm)
        assert isinstance(result, NoMatch)

    def test_high_similarity_without_confirmer_degrades_to_no_match(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("WARNING")
        embed = _stub_embedder(
            {
                "bryan went": [1.0, 0.0],
                "bryan went ": [1.0, 0.0],
            }
        )
        existing = [SubjectPage(uid="u1", name="Bryan Went ", type="person")]
        result = resolve_same_subject("Bryan Went", existing, embedder=embed)
        assert isinstance(result, NoMatch)
        assert "no-confirmer" in caplog.text


class TestResolveSameSubjectMatch:
    def test_confident_match_confirmed_by_confirmer(self) -> None:
        embed = _stub_embedder({"bryan went": [1.0, 0.0]})
        existing = [SubjectPage(uid="u1", name="Bryan Went \U0001f981", type="person")]

        def _confirm(cand, top):
            assert cand.name == "Bryan Went"
            assert [p.uid for p, _ in top] == ["u1"]
            return Match("u1")

        result = resolve_same_subject("Bryan Went", existing, embedder=embed, confirm=_confirm)
        assert result == Match("u1")

    def test_confirmer_returning_uid_outside_candidates_degrades_to_no_match(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("WARNING")
        existing = [SubjectPage(uid="u1", name="Bryan Went", type="person")]
        result = resolve_same_subject(
            "Bryan Went",
            existing,
            embedder=lambda texts: [[1.0, 0.0] for _ in texts],
            confirm=lambda cand, top: Match("does-not-exist"),
        )
        assert isinstance(result, NoMatch)
        assert "confirm-uid-outside-candidates" in caplog.text

    def test_confirmer_raising_degrades_to_no_match(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("WARNING")
        existing = [SubjectPage(uid="u1", name="Bryan Went", type="person")]

        def _confirm(cand, top):
            raise RuntimeError("boom")

        result = resolve_same_subject(
            "Bryan Went",
            existing,
            embedder=lambda texts: [[1.0, 0.0] for _ in texts],
            confirm=_confirm,
        )
        assert isinstance(result, NoMatch)
        assert "confirm-error" in caplog.text


class TestResolveSameSubjectAmbiguous:
    def test_ambiguous_confirmer_result_passes_through(self) -> None:
        embed = _stub_embedder(
            {
                "bill lennan": [1.0, 0.0],
                "bill lennan a": [0.99, 0.01],
                "bill lennan b": [0.98, 0.02],
            }
        )
        existing = [
            SubjectPage(uid="u1", name="Bill Lennan A", type="person"),
            SubjectPage(uid="u2", name="Bill Lennan B", type="person"),
        ]
        result = resolve_same_subject(
            "Bill Lennan",
            existing,
            embedder=embed,
            confirm=lambda cand, top: Ambiguous(("u1", "u2")),
        )
        assert result == Ambiguous(("u1", "u2"))


class TestResolveSameSubjectCounterFixture:
    def test_same_name_different_people_the_confirmer_declines(self) -> None:
        """The counter-fixture from the issue's plan step 1: two different
        people sharing a name must NOT be auto-merged. A realistic confirmer
        (not a stub that always confirms) declines, and the resolver must
        return exactly what the confirmer says."""
        embed = _stub_embedder({"chris martin": [1.0, 0.0]})
        existing = [SubjectPage(uid="u1", name="Chris Martin", type="person")]
        result = resolve_same_subject(
            "Chris Martin",
            existing,
            embedder=embed,
            confirm=lambda cand, top: NoMatch(),
        )
        assert isinstance(result, NoMatch)


class TestResolveSameSubjectPagePairForm:
    """AC5: the resolver accepts a pair of wiki pages, not only a name
    against the index — the call shape athenaeum#1244 needs."""

    def test_candidate_as_subject_page(self) -> None:
        candidate = SubjectPage(
            name="Bryan Went",
            uid="new-page-uid",
            type="person",
            body="Works on the widget project.",
        )
        existing = [
            SubjectPage(
                uid="existing-uid",
                name="Bryan Went \U0001f981",
                type="person",
                body="Founder, widget project.",
            )
        ]

        seen_candidate_body = {}

        def _confirm(cand, top):
            seen_candidate_body["body"] = cand.body
            assert cand.uid == "new-page-uid"
            return Match("existing-uid")

        result = resolve_same_subject(
            candidate,
            existing,
            embedder=lambda texts: [[1.0, 0.0] for _ in texts],
            confirm=_confirm,
        )
        assert result == Match("existing-uid")
        assert seen_candidate_body["body"] == "Works on the widget project."
