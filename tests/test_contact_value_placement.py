# SPDX-License-Identifier: Apache-2.0
"""The attribution placement executor and the generalised write primitive (athenaeum#2212).

``docs/design/attribution-verification.md`` §7 is the normative spec. §7.1's
table says what each verdict files; §7.2 says how the write happens — through
the ``pii`` record primitives directly, never the corrections applier, with a
re-check before every write and the corrections applier's per-value
``field_sources`` provenance shape on what lands.

Cases:

- ``TestRecordContactValue`` — ``pii.record_contact_value`` in isolation:
  field and usage-class validation, every ``OBSERVED_CONTACT_*`` guard, the
  non-``emails`` placement target, and idempotency.
- ``TestOutcomePerVerdict`` — one fixture test per design §7.1 row, calling
  ``place_contact_value`` directly in apply mode.
- ``TestReAttributionLeavesTheTargetAlone`` — ``other-known`` files on
  ``owner_uid`` and NOT on the original target.
- ``TestNoPlacementGrantsOutreachEligibility`` — design §3: a model never
  grants outreach eligibility. Asserted for EVERY outcome, and structurally
  over the executor's own source.
- ``TestReCheck`` — design §7.2: a ``suspect`` re-check goes to the human
  queue and never back to a model.
- ``TestObserveMode`` — ``apply=False`` writes nothing.
- ``TestAlias`` — ``alt_emails`` / ``phones`` plus the ``identifier_relation``
  fact entry.
- ``TestSameEntity`` — the injected proposer runs and no contact value is
  written anywhere.
- ``TestProvenanceShape`` — design §7.2 field-for-field, including §8.1's
  "``decided_by: human`` and no calibrated probability".
- ``TestIdempotency`` — a repeat placement rewrites nothing.
- ``TestVocabulariesArePinned`` — the token tuples ``pii`` restates rather
  than imports (the L3 cycle constraint) cannot drift from
  ``pii_attribution``'s.

No model is mocked here because none is reachable: ``place_contact_value`` has
no model access by construction, which is what makes design §7.2's "it does
not loop" a structural guarantee rather than a promise.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii, pii_attribution
from athenaeum.models import parse_frontmatter, render_frontmatter

_OBSERVED_AT = "2026-10-31T09:00:00Z"
_TARGET_UID = "uid-target"
_TARGET_NAME = "Target Person"
_OWNER_UID = "uid-owner"
_OWNER_NAME = "Owner Person"
_THIRD_UID = "uid-third"

_VALUE = "introducer@example.org"
_PHONE = "+1 555 0100"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _contacts(tmp_path: Path) -> Path:
    root = tmp_path / "excluded" / "contacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed_record(
    contacts_root: Path,
    filename: str,
    *,
    uid: str | None,
    emails: list[str] = (),
    alt_emails: list[str] = (),
    phones: list[str] = (),
) -> Path:
    meta: dict[str, Any] = {}
    if uid is not None:
        meta["uid"] = uid
    meta["pii"] = True
    if emails:
        meta["emails"] = list(emails)
    if alt_emails:
        meta["alt_emails"] = list(alt_emails)
    if phones:
        meta["phones"] = list(phones)
    path = contacts_root / filename
    path.write_text(render_frontmatter(meta) + "\nSynthetic contact record.\n", encoding="utf-8")
    return path


def _meta(path: Path) -> dict[str, Any]:
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return meta if isinstance(meta, dict) else {}


def _snapshot(contacts_root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(contacts_root)): p.read_bytes()
        for p in sorted(contacts_root.rglob("*"))
        if p.is_file()
    }


@dataclass(frozen=True)
class _StubCheck:
    check_id: str


@dataclass(frozen=True)
class _StubVerdict:
    disposition: str
    checks: tuple[_StubCheck, ...] = ()


class _RecheckStub:
    """Shaped like ``pii_attribution.check_contact_attribution``'s result.

    Deliberately NOT the real dataclass: the executor reads ``.disposition``
    and ``.checks`` structurally (``getattr``) precisely so ``pii`` needs no
    import of the verdict type, and a stub proves the structural read works.
    """

    def __init__(self, disposition: str, checks: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._verdict = _StubVerdict(disposition, tuple(_StubCheck(c) for c in checks))

    def __call__(self, value: str, **kwargs: Any) -> _StubVerdict:
        self.calls.append((value, kwargs))
        return self._verdict


class _ProposerStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "merge-block"


def _place(contacts_root: Path, **kwargs: Any) -> pii.ContactPlacement:
    base: dict[str, Any] = {
        "target_uid": _TARGET_UID,
        "target_name": _TARGET_NAME,
        "original_source": "raw/2026-10-31-intro.md",
        "observed_at": _OBSERVED_AT,
        "apply": True,
    }
    base.update(kwargs)
    value = base.pop("value", _VALUE)
    return pii.place_contact_value(contacts_root, value, **base)


# ---------------------------------------------------------------------------
# (1) The generalised write primitive
# ---------------------------------------------------------------------------


class TestRecordContactValue:
    def test_unknown_field_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown contact field"):
            pii.record_contact_value(
                _contacts(tmp_path),
                _VALUE,
                uid=_TARGET_UID,
                usage_class=pii.USAGE_CLASS_UNCLASSIFIED,
                field="notes",
                source="raw/x.md",
                observed_at=_OBSERVED_AT,
            )

    def test_unknown_usage_class_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown usage_class"):
            pii.record_contact_value(
                _contacts(tmp_path),
                _VALUE,
                uid=_TARGET_UID,
                usage_class="probably-fine",
                source="raw/x.md",
                observed_at=_OBSERVED_AT,
            )

    def test_recordable_fields_are_the_identifier_fields_plus_phones(self) -> None:
        assert pii.RECORDABLE_CONTACT_FIELDS == (*pii.CONTACT_IDENTIFIER_FIELDS, "phones")

    @pytest.mark.parametrize(
        ("identifier", "uid"),
        [("", _TARGET_UID), ("   ", _TARGET_UID), (_VALUE, ""), (_VALUE, "  ")],
    )
    def test_blank_inputs_skip(self, tmp_path: Path, identifier: str, uid: str) -> None:
        contacts = _contacts(tmp_path)
        write = pii.record_contact_value(
            contacts,
            identifier,
            uid=uid,
            usage_class=pii.USAGE_CLASS_UNCLASSIFIED,
            source="raw/x.md",
            observed_at=_OBSERVED_AT,
        )
        assert write.outcome == pii.OBSERVED_CONTACT_SKIPPED
        assert write.path is None
        assert _snapshot(contacts) == {}

    def test_mints_when_nothing_lists_the_value(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        write = pii.record_contact_value(
            contacts,
            _VALUE,
            uid=_TARGET_UID,
            name=_TARGET_NAME,
            usage_class=pii.USAGE_CLASS_UNCLASSIFIED,
            source="raw/x.md",
            observed_at=_OBSERVED_AT,
        )
        assert write.outcome == pii.OBSERVED_CONTACT_MINTED
        assert write.path is not None
        meta = _meta(write.path)
        assert meta["uid"] == _TARGET_UID
        assert meta["emails"] == [_VALUE]
        assert pii.classification_for_value(meta, _VALUE).usage_class == (
            pii.USAGE_CLASS_UNCLASSIFIED
        )

    def test_classifies_onto_a_record_this_uid_already_owns(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        seeded = _seed_record(contacts, "target.md", uid=_TARGET_UID, emails=[_VALUE])
        write = pii.record_contact_value(
            contacts,
            _VALUE,
            uid=_TARGET_UID,
            usage_class=pii.USAGE_CLASS_UNCLASSIFIED,
            source="raw/x.md",
            observed_at=_OBSERVED_AT,
        )
        assert write.outcome == pii.OBSERVED_CONTACT_CLASSIFIED
        assert write.path == seeded
        assert len(_snapshot(contacts)) == 1

    def test_declines_a_record_that_names_no_entity(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        _seed_record(contacts, "nameless.md", uid=None, emails=[_VALUE])
        before = _snapshot(contacts)
        write = pii.record_contact_value(
            contacts,
            _VALUE,
            uid=_TARGET_UID,
            usage_class=pii.USAGE_CLASS_UNCLASSIFIED,
            source="raw/x.md",
            observed_at=_OBSERVED_AT,
        )
        assert write.outcome == pii.OBSERVED_CONTACT_RECORD_WITHOUT_UID
        assert write.uids == ()
        assert _snapshot(contacts) == before

    def test_declines_another_entitys_value_and_names_it(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        _seed_record(contacts, "owner.md", uid=_OWNER_UID, emails=[_VALUE])
        before = _snapshot(contacts)
        write = pii.record_contact_value(
            contacts,
            _VALUE,
            uid=_TARGET_UID,
            usage_class=pii.USAGE_CLASS_UNCLASSIFIED,
            source="raw/x.md",
            observed_at=_OBSERVED_AT,
        )
        assert write.outcome == pii.OBSERVED_CONTACT_OTHER_ENTITY
        assert write.uids == (_OWNER_UID,)
        assert _snapshot(contacts) == before

    def test_a_contested_value_is_never_claimed_in_an_alias_field_either(
        self, tmp_path: Path
    ) -> None:
        """The ownership guards hold for every field, not just ``emails``."""
        contacts = _contacts(tmp_path)
        _seed_record(contacts, "owner.md", uid=_OWNER_UID, emails=[_VALUE])
        _seed_record(contacts, "target.md", uid=_TARGET_UID, emails=["target@example.org"])
        before = _snapshot(contacts)
        write = pii.record_contact_value(
            contacts,
            _VALUE,
            uid=_TARGET_UID,
            usage_class=pii.USAGE_CLASS_UNCLASSIFIED,
            field="alt_emails",
            source="raw/x.md",
            observed_at=_OBSERVED_AT,
        )
        assert write.outcome == pii.OBSERVED_CONTACT_OTHER_ENTITY
        assert _snapshot(contacts) == before

    def test_a_non_emails_field_targets_the_uids_own_record(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        own = _seed_record(contacts, "target.md", uid=_TARGET_UID, emails=["target@example.org"])
        write = pii.record_contact_value(
            contacts,
            _VALUE,
            uid=_TARGET_UID,
            usage_class=pii.USAGE_CLASS_UNCLASSIFIED,
            field="alt_emails",
            source="raw/x.md",
            observed_at=_OBSERVED_AT,
        )
        assert write.path == own
        assert _meta(own)["alt_emails"] == [_VALUE]
        assert _meta(own)["emails"] == ["target@example.org"]
        assert len(_snapshot(contacts)) == 1

    def test_a_non_emails_field_mints_at_the_uid_path_when_the_uid_has_no_record(
        self, tmp_path: Path
    ) -> None:
        contacts = _contacts(tmp_path)
        write = pii.record_contact_value(
            contacts,
            _PHONE,
            uid=_TARGET_UID,
            usage_class=pii.USAGE_CLASS_UNCLASSIFIED,
            field="phones",
            source="raw/x.md",
            observed_at=_OBSERVED_AT,
        )
        assert write.path == pii.default_uid_record_path(contacts, _TARGET_UID)
        assert _meta(write.path)["phones"] == [_PHONE]

    @pytest.mark.parametrize("field", ["emails", "alt_emails", "phones"])
    def test_repeat_write_is_byte_identical(self, tmp_path: Path, field: str) -> None:
        contacts = _contacts(tmp_path)
        value = _PHONE if field == "phones" else _VALUE
        kwargs: dict[str, Any] = {
            "uid": _TARGET_UID,
            "name": _TARGET_NAME,
            "usage_class": pii.USAGE_CLASS_UNCLASSIFIED,
            "field": field,
            "source": {"type": "attribution", "writer": "re-attribution"},
            "observed_at": _OBSERVED_AT,
        }
        pii.record_contact_value(contacts, value, **kwargs)
        before = _snapshot(contacts)
        pii.record_contact_value(contacts, value, **kwargs)
        assert _snapshot(contacts) == before

    def test_the_observed_writer_still_writes_observed(self, tmp_path: Path) -> None:
        """The delegation did not change what the pre-existing writer does."""
        contacts = _contacts(tmp_path)
        write = pii.record_observed_contact_value(
            contacts,
            _VALUE,
            uid=_TARGET_UID,
            name=_TARGET_NAME,
            source="raw/x.md",
            observed_at=_OBSERVED_AT,
        )
        assert write.outcome == pii.OBSERVED_CONTACT_MINTED
        assert write.path is not None
        assert pii.is_outreach_eligible(_meta(write.path), _VALUE) is True


# ---------------------------------------------------------------------------
# (2) One fixture per design §7.1 outcome
# ---------------------------------------------------------------------------


class TestOutcomePerVerdict:
    def test_own_files_on_the_target(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict="own")
        assert placement.outcome == pii.PLACEMENT_FILED
        assert placement.placed_uid == _TARGET_UID
        assert placement.field == "emails"
        assert placement.path is not None
        meta = _meta(placement.path)
        assert meta["uid"] == _TARGET_UID
        assert meta["emails"] == [_VALUE]

    def test_own_phone_files_into_phones(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, value=_PHONE, verdict="own")
        assert placement.outcome == pii.PLACEMENT_FILED
        assert placement.field == "phones"
        assert placement.path is not None
        assert _meta(placement.path)["phones"] == [_PHONE]

    def test_other_known_files_on_the_owner(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(
            contacts, verdict="other-known", owner_uid=_OWNER_UID, owner_name=_OWNER_NAME
        )
        assert placement.outcome == pii.PLACEMENT_FILED
        assert placement.placed_uid == _OWNER_UID
        assert placement.path is not None
        assert _meta(placement.path)["uid"] == _OWNER_UID

    def test_other_known_without_an_owner_skips(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict="other-known")
        assert placement.outcome == pii.PLACEMENT_SKIPPED
        assert "owner_uid" in placement.detail
        assert _snapshot(contacts) == {}

    def test_other_unknown_parks_without_filing(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict="other-unknown")
        assert placement.outcome == pii.PLACEMENT_PARKED
        assert placement.path is None
        assert "verdict is recorded" in placement.detail
        assert _snapshot(contacts) == {}

    def test_alias_files_into_alt_emails(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict="alias", alias_relation="role-inbox")
        assert placement.outcome == pii.PLACEMENT_FILED
        assert placement.field == "alt_emails"
        assert placement.path is not None
        assert _meta(placement.path)["alt_emails"] == [_VALUE]

    def test_same_entity_proposes_a_merge(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        proposer = _ProposerStub()
        placement = _place(
            contacts, verdict="same-entity", owner_uid=_OWNER_UID, propose_merge=proposer
        )
        assert placement.outcome == pii.PLACEMENT_MERGE_PROPOSED
        assert len(proposer.calls) == 1
        assert _snapshot(contacts) == {}

    def test_non_personal_is_logged_only(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict="non-personal")
        assert placement.outcome == pii.PLACEMENT_LOGGED
        assert placement.path is None
        assert _snapshot(contacts) == {}

    @pytest.mark.parametrize("verdict", ["unclear", "mostly-probably", ""])
    def test_unclear_and_unrecognised_verdicts_skip(self, tmp_path: Path, verdict: str) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict=verdict)
        assert placement.outcome == pii.PLACEMENT_SKIPPED
        assert _snapshot(contacts) == {}

    def test_a_blank_value_skips(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, value="   ", verdict="own")
        assert placement.outcome == pii.PLACEMENT_SKIPPED
        assert placement.detail == "blank value"

    def test_an_alias_verdict_needs_a_relation(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        with pytest.raises(ValueError, match="alias verdict needs an alias_relation"):
            _place(contacts, verdict="alias")
        with pytest.raises(ValueError, match="alias verdict needs an alias_relation"):
            _place(contacts, verdict="alias", alias_relation="because-why-not")

    def test_a_declining_write_primitive_is_never_a_silent_success(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        _seed_record(contacts, "third.md", uid=_THIRD_UID, emails=[_VALUE])
        before = _snapshot(contacts)
        placement = _place(contacts, verdict="own")
        assert placement.outcome == pii.PLACEMENT_DECLINED
        assert placement.write_outcome == pii.OBSERVED_CONTACT_OTHER_ENTITY
        assert placement.path is None
        assert _snapshot(contacts) == before


# ---------------------------------------------------------------------------
# (3) Re-attribution leaves the original target alone
# ---------------------------------------------------------------------------


class TestReAttributionLeavesTheTargetAlone:
    def test_other_known_files_on_owner_and_not_on_target(self, tmp_path: Path) -> None:
        """Design §7.1: "Nothing is filed on the original target.\""""
        contacts = _contacts(tmp_path)
        target_record = _seed_record(
            contacts, "target.md", uid=_TARGET_UID, emails=["target@example.org"]
        )
        owner_record = _seed_record(
            contacts, "owner.md", uid=_OWNER_UID, emails=["owner@example.org"]
        )

        placement = _place(
            contacts, verdict="other-known", owner_uid=_OWNER_UID, owner_name=_OWNER_NAME
        )

        assert placement.outcome == pii.PLACEMENT_FILED
        assert placement.placed_uid == _OWNER_UID

        target_meta = _meta(target_record)
        assert pii.record_lists_identifier(target_meta, _VALUE) is False
        assert "field_sources" not in target_meta

        # The owner's record is untouched too: the `emails` placement
        # convention is the surface's per-IDENTIFIER path, which is where
        # `resolve_contact_record` will look for the value later.
        assert _meta(owner_record)["emails"] == ["owner@example.org"]
        assert placement.path is not None
        assert placement.path not in (target_record, owner_record)
        assert _meta(placement.path)["uid"] == _OWNER_UID
        assert pii.resolve_contact_records(contacts, _VALUE) == [placement.path]


