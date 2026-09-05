# SPDX-License-Identifier: Apache-2.0
"""Tests for erasure classification and taint-propagation rules (athenaeum#985).

One test class per acceptance criterion (AC1-AC9), matching the issue body's
own numbering, plus a wiring-boundary test proving this module does not
perturb :mod:`athenaeum.decay_sweep`'s existing (unchanged) behavior.
"""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

from athenaeum import erasure
from athenaeum.config import (
    resolve_retention_pack_overrides,
    resolve_retention_pack_selection,
)
from athenaeum.provenance import SourceRef

# ---------------------------------------------------------------------------
# AC1 — HMAC-keyed erasure-class hashes + purgeable per-corpus key
# ---------------------------------------------------------------------------


class TestAC1HmacKeyedHashes:
    def test_key_is_generated_and_persisted_idempotently(self, tmp_path: Path) -> None:
        k1 = erasure.load_or_create_erasure_key(tmp_path)
        k2 = erasure.load_or_create_erasure_key(tmp_path)
        assert k1 == k2
        assert len(k1) == 32
        assert (tmp_path / erasure.ERASURE_KEY_FILENAME).is_file()

    def test_key_file_is_not_world_readable(self, tmp_path: Path) -> None:
        erasure.load_or_create_erasure_key(tmp_path)
        mode = (tmp_path / erasure.ERASURE_KEY_FILENAME).stat().st_mode
        assert not (mode & 0o077)

    def test_corrupt_key_file_raises_loudly(self, tmp_path: Path) -> None:
        path = erasure.resolve_erasure_key_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-hex-garbage", encoding="utf-8")
        with pytest.raises(erasure.ErasureKeyError):
            erasure.load_or_create_erasure_key(tmp_path)

    def test_different_keys_produce_different_hashes(self, tmp_path: Path) -> None:
        """The property that makes purging meaningful: key-dependence, not a plain hash."""
        text = "jane.doe@example.invalid"
        key_a = erasure.load_or_create_erasure_key(tmp_path / "a")
        key_b = erasure.load_or_create_erasure_key(tmp_path / "b")
        assert key_a != key_b
        hash_a = erasure.erasure_content_hash(text, key=key_a)
        hash_b = erasure.erasure_content_hash(text, key=key_b)
        assert hash_a != hash_b

    def test_purge_orphans_the_old_hash(self, tmp_path: Path) -> None:
        """Erasing the key erases linkability: a stored hash can no longer be
        reproduced by re-hashing the same candidate content post-purge."""
        text = "founder, kromatic"
        key1 = erasure.load_or_create_erasure_key(tmp_path)
        stored_hash = erasure.erasure_content_hash(text, key=key1)

        assert erasure.purge_erasure_key(tmp_path) is True
        # purging an already-purged key is a no-op, reported honestly
        assert erasure.purge_erasure_key(tmp_path) is False

        key2 = erasure.load_or_create_erasure_key(tmp_path)
        assert key2 != key1
        rehash = erasure.erasure_content_hash(text, key=key2)
        assert rehash != stored_hash

    def test_erasure_content_hash_never_a_plain_hash(self) -> None:
        """No plain hash of erasure-class content is ever written (AC1's own
        test requirement, verbatim). Asserted at the source level: the ONLY
        hashing primitive this function may use is the keyed HMAC one."""
        source = inspect.getsource(erasure.erasure_content_hash)
        assert "hmac.new(" in source
        # Plain-call shapes a content hash would take if this function ever
        # regressed to hashing unkeyed -- distinct from the digestmod usage
        # (`hashlib.sha256` as hmac.new's third arg) the docstring discusses.
        assert "hashlib.sha256(text.encode" not in source
        assert "hashlib.sha1(text.encode" not in source
        assert "hashlib.md5(text.encode" not in source

        # And a positive control: the function's actual output is NOT the
        # plain sha256 of the text — confirming the assertion above isn't
        # merely a string-match technicality.
        text = "a low-entropy fact: role=cfo"
        key = b"\x00" * 32
        keyed = erasure.erasure_content_hash(text, key=key)
        plain = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert keyed != plain


# ---------------------------------------------------------------------------
# AC2 — opaque, uid-based person-entity slugs and pair ids
# ---------------------------------------------------------------------------


