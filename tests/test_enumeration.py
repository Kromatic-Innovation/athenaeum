# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.enumeration — the generalized ENUMERATION primitive (athenaeum#965).

Covers every base acceptance criterion plus the three code-impacting AC
amendments (output-field selection + PII gating, pagination, backend). Uses
a synthetic fixture wiki with a custom entity class (``widget``) as well as
``person`` pages, matching the public-repository fixture convention already
used by ``tests/test_type_filter.py`` / ``tests/test_entity_schema.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.enumeration import (
    DEFAULT_LIMIT,
    EnumerationResult,
    FieldPredicate,
    enumerate_entities,
    is_pii_gated_field,
)
from athenaeum.search import FTS5Backend


def _write_page(
    wiki: Path,
    filename: str,
    *,
    uid: str,
    page_type: str,
    name: str,
    extra_lines: list[str] | None = None,
) -> None:
    lines = [f"uid: {uid}", f"type: {page_type}", f"name: {name}"]
    lines.extend(extra_lines or [])
    (wiki / filename).write_text(
        "---\n" + "\n".join(lines) + "\n---\n\nBody.\n", encoding="utf-8"
    )


@pytest.fixture
def enum_wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "_schema").mkdir()
    # AC 3: a declared class absent from athenaeum's own source (KNOWN_TYPES).
    (wiki / "_schema" / "types.md").write_text(
        "| Type |\n|---|\n| person |\n| widget |\n"
    )

    _write_page(
        wiki,
        "alice.md",
        uid="u-alice",
        page_type="person",
        name="Alice Example",
        extra_lines=[
            "current_company: Acme Corp",
            "current_title: Staff Engineer",
            "tags: [warm, tier:warm-a]",
            "warm_score: 10",
            "do_not_email: true",
            "google_contact_kromatic: gc-alice",
        ],
    )
    _write_page(
        wiki,
        "bob.md",
        uid="u-bob",
        page_type="person",
        name="Bob Example",
        extra_lines=[
            "current_company: Other Co",
            "linkedin_company_at_connect: Acme Subsidiary",
            "current_title: Manager",
            "tags: [cold]",
            "warm_score: 5",
            "do_not_email: false",
        ],
    )
    _write_page(
        wiki,
        "carol.md",
        uid="u-carol",
        page_type="person",
        name="Carol Example",
        extra_lines=[
            "current_company: Zzz Inc",
            "current_title: Director of Engineering",
            "warm_score: 5",
            "audience: [ops]",
        ],
    )
    # Untyped / other-typed page must never leak into a `person` enumeration.
    _write_page(wiki, "widget-1.md", uid="u-w1", page_type="widget", name="Widget One")
    return wiki


def _cache(tmp_path: Path) -> Path:
    return tmp_path / "cache"


class TestBaseEnumeration:
    def test_no_predicates_returns_every_page_of_type(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", limit=0
        )
        assert {h["uid"] for h in result.hits} == {"u-alice", "u-bob", "u-carol"}

    def test_does_not_route_through_query_text_ranking(
        self, enum_wiki: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Enumeration has no query-string parameter at all, and must never
        # call the ranked `.query()` path — the exact path that returns `[]`
        # for empty/no query text (search.py's own documented behavior).
        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("enumerate_entities must not call FTS5Backend.query()")

        monkeypatch.setattr(FTS5Backend, "query", _boom)
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", limit=0
        )
        assert len(result.hits) == 3

    def test_custom_declared_class_absent_from_source_is_enumerable(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        # "widget" is declared in this deployment's types.md but is not one
        # of athenaeum's own KNOWN_TYPES — proves types/fields are derived
        # from the deployment's schema, not hardcoded.
        result = enumerate_entities(enum_wiki, _cache(tmp_path), entity_type="widget")
        assert [h["uid"] for h in result.hits] == ["u-w1"]

    def test_unrecognized_type_does_not_raise_and_names_known_classes(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="not-a-real-class"
        )
        assert result.hits == ()
        assert "person" in result.known_classes
        assert "widget" in result.known_classes

    def test_hits_carry_uid_type_name(self, enum_wiki: Path, tmp_path: Path) -> None:
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="widget"
        )
        assert result.hits[0]["uid"] == "u-w1"
        assert result.hits[0]["type"] == "widget"
        assert result.hits[0]["name"] == "Widget One"