# ---------------------------------------------------------------------------
# (4) A model never grants outreach eligibility (design §3)
# ---------------------------------------------------------------------------


def _filing_cases() -> list[Any]:
    """Every ``place_contact_value`` case that FILES, one ``pytest.param`` each.

    **Ids are explicit, and every case value is a plain token.** Two reasons,
    and the second is load-bearing:

    - The sweeps below are only legible in a failure report if each case
      names itself; ``[('alias_relation', 'shared'), ('verdict', 'alias')]``
      is not a name.
    - A pytest-derived id must be STABLE ACROSS PROCESSES. A parametrize
      value whose type has the default ``repr`` renders its MEMORY ADDRESS
      into the node id, so two ``pytest-xdist`` workers collect different
      id sets and xdist aborts the whole shard at collection — a failure a
      single-process local run cannot see. That is why no case here carries
      an object: the re-check case passes the plain disposition token and the
      check ids, and the test body builds the :class:`_RecheckStub` from them.
    """
    return [
        pytest.param({"verdict": "own"}, id="own-email"),
        pytest.param({"verdict": "own", "value": _PHONE}, id="own-phone"),
        pytest.param(
            {
                "verdict": "other-known",
                "owner_uid": _OWNER_UID,
                "owner_name": _OWNER_NAME,
            },
            id="other-known-email",
        ),
        pytest.param(
            {"verdict": "alias", "alias_relation": "shared"}, id="alias-email"
        ),
        pytest.param(
            {"verdict": "alias", "alias_relation": "shared", "value": _PHONE},
            id="alias-phone",
        ),
    ]