class TestAC2OpaqueIdentity:
    def test_person_slug_is_uid_only_never_name(self) -> None:
        uid = "abcd1234"
        slug = erasure.opaque_person_slug(uid)
        assert slug == "person-abcd1234"
        assert "-" not in uid  # sanity: the uid itself carries no separators
        # No name fragment can appear in the slug, because the function
        # never accepts one -- see test_person_slug_rejects_name_derived_input.

    def test_person_slug_rejects_name_derived_input(self) -> None:
        with pytest.raises(erasure.OpaqueIdentityError):
            erasure.opaque_person_slug("tristan-kromer")
        with pytest.raises(erasure.OpaqueIdentityError):
            erasure.opaque_person_slug("Alice Example")

    def test_pair_id_is_order_independent(self) -> None:
        a, b = "abcd1234", "00001111"
        assert erasure.opaque_pair_id(a, b) == erasure.opaque_pair_id(b, a)
        assert erasure.opaque_pair_id(a, b) == "pair-00001111-abcd1234"

    def test_pair_id_rejects_non_uid_input(self) -> None:
        with pytest.raises(erasure.OpaqueIdentityError):
            erasure.opaque_pair_id("Alice", "Bob")

    def test_ledger_record_never_contains_a_name(self, tmp_path: Path) -> None:
        """Ledgers and breadcrumbs never quote erasure-class content (AC2's
        second half) -- exercised against the actual redaction ledger (AC8)."""
        person_name = "Alice Q. Example"
        uid = "deadbeef"
        key = erasure.load_or_create_erasure_key(tmp_path)
        record = erasure.build_redaction_record(
            reason_code="erasure-demand",
            subject_ref=erasure.opaque_person_slug(uid),
            data_class="pii",
            memory_class="entity",
            content_hash=erasure.erasure_content_hash(person_name, key=key),
            action_taken="store-off-corpus",
        )
        rendered = str(record.to_dict())
        assert person_name not in rendered
        assert "Alice" not in rendered
        assert uid in record.subject_ref  # opaque, but present -- that's fine


# ---------------------------------------------------------------------------
# AC3 — conservative default classification
# ---------------------------------------------------------------------------


class TestAC3ConservativeDefault:
    def test_unknown_jurisdiction_is_erasure_class(self) -> None:
        pack = erasure.available_retention_packs(None)["us-default"]
        for jurisdiction in (None, "", "   "):
            rule = erasure.classify_retention(
                memory_class="entity", data_class="pii", jurisdiction=jurisdiction, pack=pack
            )
            assert rule.action == erasure.UNKNOWN_JURISDICTION_ACTION
            assert rule.is_erasure_class()

    def test_no_pack_can_loosen_the_unknown_jurisdiction_default(self) -> None:
        """A pack defining jurisdiction: unknown must not be reachable for the
        unknown case -- classify_retention never routes to pack.lookup at all
        when the jurisdiction is unknown."""
        loose_pack = erasure.RetentionPack(
            name="looser-than-allowed",
            default_action="demote-cold",
            rules=(
                erasure.RetentionRule(
                    memory_class="entity",
                    data_class="pii",
                    jurisdiction="unknown",
                    action="demote-cold",
                ),
            ),
        )
        rule = erasure.classify_retention(
            memory_class="entity", data_class="pii", jurisdiction=None, pack=loose_pack
        )
        assert rule.action == erasure.UNKNOWN_JURISDICTION_ACTION
        assert rule.action != "demote-cold"

    def test_known_jurisdiction_routes_through_the_pack(self) -> None:
        pack = erasure.available_retention_packs(None)["us-default"]
        rule = erasure.classify_retention(
            memory_class="entity", data_class="pii", jurisdiction="US", pack=pack
        )
        assert rule.jurisdiction == "us"  # normalized
        assert rule.action == "retain-until"
        assert rule.period == "P7Y"

    def test_known_jurisdiction_not_in_table_falls_to_pack_default(self) -> None:
        pack = erasure.available_retention_packs(None)["us-default"]
        rule = erasure.classify_retention(
            memory_class="procedure", data_class="pii", jurisdiction="ca", pack=pack
        )
        assert rule.action == pack.default_action


# ---------------------------------------------------------------------------
# AC4 — taint rule 1: derivation
# ---------------------------------------------------------------------------