class TestFieldPredicates:
    def test_exact_match(self, enum_wiki: Path, tmp_path: Path) -> None:
        pred = FieldPredicate(fields=("current_title",), kind="eq", value="Manager")
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", predicates=[pred]
        )
        assert [h["uid"] for h in result.hits] == ["u-bob"]

    def test_substring_match_case_insensitive(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        pred = FieldPredicate(
            fields=("current_title",), kind="substring", value="engineer"
        )
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", predicates=[pred]
        )
        # Matches both "Staff Engineer" and "Director of Engineering".
        assert {h["uid"] for h in result.hits} == {"u-alice", "u-carol"}

    def test_regex_match_case_insensitive(self, enum_wiki: Path, tmp_path: Path) -> None:
        pred = FieldPredicate(
            fields=("current_title",), kind="regex", value=r"^director\b"
        )
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", predicates=[pred]
        )
        assert [h["uid"] for h in result.hits] == ["u-carol"]

    def test_repeated_predicates_combine_with_and(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        preds = [
            FieldPredicate(
                fields=("current_title",), kind="substring", value="engineer"
            ),
            FieldPredicate(fields=("current_company",), kind="eq", value="Acme Corp"),
        ]
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", predicates=preds
        )
        # Carol also matches "engineer" but not "current_company: Acme Corp".
        assert [h["uid"] for h in result.hits] == ["u-alice"]

    def test_fallback_field_matches_via_second_field(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        # Bob's `current_company` is "Other Co" — only the fallback field
        # `linkedin_company_at_connect` carries "Acme".
        pred = FieldPredicate(
            fields=("current_company", "linkedin_company_at_connect"),
            kind="substring",
            value="acme",
        )
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", predicates=[pred]
        )
        assert {h["uid"] for h in result.hits} == {"u-alice", "u-bob"}


class TestSortingAndTiebreak:
    def test_descending_by_default(self, enum_wiki: Path, tmp_path: Path) -> None:
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", sort_key="warm_score"
        )
        scores = [h["uid"] for h in result.hits]
        # Alice (10) first; Bob/Carol tie at 5, broken by uid ascending.
        assert scores == ["u-alice", "u-bob", "u-carol"]

    def test_equal_sort_values_are_stable_across_calls(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        first = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", sort_key="warm_score"
        )
        second = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", sort_key="warm_score"
        )
        assert [h["uid"] for h in first.hits] == [h["uid"] for h in second.hits]
        # Bob and Carol are tied at warm_score=5; documented tiebreak is
        # uid-ascending regardless of the primary sort direction.
        tied = [h["uid"] for h in first.hits if h["uid"] in ("u-bob", "u-carol")]
        assert tied == ["u-bob", "u-carol"]


class TestLimitAndUnlimited:
    def test_default_limit_applied(self, enum_wiki: Path, tmp_path: Path) -> None:
        result = enumerate_entities(enum_wiki, _cache(tmp_path), entity_type="person")
        assert len(result.hits) <= DEFAULT_LIMIT

    def test_explicit_limit_honoured(self, enum_wiki: Path, tmp_path: Path) -> None:
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", limit=1
        )
        assert len(result.hits) == 1
        assert result.next_cursor is not None

    def test_limit_zero_is_unlimited(self, enum_wiki: Path, tmp_path: Path) -> None:
        result = enumerate_entities(
            enum_wiki, _cache(tmp_path), entity_type="person", limit=0
        )
        assert len(result.hits) == 3
        assert result.next_cursor is None


