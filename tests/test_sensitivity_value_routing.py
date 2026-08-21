# SPDX-License-Identifier: Apache-2.0
"""Tests for the routing/redaction mechanism (issue athenaeum#1023).

Slice 2/4 of athenaeum#949's design note
(`docs/sensitivity-value-routing.md`):
:func:`athenaeum.sensitivity_routing.route_sensitive_values`. Covers the
pointer contract (AC2), fail-closed behaviour on every failure mode the
design note's §6 names (AC10), deterministic overlap precedence (AC7),
and idempotent re-entrancy (AC11) — all against fixture temp directories,
synthetic values only, never a live vault.

Deliberately out of scope here (see the module docstring and the issue's
"Slice discipline"): `resolve_sensitive_record` (the record-keyed read
path, athenaeum#1024) and any `librarian.process_one` wiring
(athenaeum#1025) — this module is not called from anywhere in this repo
yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from athenaeum import sensitivity
from athenaeum.sensitivity_routing import SensitivityRoutingError, route_sensitive_values

SYNTHETIC_EMAIL = "test.user.demo@example.invalid"
SYNTHETIC_EMAIL_2 = "second.user.demo@example.invalid"
ROUTING_ON = {"sensitivity": {"routing": {"enabled": True}}}


@pytest.fixture(autouse=True)
def _isolate_registered_recognizers() -> None:
    """Snapshot/restore the in-process recogniser registry (mirrors test_sensitivity.py).

    Two tests below register a throwaway recognizer to exercise the
    field-based-match and multi-class-precedence paths; this keeps that
    registration from leaking into other test modules.
    """
    snapshot = dict(sensitivity._REGISTERED_RECOGNIZERS)
    try:
        yield
    finally:
        sensitivity._REGISTERED_RECOGNIZERS.clear()
        sensitivity._REGISTERED_RECOGNIZERS.update(snapshot)


class TestRouteSensitiveValues:
    def test_disabled_by_default_is_a_no_op(self, tmp_path: Path) -> None:
        text = f"Reach out to {SYNTHETIC_EMAIL} about the offsite."
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=None,
            knowledge_root=tmp_path,
        )
        assert out == text

    def test_disabled_explicitly_is_a_no_op(self, tmp_path: Path) -> None:
        text = f"Reach out to {SYNTHETIC_EMAIL}."
        cfg = {"sensitivity": {"routing": {"enabled": False}}}
        out = route_sensitive_values(
            raw_ref="src/a.md", text=text, frontmatter={}, config=cfg, knowledge_root=tmp_path
        )
        assert out == text

    def test_no_match_returns_text_unchanged(self, tmp_path: Path) -> None:
        text = "Nothing sensitive here at all."
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        assert out == text

    def test_routes_and_redacts_a_match(self, tmp_path: Path) -> None:
        text = f"Reach out to {SYNTHETIC_EMAIL} about the offsite."
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        assert SYNTHETIC_EMAIL not in out
        assert "[sensitive:pii:" in out

    def test_pointer_does_not_leak_the_value(self, tmp_path: Path) -> None:
        """AC2/AC12: the pointer string itself must never contain the value."""
        text = f"Contact {SYNTHETIC_EMAIL} directly."
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        assert SYNTHETIC_EMAIL not in out
        assert SYNTHETIC_EMAIL.split("@")[0] not in out
        # The vault record filename must not embed the value either.
        vault_files = list((tmp_path / "excluded" / "sensitivity" / "pii").glob("*.md"))
        assert len(vault_files) == 1
        assert SYNTHETIC_EMAIL not in vault_files[0].name

    def test_vault_record_holds_the_value_and_no_usage_class(self, tmp_path: Path) -> None:
        """AC9: routed values are never auto-stamped with a usage_class."""
        text = f"Contact {SYNTHETIC_EMAIL} directly."
        route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        vault_files = list((tmp_path / "excluded" / "sensitivity" / "pii").glob("*.md"))
        assert len(vault_files) == 1
        record_text = vault_files[0].read_text()
        assert SYNTHETIC_EMAIL in record_text
        assert "usage_class" not in record_text

    def test_distinct_values_get_distinguishable_pointers(self, tmp_path: Path) -> None:
        text = f"{SYNTHETIC_EMAIL} and {SYNTHETIC_EMAIL_2} were both cc'd."
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        pointers = re.findall(r"\[sensitive:pii:[0-9a-f]{32}", out)
        assert len(pointers) == 2
        assert pointers[0] != pointers[1]

    def test_repeated_value_gets_distinguishable_pointers(self, tmp_path: Path) -> None:
        """AC2: 'distinct values on one page yield distinguishable pointers' —
        and repeated occurrences of the SAME value get distinct pointers too
        (they are different spans, so a reader asking for 'the second one'
        can).
        """
        text = f"{SYNTHETIC_EMAIL} ... and again, {SYNTHETIC_EMAIL}."
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        pointers = re.findall(r"\[sensitive:pii:[0-9a-f]{32}", out)
        assert len(pointers) == 2
        assert pointers[0] != pointers[1]

    def test_per_class_action_off_skips_that_class(self, tmp_path: Path) -> None:
        text = f"Reach {SYNTHETIC_EMAIL} please."
        cfg = {"sensitivity": {"routing": {"enabled": True, "classes": {"pii": {"action": "off"}}}}}
        out = route_sensitive_values(
            raw_ref="src/a.md", text=text, frontmatter={}, config=cfg, knowledge_root=tmp_path
        )
        assert out == text
        assert not (tmp_path / "excluded").exists()

    def test_idempotent_rerun_produces_identical_output_and_no_duplicate_records(
        self, tmp_path: Path
    ) -> None:
        """AC11: re-running the sweep over the same raw content must not
        double-redact or create duplicate vault records."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        out1 = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        out2 = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        assert out1 == out2
        vault_files = list((tmp_path / "excluded" / "sensitivity" / "pii").glob("*.md"))
        assert len(vault_files) == 1

    def test_different_raw_ref_gets_a_different_record_id(self, tmp_path: Path) -> None:
        """Same value, different source file -> different (non-colliding) pointer."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        out1 = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        out2 = route_sensitive_values(
            raw_ref="src/b.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        p1 = re.search(r"[0-9a-f]{32}", out1).group(0)
        p2 = re.search(r"[0-9a-f]{32}", out2).group(0)
        assert p1 != p2
        # Both records exist independently — vault records never
        # deduplicate a value across raw files (see module docstring).
        vault_files = list((tmp_path / "excluded" / "sensitivity" / "pii").glob("*.md"))
        assert len(vault_files) == 2

    def test_unsafe_vault_surface_fails_closed(self, tmp_path: Path) -> None:
        """AC10, failure mode 3: an operator storage.mapping that routes the
        class onto an IN-CORPUS adapter must refuse to write, not silently
        leak there."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        cfg = {
            "sensitivity": {"routing": {"enabled": True}},
            "storage": {"mapping": {"pii": "wiki-markdown-embedded"}},
        }
        with pytest.raises(SensitivityRoutingError) as excinfo:
            route_sensitive_values(
                raw_ref="src/a.md",
                text=text,
                frontmatter={},
                config=cfg,
                knowledge_root=tmp_path,
            )
        assert SYNTHETIC_EMAIL not in str(excinfo.value)
        # And nothing should have landed anywhere in-corpus.
        assert not any((tmp_path / "wiki").rglob("*.md"))

    def test_mapped_to_unknown_adapter_fails_closed(self, tmp_path: Path) -> None:
        """AC10, failure mode 1's storage-layer sibling: storage.mapping
        naming an adapter that does not exist must surface as
        SensitivityRoutingError, not the lower-level StorageConfigError."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        cfg = {
            "sensitivity": {"routing": {"enabled": True}},
            "storage": {"mapping": {"pii": "does-not-exist"}},
        }
        with pytest.raises(SensitivityRoutingError) as excinfo:
            route_sensitive_values(
                raw_ref="src/a.md",
                text=text,
                frontmatter={},
                config=cfg,
                knowledge_root=tmp_path,
            )
        assert SYNTHETIC_EMAIL not in str(excinfo.value)

    def test_mapped_to_a_safe_custom_adapter_is_honored(self, tmp_path: Path) -> None:
        """An explicit storage.mapping entry pointing at a SAFE (not
        in-corpus) custom adapter is a legitimate operator choice and must
        be honored, not overridden by the excluded-adapter default."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        cfg = {
            "sensitivity": {"routing": {"enabled": True}},
            "storage": {
                "mapping": {"pii": "custom-vault"},
                "adapters": {
                    "custom-vault": {
                        "backing_store": "markdown",
                        "surface_root": "custom-vault",
                        "corpus_policy": {
                            "embedded": False,
                            "recallable": False,
                            "merge_eligible": False,
                        },
                    }
                },
            },
        }
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=cfg,
            knowledge_root=tmp_path,
        )
        assert "[sensitive:pii:" in out
        assert SYNTHETIC_EMAIL not in out
        vault_files = list((tmp_path / "custom-vault" / "sensitivity" / "pii").glob("*.md"))
        assert len(vault_files) == 1
        assert not (tmp_path / "excluded").exists()

    def test_unmapped_class_defaults_to_the_excluded_adapter_directly(self, tmp_path: Path) -> None:
        """AC10, failure mode 3's other half: with NO storage.mapping entry
        for the class, the vault root resolves to the built-in `excluded`
        adapter directly — never the generic layer's own wiki default."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        assert "[sensitive:pii:" in out
        assert (tmp_path / "excluded" / "sensitivity" / "pii").is_dir()
        assert not (tmp_path / "wiki").exists()

    def test_vault_write_failure_fails_closed_and_does_not_leak_value(self, tmp_path: Path) -> None:
        """AC10, failure mode 4: any exception writing the vault record
        itself must become SensitivityRoutingError, message scrubbed."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        vault_root = tmp_path / "excluded" / "sensitivity" / "pii"
        vault_root.mkdir(parents=True)
        vault_root.chmod(0o400)
        try:
            with pytest.raises(SensitivityRoutingError) as excinfo:
                route_sensitive_values(
                    raw_ref="src/a.md",
                    text=text,
                    frontmatter={},
                    config=ROUTING_ON,
                    knowledge_root=tmp_path,
                )
            assert SYNTHETIC_EMAIL not in str(excinfo.value)
        finally:
            vault_root.chmod(0o700)

    def test_field_based_match_with_no_span_fails_closed(self, tmp_path: Path) -> None:
        """AC10, failure mode 2: a hypothetical frontmatter-aware recognizer
        that reports a match with no character span must never be silently
        skipped — it fails the whole file closed rather than risk an
        un-redacted value."""

        class _FieldOnlyRecognizer:
            name = "field-only-secret"

            def detect(self, *, text, frontmatter):
                return [
                    sensitivity.SensitivityMatch(
                        recognizer=self.name, value="secret-value", field="token"
                    )
                ]

        sensitivity.register_recognizer(_FieldOnlyRecognizer())
        cfg = {
            "sensitivity": {
                "routing": {"enabled": True},
                "classes": {
                    "secret": {
                        "recognizers": ["field-only-secret"],
                        "read_policy": {"access": "confidential"},
                    }
                },
            }
        }
        with pytest.raises(SensitivityRoutingError) as excinfo:
            route_sensitive_values(
                raw_ref="src/a.md",
                text="irrelevant body",
                frontmatter={"token": "secret-value"},
                config=cfg,
                knowledge_root=tmp_path,
            )
        assert "secret-value" not in str(excinfo.value)
        assert "no text span" in str(excinfo.value)

    def test_malformed_routing_config_fails_closed_as_one_error_family(
        self, tmp_path: Path
    ) -> None:
        """AC10, failure mode 1: a malformed sensitivity.routing config is
        surfaced as SensitivityRoutingError, not the lower-level config-error
        type leaking through."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        cfg = {"sensitivity": {"routing": {"enabled": "not-a-bool"}}}
        with pytest.raises(SensitivityRoutingError):
            route_sensitive_values(
                raw_ref="src/a.md",
                text=text,
                frontmatter={},
                config=cfg,
                knowledge_root=tmp_path,
            )

    def test_deterministic_precedence_for_overlapping_matches(self, tmp_path: Path) -> None:
        """AC7: two recognisers bound to two different classes both firing on
        the SAME span (design note §7 Decision D6's escape hatch) must
        resolve deterministically rather than double-route or crash."""

        class _AltEmailRecognizer:
            name = "alt-email"

            def detect(self, *, text, frontmatter):
                start = text.find(SYNTHETIC_EMAIL)
                if start == -1:
                    return []
                return [
                    sensitivity.SensitivityMatch(
                        recognizer=self.name,
                        value=SYNTHETIC_EMAIL,
                        span=(start, start + len(SYNTHETIC_EMAIL)),
                    )
                ]

        sensitivity.register_recognizer(_AltEmailRecognizer())
        cfg = {
            "sensitivity": {
                "routing": {"enabled": True},
                "classes": {
                    "zzz-alt": {
                        "recognizers": ["alt-email"],
                        "read_policy": {"access": "confidential"},
                    }
                },
            }
        }
        text = f"Reach {SYNTHETIC_EMAIL} please."
        out = route_sensitive_values(
            raw_ref="src/a.md", text=text, frontmatter={}, config=cfg, knowledge_root=tmp_path
        )
        # Exactly one pointer for the one span — not two overlapping ones —
        # and it deterministically picks the alphabetically-first class
        # ("pii" < "zzz-alt").
        assert out.count("[sensitive:") == 1
        assert "[sensitive:pii:" in out
        assert SYNTHETIC_EMAIL not in out

    def test_precedence_sorts_by_span_start_then_class_name(self, tmp_path: Path) -> None:
        """AC7: non-overlapping matches at different span starts each keep
        their own class — precedence only arbitrates when spans actually
        overlap."""
        text = f"{SYNTHETIC_EMAIL} then later {SYNTHETIC_EMAIL_2}."
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        assert out.count("[sensitive:pii:") == 2
