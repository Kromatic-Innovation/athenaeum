# SPDX-License-Identifier: Apache-2.0
"""Tests for the routing/redaction mechanism (issues athenaeum#1023, athenaeum#1024).

Slices 2/4 and 3/4 of athenaeum#949's design note
(`docs/sensitivity-value-routing.md`):
:func:`athenaeum.sensitivity_routing.route_sensitive_values` (write path,
athenaeum#1023) and :func:`athenaeum.sensitivity_routing.resolve_sensitive_record`
(read path, athenaeum#1024) — kept in one file because the read tests share
the write path's fixtures (a routed record must exist before it can be
resolved), per the issue's own "Plan" section.

``TestRouteSensitiveValues`` covers the pointer contract (AC2), fail-closed
behaviour on every failure mode the design note's §6 names (AC10),
deterministic overlap precedence (AC7), and idempotent re-entrancy (AC11).
``TestResolveSensitiveRecord`` covers the read path's own fail-closed
contract (never raises with content, AC2/AC3's malformed/path-traversal/
missing/cross-class failure modes), the round trip against a write-path
record, and access-control gating on the matched class's ``read_policy``
(AC2, and the issue's own athenaeum#1024 comment thread "AC14" — the drift
guard between this path and the existing uid-keyed one).

Deliberately out of scope here: any `librarian.process_one` wiring
(athenaeum#1025) — neither function is called from anywhere else in this
repo yet. All values below are synthetic; nothing here ever touches a live
vault.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from athenaeum import sensitivity
from athenaeum.models import is_page_authorized
from athenaeum.sensitivity_routing import (
    SensitivityRoutingError,
    resolve_sensitive_record,
    route_sensitive_values,
)

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


SYNTHETIC_SECRET = "sk-test-demo-1234567890abcdef"

_POINTER_RE = re.compile(r"\[sensitive:([A-Za-z0-9_-]+):([0-9a-f]{32})")


class _SecretRecognizer:
    """A throwaway recogniser bound to a custom "secret" class (test-only)."""

    name = "secret-token"

    def detect(self, *, text, frontmatter):
        start = text.find(SYNTHETIC_SECRET)
        if start == -1:
            return []
        return [
            sensitivity.SensitivityMatch(
                recognizer=self.name,
                value=SYNTHETIC_SECRET,
                span=(start, start + len(SYNTHETIC_SECRET)),
            )
        ]


def _secret_class_config(
    *, access: str = "confidential", audience: list[str] | None = None
) -> dict:
    read_policy: dict[str, object] = {"access": access}
    if audience is not None:
        read_policy["audience"] = audience
    return {
        "sensitivity": {
            "routing": {"enabled": True},
            "classes": {
                "secret": {
                    "recognizers": ["secret-token"],
                    "read_policy": read_policy,
                }
            },
        }
    }


def _route_secret(
    tmp_path: Path, *, access: str = "confidential", audience: list[str] | None = None
) -> tuple[str, str, dict]:
    """Register the secret recogniser, route SYNTHETIC_SECRET, return (class, record_id, config)."""
    sensitivity.register_recognizer(_SecretRecognizer(), replace=True)
    cfg = _secret_class_config(access=access, audience=audience)
    text = f"token={SYNTHETIC_SECRET} — do not share."
    out = route_sensitive_values(
        raw_ref="src/secret.md", text=text, frontmatter={}, config=cfg, knowledge_root=tmp_path
    )
    match = _POINTER_RE.search(out)
    assert match is not None
    return match.group(1), match.group(2), cfg


class TestResolveSensitiveRecord:
    """The read path (athenaeum#1024). See the module docstring for scope."""

    def test_round_trips_a_value_written_by_route_sensitive_values(self, tmp_path: Path) -> None:
        """AC5: the value resolved back must equal the original value routed."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        match = _POINTER_RE.search(out)
        assert match is not None
        sensitivity_class, record_id = match.group(1), match.group(2)
        resolved = resolve_sensitive_record(sensitivity_class, record_id, ROUTING_ON, tmp_path)
        assert resolved == SYNTHETIC_EMAIL

    def test_round_trips_a_custom_class_value(self, tmp_path: Path) -> None:
        """AC5 against a non-built-in class, sharing the write path's own fixture."""
        cls, record_id, cfg = _route_secret(tmp_path)
        assert resolve_sensitive_record(cls, record_id, cfg, tmp_path) == SYNTHETIC_SECRET

    def test_malformed_record_id_returns_none(self, tmp_path: Path) -> None:
        for bad_id in ("", "not-a-valid-id", "a" * 31, "A" * 32, "a" * 33, "1234"):
            assert resolve_sensitive_record("pii", bad_id, ROUTING_ON, tmp_path) is None

    def test_malformed_sensitivity_class_returns_none(self, tmp_path: Path) -> None:
        valid_id = "a" * 32
        for bad_class in ("", "  ", "pii\x00", "pii class", "pii/secret"):
            assert resolve_sensitive_record(bad_class, valid_id, ROUTING_ON, tmp_path) is None

    def test_path_traversal_shaped_inputs_never_read_outside_the_vault_root(
        self, tmp_path: Path
    ) -> None:
        """Security-relevant negative test (explicit AC): a crafted
        parent-directory-traversal sequence in either parameter must never
        cause a read outside the resolved vault root — proven by planting a
        decoy file outside the vault and confirming every traversal-shaped
        input still resolves to nothing."""
        decoy = tmp_path / "outside-the-vault.md"
        decoy.write_text("SHOULD-NEVER-BE-RESOLVABLE", encoding="utf-8")

        valid_id = "a" * 32
        traversal_record_ids = [
            "../" * 6 + "outside-the-vault",
            "..%2f..%2foutside-the-vault",
            "../../../../../../etc/passwd",
            "....//....//outside-the-vault",
        ]
        for bad_id in traversal_record_ids:
            assert resolve_sensitive_record("pii", bad_id, ROUTING_ON, tmp_path) is None

        traversal_classes = [
            "../../../..",
            "pii/../../outside",
            "..",
            "pii/../secret",
            "/etc",
            "..\\..\\outside",
        ]
        for bad_class in traversal_classes:
            assert resolve_sensitive_record(bad_class, valid_id, ROUTING_ON, tmp_path) is None

    def test_unknown_record_id_returns_none(self, tmp_path: Path) -> None:
        """Valid shape, valid class, but no such record on disk."""
        assert resolve_sensitive_record("pii", "a" * 32, ROUTING_ON, tmp_path) is None

    def test_unknown_class_returns_none(self, tmp_path: Path) -> None:
        assert resolve_sensitive_record("not-a-real-class", "a" * 32, ROUTING_ON, tmp_path) is None

    def test_record_under_a_different_class_is_not_resolvable(self, tmp_path: Path) -> None:
        """AC3: a record_id that resolves under a DIFFERENT class than requested."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        out = route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        match = _POINTER_RE.search(out)
        assert match is not None
        record_id = match.group(2)
        cfg = {
            "sensitivity": {
                "routing": {"enabled": True},
                "classes": {"other": {"read_policy": {"access": "internal"}}},
            }
        }
        # The record physically lives under "pii"'s vault directory — asking
        # for it under a different (but otherwise valid/known) class must
        # fail, never silently return pii's value.
        assert resolve_sensitive_record("other", record_id, cfg, tmp_path) is None

    def test_frontmatter_class_mismatch_is_refused_even_under_the_matching_directory(
        self, tmp_path: Path
    ) -> None:
        """Defense-in-depth for the same AC: even if a record's own
        frontmatter disagrees with the directory it was found under (e.g. a
        `storage.mapping` misconfiguration collapsing two classes onto one
        physical root), this function refuses rather than trusting path
        placement alone."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        vault_files = list((tmp_path / "excluded" / "sensitivity" / "pii").glob("*.md"))
        assert len(vault_files) == 1
        record_path = vault_files[0]
        tampered = record_path.read_text(encoding="utf-8").replace(
            "sensitivity_class: pii", "sensitivity_class: not-pii"
        )
        assert "sensitivity_class: not-pii" in tampered
        record_path.write_text(tampered, encoding="utf-8")
        record_id = record_path.stem
        assert resolve_sensitive_record("pii", record_id, ROUTING_ON, tmp_path) is None

    def test_missing_sensitivity_routed_flag_is_refused(self, tmp_path: Path) -> None:
        """Belt-and-suspenders: a file at the right path with the right id
        but no `sensitivity_routed: true` flag is not treated as a routed
        record."""
        text = f"Reach {SYNTHETIC_EMAIL} please."
        route_sensitive_values(
            raw_ref="src/a.md",
            text=text,
            frontmatter={},
            config=ROUTING_ON,
            knowledge_root=tmp_path,
        )
        vault_files = list((tmp_path / "excluded" / "sensitivity" / "pii").glob("*.md"))
        record_path = vault_files[0]
        tampered = record_path.read_text(encoding="utf-8").replace(
            "sensitivity_routed: true", "sensitivity_routed: false"
        )
        record_path.write_text(tampered, encoding="utf-8")
        record_id = record_path.stem
        assert resolve_sensitive_record("pii", record_id, ROUTING_ON, tmp_path) is None

    def test_malformed_class_config_returns_none_rather_than_raising(self, tmp_path: Path) -> None:
        cfg = {
            "sensitivity": {"classes": {"pii": {"read_policy": {"access": "not-a-real-level"}}}}
        }
        assert resolve_sensitive_record("pii", "a" * 32, cfg, tmp_path) is None

    def test_never_raises_on_odd_shaped_input(self, tmp_path: Path) -> None:
        """Contract: resolves to a value or to nothing — never raises."""
        assert (
            resolve_sensitive_record(None, "a" * 32, ROUTING_ON, tmp_path)  # type: ignore[arg-type]
            is None
        )
        assert (
            resolve_sensitive_record("pii", None, ROUTING_ON, tmp_path)  # type: ignore[arg-type]
            is None
        )
        assert (
            resolve_sensitive_record(
                "pii", "a" * 32, ROUTING_ON, tmp_path / "does" / "not" / "exist"
            )
            is None
        )

    def test_owner_caller_resolves_regardless_of_class_audience(self, tmp_path: Path) -> None:
        """``caller_audience=None`` is the owner/trusted default (issue
        athenaeum#312/#538 convention) — authorized for everything."""
        cls, record_id, cfg = _route_secret(tmp_path, access="confidential", audience=["ops"])
        assert (
            resolve_sensitive_record(cls, record_id, cfg, tmp_path, caller_audience=None)
            == SYNTHETIC_SECRET
        )

    def test_restricted_caller_with_granted_role_resolves(self, tmp_path: Path) -> None:
        cls, record_id, cfg = _route_secret(tmp_path, access="confidential", audience=["ops"])
        assert (
            resolve_sensitive_record(cls, record_id, cfg, tmp_path, caller_audience={"ops"})
            == SYNTHETIC_SECRET
        )

    def test_restricted_caller_without_granted_role_is_refused(self, tmp_path: Path) -> None:
        """AC2: gates on the matched class's read_policy — access control,
        not merely existence."""
        cls, record_id, cfg = _route_secret(tmp_path, access="confidential", audience=["ops"])
        assert (
            resolve_sensitive_record(
                cls, record_id, cfg, tmp_path, caller_audience={"someone-else"}
            )
            is None
        )
        assert (
            resolve_sensitive_record(cls, record_id, cfg, tmp_path, caller_audience=set())
            is None
        )

    def test_open_access_class_resolves_for_any_restricted_caller(self, tmp_path: Path) -> None:
        cls, record_id, cfg = _route_secret(tmp_path, access="open", audience=None)
        assert (
            resolve_sensitive_record(cls, record_id, cfg, tmp_path, caller_audience={"anyone"})
            == SYNTHETIC_SECRET
        )

    def test_access_decision_matches_the_shared_is_page_authorized_gate(
        self, tmp_path: Path
    ) -> None:
        """The issue's own athenaeum#1024 comment thread ("AC14"): the two
        permanently-independent excluded-surface read paths must not be able
        to silently drift on the access-control DECISION. This function
        gates through `models.is_page_authorized` — the SAME function the
        existing uid-keyed path's caller (`mcp_server`'s Layer C) already
        gates on — rather than a parallel re-implementation, so the two
        cannot diverge on how an access/audience decision is computed, only
        on which policy each supplies to it (an accepted, documented
        consequence of design note §2, not the thing this test guards).
        Proven here across an access/audience/caller matrix by checking this
        function's allow/deny outcome against `is_page_authorized` called
        directly with the equivalent policy shape."""
        cases: list[tuple[str, list[str], set[str] | None]] = [
            ("open", [], None),
            ("open", [], set()),
            ("open", [], {"anyone"}),
            ("internal", [], None),
            ("internal", [], set()),
            ("confidential", ["ops"], {"ops"}),
            ("confidential", ["ops"], {"finance"}),
            ("personal", ["ops", "finance"], {"finance"}),
            ("personal", [], {"finance"}),
        ]
        for access, audience, caller_audience in cases:
            cls, record_id, cfg = _route_secret(
                tmp_path, access=access, audience=audience or None
            )
            expected_allowed = is_page_authorized(
                {"access": access, "audience": audience}, caller_audience
            )
            resolved = resolve_sensitive_record(
                cls, record_id, cfg, tmp_path, caller_audience=caller_audience
            )
            assert (resolved == SYNTHETIC_SECRET) == expected_allowed, (
                access,
                audience,
                caller_audience,
            )
