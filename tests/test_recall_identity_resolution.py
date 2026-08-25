# SPDX-License-Identifier: Apache-2.0
"""Handle-shaped identity resolution for `recall` (issue athenaeum#907).

One test class per acceptance criterion / case group:

- ``TestExactReverseLookupNotSimilaritySearch`` — AC1. A handle-shaped query
  never reaches the search backend.
- ``TestResolvedHandleCarriesIdentity`` — AC2. `uid`/`display_name`/
  `entity_class` on a resolved email handle AND a resolved registry handle.
- ``TestResolvedHandleCarriesFactFields`` — AC3. Per-value usage/provenance
  classification, bounce history, validity dates, under `with_pii=True`.
- ``TestWithPiiGatingAndLayerOrdering`` — AC4. Values only under `with_pii`;
  the join runs strictly after the audience filter and the `recallable`
  drop, and either drop performs ZERO excluded-surface scans.
- ``TestUsageClassesFilterOnHandlePath`` — regression for the athenaeum#907
  follow-up: ``usage_classes`` restricts which classes' values come back on
  the handle-shaped path exactly as it already does on the similarity-search
  path, defaults to every class, and never perturbs the ``with_pii=False``
  redaction path.
- ``TestNoActionPredicate`` — AC5. No eligibility/permission/action predicate
  of any kind, in any resolution outcome.
- ``TestParseableWithoutNaturalLanguage`` — AC6. Pure JSON, no prose wrapper.
- ``TestUnresolvedReasons`` — AC7 (part 1). Every D4 disposition reason:
  `no-match`, `record-without-uid`, `ambiguous`, `orphan-uid`.
- ``TestBothEntryPoints`` — AC7 (part 2) / D7. The MCP tool path
  (`recall_search`) and the CLI path (`cmd_recall`) agree, since both call
  the one shared resolver.
- ``TestNonHandleQueryFallsThroughUnchanged`` — D2's hard requirement: an
  ordinary query's output is byte-identical to calling the backend directly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from athenaeum import identity_resolution, pii
from athenaeum._cmd_query import cmd_recall
from athenaeum.mcp_server import recall_search
from athenaeum.models import EntityIndex

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}

RESTRICTED = {"secondary"}

UNRECALLABLE_CONFIG: dict[str, object] = {
    "storage": {
        "mapping": {"pii": "excluded", "person": "unrecallable"},
        "adapters": {
            "unrecallable": {
                "backing_store": "wiki-markdown",
                "surface_root": "wiki",
                "corpus_policy": {
                    "embedded": True,
                    "recallable": False,
                    "merge_eligible": False,
                },
            }
        },
    }
}


def _write_page(
    wiki_root: Path,
    uid: str,
    *,
    name: str,
    entity_type: str = "person",
    extra: str = "",
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: {entity_type}\n{extra}---\n\n"
        f"{name} works on widget calibration.\n",
        encoding="utf-8",
    )
    return path


def _write_record(
    root: Path,
    filename: str,
    *,
    uid: str | None,
    fields: str,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    uid_line = f"uid: {uid}\n" if uid is not None else ""
    path.write_text(
        f"---\n{uid_line}pii: true\n{fields}---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return path


def _write_registry(knowledge: Path, entities: dict[str, object]) -> Path:
    path = knowledge / "registry.json"
    path.write_text(json.dumps({"entities": entities}), encoding="utf-8")
    return path


def _write_athenaeum_yaml(knowledge: Path, config: dict[str, object]) -> None:
    """Write ``athenaeum.yaml`` so the CLI path (which loads its own config
    from disk rather than accepting a dict) sees the same storage policy the
    MCP-path tests pass directly."""
    import yaml

    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "athenaeum.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """One resolvable person: an email handle, classified, unbounced, with an
    open validity window."""
    knowledge = tmp_path / "knowledge"
    _write_page(knowledge / "wiki", "alex", name="Alex Widget")
    _write_record(
        pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
        "alex-contact.md",
        uid="alex",
        fields=(
            "emails:\n"
            "  - alex@example.org\n"
            "contact_classification:\n"
            "  - identifier: alex@example.org\n"
            "    usage_class: observed\n"
            "    source: voltaire:inbox\n"
            "    observed_at: '2026-06-01'\n"
            "identifier_validity:\n"
            "  - identifier: alex@example.org\n"
            "    valid_from: '2026-01-01'\n"
            "    valid_until: '2026-12-31'\n"
        ),
    )
    return knowledge


def _recall_args(knowledge: Path, query: str, **kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(
        path=knowledge,
        query=query,
        top_k=kwargs.get("top_k", 5),
        cache_dir=knowledge / ".cache",
        backend="keyword",
        audience=kwargs.get("audience", None),
        as_of=None,
        with_pii=kwargs.get("with_pii", False),
        usage_class=kwargs.get("usage_class", []),
    )


class TestExactReverseLookupNotSimilaritySearch:
    """AC1 — a handle-shaped query never reaches the search backend at all."""

    def test_email_handle_never_calls_get_backend(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import search as search_mod

        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("a handle-shaped query must not reach the search backend")

        monkeypatch.setattr(search_mod, "get_backend", _explode)

        result = recall_search(corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG)

        payload = json.loads(result)
        assert payload["resolved"] is True
        assert payload["uid"] == "alex"

    def test_registry_handle_never_calls_get_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import search as search_mod

        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "kim", name="Kim Widget", entity_type="org")
        _write_registry(knowledge, {"kim": {"handles": {"domains": ["kromatic-widgets.example"]}}})

        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("a handle-shaped query must not reach the search backend")

        monkeypatch.setattr(search_mod, "get_backend", _explode)

        result = recall_search(
            knowledge / "wiki",
            "who owns kromatic-widgets.example?",
            config=EXCLUDED_CONFIG,
        )

        payload = json.loads(result)
        assert payload["resolved"] is True
        assert payload["uid"] == "kim"

    def test_unresolvable_handle_also_never_calls_get_backend(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum import search as search_mod

        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("an unresolvable handle must still bypass the backend")

        monkeypatch.setattr(search_mod, "get_backend", _explode)

        result = recall_search(corpus / "wiki", "stranger@example.org", config=EXCLUDED_CONFIG)

        payload = json.loads(result)
        assert payload["resolved"] is False


class TestResolvedHandleCarriesIdentity:
    """AC2 — uid, display_name, entity_class on both handle shapes."""

    def test_email_handle_resolves_identity(self, corpus: Path) -> None:
        payload = json.loads(
            recall_search(corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG)
        )

        assert payload["resolved"] is True
        assert payload["uid"] == "alex"
        assert payload["display_name"] == "Alex Widget"
        assert payload["entity_class"] == "person"

    def test_interrogative_email_query_resolves_identically(self, corpus: Path) -> None:
        payload = json.loads(
            recall_search(corpus / "wiki", "who is alex@example.org?", config=EXCLUDED_CONFIG)
        )

        assert payload["resolved"] is True
        assert payload["uid"] == "alex"

    def test_registry_handle_resolves_identity(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "kim", name="Kim Widget", entity_type="org")
        _write_registry(knowledge, {"kim": {"handles": {"domains": ["kromatic-widgets.example"]}}})

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "is kromatic-widgets.example still current",
                config=EXCLUDED_CONFIG,
            )
        )

        assert payload["resolved"] is True
        assert payload["uid"] == "kim"
        assert payload["display_name"] == "Kim Widget"
        assert payload["entity_class"] == "org"


class TestResolvedHandleCarriesFactFields:
    """AC3 — per-value usage/provenance classification, bounce, validity."""

    def test_contact_values_carry_classification_bounce_and_validity(self, corpus: Path) -> None:
        payload = json.loads(
            recall_search(
                corpus / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=True,
            )
        )

        assert len(payload["contact_values"]) == 1
        entry = payload["contact_values"][0]
        assert entry["identifier"] == "alex@example.org"
        assert entry["usage_class"] == "observed"
        assert entry["source"] == "voltaire:inbox"
        assert entry["observed_at"] == "2026-06-01"
        assert entry["bounced"] is False
        assert entry["valid_from"] == "2026-01-01"
        assert entry["valid_until"] == "2026-12-31"

    def test_bounced_value_reports_bounced_true(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "sam", name="Sam Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "sam-contact.md",
            uid="sam",
            fields=(
                "emails:\n"
                "  - sam@example.org\n"
                "identifier_validity:\n"
                "  - identifier: sam@example.org\n"
                "    valid_until: '2020-01-01'\n"
            ),
        )

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "sam@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=True,
            )
        )

        entry = payload["contact_values"][0]
        assert entry["bounced"] is True
        assert entry["valid_until"] == "2020-01-01"

    def test_unclassified_value_reports_the_unclassified_class(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "sam", name="Sam Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "sam-contact.md",
            uid="sam",
            fields="emails:\n  - sam@example.org\n",
        )

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "sam@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=True,
            )
        )

        entry = payload["contact_values"][0]
        assert entry["usage_class"] == "unclassified"
        assert entry["source"] is None
        assert entry["valid_from"] is None
        assert entry["valid_until"] is None


class TestWithPiiGatingAndLayerOrdering:
    """AC4 — values only under `with_pii`; join strictly after both drops."""

    def test_with_pii_false_withholds_values_but_not_existence(self, corpus: Path) -> None:
        payload = json.loads(
            recall_search(corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG)
        )

        assert payload["with_pii"] is False
        assert payload["contact_values"] == []
        assert payload["redactions"] == [{"field": "emails", "value_count": 1, "redacted": True}]
        # The queried handle is legitimately echoed back in `handle` (the
        # caller already knows what they asked about) — but the excluded
        # VALUE must never appear anywhere in `contact_values`/`redactions`.
        assert "alex@example.org" not in json.dumps(
            {
                "contact_values": payload["contact_values"],
                "redactions": payload["redactions"],
            }
        )

    def test_with_pii_true_exposes_values_and_empties_redactions(self, corpus: Path) -> None:
        payload = json.loads(
            recall_search(
                corpus / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=True,
            )
        )

        assert payload["with_pii"] is True
        assert payload["redactions"] == []
        assert payload["contact_values"][0]["identifier"] == "alex@example.org"

    def test_unauthorized_page_reports_no_match_with_zero_excluded_scans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uses a REGISTRY handle deliberately: its walk never touches
        `iter_contact_records`, so a monkeypatched explode proves the
        excluded-surface lookup performed ZERO scans for a dropped page —
        not merely that it happened before or after some other scan."""
        knowledge = tmp_path / "knowledge"
        # No `access:` -> a restricted caller is not authorized (fail-closed).
        _write_page(knowledge / "wiki", "kim", name="Kim Widget", entity_type="org")
        _write_registry(knowledge, {"kim": {"handles": {"domains": ["kromatic-widgets.example"]}}})

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError(
                "an unauthorized handle resolution must never scan the excluded surface"
            )

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "who owns kromatic-widgets.example",
                config=EXCLUDED_CONFIG,
                caller_audience=RESTRICTED,
                with_pii=True,
            )
        )

        assert payload["resolved"] is False
        assert payload["reason"] == "no-match"

    def test_non_recallable_page_reports_no_match_with_zero_excluded_scans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "kim", name="Kim Widget", entity_type="person")
        _write_registry(knowledge, {"kim": {"handles": {"domains": ["kromatic-widgets.example"]}}})

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError(
                "a recallable:false handle resolution must never scan the excluded surface"
            )

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "who owns kromatic-widgets.example",
                config=UNRECALLABLE_CONFIG,
                with_pii=True,
            )
        )

        assert payload["resolved"] is False
        assert payload["reason"] == "no-match"

    def test_authorized_restricted_caller_still_receives_values(self, tmp_path: Path) -> None:
        """The rule matches recall's own: survive the audience check, get values."""
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget", extra="access: open\n")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                caller_audience=RESTRICTED,
                with_pii=True,
            )
        )

        assert payload["resolved"] is True
        assert payload["contact_values"][0]["identifier"] == "alex@example.org"