def _every_case() -> list[Any]:
    """:func:`_filing_cases` plus every outcome that files nothing.

    Same id and plain-token discipline — see :func:`_filing_cases`.
    """
    return [
        *_filing_cases(),
        pytest.param({"verdict": "other-unknown"}, id="other-unknown"),
        pytest.param({"verdict": "non-personal"}, id="non-personal"),
        pytest.param({"verdict": "unclear"}, id="unclear"),
        pytest.param(
            {"verdict": "same-entity", "owner_uid": _OWNER_UID}, id="same-entity"
        ),
        pytest.param({"verdict": "own", "apply": False}, id="own-observe-mode"),
        pytest.param(
            {
                "verdict": "own",
                "recheck_disposition": "suspect",
                "recheck_checks": ("D2",),
            },
            id="own-suspect-recheck",
        ),
    ]


def _case_kwargs(case: dict[str, Any]) -> dict[str, Any]:
    """Expand a :func:`_every_case` case into ``_place`` kwargs.

    The injected doubles are built HERE rather than carried in the
    parametrize list, so no node id can depend on an object's identity — see
    :func:`_filing_cases`.
    """
    kwargs = dict(case)
    checks = kwargs.pop("recheck_checks", ())
    disposition = kwargs.pop("recheck_disposition", None)
    if disposition is not None:
        kwargs["recheck"] = _RecheckStub(disposition, checks)
    if kwargs.get("verdict") == "same-entity":
        kwargs["propose_merge"] = _ProposerStub()
    return kwargs