class TestAC4DerivationTaint:
    def test_derived_inference_block_is_tainted_by_erasure_class_basis(self) -> None:
        text = (
            "## Inference\n"
            "**Basis**: [[fact-a]], [[fact-b|Fact B]]\n"
            "**Confidence**: 0.8\n"
            "The derived claim goes here.\n"
        )
        tainted = erasure.classify_inference_taint(text, erasure_class_slugs={"fact-a"})
        assert len(tainted) == 1
        assert tainted[0].basis == ["fact-a", "fact-b"]

    def test_untainted_when_basis_is_clean(self) -> None:
        text = (
            "## Inference\n**Basis**: [[fact-c]]\n**Confidence**: 0.5\nSome other derived claim.\n"
        )
        tainted = erasure.classify_inference_taint(text, erasure_class_slugs={"fact-a"})
        assert tainted == []

    def test_matching_is_slug_normalized(self) -> None:
        text = (
            "## Inference\n"
            "**Basis**: [[Fact About Alice]]\n"
            "**Confidence**: 0.9\n"
            "A paraphrase, not a quote -- still tainted.\n"
        )
        tainted = erasure.classify_inference_taint(text, erasure_class_slugs={"fact-about-alice"})
        assert len(tainted) == 1

    def test_no_erasure_class_slugs_means_nothing_tainted(self) -> None:
        text = "## Inference\n**Basis**: [[fact-a]]\n**Confidence**: 0.5\nBody.\n"
        assert erasure.classify_inference_taint(text, erasure_class_slugs=[]) == []

    def test_end_to_end_with_classify_retention(self) -> None:
        """A page about a subject with unknown jurisdiction is erasure-class
        (AC3); an inference block whose basis cites that page is tainted
        (AC4) -- wired together the way a real caller would."""
        pack = erasure.available_retention_packs(None)["us-default"]
        subject_rule = erasure.classify_retention(
            memory_class="entity", data_class="pii", jurisdiction=None, pack=pack
        )
        assert subject_rule.is_erasure_class()

        erasure_class_slugs = {"jane-doe"} if subject_rule.is_erasure_class() else set()
        text = (
            "## Inference\n"
            "**Basis**: [[Jane Doe]]\n"
            "**Confidence**: 0.7\n"
            "Derived claim about Jane's employer.\n"
        )
        tainted = erasure.classify_inference_taint(text, erasure_class_slugs=erasure_class_slugs)
        assert len(tainted) == 1


# ---------------------------------------------------------------------------
# AC5 — taint rule 2: re-ingestion (provenance, never re-guess)
# ---------------------------------------------------------------------------


class TestAC5ReingestionTaint:
    def test_off_corpus_recall_source_round_trips(self) -> None:
        ref = erasure.opaque_person_slug("abcd1234")
        scalar = erasure.off_corpus_recall_source(ref)
        assert scalar == f"recall-offcorpus:{ref}"
        assert erasure.classify_by_provenance(scalar) is True

    def test_ordinary_source_is_not_tainted(self) -> None:
        assert erasure.classify_by_provenance("api:apollo:2026-05-09") is False
        assert erasure.classify_by_provenance("claude:tier3-session") is False

    def test_none_source_is_not_tainted(self) -> None:
        assert erasure.classify_by_provenance(None) is False

    def test_accepts_a_parsed_sourceref_directly(self) -> None:
        ref = SourceRef(type=erasure.OFF_CORPUS_RECALL_SOURCE_TYPE, ref="person-abcd1234")
        assert erasure.classify_by_provenance(ref) is True

    def test_classification_is_never_content_based(self) -> None:
        """The system knows where it came from; it must not re-guess: a source
        that LOOKS like ordinary content but has the off-corpus-recall
        provenance type is tainted regardless of what the text itself says."""
        scalar = erasure.off_corpus_recall_source("pair-00001111-abcd1234")
        assert erasure.classify_by_provenance(scalar) is True


# ---------------------------------------------------------------------------
# AC6 — taint rule 3: push is egress (honest disclosure)
# ---------------------------------------------------------------------------