class TestUsageClassesFilterOnHandlePath:
    """Regression for the athenaeum#907 follow-up (Sentry Seer finding on
    athenaeum#919): both `recall` entry points accept `usage_classes` to restrict
    which excluded contact values come back, but the handle-shaped branch
    never threaded it to `pii.assemble_excluded_read` — a caller asking for
    one usage class silently received every class instead. `resolve_handle_query`
    must filter identically to the similarity-search path (AC4's join is
    unaffected: this only narrows WITHIN it, never widens it)."""

    def _write_two_class_record(self, knowledge: Path) -> None:
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields=(
                "emails:\n"
                "  - alex@example.org\n"
                "  - alex@provider.example\n"
                "contact_classification:\n"
                "  - identifier: alex@example.org\n"
                "    usage_class: observed\n"
                "    source: voltaire:inbox\n"
                "    observed_at: '2026-06-01'\n"
                "  - identifier: alex@provider.example\n"
                "    usage_class: provider\n"
                "    source: clearbit\n"
                "    observed_at: '2026-06-02'\n"
            ),
        )

    def test_restricting_to_one_usage_class_withholds_the_other(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        self._write_two_class_record(knowledge)

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=True,
                usage_classes=["observed"],
            )
        )

        classes = {entry["usage_class"] for entry in payload["contact_values"]}
        assert classes == {"observed"}

    def test_usage_classes_none_returns_every_class(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        self._write_two_class_record(knowledge)

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=True,
                usage_classes=None,
            )
        )

        classes = {entry["usage_class"] for entry in payload["contact_values"]}
        assert classes == {"observed", "provider"}

    def test_filter_does_not_perturb_the_with_pii_false_redaction_path(
        self, tmp_path: Path
    ) -> None:
        knowledge = tmp_path / "knowledge"
        self._write_two_class_record(knowledge)

        filtered = json.loads(
            recall_search(
                knowledge / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=False,
                usage_classes=["observed"],
            )
        )
        unfiltered = json.loads(
            recall_search(
                knowledge / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=False,
                usage_classes=None,
            )
        )

        assert filtered == unfiltered
        assert filtered["contact_values"] == []
        assert filtered["redactions"] == [{"field": "emails", "value_count": 2, "redacted": True}]

    def test_cli_entry_point_honors_the_repeatable_usage_class_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = tmp_path / "knowledge"
        self._write_two_class_record(knowledge)
        _write_athenaeum_yaml(knowledge, EXCLUDED_CONFIG)

        rc = cmd_recall(
            _recall_args(knowledge, "alex@example.org", with_pii=True, usage_class=["observed"])
        )
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        classes = {entry["usage_class"] for entry in payload["contact_values"]}
        assert classes == {"observed"}

    def test_mcp_and_cli_agree_under_the_filter(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = tmp_path / "knowledge"
        self._write_two_class_record(knowledge)
        _write_athenaeum_yaml(knowledge, EXCLUDED_CONFIG)

        mcp_payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=True,
                usage_classes=["observed"],
            )
        )
        rc = cmd_recall(
            _recall_args(knowledge, "alex@example.org", with_pii=True, usage_class=["observed"])
        )
        cli_payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert cli_payload == mcp_payload