class TestNoPlacementGrantsOutreachEligibility:
    @pytest.mark.parametrize("case", _every_case())
    def test_no_outcome_ever_writes_observed(self, tmp_path: Path, case: dict[str, Any]) -> None:
        contacts = _contacts(tmp_path)
        kwargs = _case_kwargs(case)
        value = kwargs.get("value", _VALUE)

        placement = _place(contacts, **kwargs)

        # Nothing anywhere on the surface carries an outreach-eligible class.
        for path in sorted(contacts.rglob("*.md")):
            meta = _meta(path)
            for entry in pii.contact_classification_entries(meta):
                assert entry.get("usage_class") == pii.USAGE_CLASS_UNCLASSIFIED
            for identifier in pii.identifiers_on_record(meta):
                assert pii.is_outreach_eligible(meta, identifier) is False

        if placement.outcome == pii.PLACEMENT_FILED:
            assert placement.usage_class == pii.USAGE_CLASS_UNCLASSIFIED
            assert placement.path is not None
            meta = _meta(placement.path)
            assert pii.classification_for_value(meta, value).usage_class == (
                pii.USAGE_CLASS_UNCLASSIFIED
            )
            assert pii.is_outreach_eligible(meta, value) is False
        if placement.field is not None:
            assert placement.usage_class == pii.USAGE_CLASS_UNCLASSIFIED

    def test_no_branch_of_the_executor_mentions_the_observed_class(self) -> None:
        """Structural, so a future edit reintroducing it fails loudly.

        Every branch reads ``pii._PLACEMENT_USAGE_CLASS``; neither the
        ``USAGE_CLASS_OBSERVED`` name nor a bare ``"observed"`` literal
        appears in the executor's source.
        """
        source = inspect.getsource(pii.place_contact_value)
        assert "USAGE_CLASS_OBSERVED" not in source
        assert '"observed"' not in source
        assert "'observed'" not in source
        assert "_PLACEMENT_USAGE_CLASS" in source
        assert pii._PLACEMENT_USAGE_CLASS == pii.USAGE_CLASS_UNCLASSIFIED
        assert pii._PLACEMENT_USAGE_CLASS not in pii.OUTREACH_ELIGIBLE_CLASSES

    def test_there_is_no_parameter_to_override_the_usage_class(self) -> None:
        params = inspect.signature(pii.place_contact_value).parameters
        assert "usage_class" not in params