class TestAC6PushIsEgress:
    def test_egress_disclosure_is_carried_in_every_ledger_record(self, tmp_path: Path) -> None:
        key = erasure.load_or_create_erasure_key(tmp_path)
        record = erasure.build_redaction_record(
            reason_code="erasure-demand",
            subject_ref=erasure.opaque_person_slug("abcd1234"),
            data_class="pii",
            memory_class="entity",
            content_hash=erasure.erasure_content_hash("x", key=key),
            action_taken="store-off-corpus",
        )
        rendered = record.to_dict()
        assert rendered["egress_guarantee"] == erasure.EGRESS_DISCLOSURE
        assert "session" in erasure.EGRESS_DISCLOSURE.lower()

    def test_disclosure_names_session_logs_as_unreachable(self) -> None:
        assert "session transcript" in erasure.EGRESS_DISCLOSURE
        assert "single-store delete" in erasure.EGRESS_DISCLOSURE


# ---------------------------------------------------------------------------
# AC7 — named remediation path (documented; ledger entry only)
# ---------------------------------------------------------------------------


class TestAC7RemediationPath:
    def test_remediation_record_shape(self, tmp_path: Path) -> None:
        key = erasure.load_or_create_erasure_key(tmp_path)
        record = erasure.build_history_rewrite_remediation_record(
            subject_ref=erasure.opaque_person_slug("abcd1234"),
            data_class="pii",
            memory_class="entity",
            content_hash=erasure.erasure_content_hash("leaked fact", key=key),
        )
        assert record.reason_code == erasure.HISTORY_REWRITE_REMEDIATION_REASON
        assert record.action_taken == "refuse-write"

    def test_remediation_reason_code_is_in_the_closed_vocabulary(self) -> None:
        assert erasure.HISTORY_REWRITE_REMEDIATION_REASON in erasure.REDACTION_REASON_CODES

    def test_security_posture_doc_names_the_blast_radius(self) -> None:
        doc = Path(__file__).resolve().parents[1] / "docs" / "design" / "security-posture.md"
        text = doc.read_text(encoding="utf-8")
        assert "history-rewrite" in text.lower() or "history rewrite" in text.lower()
        assert "re-clone" in text.lower()
        assert "ledger re-anchor" in text.lower() or "re-anchor" in text.lower()


# ---------------------------------------------------------------------------
# AC8 — the redaction ledger: that-and-why, never what
# ---------------------------------------------------------------------------