class TestNoActionPredicate:
    """AC5 — no eligibility/permission/action predicate anywhere in the output."""

    _FORBIDDEN_SUBSTRINGS = ("outreach", "eligible", "permitted", "allowed")

    def test_resolved_with_pii_true_carries_no_action_predicate(self, corpus: Path) -> None:
        result = recall_search(
            corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG, with_pii=True
        )

        lowered = result.lower()
        for term in self._FORBIDDEN_SUBSTRINGS:
            assert term not in lowered

    def test_resolved_with_pii_false_carries_no_action_predicate(self, corpus: Path) -> None:
        result = recall_search(corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG)

        lowered = result.lower()
        for term in self._FORBIDDEN_SUBSTRINGS:
            assert term not in lowered

    def test_unresolved_handle_carries_no_action_predicate(self, corpus: Path) -> None:
        result = recall_search(corpus / "wiki", "stranger@example.org", config=EXCLUDED_CONFIG)

        lowered = result.lower()
        for term in self._FORBIDDEN_SUBSTRINGS:
            assert term not in lowered

    def test_contact_value_fact_to_dict_has_no_outreach_eligible_key(self) -> None:
        fact = identity_resolution.ContactValueFact(
            identifier="alex@example.org",
            usage_class="observed",
            source="voltaire:inbox",
            observed_at="2026-06-01",
            bounced=False,
            valid_from=None,
            valid_until=None,
            do_not_email=pii.DoNotEmailState(marked=False),
            validity=pii.IdentifierValidity(identifier="alex@example.org", closed=False),
        )

        assert "outreach_eligible" not in fact.to_dict()


