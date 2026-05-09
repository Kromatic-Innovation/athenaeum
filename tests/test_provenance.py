# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.provenance — per-claim source parsing/validation."""
from __future__ import annotations

import pytest

from athenaeum.provenance import (
    SourceRef,
    parse_per_value_field_sources,
    parse_source,
    resolve_remember_sources,
    validate_field_sources,
    validate_source_value,
)


class TestParseSourceScalar:
    def test_simple_scalar(self) -> None:
        ref = parse_source("api:apollo")
        assert isinstance(ref, SourceRef)
        assert ref.type == "api"
        assert ref.ref == "apollo"

    def test_scalar_with_colons_in_ref(self) -> None:
        # ref segment is permissive — colons allowed after the first split.
        ref = parse_source("claude:session-2026-05-08:turn-3")
        assert ref.type == "claude"
        assert ref.ref == "session-2026-05-08:turn-3"

    def test_scalar_with_dashes_in_type(self) -> None:
        ref = parse_source("manual-import:contacts-2026-04")
        assert ref.type == "manual-import"
        assert ref.ref == "contacts-2026-04"

    @pytest.mark.parametrize(
        "bad",
        [
            ":missing-type",
            "type:",
            "Type:has-uppercase",
            "1numeric:start",
            "type:has\nnewline",
            "Has-Uppercase",  # legacy form must also be lowercase-only
            "has spaces",
            "",  # empty
            "type:   ",  # whitespace-only ref
            "api:apollo ",  # trailing whitespace on typed form
            " api:apollo",  # leading whitespace
        ],
    )
    def test_malformed_scalar_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_source(bad)

    def test_legacy_single_token_accepted(self) -> None:
        # Pre-#90 wikis store ``source:`` as a bare slug (no colon).
        # ~15k live wikis use this form; the validator must accept them.
        ref = parse_source("extended-tier-build")
        assert ref.type == "legacy"
        assert ref.ref == "extended-tier-build"

        ref2 = parse_source("warm-network-detect")
        assert ref2.ref == "warm-network-detect"

        # And the typed form still works on the same call site.
        typed = parse_source("script:extended-tier-build")
        assert typed.type == "script"
        assert typed.ref == "extended-tier-build"


class TestParseSourceStructured:
    def test_minimal_dict(self) -> None:
        ref = parse_source({"type": "api", "ref": "apollo"})
        assert ref.type == "api"
        assert ref.ref == "apollo"
        assert ref.ts is None
        assert ref.confidence is None

    def test_full_dict(self) -> None:
        ref = parse_source(
            {
                "type": "api",
                "ref": "apollo",
                "ts": "2026-05-07T12:00:00Z",
                "confidence": 0.92,
                "notes": "bulk_match endpoint",
            }
        )
        assert ref.confidence == 0.92
        assert ref.notes == "bulk_match endpoint"

    def test_unknown_extra_keys_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_source({"type": "api", "ref": "x", "ssource": "typo"})

    def test_extra_keys_forbid_explicit(self) -> None:
        # Explicit Quine-flagged case: extra="forbid" must reject any
        # unknown key on the structured form, not just typos.
        with pytest.raises(ValueError):
            parse_source({"type": "api", "ref": "x", "extra_key": "y"})

    def test_missing_required_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_source({"type": "api"})

    def test_confidence_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_source({"type": "api", "ref": "x", "confidence": 1.5})


class TestParseSourceNone:
    def test_none_passes_through(self) -> None:
        assert parse_source(None) is None

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_source(42)


class TestValidateSourceValue:
    def test_returns_original_scalar_unchanged(self) -> None:
        # Round-trip fidelity: validator must not normalize scalar to dict.
        assert validate_source_value("api:apollo") == "api:apollo"

    def test_returns_original_dict_unchanged(self) -> None:
        d = {"type": "api", "ref": "apollo"}
        assert validate_source_value(d) is d

    def test_none_passes(self) -> None:
        assert validate_source_value(None) is None

    def test_malformed_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_source_value("Has-Uppercase")  # neither typed nor legacy

    def test_legacy_passes(self) -> None:
        # Legacy single-token form must round-trip unchanged.
        assert validate_source_value("extended-tier-build") == "extended-tier-build"


class TestValidateFieldSources:
    def test_dict_of_scalars(self) -> None:
        v = {"emails": "api:apollo", "current_title": "linkedin:nicole"}
        assert validate_field_sources(v) is v

    def test_dict_of_mixed(self) -> None:
        v = {
            "emails": "api:apollo",
            "phone": {"type": "manual", "ref": "vcard-import-2026-04"},
        }
        assert validate_field_sources(v) is v

    def test_none_passes(self) -> None:
        assert validate_field_sources(None) is None

    def test_non_dict_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_field_sources("api:apollo")

    def test_bad_inner_value_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_field_sources({"emails": "Has-Uppercase"})

    def test_empty_key_raises(self) -> None:
        with pytest.raises(ValueError):
            validate_field_sources({"": "api:apollo"})


