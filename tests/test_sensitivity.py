# SPDX-License-Identifier: Apache-2.0
"""Tests for the sensitivity recogniser protocol + registry.

S1a of athenaeum#910's design note; filed as athenaeum#989.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from athenaeum import sensitivity
from athenaeum.sensitivity import (
    SensitivityConfigError,
    SensitivityMatch,
    SensitivityRecognizer,
    available_recognizers,
    register_recognizer,
)


@pytest.fixture(autouse=True)
def _isolate_registered_recognizers() -> None:
    """Snapshot/restore the in-process recogniser registry between tests.

    Mirrors :mod:`tests.test_storage`'s ``_isolate_registered_adapters``
    fixture for the same reason: a test that registers a custom recogniser
    (or attempts to and fails) must never leak state into the next test.
    """
    snapshot = dict(sensitivity._REGISTERED_RECOGNIZERS)
    try:
        yield
    finally:
        sensitivity._REGISTERED_RECOGNIZERS.clear()
        sensitivity._REGISTERED_RECOGNIZERS.update(snapshot)


# ---------------------------------------------------------------------------
# SensitivityMatch / SensitivityConfigError shape
# ---------------------------------------------------------------------------


class TestShapes:
    def test_sensitivity_match_is_a_frozen_dataclass(self) -> None:
        m = SensitivityMatch(recognizer="email", value="a@b.com", field=None, span=(0, 7))
        assert (m.recognizer, m.value, m.field, m.span) == ("email", "a@b.com", None, (0, 7))
        with pytest.raises(Exception):
            m.value = "changed"  # type: ignore[misc]

    def test_sensitivity_match_field_and_span_default_none(self) -> None:
        m = SensitivityMatch(recognizer="email", value="a@b.com")
        assert m.field is None
        assert m.span is None

    def test_sensitivity_config_error_is_a_value_error(self) -> None:
        assert issubclass(SensitivityConfigError, ValueError)

    def test_protocol_shape(self) -> None:
        # AC1: the protocol carries a `name` attribute and a keyword-only
        # `detect(*, text, frontmatter)` method.
        assert hasattr(SensitivityRecognizer, "detect")
        recognizers = available_recognizers(None)
        for rec in recognizers.values():
            assert isinstance(rec, SensitivityRecognizer)
            assert isinstance(rec.name, str) and rec.name


# ---------------------------------------------------------------------------
# register_recognizer — the three documented outcomes (AC2)
# ---------------------------------------------------------------------------


class TestRegisterRecognizer:
    def test_cannot_shadow_builtin(self) -> None:
        class _FakeEmail:
            name = "email"

            def detect(self, *, text: str, frontmatter=None) -> list[SensitivityMatch]:
                return []

        with pytest.raises(SensitivityConfigError, match="shadows a built-in"):
            register_recognizer(_FakeEmail())

    def test_cannot_shadow_builtin_even_with_replace(self) -> None:
        class _FakePhone:
            name = "phone"

            def detect(self, *, text: str, frontmatter=None) -> list[SensitivityMatch]:
                return []

        with pytest.raises(SensitivityConfigError, match="shadows a built-in"):
            register_recognizer(_FakePhone(), replace=True)

    def test_duplicate_registration_raises_without_replace(self) -> None:
        class _Custom:
            name = "custom-dup"

            def detect(self, *, text: str, frontmatter=None) -> list[SensitivityMatch]:
                return []

        register_recognizer(_Custom())
        with pytest.raises(SensitivityConfigError, match="already registered"):
            register_recognizer(_Custom())

    def test_replace_true_overrides(self) -> None:
        class _CustomV1:
            name = "custom-replace"

            def detect(self, *, text: str, frontmatter=None) -> list[SensitivityMatch]:
                return [SensitivityMatch(recognizer=self.name, value="v1")]

        class _CustomV2:
            name = "custom-replace"

            def detect(self, *, text: str, frontmatter=None) -> list[SensitivityMatch]:
                return [SensitivityMatch(recognizer=self.name, value="v2")]

        register_recognizer(_CustomV1())
        register_recognizer(_CustomV2(), replace=True)
        result = available_recognizers(None)["custom-replace"].detect(text="", frontmatter=None)
        assert result == [SensitivityMatch(recognizer="custom-replace", value="v2")]


# ---------------------------------------------------------------------------
# available_recognizers — built-ins union code-registered; config never
# springs one into existence (AC3)
# ---------------------------------------------------------------------------


class TestAvailableRecognizers:
    def test_builtins_present_by_default(self) -> None:
        recognizers = available_recognizers(None)
        assert {"email", "phone"} <= set(recognizers)

    def test_config_naming_an_unregistered_recognizer_does_not_create_one(self) -> None:
        config = {
            "sensitivity": {
                "classes": {
                    "custom": {"recognizers": ["totally-unregistered-recognizer"]}
                }
            }
        }
        recognizers = available_recognizers(config)
        assert "totally-unregistered-recognizer" not in recognizers
        # Built-ins are unaffected by an unrelated config block.
        assert {"email", "phone"} <= set(recognizers)

    def test_custom_recognizer_indistinguishable_in_shape_from_builtins(self) -> None:
        class _Custom:
            name = "custom-shape"

            def detect(self, *, text: str, frontmatter=None) -> list[SensitivityMatch]:
                return [SensitivityMatch(recognizer=self.name, value="x")]

        register_recognizer(_Custom())
        recognizers = available_recognizers(None)
        assert set(["email", "phone", "custom-shape"]) <= set(recognizers)
        # Every entry — built-in or custom — is reached the same way: a plain
        # dict lookup returning something satisfying the SAME protocol, with
        # no is-builtin flag or wrapper distinguishing the two populations.
        for name in ("email", "phone", "custom-shape"):
            rec = recognizers[name]
            assert isinstance(rec, SensitivityRecognizer)
            assert rec.name == name
            assert isinstance(rec.detect(text="", frontmatter=None), list)


# ---------------------------------------------------------------------------
# Built-in registration happens through the public call, at import time (AC4)
# ---------------------------------------------------------------------------


class TestBuiltinRegistrationCall:
    def test_email_and_phone_are_registered_at_import_time(self) -> None:
        # No test in this module calls register_recognizer(email/phone) —
        # they must already be present purely from having imported the module.
        assert "email" in sensitivity._REGISTERED_RECOGNIZERS
        assert "phone" in sensitivity._REGISTERED_RECOGNIZERS

    def test_builtin_names_are_the_protected_set(self) -> None:
        assert sensitivity._BUILTIN_RECOGNIZER_NAMES == frozenset({"email", "phone"})


# ---------------------------------------------------------------------------
# detect() purity/offline-ness + phone false-positive suppression (AC5)
# ---------------------------------------------------------------------------


class TestEmailRecognizer:
    def test_detects_emails_with_spans(self) -> None:
        rec = available_recognizers(None)["email"]
        text = "reach alice@example.com or bob@test.co"
        matches = rec.detect(text=text, frontmatter=None)
        assert [m.value for m in matches] == ["alice@example.com", "bob@test.co"]
        for m in matches:
            assert m.recognizer == "email"
            assert m.span is not None
            start, end = m.span
            assert text[start:end] == m.value

    def test_no_match_returns_empty_list(self) -> None:
        rec = available_recognizers(None)["email"]
        assert rec.detect(text="no email here", frontmatter=None) == []

    def test_repeated_value_yields_one_match_per_occurrence(self) -> None:
        # Option (a)'s span decision: unlike find_inline_emails (deduped),
        # each occurrence gets its own SensitivityMatch with its own span.
        rec = available_recognizers(None)["email"]
        text = "alice@example.com and again alice@example.com"
        matches = rec.detect(text=text, frontmatter=None)
        assert len(matches) == 2
        assert matches[0].span != matches[1].span


class TestPhoneRecognizer:
    def test_detects_phones_with_spans(self) -> None:
        rec = available_recognizers(None)["phone"]
        text = "call +1-555-0100 now"
        matches = rec.detect(text=text, frontmatter=None)
        assert [m.value for m in matches] == ["+1-555-0100"]
        start, end = matches[0].span
        assert text[start:end] == "+1-555-0100"

    def test_repeated_value_yields_one_match_per_occurrence(self) -> None:
        rec = available_recognizers(None)["phone"]
        text = "call +1-555-0100 now and again +1-555-0100 later"
        matches = rec.detect(text=text, frontmatter=None)
        assert len(matches) == 2
        assert matches[0].value == matches[1].value == "+1-555-0100"
        assert matches[0].span != matches[1].span

    def test_no_match_returns_empty_list(self) -> None:
        rec = available_recognizers(None)["phone"]
        assert rec.detect(text="issue athenaeum#427 page 12", frontmatter=None) == []

    @pytest.mark.parametrize(
        "text",
        [
            "Last contact: 2015-12-03 per CRM",  # ISO date (athenaeum#500)
            "Active 2019-2020 season",  # year range
            "uid 00075741 tail",  # bare id fragment below E.164 band
            "closed 2026-04-27)\n\n1 item done",  # line-spanning bleed (athenaeum#683)
            "see 9798183760910 elsewhere",  # bare ISBN-13 (athenaeum#732)
            "410-414-416-412",  # 4-group single-dash list (athenaeum#732)
        ],
    )
    def test_suppresses_excluded_phone_shapes(self, text: str) -> None:
        # AC5: _is_excluded_phone_shape's exclusions (ISO dates, year ranges,
        # bare id/analytics fragments, line-spanning bleed, bare ISBN-13, and
        # the 4-group-no-plus list shape) must still be suppressed.
        rec = available_recognizers(None)["phone"]
        assert rec.detect(text=text, frontmatter=None) == []

    @pytest.mark.parametrize(
        "text",
        [
            'the business entity "Kromatic" (QBO realm 1008563730)',
            "prod GA4 stream (`G-EYDNWEV55B`, stream `5139685489`)",
            "billing realm: 1008563730 for the tenant",
        ],
    )
    def test_suppresses_labeled_identifier_prefixes(self, text: str) -> None:
        # AC5: _has_labeled_identifier_prefix's exclusions (athenaeum#732) must
        # still be suppressed.
        rec = available_recognizers(None)["phone"]
        assert rec.detect(text=text, frontmatter=None) == []

    def test_label_does_not_eat_an_unrelated_following_phone(self) -> None:
        rec = available_recognizers(None)["phone"]
        matches = rec.detect(text="realm42 917-231-6130", frontmatter=None)
        assert [m.value for m in matches] == ["917-231-6130"]


class TestDetectIsPureAndOffline:
    def test_no_module_level_io_or_network_symbol_imported(self) -> None:
        # AC5: pure, offline, deterministic. sensitivity.py imports nothing
        # network- or filesystem-capable (see the module's own layering note).
        src = Path(sensitivity.__file__).read_text(encoding="utf-8")
        for forbidden in ("socket", "requests", "urllib", "httpx", " open(", "atomic_write_text"):
            assert forbidden not in src, forbidden

    def test_detect_is_deterministic(self) -> None:
        text = "alice@example.com and +1-555-0100"
        for rec in available_recognizers(None).values():
            first = rec.detect(text=text, frontmatter=None)
            second = rec.detect(text=text, frontmatter=None)
            assert first == second


# ---------------------------------------------------------------------------
# pii.py is untouched (AC7) — a repo-level check, not a unit test of
# behaviour, but cheap and load-bearing enough to assert here.
# ---------------------------------------------------------------------------


class TestPiiModuleUntouched:
    def test_pii_public_detectors_still_importable_and_unchanged_shape(self) -> None:
        from athenaeum.pii import find_inline_emails, find_inline_phones

        assert find_inline_emails("a@b.com") == ["a@b.com"]
        assert find_inline_phones("call +1-555-0100 now") == ["+1-555-0100"]


# ---------------------------------------------------------------------------
# Import-time side effects (AC8) — fresh interpreter, track every open()
# ---------------------------------------------------------------------------


def test_import_has_no_file_io_beyond_module_source_loading() -> None:
    """Importing athenaeum.sensitivity reads/writes no data file (AC8).

    Runs in a FRESH interpreter (a subprocess) so nothing already imported by
    the test session's own collection masks a real side effect. Every
    ``open()`` call made while importing the module is recorded; the only
    tolerated opens are Python's own module-source loading (``.py``/``.pyc``,
    ``__pycache__``) — anything else (a wiki page, a config file, a knowledge
    corpus path) would mean the module does I/O merely by being imported,
    which the design note's layering note says it must not.
    """
    src_root = Path(__file__).resolve().parent.parent / "src"
    script = """
import builtins
opened = []
_real_open = builtins.open

def _tracking_open(file, *a, **kw):
    opened.append(str(file))
    return _real_open(file, *a, **kw)

builtins.open = _tracking_open

import athenaeum.sensitivity  # noqa: F401

def _is_source_load(p):
    return p.endswith(".py") or p.endswith(".pyc") or "__pycache__" in p

non_source = [p for p in opened if not _is_source_load(p)]
assert non_source == [], f"unexpected file I/O during import: {non_source!r}"
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PYTHONPATH": str(src_root), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