class TestParseableWithoutNaturalLanguage:
    """AC6 — pure JSON document, parseable without natural-language interpretation."""

    def test_output_is_a_bare_json_object(self, corpus: Path) -> None:
        result = recall_search(corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG)

        assert result.strip().startswith("{")
        payload = json.loads(result)
        assert isinstance(payload, dict)

    def test_fields_have_stable_parseable_types(self, corpus: Path) -> None:
        payload = json.loads(
            recall_search(
                corpus / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=True,
            )
        )

        assert isinstance(payload["resolved"], bool)
        assert isinstance(payload["uid"], str)
        assert isinstance(payload["candidate_uids"], list)
        assert isinstance(payload["contact_values"], list)
        assert isinstance(payload["redactions"], list)
        assert isinstance(payload["with_pii"], bool)

    def test_unresolved_reason_is_null_on_success_and_a_string_on_failure(
        self, corpus: Path
    ) -> None:
        resolved = json.loads(
            recall_search(corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG)
        )
        unresolved = json.loads(
            recall_search(corpus / "wiki", "stranger@example.org", config=EXCLUDED_CONFIG)
        )

        assert resolved["reason"] is None
        assert isinstance(unresolved["reason"], str)
        assert unresolved["reason"] in identity_resolution.RESOLUTION_REASONS