class TestAC8RedactionLedger:
    def test_record_has_no_content_field(self) -> None:
        fields = {f for f in erasure.RedactionLedgerRecord.__dataclass_fields__}
        for forbidden in ("content", "text", "body", "value", "notes"):
            assert forbidden not in fields

    def test_invalid_reason_code_rejected(self, tmp_path: Path) -> None:
        key = erasure.load_or_create_erasure_key(tmp_path)
        with pytest.raises(erasure.RedactionLedgerError):
            erasure.build_redaction_record(
                reason_code="because-i-said-so",
                subject_ref=erasure.opaque_person_slug("abcd1234"),
                data_class="pii",
                memory_class="entity",
                content_hash=erasure.erasure_content_hash("x", key=key),
                action_taken="store-off-corpus",
            )

    def test_invalid_action_rejected(self, tmp_path: Path) -> None:
        key = erasure.load_or_create_erasure_key(tmp_path)
        with pytest.raises(erasure.RedactionLedgerError):
            erasure.build_redaction_record(
                reason_code="erasure-demand",
                subject_ref=erasure.opaque_person_slug("abcd1234"),
                data_class="pii",
                memory_class="entity",
                content_hash=erasure.erasure_content_hash("x", key=key),
                action_taken="delete-immediately",  # not in RETENTION_ACTIONS
            )

    def test_append_and_read_round_trip(self, tmp_path: Path) -> None:
        key = erasure.load_or_create_erasure_key(tmp_path)
        record = erasure.build_redaction_record(
            reason_code="erasure-demand",
            subject_ref=erasure.opaque_person_slug("abcd1234"),
            data_class="pii",
            memory_class="entity",
            content_hash=erasure.erasure_content_hash("a real fact", key=key),
            action_taken="store-off-corpus",
        )
        erasure.append_redaction_ledger([record], cache_dir=tmp_path)
        rows = erasure.read_redaction_ledger(cache_dir=tmp_path)
        assert len(rows) == 1
        assert rows[0]["record_id"] == record.record_id
        assert rows[0]["reason_code"] == "erasure-demand"
        assert "a real fact" not in str(rows[0])

    def test_append_empty_is_a_noop(self, tmp_path: Path) -> None:
        erasure.append_redaction_ledger([], cache_dir=tmp_path)
        assert not erasure.redaction_ledger_path(tmp_path).exists()

    def test_read_tolerates_torn_trailing_line(self, tmp_path: Path) -> None:
        path = erasure.redaction_ledger_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"v": 1, "record_id": "abc"}\n{"v": 1, "record_i', encoding="utf-8")
        rows = erasure.read_redaction_ledger(tmp_path)
        assert len(rows) == 1

    def test_read_missing_ledger_returns_empty(self, tmp_path: Path) -> None:
        assert erasure.read_redaction_ledger(tmp_path) == []


# ---------------------------------------------------------------------------
# AC9 — retention policy packs as data
# ---------------------------------------------------------------------------


class TestAC9RetentionPacks:
    def test_two_default_packs_are_shipped(self) -> None:
        packs = erasure.available_retention_packs(None)
        assert set(erasure.PACKAGED_RETENTION_PACK_NAMES) <= set(packs)
        assert "us-default" in packs
        assert "eu-gdpr" in packs

    def test_packs_are_yaml_data_files_not_python_literals(self) -> None:
        pack_dir = Path(erasure.__file__).resolve().parent / "retention_packs"
        for name in erasure.PACKAGED_RETENTION_PACK_NAMES:
            assert (pack_dir / f"{name}.yaml").is_file()
        # No RetentionPack(...) literal construction for the built-ins
        # anywhere in erasure.py itself -- they are loaded, not hardcoded.
        source = inspect.getsource(erasure._load_packaged_pack)
        assert "yaml.safe_load" in source

    def test_selection_default_is_us_default(self) -> None:
        assert resolve_retention_pack_selection(None) == "us-default"

    def test_selection_from_config(self) -> None:
        assert resolve_retention_pack_selection({"erasure": {"retention_pack": "eu-gdpr"}}) == (
            "eu-gdpr"
        )

    def test_selection_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RETENTION_PACK", "eu-gdpr")
        assert resolve_retention_pack_selection(None) == "eu-gdpr"

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_RETENTION_PACK", "eu-gdpr")
        assert resolve_retention_pack_selection({"erasure": {"retention_pack": "us-default"}}) == (
            "eu-gdpr"
        )

    def test_resolve_active_pack_raises_on_unknown_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_RETENTION_PACK", "does-not-exist")
        with pytest.raises(erasure.RetentionPackError):
            erasure.resolve_active_retention_pack(None)

    def test_operator_pack_override_is_wholesale(self) -> None:
        config = {
            "erasure": {
                "retention_packs": {
                    "us-default": {
                        "default_action": "refuse-write",
                        "rules": [],
                    }
                }
            }
        }
        packs = erasure.available_retention_packs(config)
        assert packs["us-default"].default_action == "refuse-write"
        assert packs["us-default"].rules == ()

    def test_operator_can_add_a_brand_new_pack(self) -> None:
        config = {
            "erasure": {
                "retention_packs": {
                    "internal-strict": {
                        "default_action": "refuse-write",
                        "rules": [],
                    }
                }
            }
        }
        packs = erasure.available_retention_packs(config)
        assert "internal-strict" in packs
        assert "us-default" in packs  # still resolved, unaffected

    def test_malformed_pack_raises_at_build_time(self) -> None:
        config = {
            "erasure": {"retention_packs": {"broken": {"default_action": "not-a-real-action"}}}
        }
        with pytest.raises(erasure.RetentionPackError):
            erasure.available_retention_packs(config)

    def test_config_resolver_drops_malformed_entries_defensively(self) -> None:
        config = {
            "erasure": {
                "retention_packs": {
                    "": {"default_action": "refuse-write"},
                    123: {"default_action": "refuse-write"},
                    "valid-name": "not-a-dict",
                }
            }
        }
        assert resolve_retention_pack_overrides(config) == {}

    def test_action_vocabulary_matches_the_issue_text(self) -> None:
        assert erasure.RETENTION_ACTIONS == {
            "refuse-write",
            "store-off-corpus",
            "demote-cold",
            "delete-after",
            "retain-until",
        }

    def test_period_required_for_delete_after_and_retain_until(self) -> None:
        with pytest.raises(erasure.RetentionPackError):
            erasure.RetentionRule(
                memory_class="entity", data_class="pii", jurisdiction="us", action="delete-after"
            )
        with pytest.raises(erasure.RetentionPackError):
            erasure.RetentionRule(
                memory_class="entity", data_class="pii", jurisdiction="us", action="retain-until"
            )
        # No period required for the other three.
        erasure.RetentionRule(
            memory_class="entity", data_class="pii", jurisdiction="us", action="demote-cold"
        )
        erasure.RetentionRule(
            memory_class="entity", data_class="pii", jurisdiction="us", action="refuse-write"
        )
        erasure.RetentionRule(
            memory_class="entity", data_class="pii", jurisdiction="us", action="store-off-corpus"
        )

    def test_pack_keys_on_subjects_jurisdiction_when_known(self) -> None:
        """Where the data subject's jurisdiction is known, packs key on the
        SUBJECT's jurisdiction, not only the operator's -- exercised by
        showing both a us-subject and an eu-subject resolve differently
        through the SAME (operator-neutral) pack."""
        pack = erasure.available_retention_packs(None)["us-default"]
        us_rule = erasure.classify_retention(
            memory_class="entity", data_class="pii", jurisdiction="us", pack=pack
        )
        eu_rule = erasure.classify_retention(
            memory_class="entity", data_class="pii", jurisdiction="eu", pack=pack
        )
        assert us_rule.action != eu_rule.action
        assert eu_rule.is_erasure_class()  # store-off-corpus
        assert not us_rule.is_erasure_class()  # retain-until, not erasure-class

    def test_bucket_daily_reconciliation_honors_provenance_shape_869(self) -> None:
        """docs/design/provenance-shape.md §8.8: bucket: daily compiles to a
        delete-after rule keyed by (memory_class, data_class). Once a pack
        exists, it is the authority for that reconciliation."""
        pack = erasure.available_retention_packs(None)["eu-gdpr"]
        rule = erasure.reconcile_bucket_daily_with_pack(
            memory_class="fact", data_class="pii", pack=pack
        )
        # bucket: daily carries no jurisdiction signal, so this always
        # evaluates at the unknown-jurisdiction default -- see the
        # function's own docstring for why a future wiring slice must
        # reconcile this explicitly rather than this function guessing.
        assert rule.action == erasure.UNKNOWN_JURISDICTION_ACTION

    def test_reconciliation_is_deterministic_pure_computation(self) -> None:
        pack = erasure.available_retention_packs(None)["us-default"]
        r1 = erasure.reconcile_bucket_daily_with_pack(
            memory_class="fact", data_class="pii", pack=pack
        )
        r2 = erasure.reconcile_bucket_daily_with_pack(
            memory_class="fact", data_class="pii", pack=pack
        )
        assert r1 == r2


# ---------------------------------------------------------------------------
# Wiring boundary: as of athenaeum#985, decay_sweep did not import this module
# (see reconcile_bucket_daily_with_pack's docstring for why). Issue
# athenaeum#1116 is exactly the wiring slice that docstring named — it
# supplies decay_sweep's own known ``memory_class`` frontmatter field as
# reconcile_bucket_daily_with_pack's *memory_class*, gated on a page also
# carrying an explicit ``data_class`` (no shipped write path stamps one, so
# this is a no-op on every corpus produced by shipped code today) — see
# tests/test_decay_sweep.py for the round-trip coverage of that wiring.
# ---------------------------------------------------------------------------


class TestWiringBoundary:
    def test_decay_sweep_consults_erasure_for_pack_authority(self) -> None:
        """Issue athenaeum#1116 AC3 flipped this invariant deliberately — see the
        comment above."""
        import athenaeum.decay_sweep as decay_sweep_mod

        source = inspect.getsource(decay_sweep_mod)
        assert "erasure" in source

    def test_erasure_module_makes_no_llm_calls(self) -> None:
        """Structurally, not just in practice -- mirrors
        tests/test_decay_sweep.py::TestNoLLMCalls."""
        import athenaeum.erasure as erasure_mod

        for name, obj in vars(erasure_mod).items():
            if not inspect.isfunction(obj) or obj.__module__ != erasure_mod.__name__:
                continue
            params = inspect.signature(obj).parameters
            assert "client" not in params, f"{name} carries a client param"
            assert "provider" not in params, f"{name} carries a provider param"