class TestAudienceScoping:
    def test_restricted_caller_never_sees_unauthorized_page(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        # Carol is scoped to `audience: [ops]`; Alice/Bob carry no audience
        # grant at all, so a restricted caller without the `ops` role must
        # see NEITHER of them either (fail-closed default: untagged pages
        # are owner-only).
        result = enumerate_entities(
            enum_wiki,
            _cache(tmp_path),
            entity_type="person",
            caller_audience={"someone-else"},
        )
        assert result.hits == ()

    def test_restricted_caller_with_matching_role_sees_that_page(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        result = enumerate_entities(
            enum_wiki,
            _cache(tmp_path),
            entity_type="person",
            caller_audience={"ops"},
        )
        assert [h["uid"] for h in result.hits] == ["u-carol"]


class TestPiiGatedFields:
    def test_is_pii_gated_field(self) -> None:
        # athenaeum#1122: `do_not_email` (and its reason/date) are NOT gated —
        # the operator ruled they are not PII. `google_contact_*` still is.
        assert is_pii_gated_field("do_not_email") is False
        assert is_pii_gated_field("do_not_email_reason") is False
        assert is_pii_gated_field("do_not_email_date") is False
        assert is_pii_gated_field("google_contact_kromatic") is True
        assert is_pii_gated_field("google_contact") is True
        assert is_pii_gated_field("current_company") is False

    def test_do_not_email_as_output_field_does_not_require_with_pii(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        result = enumerate_entities(
            enum_wiki,
            _cache(tmp_path),
            entity_type="person",
            fields=["do_not_email"],
        )
        by_uid = {h["uid"]: h for h in result.hits}
        assert by_uid["u-alice"]["do_not_email"] is True
        assert by_uid["u-bob"]["do_not_email"] is False
        # Carol never set the field at all — present with a null, not omitted.
        assert by_uid["u-carol"]["do_not_email"] is None

    def test_pii_field_as_output_field_requires_with_pii(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="with_pii"):
            enumerate_entities(
                enum_wiki,
                _cache(tmp_path),
                entity_type="person",
                fields=["google_contact_kromatic"],
            )

    def test_pii_field_as_predicate_requires_with_pii(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        pred = FieldPredicate(fields=("google_contact_kromatic",), kind="eq", value="x")
        with pytest.raises(ValueError, match="with_pii"):
            enumerate_entities(
                enum_wiki,
                _cache(tmp_path),
                entity_type="person",
                predicates=[pred],
            )

    def test_with_pii_true_still_allows_do_not_email_output_field(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        # with_pii=True must remain harmless for an ungated field — it is
        # simply no longer required for this one.
        result = enumerate_entities(
            enum_wiki,
            _cache(tmp_path),
            entity_type="person",
            fields=["do_not_email"],
            with_pii=True,
        )
        by_uid = {h["uid"]: h for h in result.hits}
        assert by_uid["u-alice"]["do_not_email"] is True
        assert by_uid["u-bob"]["do_not_email"] is False
        assert by_uid["u-carol"]["do_not_email"] is None

    def test_do_not_email_not_equal_true_predicate_without_with_pii(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        # AC: the ne-predicate's own motivating example
        # (`do_not_email != true`) must succeed WITHOUT with_pii=True and
        # return the expected uids (athenaeum#1122).
        pred = FieldPredicate(
            fields=("do_not_email",), kind="eq", value="true", negate=True
        )
        result = enumerate_entities(
            enum_wiki,
            _cache(tmp_path),
            entity_type="person",
            predicates=[pred],
        )
        uids = {h["uid"] for h in result.hits}
        assert "u-alice" not in uids  # do_not_email: true — excluded
        assert {"u-bob", "u-carol"} <= uids

    def test_google_contact_prefix_gating_unwidened(self, enum_wiki: Path, tmp_path: Path) -> None:
        # Pin that athenaeum#1122 did not touch the prefix set: both the bare
        # prefix and a concrete google_contact_* field stay gated.
        assert is_pii_gated_field("google_contact") is True
        assert is_pii_gated_field("google_contact_kromatic") is True
        with pytest.raises(ValueError, match="with_pii"):
            enumerate_entities(
                enum_wiki,
                _cache(tmp_path),
                entity_type="person",
                fields=["google_contact_kromatic"],
            )
        result = enumerate_entities(
            enum_wiki,
            _cache(tmp_path),
            entity_type="person",
            fields=["google_contact_kromatic"],
            with_pii=True,
        )
        by_uid = {h["uid"]: h for h in result.hits}
        assert by_uid["u-alice"]["google_contact_kromatic"] == "gc-alice"

    def test_do_not_email_predicate_still_respects_audience_scoping(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        # AC: audience scoping (athenaeum#538, fail-closed) is unaffected by
        # ungating do_not_email — a restricted caller_audience must never
        # receive a page it may not read, predicate or not.
        pred = FieldPredicate(
            fields=("do_not_email",), kind="eq", value="true", negate=True
        )
        result = enumerate_entities(
            enum_wiki,
            _cache(tmp_path),
            entity_type="person",
            predicates=[pred],
            caller_audience={"someone-else"},
        )
        # Bob (do_not_email: false) would match the predicate, but carries no
        # audience grant, so a caller restricted to "someone-else" must still
        # see nothing.
        assert result.hits == ()

        # Positive half, so a vacuous pass (predicate matched nothing at all)
        # can't masquerade as scoping working: with the SAME predicate but a
        # caller_audience that DOES grant access, Carol (audience: [ops],
        # do_not_email unset so `!= true` matches) comes back. That proves
        # the empty result above came from audience scoping, not the
        # predicate.
        scoped_result = enumerate_entities(
            enum_wiki,
            _cache(tmp_path),
            entity_type="person",
            predicates=[pred],
            caller_audience={"ops"},
        )
        assert [h["uid"] for h in scoped_result.hits] == ["u-carol"]


class TestPagination:
    def test_pagination_covers_every_page_exactly_once(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            result = enumerate_entities(
                enum_wiki,
                _cache(tmp_path),
                entity_type="person",
                sort_key="warm_score",
                limit=1,
                cursor=cursor,
            )
            seen.extend(h["uid"] for h in result.hits)
            cursor = result.next_cursor
            if cursor is None:
                break
        assert seen == ["u-alice", "u-bob", "u-carol"]

    def test_cursor_mismatched_query_shape_raises(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        first = enumerate_entities(
            enum_wiki,
            _cache(tmp_path),
            entity_type="person",
            sort_key="warm_score",
            limit=1,
        )
        assert first.next_cursor is not None
        with pytest.raises(ValueError, match="does not match"):
            enumerate_entities(
                enum_wiki,
                _cache(tmp_path),
                entity_type="person",
                sort_key="name",  # different sort key than the cursor was minted under
                limit=1,
                cursor=first.next_cursor,
            )


class TestEnumerationResultShape:
    def test_known_classes_empty_on_ordinary_call(
        self, enum_wiki: Path, tmp_path: Path
    ) -> None:
        result = enumerate_entities(enum_wiki, _cache(tmp_path), entity_type="person")
        assert isinstance(result, EnumerationResult)
        assert result.known_classes == ()
