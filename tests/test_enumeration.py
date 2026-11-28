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
        with pytest.raises(ValueError, match="include_sensitive"):
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
        with pytest.raises(ValueError, match="include_sensitive"):
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
        with pytest.raises(ValueError, match="include_sensitive"):
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


class TestSplitChildrenHiddenByDefault:
    """Issue athenaeum#2538 AC7: a page whose relation is ``via: split`` is
    excluded from enumeration by default; a flag includes it. Only the
    ``split`` via is hidden -- attach/fold/manual stay listed."""

    @pytest.fixture
    def split_wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text("| Type |\n|---|\n| company |\n")
        _write_page(wiki, "parent.md", uid="company-acme", page_type="company", name="Acme")
        # Real shape the now-retired `tiers.py:_build_split_child_pages`
        # used to write (deleted by issue athenaeum#2550): only the legacy
        # `split-from` row, no top-level `parent:` key.
        _write_page(
            wiki,
            "split-child.md",
            uid="company-acme-split",
            page_type="company",
            name="Acme Split",
            extra_lines=[
                "related:",
                "  - uid: company-acme",
                "    role: split-from",
            ],
        )
        _write_page(
            wiki,
            "attach-child.md",
            uid="company-acme-attach",
            page_type="company",
            name="Acme Attach",
            extra_lines=["parent: company-acme"],
        )
        _write_page(
            wiki,
            "fold-child.md",
            uid="company-acme-fold",
            page_type="company",
            name="Acme Fold",
            extra_lines=[
                "related:",
                "  - uid: company-acme",
                "    role: child",
                "    via: fold",
            ],
        )
        _write_page(
            wiki,
            "manual-child.md",
            uid="company-acme-manual",
            page_type="company",
            name="Acme Manual",
            extra_lines=[
                "related:",
                "  - uid: company-acme",
                "    role: child",
                "    via: manual",
            ],
        )
        return wiki

    def test_split_child_excluded_by_default(
        self, split_wiki: Path, tmp_path: Path
    ) -> None:
        result = enumerate_entities(
            split_wiki, _cache(tmp_path), entity_type="company", limit=0
        )
        uids = {h["uid"] for h in result.hits}
        assert "company-acme-split" not in uids
        # attach/fold/manual are ALWAYS listed, regardless of the default.
        assert uids == {
            "company-acme",
            "company-acme-attach",
            "company-acme-fold",
            "company-acme-manual",
        }

    def test_include_split_flag_includes_it(
        self, split_wiki: Path, tmp_path: Path
    ) -> None:
        result = enumerate_entities(
            split_wiki,
            _cache(tmp_path),
            entity_type="company",
            limit=0,
            include_split=True,
        )
        uids = {h["uid"] for h in result.hits}
        assert "company-acme-split" in uids
        assert len(uids) == 5