class TestUnresolvedReasons:
    """AC7 (part 1) — every D4 disposition reason, both handle shapes."""

    def test_email_no_match(self, corpus: Path) -> None:
        payload = json.loads(
            recall_search(corpus / "wiki", "stranger@example.org", config=EXCLUDED_CONFIG)
        )

        assert payload["resolved"] is False
        assert payload["reason"] == "no-match"
        assert payload["candidate_uids"] == []

    def test_email_record_without_uid(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "no-uid.md",
            uid=None,
            fields="emails:\n  - nouid@example.org\n",
        )

        payload = json.loads(
            recall_search(knowledge / "wiki", "nouid@example.org", config=EXCLUDED_CONFIG)
        )

        assert payload["resolved"] is False
        assert payload["reason"] == "record-without-uid"

    def test_email_ambiguous(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_page(knowledge / "wiki", "sam", name="Sam Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "a.md",
            uid="alex",
            fields="emails:\n  - shared@example.org\n",
        )
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "b.md",
            uid="sam",
            fields="emails:\n  - shared@example.org\n",
        )

        payload = json.loads(
            recall_search(knowledge / "wiki", "shared@example.org", config=EXCLUDED_CONFIG)
        )

        assert payload["resolved"] is False
        assert payload["reason"] == "ambiguous"
        assert payload["candidate_uids"] == ["alex", "sam"]

    def test_email_orphan_uid(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "ghost.md",
            uid="ghost",
            fields="emails:\n  - ghost@example.org\n",
        )

        payload = json.loads(
            recall_search(knowledge / "wiki", "ghost@example.org", config=EXCLUDED_CONFIG)
        )

        assert payload["resolved"] is False
        assert payload["reason"] == "orphan-uid"

    def test_registry_ambiguous(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget", entity_type="org")
        _write_page(knowledge / "wiki", "sam", name="Sam Widget", entity_type="org")
        _write_registry(
            knowledge,
            {
                "alex": {"handles": {"domains": ["shared-widgets.example"]}},
                "sam": {"handles": {"domains": ["shared-widgets.example"]}},
            },
        )

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "who owns shared-widgets.example",
                config=EXCLUDED_CONFIG,
            )
        )

        assert payload["resolved"] is False
        assert payload["reason"] == "ambiguous"
        assert payload["candidate_uids"] == ["alex", "sam"]

    def test_registry_orphan_uid(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        _write_registry(knowledge, {"ghost": {"handles": {"domains": ["ghost-widgets.example"]}}})

        payload = json.loads(
            recall_search(
                knowledge / "wiki",
                "who owns ghost-widgets.example",
                config=EXCLUDED_CONFIG,
            )
        )

        assert payload["resolved"] is False
        assert payload["reason"] == "orphan-uid"

    def test_registry_no_match_is_not_handle_shaped_at_all(self, tmp_path: Path) -> None:
        """(b)'s detection IS the match — a query matching nothing in the
        registry is not handle-shaped, so it falls through instead of
        producing an unresolvable-handle payload."""
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "kim", name="Kim Widget", entity_type="org")

        result = recall_search(
            knowledge / "wiki", "who owns nowhere.example", config=EXCLUDED_CONFIG
        )

        # Not our JSON shape — it fell through to whatever `recall`'s
        # ordinary (hit or no-hit) text output is for this query.
        assert not result.strip().startswith("{")