class TestPerValueFieldSources:
    """Per-value list-of-records shape for list fields (issue #102)."""

    def test_parse_basic_per_value_list(self) -> None:
        v = [
            {"value": "a@x.com", "source": "api:apollo:2026-04-29"},
            {"value": "b@y.com", "source": "linkedin:tristankromer"},
        ]
        assert parse_per_value_field_sources(v) is v

    def test_parse_per_value_with_dict_source(self) -> None:
        v = [
            {
                "value": "a@x.com",
                "source": {"type": "api", "ref": "apollo", "confidence": 0.9},
            },
        ]
        assert parse_per_value_field_sources(v) is v

    def test_parse_per_value_dict_value(self) -> None:
        # value can be a dict (employment_history shape, §2.2)
        v = [
            {
                "value": {"company": "Kromatic", "title": "Founder"},
                "source": "api:apollo:2026-04-29",
            },
        ]
        assert parse_per_value_field_sources(v) is v

    def test_empty_list_accepted(self) -> None:
        assert parse_per_value_field_sources([]) == []

    def test_missing_value_key_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_per_value_field_sources([{"source": "api:apollo"}])

    def test_missing_source_key_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_per_value_field_sources([{"value": "a@x.com"}])

    def test_extra_keys_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_per_value_field_sources(
                [{"value": "a@x.com", "source": "api:apollo", "ts": "2026"}]
            )

    def test_bad_source_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_per_value_field_sources(
                [{"value": "a@x.com", "source": "Has-Uppercase"}]
            )

    def test_non_list_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_per_value_field_sources({"value": "a@x.com", "source": "api:apollo"})

    def test_validate_field_sources_accepts_per_value(self) -> None:
        v = {
            "emails": [
                {"value": "a@x.com", "source": "api:apollo:2026"},
                {"value": "b@y.com", "source": "linkedin:foo"},
            ]
        }
        assert validate_field_sources(v) is v

    def test_validate_field_sources_mixed_legacy_and_per_value(self) -> None:
        # Same map can carry legacy scalar (current_title) and per-value
        # list (emails) entries — readers must accept both.
        v = {
            "current_title": "linkedin:foo",
            "emails": [
                {"value": "a@x.com", "source": "api:apollo:2026"},
            ],
        }
        assert validate_field_sources(v) is v

    def test_validate_field_sources_per_value_invalid_record(self) -> None:
        with pytest.raises(ValueError):
            validate_field_sources({"emails": [{"value": "a@x.com"}]})  # missing source


class TestSourceRefToScalar:
    def test_round_trip(self) -> None:
        ref = parse_source("api:apollo")
        assert ref.to_scalar() == "api:apollo"


class TestResolveRememberSources:
    """Disambiguation helper for MCP ``remember(sources=...)`` (issue #96).

    Locks design-lock §4 in ``docs/provenance-shape.md``: explicit
    ``_source`` / ``_field_sources`` wrappers; the bare-dict heuristic
    is removed; bare scalar shorthand stays.
    """

    def test_none_returns_both_none(self) -> None:
        assert resolve_remember_sources(None) == (None, None)

    def test_bare_scalar(self) -> None:
        ws, fs = resolve_remember_sources("api:apollo:2026-04-29")
        assert ws == "api:apollo:2026-04-29"
        assert fs is None

    def test_source_wrapper_structured(self) -> None:
        payload = {"_source": {"type": "api", "ref": "apollo"}}
        ws, fs = resolve_remember_sources(payload)
        assert ws == {"type": "api", "ref": "apollo"}
        assert fs is None

    def test_field_sources_wrapper(self) -> None:
        payload = {"_field_sources": {"emails": "api:apollo:2026-04-29"}}
        ws, fs = resolve_remember_sources(payload)
        assert ws is None
        assert fs == {"emails": "api:apollo:2026-04-29"}

    def test_both_wrappers(self) -> None:
        payload = {
            "_source": "api:apollo:2026-04-29",
            "_field_sources": {"linkedin_url": "linkedin:tristankromer"},
        }
        ws, fs = resolve_remember_sources(payload)
        assert ws == "api:apollo:2026-04-29"
        assert fs == {"linkedin_url": "linkedin:tristankromer"}

    def test_pathological_bare_type_ref_rejected(self) -> None:
        # Locked pathological case from issue #96: bare dict whose user
        # fields happen to be named ``type`` / ``ref``.
        with pytest.raises(ValueError, match="_field_sources"):
            resolve_remember_sources({"type": "api:x", "ref": "linkedin:y"})

    def test_pathological_via_field_sources_wrapper_works(self) -> None:
        payload = {"_field_sources": {"type": "api:x", "ref": "linkedin:y"}}
        ws, fs = resolve_remember_sources(payload)
        assert ws is None
        assert fs == {"type": "api:x", "ref": "linkedin:y"}

    def test_bare_dict_without_wrappers_rejected(self) -> None:
        with pytest.raises(ValueError, match="_field_sources"):
            resolve_remember_sources({"emails": "api:apollo:2026-04-29"})

    def test_wrong_type_rejected(self) -> None:
        with pytest.raises(TypeError):
            resolve_remember_sources(42)

    def test_empty_dict_rejected(self) -> None:
        with pytest.raises(ValueError, match="_field_sources"):
            resolve_remember_sources({})

    def test_mixed_unknown_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="_field_sources"):
            resolve_remember_sources({"_source": "api:x:y", "stray": "junk"})

    def test_field_sources_value_not_dict_rejected(self) -> None:
        with pytest.raises(ValueError):
            resolve_remember_sources({"_field_sources": "not-a-dict"})

    def test_invalid_inner_source_rejected(self) -> None:
        with pytest.raises(ValueError):
            resolve_remember_sources({"_source": "Has-Uppercase"})