# ---------------------------------------------------------------------------
# (5) The design §7.2 re-check
# ---------------------------------------------------------------------------


class TestReCheck:
    def test_suspect_goes_to_the_human_queue_and_writes_nothing(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        target_record = _seed_record(
            contacts, "target.md", uid=_TARGET_UID, emails=["target@example.org"]
        )
        owner_record = _seed_record(
            contacts, "owner.md", uid=_OWNER_UID, emails=["owner@example.org"]
        )
        before = _snapshot(contacts)
        stub = _RecheckStub("suspect", ("D2", "C1"))

        placement = _place(
            contacts,
            verdict="other-known",
            owner_uid=_OWNER_UID,
            owner_name=_OWNER_NAME,
            recheck=stub,
        )

        assert placement.outcome == pii.PLACEMENT_QUEUED
        assert placement.recheck_disposition == "suspect"
        assert placement.recheck_checks == ("D2", "C1")
        assert placement.path is None
        assert placement.write_outcome is None
        assert len(stub.calls) == 1
        assert _snapshot(contacts) == before
        assert pii.record_lists_identifier(_meta(target_record), _VALUE) is False
        assert pii.record_lists_identifier(_meta(owner_record), _VALUE) is False

    def test_the_recheck_is_bound_to_the_uid_about_to_be_written_to(
        self, tmp_path: Path
    ) -> None:
        contacts = _contacts(tmp_path)
        stub = _RecheckStub(pii_attribution.DISPOSITION_PASS)
        _place(
            contacts,
            verdict="other-known",
            owner_uid=_OWNER_UID,
            owner_name=_OWNER_NAME,
            recheck=stub,
            writer=pii_attribution.WRITER_RE_ATTRIBUTION,
        )
        value, kwargs = stub.calls[0]
        assert value == _VALUE
        assert kwargs["target_uid"] == _OWNER_UID
        assert kwargs["target_name"] == _OWNER_NAME
        assert kwargs["source_ref"] == "raw/2026-10-31-intro.md"
        assert kwargs["writer"] == pii_attribution.WRITER_RE_ATTRIBUTION
        assert kwargs["target_type"] is None

    def test_a_pass_recheck_files_and_reports_the_disposition(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        stub = _RecheckStub(pii_attribution.DISPOSITION_PASS)
        placement = _place(contacts, verdict="own", recheck=stub)
        assert placement.outcome == pii.PLACEMENT_FILED
        assert placement.recheck_disposition == pii_attribution.DISPOSITION_PASS

    def test_a_non_personal_recheck_files_nothing(self, tmp_path: Path) -> None:
        """Design §9.1: a D1-narrow value is never filed on its target."""
        contacts = _contacts(tmp_path)
        stub = _RecheckStub(
            pii_attribution.DISPOSITION_NON_PERSONAL, (pii_attribution.CHECK_D1_NARROW,)
        )
        placement = _place(contacts, verdict="own", recheck=stub)
        assert placement.outcome == pii.PLACEMENT_LOGGED
        assert placement.recheck_checks == (pii_attribution.CHECK_D1_NARROW,)
        assert _snapshot(contacts) == {}

    @pytest.mark.parametrize(
        "disposition", ["probably-fine", "", "PASS", "pass ", "suspect-ish"]
    )
    def test_an_unrecognised_disposition_refuses_the_write(
        self, tmp_path: Path, disposition: str
    ) -> None:
        """Design §6.3: the seam fails CLOSED, so only ``pass`` writes.

        A token outside the vocabulary — a typo, a renamed disposition, a
        result this function cannot read structurally — must refuse the write
        and go to the human queue. Falling through to the write would be
        fail-OPEN on exactly the gate the re-check exists to be.
        """
        contacts = _contacts(tmp_path)
        stub = _RecheckStub(disposition)
        placement = _place(contacts, verdict="own", recheck=stub)
        assert placement.outcome == pii.PLACEMENT_QUEUED
        assert placement.recheck_disposition == (disposition or None)
        assert "unrecognised disposition" in placement.detail
        assert placement.path is None
        assert placement.write_outcome is None
        assert len(stub.calls) == 1
        assert _snapshot(contacts) == {}

    def test_a_result_with_no_disposition_attribute_refuses_the_write(
        self, tmp_path: Path
    ) -> None:
        """The structural read's own failure mode: ``getattr`` yields nothing.

        A re-check whose result this module cannot read has not said the
        placement is safe, so it is queued rather than written.
        """

        class _Shapeless:
            def __call__(self, _value: str, **_kwargs: Any) -> object:
                return object()

        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict="own", recheck=_Shapeless())
        assert placement.outcome == pii.PLACEMENT_QUEUED
        assert placement.recheck_disposition is None
        assert _snapshot(contacts) == {}

    @pytest.mark.parametrize(
        "verdict", ["other-unknown", "non-personal", "unclear", "same-entity"]
    )
    def test_a_verdict_that_writes_nothing_never_re_checks(
        self, tmp_path: Path, verdict: str
    ) -> None:
        contacts = _contacts(tmp_path)
        stub = _RecheckStub("suspect")
        _place(
            contacts,
            verdict=verdict,
            owner_uid=_OWNER_UID,
            recheck=stub,
            propose_merge=_ProposerStub(),
        )
        assert stub.calls == []


# ---------------------------------------------------------------------------
# (6) Observe mode
# ---------------------------------------------------------------------------


class TestObserveMode:
    @pytest.mark.parametrize(
        ("case", "expected_uid", "expected_field"),
        [
            ({"verdict": "own"}, _TARGET_UID, "emails"),
            (
                {"verdict": "other-known", "owner_uid": _OWNER_UID},
                _OWNER_UID,
                "emails",
            ),
            (
                {"verdict": "alias", "alias_relation": "personal-alias"},
                _TARGET_UID,
                "alt_emails",
            ),
            ({"verdict": "other-unknown"}, None, None),
            ({"verdict": "non-personal"}, None, None),
        ],
    )
    def test_apply_false_writes_nothing_and_reports_the_proposal(
        self,
        tmp_path: Path,
        case: dict[str, Any],
        expected_uid: str | None,
        expected_field: str | None,
    ) -> None:
        contacts = _contacts(tmp_path)
        stub = _RecheckStub("suspect")
        proposer = _ProposerStub()

        placement = _place(
            contacts, apply=False, recheck=stub, propose_merge=proposer, **case
        )

        assert placement.outcome == pii.PLACEMENT_PROPOSED
        assert placement.placed_uid == expected_uid
        assert placement.field == expected_field
        assert placement.path is None
        assert placement.write_outcome is None
        assert stub.calls == []
        assert proposer.calls == []
        assert _snapshot(contacts) == {}

    def test_observe_mode_is_the_default(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = pii.place_contact_value(
            contacts,
            _VALUE,
            verdict="own",
            target_uid=_TARGET_UID,
            observed_at=_OBSERVED_AT,
        )
        assert placement.outcome == pii.PLACEMENT_PROPOSED
        assert _snapshot(contacts) == {}

    def test_observe_mode_names_the_outcome_apply_would_have_produced(
        self, tmp_path: Path
    ) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, apply=False, verdict="non-personal")
        assert pii.PLACEMENT_LOGGED in placement.detail


# ---------------------------------------------------------------------------
# (7) Alias placement
# ---------------------------------------------------------------------------


class TestAlias:
    def test_alias_writes_alt_emails_provenance_and_the_relation(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        own = _seed_record(contacts, "target.md", uid=_TARGET_UID, emails=["target@example.org"])

        placement = _place(contacts, verdict="alias", alias_relation="role-inbox")

        assert placement.outcome == pii.PLACEMENT_FILED
        assert placement.path == own
        meta = _meta(own)
        assert meta["alt_emails"] == [_VALUE]
        entries = meta["field_sources"]["alt_emails"]
        assert [set(e) for e in entries] == [{"value", "source"}]
        assert entries[0]["value"] == _VALUE
        assert entries[0]["source"]["type"] == "attribution"
        assert entries[0]["source"]["verdict"] == "alias"
        assert entries[0]["source"]["original_target"] == _TARGET_UID
        assert meta[pii.IDENTIFIER_RELATION_FIELD] == [
            {
                "identifier": _VALUE,
                "relation": "role-inbox",
                "source": "raw/2026-10-31-intro.md",
                "decided_by": "model",
            }
        ]

    def test_a_phone_alias_writes_phones(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        own = _seed_record(contacts, "target.md", uid=_TARGET_UID, emails=["target@example.org"])

        placement = _place(contacts, value=_PHONE, verdict="alias", alias_relation="shared")

        assert placement.field == "phones"
        meta = _meta(own)
        assert meta["phones"] == [_PHONE]
        assert "alt_emails" not in meta
        assert [e["identifier"] for e in meta[pii.IDENTIFIER_RELATION_FIELD]] == [
            pii.normalize_identifier(_PHONE)
        ]

    def test_a_changed_relation_replaces_the_entry_rather_than_appending(
        self, tmp_path: Path
    ) -> None:
        contacts = _contacts(tmp_path)
        own = _seed_record(contacts, "target.md", uid=_TARGET_UID, emails=["target@example.org"])
        _place(contacts, verdict="alias", alias_relation="role-inbox")
        _place(contacts, verdict="alias", alias_relation="former-name")
        entries = _meta(own)[pii.IDENTIFIER_RELATION_FIELD]
        assert len(entries) == 1
        assert entries[0]["relation"] == "former-name"

    def test_the_relation_vocabulary_is_validated_at_the_merge(self) -> None:
        with pytest.raises(ValueError, match="unknown identifier relation"):
            pii._merge_identifier_relation(
                {}, _VALUE, relation="role-inbox-ish", source=None, decided_by="model"
            )


# ---------------------------------------------------------------------------
# (8) same-entity
# ---------------------------------------------------------------------------


class TestSameEntity:
    def test_the_proposer_is_called_with_the_documented_contract(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        _seed_record(contacts, "target.md", uid=_TARGET_UID, emails=["target@example.org"])
        _seed_record(contacts, "owner.md", uid=_OWNER_UID, emails=["owner@example.org"])
        before = _snapshot(contacts)
        proposer = _ProposerStub()

        placement = _place(
            contacts, verdict="same-entity", owner_uid=_OWNER_UID, propose_merge=proposer
        )

        assert placement.outcome == pii.PLACEMENT_MERGE_PROPOSED
        assert placement.path is None
        assert placement.write_outcome is None
        assert _snapshot(contacts) == before
        assert len(proposer.calls) == 1
        call = proposer.calls[0]
        assert set(call) == {"target_uid", "owner_uid", "value", "rationale"}
        assert call["target_uid"] == _TARGET_UID
        assert call["owner_uid"] == _OWNER_UID
        assert call["value"] == _VALUE

    def test_the_rationale_carries_a_masked_value_only(self, tmp_path: Path) -> None:
        """``_pending_merges.md`` lives in the wiki; design §3 keeps contact
        values off it."""
        contacts = _contacts(tmp_path)
        proposer = _ProposerStub()
        _place(contacts, verdict="same-entity", owner_uid=_OWNER_UID, propose_merge=proposer)
        rationale = proposer.calls[0]["rationale"]
        assert _VALUE not in rationale
        assert pii.mask_contact_address(_VALUE) in rationale

    def test_no_proposer_writes_nothing_at_all(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict="same-entity", owner_uid=_OWNER_UID)
        assert placement.outcome == pii.PLACEMENT_SKIPPED
        assert "no propose_merge injected" in placement.detail
        assert _snapshot(contacts) == {}

    def test_no_owner_skips(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        proposer = _ProposerStub()
        placement = _place(contacts, verdict="same-entity", propose_merge=proposer)
        assert placement.outcome == pii.PLACEMENT_SKIPPED
        assert proposer.calls == []


# ---------------------------------------------------------------------------
# (9) Provenance shape (design §7.2, §8.1)
# ---------------------------------------------------------------------------


class TestProvenanceShape:
    def test_a_model_decision_carries_every_design_field(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(
            contacts,
            verdict="other-known",
            owner_uid=_OWNER_UID,
            owner_name=_OWNER_NAME,
            writer=pii_attribution.WRITER_RE_ATTRIBUTION,
            model="a-measured-model",
            prompt_sha256="0" * 64,
            calibrated_p=0.97,
        )
        assert placement.path is not None
        entries = _meta(placement.path)["field_sources"]["emails"]
        assert len(entries) == 1
        assert entries[0]["value"] == _VALUE
        assert entries[0]["source"] == {
            "type": "attribution",
            "original_source": "raw/2026-10-31-intro.md",
            "original_target": _TARGET_UID,
            "writer": "re-attribution",
            "verdict": "other-known",
            "model": "a-measured-model",
            "prompt_sha256": "0" * 64,
            "calibrated_p": 0.97,
            "decided_by": "model",
        }

    def test_a_human_decision_carries_no_calibrated_probability(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict="own", decided_by="human")
        assert placement.path is not None
        source = _meta(placement.path)["field_sources"]["emails"][0]["source"]
        assert source["decided_by"] == "human"
        for omitted in ("model", "prompt_sha256", "calibrated_p"):
            assert omitted not in source
        for always in ("type", "original_target", "writer", "verdict", "decided_by"):
            assert always in source

    def test_the_classification_entry_carries_the_same_provenance(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict="own", model="a-measured-model")
        assert placement.path is not None
        meta = _meta(placement.path)
        classification = pii.classification_for_value(meta, _VALUE)
        assert classification.source == meta["field_sources"]["emails"][0]["source"]

    def test_a_changed_provenance_replaces_the_entry(self, tmp_path: Path) -> None:
        contacts = _contacts(tmp_path)
        placement = _place(contacts, verdict="own", calibrated_p=0.91)
        _place(contacts, verdict="own", calibrated_p=0.99)
        assert placement.path is not None
        entries = _meta(placement.path)["field_sources"]["emails"]
        assert len(entries) == 1
        assert entries[0]["source"]["calibrated_p"] == 0.99


# ---------------------------------------------------------------------------
# (10) Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.parametrize("case", _filing_cases())
    def test_a_repeat_placement_rewrites_nothing(
        self, tmp_path: Path, case: dict[str, Any]
    ) -> None:
        contacts = _contacts(tmp_path)
        _seed_record(contacts, "target.md", uid=_TARGET_UID, emails=["target@example.org"])
        kwargs = _case_kwargs(case)
        first = _place(contacts, **kwargs)
        assert first.outcome == pii.PLACEMENT_FILED
        before = _snapshot(contacts)

        second = _place(contacts, **_case_kwargs(case))

        assert second.outcome == pii.PLACEMENT_FILED
        assert _snapshot(contacts) == before


# ---------------------------------------------------------------------------
# (11) Restated vocabularies cannot drift
# ---------------------------------------------------------------------------


class TestVocabulariesArePinned:
    def test_identifier_relations_match_the_alias_relation_vocabulary(self) -> None:
        assert pii.IDENTIFIER_RELATIONS == pii_attribution.ALIAS_RELATIONS

    def test_placement_verdicts_match_the_judgement_vocabulary(self) -> None:
        assert pii.PLACEMENT_VERDICTS == pii_attribution.JUDGEMENT_VERDICTS

    def test_the_default_writer_is_the_re_attribution_writer_id(self) -> None:
        default = inspect.signature(pii.place_contact_value).parameters["writer"].default
        assert default == pii_attribution.WRITER_RE_ATTRIBUTION
        assert default in pii_attribution.WRITER_IDS

    def test_the_dispositions_the_executor_tests_against_are_the_real_ones(self) -> None:
        assert pii._DISPOSITION_PASS == pii_attribution.DISPOSITION_PASS
        assert pii._DISPOSITION_SUSPECT == pii_attribution.DISPOSITION_SUSPECT
        assert pii._DISPOSITION_NON_PERSONAL == pii_attribution.DISPOSITION_NON_PERSONAL