class TestBothEntryPoints:
    """AC7 (part 2) / D7 — the MCP tool path and the CLI path agree."""

    def test_mcp_and_cli_resolve_the_same_handle_identically(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_athenaeum_yaml(corpus, EXCLUDED_CONFIG)

        mcp_payload = json.loads(
            recall_search(
                corpus / "wiki",
                "alex@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=True,
            )
        )

        rc = cmd_recall(_recall_args(corpus, "alex@example.org", with_pii=True))
        cli_payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert cli_payload == mcp_payload

    def test_cli_path_handles_an_unresolvable_handle(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_athenaeum_yaml(corpus, EXCLUDED_CONFIG)

        rc = cmd_recall(_recall_args(corpus, "stranger@example.org"))
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["resolved"] is False
        assert payload["reason"] == "no-match"

    def test_cli_path_falls_through_for_a_non_handle_query(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_athenaeum_yaml(corpus, EXCLUDED_CONFIG)

        rc = cmd_recall(_recall_args(corpus, "widget"))
        out = capsys.readouterr().out

        assert rc == 0
        assert not out.strip().startswith("{")
        assert "alex.md" in out


class TestNonHandleQueryFallsThroughUnchanged:
    """D2's hard requirement — byte-identical to calling the backend directly."""

    def test_recall_search_matches_the_backend_call_directly(self, corpus: Path) -> None:
        from athenaeum.mcp_server import _recall_via_backend

        direct = _recall_via_backend(
            corpus / "wiki",
            "widget",
            5,
            "keyword",
            None,
            [],
            None,
            EXCLUDED_CONFIG,
            with_pii=False,
            usage_classes=None,
        )
        via_recall_search = recall_search(corpus / "wiki", "widget", config=EXCLUDED_CONFIG)

        assert via_recall_search == direct

    def test_a_query_that_merely_looks_like_a_bare_token_is_unaffected(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No registry.json at all — `load_registry` degrades to `{}`, never raises."""
        result = recall_search(corpus / "wiki", "Widget", config=EXCLUDED_CONFIG)

        assert "Alex Widget" in result

    def test_detection_itself_never_raises_on_a_missing_registry(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")

        assert not (knowledge / "registry.json").exists()
        result = recall_search(knowledge / "wiki", "widget", config=EXCLUDED_CONFIG)

        assert "Alex Widget" in result


class TestHandleDetectionIsConservative:
    """D2 — detection helper unit coverage, independent of the recall wiring."""

    def test_two_email_tokens_are_not_handle_shaped(self) -> None:
        assert (
            identity_resolution._detect_email_handle("alex@example.org or sam@example.org") is None
        )

    def test_bare_email_is_handle_shaped(self) -> None:
        assert identity_resolution._detect_email_handle("alex@example.org") == "alex@example.org"

    def test_framing_is_stripped_from_both_ends(self) -> None:
        assert (
            identity_resolution._strip_interrogative_framing(
                "who is kromatic-widgets.example still current?"
            )
            == "kromatic-widgets.example"
        )

    def test_no_framing_present_is_unchanged(self) -> None:
        assert identity_resolution._strip_interrogative_framing("kromatic-widgets.example") == (
            "kromatic-widgets.example"
        )

    def test_query_that_is_pure_framing_strips_to_empty_and_is_not_handle_shaped(
        self, tmp_path: Path
    ) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)

        assert identity_resolution._strip_interrogative_framing("is ?") == ""
        assert (
            identity_resolution.resolve_handle_query(knowledge, knowledge / "wiki", "is ?") is None
        )

    def test_validity_bounds_fallback_reads_record_level_bounds(self) -> None:
        """The second shape `is_bounced_identifier` reads: a slug-keyed record
        whose top-level `identifier:` IS the value asked about."""
        record_meta = {
            "identifier": "solo@example.org",
            "valid_from": "2026-02-01",
            "valid_until": "2026-11-30",
        }

        bounds = identity_resolution._validity_bounds_for_value(record_meta, "solo@example.org")

        assert bounds == ("2026-02-01", "2026-11-30")

    def test_unreadable_page_reports_orphan_uid(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`EntityIndex` construction reads `alex.md` once already (indexing
        it); only the SECOND read — `_finish`'s own fresh re-read — is made
        to fail here, so this exercises `_finish`'s own except-branch rather
        than short-circuiting earlier at `EntityIndex`'s own tolerant skip."""
        original_read_text = Path.read_text
        seen: set[str] = set()

        def _explode(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "alex.md":
                if "alex.md" in seen:
                    raise OSError("simulated unreadable page")
                seen.add("alex.md")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _explode)

        payload = json.loads(
            recall_search(corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG)
        )

        assert payload["resolved"] is False
        assert payload["reason"] == "orphan-uid"


class TestPreparedIndexGatingParity:
    """athenaeum#1124 — the prepared-index path must apply the SAME D6
    audience/`recallable` gating `_finish` already applies on the unprepared
    path. This is the one thing that must not regress: a prepared index must
    never become a way to bypass that gating."""

    def test_restricted_caller_still_denied_on_prepared_path(self, tmp_path: Path) -> None:
        """No `access:` on the page -> a restricted caller is unauthorized
        (fail-closed), exactly as on the unprepared path — even though both
        indexes are prepared and `with_pii=True` is requested."""
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )
        excluded_index = pii.ExcludedRecordIndex(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        )
        entity_index = EntityIndex(knowledge / "wiki")

        result = identity_resolution.resolve_handle_query(
            knowledge,
            knowledge / "wiki",
            "alex@example.org",
            caller_audience=RESTRICTED,
            config=EXCLUDED_CONFIG,
            with_pii=True,
            excluded_index=excluded_index,
            entity_index=entity_index,
        )

        assert result is not None
        assert result.resolved is False
        assert result.reason == "no-match"
        assert result.uid is None
        assert result.contact_values == ()
        assert result.redactions == ()

    def test_non_recallable_page_still_denied_on_prepared_path(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, UNRECALLABLE_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )
        excluded_index = pii.ExcludedRecordIndex(
            pii.contacts_surface_root(knowledge, UNRECALLABLE_CONFIG)
        )
        entity_index = EntityIndex(knowledge / "wiki")

        result = identity_resolution.resolve_handle_query(
            knowledge,
            knowledge / "wiki",
            "alex@example.org",
            config=UNRECALLABLE_CONFIG,
            with_pii=True,
            excluded_index=excluded_index,
            entity_index=entity_index,
        )

        assert result is not None
        assert result.resolved is False
        assert result.reason == "no-match"
        assert result.contact_values == ()

    def test_authorized_caller_still_receives_values_on_prepared_path(self, tmp_path: Path) -> None:
        """The positive control for the two denial tests above: a caller that
        legitimately survives both gates still gets values through the
        prepared path — the fix does not accidentally over-restrict either."""
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget", extra="access: open\n")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )
        excluded_index = pii.ExcludedRecordIndex(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        )
        entity_index = EntityIndex(knowledge / "wiki")

        result = identity_resolution.resolve_handle_query(
            knowledge,
            knowledge / "wiki",
            "alex@example.org",
            caller_audience=RESTRICTED,
            config=EXCLUDED_CONFIG,
            with_pii=True,
            excluded_index=excluded_index,
            entity_index=entity_index,
        )

        assert result is not None
        assert result.resolved is True
        assert result.uid == "alex"
        assert result.contact_values[0].identifier == "alex@example.org"

    def test_prepared_path_matches_unprepared_path_byte_for_byte(self, corpus: Path) -> None:
        """Same query, same config: the prepared and unprepared paths must
        return identical `HandleResolution.to_dict()` payloads."""
        excluded_index = pii.ExcludedRecordIndex(pii.contacts_surface_root(corpus, EXCLUDED_CONFIG))
        entity_index = EntityIndex(corpus / "wiki")

        unprepared = identity_resolution.resolve_handle_query(
            corpus, corpus / "wiki", "alex@example.org", config=EXCLUDED_CONFIG, with_pii=True
        )
        prepared = identity_resolution.resolve_handle_query(
            corpus,
            corpus / "wiki",
            "alex@example.org",
            config=EXCLUDED_CONFIG,
            with_pii=True,
            excluded_index=excluded_index,
            entity_index=entity_index,
        )

        assert unprepared is not None
        assert prepared is not None
        assert prepared.to_dict() == unprepared.to_dict()


class TestPreparedIndexCounterGuard:
    """athenaeum#1124's regression guard: a COUNTER assertion, not a timing
    assertion (it must not drift with host speed or corpus growth). Resolving
    N handles in one process must construct `models.EntityIndex` exactly
    once and call `pii.iter_contact_records` exactly once, regardless of N."""

    def test_n_handles_build_entity_index_once_and_scan_contacts_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        contacts_root = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        n = 5
        for i in range(n):
            uid = f"person{i}"
            _write_page(knowledge / "wiki", uid, name=f"Person {i}")
            _write_record(
                contacts_root,
                f"{uid}-contact.md",
                uid=uid,
                fields=f"emails:\n  - person{i}@example.org\n",
            )

        entity_index_builds = 0
        original_entity_init = EntityIndex.__init__

        def _counting_entity_init(self: EntityIndex, wiki_root: Path) -> None:
            nonlocal entity_index_builds
            entity_index_builds += 1
            original_entity_init(self, wiki_root)

        monkeypatch.setattr(EntityIndex, "__init__", _counting_entity_init)

        iter_contact_records_calls = 0
        original_iter = pii.iter_contact_records

        def _counting_iter(root: Path) -> list[Path]:
            nonlocal iter_contact_records_calls
            iter_contact_records_calls += 1
            return original_iter(root)

        monkeypatch.setattr(pii, "iter_contact_records", _counting_iter)

        # The one-time, process-lifetime build a caller like maecenas'
        # `default_resolver`/`default_uid_reader` closures perform once.
        excluded_index = pii.ExcludedRecordIndex(contacts_root)
        entity_index = EntityIndex(knowledge / "wiki")
        assert entity_index_builds == 1
        assert iter_contact_records_calls == 0  # ExcludedRecordIndex is lazy

        for i in range(n):
            result = identity_resolution.resolve_handle_query(
                knowledge,
                knowledge / "wiki",
                f"person{i}@example.org",
                config=EXCLUDED_CONFIG,
                with_pii=True,
                excluded_index=excluded_index,
                entity_index=entity_index,
            )
            assert result is not None
            assert result.resolved is True
            assert result.uid == f"person{i}"

        assert entity_index_builds == 1, "EntityIndex must not be rebuilt per handle"
        assert iter_contact_records_calls == 1, (
            "iter_contact_records must be called exactly once for the whole batch, "
            "not once per handle"
        )