class TestPastValidUntilIsListed:
    """Issue athenaeum#3321 (operator ruling: option 1). An entity page does
    not expire from enumeration: a page whose ``valid_until`` is past is
    listed by default, because on a company or person page that date usually
    records when a relationship ended. ``recall``'s validity window
    (athenaeum#308) is unchanged: the index still leaves the page out, and
    enumeration reads it from disk instead. A ``bucket: daily`` page keeps
    its existing behaviour (listed; the decay sweep, not enumeration,
    removes it). Deprecated / ``pii: true`` / ``embedded: false`` pages stay
    out."""

    @pytest.fixture
    def ended_wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "_schema").mkdir()
        (wiki / "_schema" / "types.md").write_text(
            "| Type |\n|---|\n| company |\n| person |\n"
        )
        _write_page(
            wiki,
            "current-client.md",
            uid="company-current",
            page_type="company",
            name="Current Client Co",
            extra_lines=["former_client: true"],
        )
        _write_page(
            wiki,
            "ended-client.md",
            uid="company-ended",
            page_type="company",
            name="Ended Client Co",
            extra_lines=["former_client: true", "valid_until: '2018-12-31'"],
        )
        _write_page(
            wiki,
            "ended-other.md",
            uid="company-ended-other",
            page_type="company",
            name="Ended Other Co",
            extra_lines=["valid_until: '2014-06-30'"],
        )
        _write_page(
            wiki,
            "future-bound.md",
            uid="company-future",
            page_type="company",
            name="Future Bound Co",
            extra_lines=["valid_until: '2999-01-01'"],
        )
        _write_page(
            wiki,
            "ended-deprecated.md",
            uid="company-deprecated",
            page_type="company",
            name="Deprecated Co",
            extra_lines=["valid_until: '2018-12-31'", "deprecated: true"],
        )
        _write_page(
            wiki,
            "ended-pii.md",
            uid="company-pii",
            page_type="company",
            name="Pii Co",
            extra_lines=["valid_until: '2018-12-31'", "pii: true"],
        )
        _write_page(
            wiki,
            "ended-not-embedded.md",
            uid="company-not-embedded",
            page_type="company",
            name="Not Embedded Co",
            extra_lines=["valid_until: '2018-12-31'", "embedded: false"],
        )
        _write_page(
            wiki,
            "ended-person.md",
            uid="person-ended",
            page_type="person",
            name="Ended Person",
            extra_lines=["valid_until: '2018-12-31'"],
        )
        _write_page(
            wiki,
            "daily-status.md",
            uid="company-daily",
            page_type="company",
            name="Daily Status Co",
            extra_lines=["bucket: daily", "valid_until: '2018-12-31'"],
        )
        return wiki

    def _uids(self, wiki: Path, tmp_path: Path, **kwargs: object) -> set[str]:
        result = enumerate_entities(
            wiki, _cache(tmp_path), entity_type="company", limit=0, **kwargs
        )
        return {h["uid"] for h in result.hits}

    def test_past_valid_until_company_listed_by_default(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        assert self._uids(ended_wiki, tmp_path) == {
            "company-current",
            "company-ended",
            "company-ended-other",
            "company-future",
            "company-daily",
        }

    def test_field_predicate_reaches_ended_relationship(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        # The issue's own failing query: `--where former_client:eq:true`.
        uids = self._uids(
            ended_wiki,
            tmp_path,
            predicates=[FieldPredicate(("former_client",), "eq", "true")],
        )
        assert uids == {"company-current", "company-ended"}

    @pytest.mark.parametrize(
        ("field", "value"),
        [("uid", "company-ended"), ("name", "Ended Client Co")],
    )
    def test_exact_lookup_of_ended_page_hits(
        self, ended_wiki: Path, tmp_path: Path, field: str, value: str
    ) -> None:
        uids = self._uids(
            ended_wiki, tmp_path, predicates=[FieldPredicate((field,), "eq", value)]
        )
        assert uids == {"company-ended"}

    def test_ended_page_carries_requested_fields(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        result = enumerate_entities(
            ended_wiki,
            _cache(tmp_path),
            entity_type="company",
            limit=0,
            fields=["valid_until", "former_client"],
            predicates=[FieldPredicate(("uid",), "eq", "company-ended")],
        )
        assert result.hits == (
            {
                "uid": "company-ended",
                "type": "company",
                "name": "Ended Client Co",
                "valid_until": "2018-12-31",
                "former_client": True,
            },
        )

    def test_recall_index_still_leaves_ended_page_out(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        # athenaeum#308's recall window is unchanged: the page is listed by
        # the side pass, not because the index started holding it.
        cache = _cache(tmp_path)
        enumerate_entities(ended_wiki, cache, entity_type="company", limit=0)
        indexed = set(FTS5Backend().candidates_by_type(cache, "company"))
        assert "ended-client.md" not in indexed
        assert "ended-other.md" not in indexed
        assert "current-client.md" in indexed

    def test_daily_bucket_page_behaviour_unchanged(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        # athenaeum#904: an expired `bucket: daily` page was already indexed
        # (recall deprioritizes it rather than dropping it), so it was
        # already listed. It still comes from the index, not the side pass.
        cache = _cache(tmp_path)
        uids = self._uids(ended_wiki, tmp_path)
        assert "company-daily" in uids
        backend = FTS5Backend()
        assert "daily-status.md" in backend.candidates_by_type(cache, "company")
        assert "daily-status.md" not in backend.expired_candidates_by_type(
            ended_wiki, cache, "company"
        )

    def test_daily_page_missing_from_index_is_still_listed(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        # The incremental build's stat fast-path can drop a daily page whose
        # `valid_until` just passed until the next full rebuild. Simulate that
        # lag by deleting its row: the side pass still lists it.
        import sqlite3

        from athenaeum.search import _DB_NAME

        cache = _cache(tmp_path)
        backend = FTS5Backend()
        backend.build_index(ended_wiki, cache)
        conn = sqlite3.connect(str(cache / _DB_NAME))
        conn.execute("DELETE FROM wiki WHERE filename = ?", ("daily-status.md",))
        conn.commit()
        conn.close()
        assert "daily-status.md" in backend.expired_candidates_by_type(
            ended_wiki, cache, "company"
        )

    def test_deprecated_pii_and_not_embedded_stay_out(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        uids = self._uids(ended_wiki, tmp_path)
        assert not uids & {
            "company-deprecated",
            "company-pii",
            "company-not-embedded",
        }

    def test_other_type_not_leaked(self, ended_wiki: Path, tmp_path: Path) -> None:
        result = enumerate_entities(
            ended_wiki, _cache(tmp_path), entity_type="person", limit=0
        )
        assert {h["uid"] for h in result.hits} == {"person-ended"}

    def test_audience_scoping_applies_to_ended_page(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        _write_page(
            ended_wiki,
            "ended-restricted.md",
            uid="company-restricted",
            page_type="company",
            name="Restricted Co",
            extra_lines=["valid_until: '2018-12-31'", "audience: [finance]"],
        )
        owner = self._uids(ended_wiki, tmp_path)
        assert "company-restricted" in owner
        restricted = self._uids(ended_wiki, tmp_path, caller_audience={"public"})
        assert "company-restricted" not in restricted

    def test_ended_split_child_hidden_by_default(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        _write_page(
            ended_wiki,
            "ended-split.md",
            uid="company-ended-split",
            page_type="company",
            name="Ended Split",
            extra_lines=[
                "valid_until: '2018-12-31'",
                "related:",
                "  - uid: company-ended",
                "    role: split-from",
            ],
        )
        assert "company-ended-split" not in self._uids(ended_wiki, tmp_path)
        assert "company-ended-split" in self._uids(
            ended_wiki, tmp_path, include_split=True
        )

    def test_ended_page_in_extra_root_listed(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        extra = tmp_path / "intake"
        extra.mkdir()
        _write_page(
            extra,
            "ended-extra.md",
            uid="company-ended-extra",
            page_type="company",
            name="Ended Extra Co",
            extra_lines=["valid_until: '2018-12-31'"],
        )
        assert "company-ended-extra" in self._uids(
            ended_wiki, tmp_path, extra_roots=[extra]
        )

    def test_index_globs_scope_the_side_pass(
        self, ended_wiki: Path, tmp_path: Path
    ) -> None:
        # A page the operator scoped out of the index (athenaeum#348) is not
        # listed through the side pass.
        cache = _cache(tmp_path)
        backend = FTS5Backend()
        config = {"recall": {"exclude_globs": ["ended-other.md"]}}
        backend.build_index(ended_wiki, cache)
        found = backend.expired_candidates_by_type(
            ended_wiki, cache, "company", config=config
        )
        assert "ended-other.md" not in found
        assert "ended-client.md" in found

    def test_missing_index_returns_nothing(self, ended_wiki: Path, tmp_path: Path) -> None:
        assert (
            FTS5Backend().expired_candidates_by_type(
                ended_wiki, _cache(tmp_path), "company"
            )
            == []
        )


class TestIndexExclusionReason:
    """Issue athenaeum#3321: the index-build exclusion chain as one reason."""

    @pytest.mark.parametrize(
        ("meta", "reason"),
        [
            ({}, None),
            ({"type": "company"}, None),
            ({"valid_until": "2999-01-01"}, None),
            ({"valid_until": "2018-12-31"}, "expired"),
            ({"valid_until": "2018-12-31", "bucket": "daily"}, None),
            ({"deprecated": True}, "deprecated"),
            ({"deprecated": True, "valid_until": "2018-12-31"}, "deprecated"),
            ({"pii": True, "valid_until": "2018-12-31"}, "pii"),
            ({"embedded": False, "valid_until": "2018-12-31"}, "not-embedded"),
        ],
    )
    def test_reason(self, meta: dict[str, object], reason: str | None) -> None:
        from athenaeum.search import _index_exclusion_reason

        assert _index_exclusion_reason(meta) == reason
