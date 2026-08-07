# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.corrections (issue athenaeum#797, slices 1-4 of the
field-correction fast path, docs/field-corrections.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.corrections import (
    BatchOutcome,
    CorrectionRecordResult,
    build_ledger_record,
    compute_correction_id,
    decide_verdict,
    find_correction_batches,
    hoist_record,
    load_registry,
    parse_batch_envelope,
    process_batch_file,
    process_correction_record,
    resolve_target,
    retire_batch,
    run_correction_phase,
    write_correction_handoff,
)
from athenaeum.models import EntityIndex

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_page(wiki: Path, filename: str, meta: dict, body: str = "Body.\n") -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    page = wiki / filename
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        elif isinstance(v, dict):
            lines.append(f"{k}:")
            for kk, vv in v.items():
                lines.append(f"  {kk}: {vv!r}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    page.write_text("\n".join(lines) + f"\n\n# {meta.get('name')}\n\n{body}", encoding="utf-8")
    return page


def _envelope(**overrides) -> dict:
    env = {
        "record": "batch",
        "schema_version": 1,
        "submitter": "delivery-monitor",
        "batch_id": "20260806T140211Z-9f3ac1d2",
        "created_at": "2026-08-06T14:02:11Z",
        "defaults": {},
    }
    env.update(overrides)
    return env


def _fields_config(**fields) -> dict:
    return {"librarian": {"corrections": {"fields": fields}}}


# ---------------------------------------------------------------------------
# §3.1 envelope (light coverage -- the heavy discovery-shape coverage lives
# in tests/test_librarian.py::TestDiscoverRawFilesCorrections)
# ---------------------------------------------------------------------------


class TestParseBatchEnvelope:
    def test_valid(self) -> None:
        line = json.dumps(_envelope())
        assert parse_batch_envelope(line) is not None

    def test_unknown_schema_version(self) -> None:
        line = json.dumps(_envelope(schema_version=7))
        assert parse_batch_envelope(line) is None

    def test_not_json(self) -> None:
        assert parse_batch_envelope("not json") is None

    def test_wrong_record(self) -> None:
        line = json.dumps({"record": "note"})
        assert parse_batch_envelope(line) is None


# ---------------------------------------------------------------------------
# §5.2 correction_id -- post-hoist hashing
# ---------------------------------------------------------------------------


class TestCorrectionId:
    def test_inline_and_defaulted_records_hash_identically(self) -> None:
        """An inlined record and one inheriting op/field from envelope
        defaults must produce the SAME correction_id -- the core AC."""
        defaults = {"op": "add", "field": "backlinks", "source": "script:x"}
        inline_record = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "add",
            "field": "backlinks",
            "value": "company-b",
            "source": "script:x",
            "observed_at": "2026-08-06T03:00:00Z",
        }
        defaulted_record = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "value": "company-b",
            "observed_at": "2026-08-06T03:00:00Z",
        }
        eff_inline = hoist_record(inline_record, defaults)
        eff_defaulted = hoist_record(defaulted_record, defaults)
        id_inline = compute_correction_id(
            schema_version=1,
            target=eff_inline["target"],
            op=eff_inline["op"],
            field_name=eff_inline["field"],
            value=eff_inline["value"],
        )
        id_defaulted = compute_correction_id(
            schema_version=1,
            target=eff_defaulted["target"],
            op=eff_defaulted["op"],
            field_name=eff_defaulted["field"],
            value=eff_defaulted["value"],
        )
        assert id_inline == id_defaulted

    def test_source_and_observed_at_excluded(self) -> None:
        id_a = compute_correction_id(
            schema_version=1, target={"uid": "x"}, op="set", field_name="f", value="v"
        )
        id_b = compute_correction_id(
            schema_version=1, target={"uid": "x"}, op="set", field_name="f", value="v"
        )
        assert id_a == id_b


# ---------------------------------------------------------------------------
# §3.3 target resolution
# ---------------------------------------------------------------------------


class TestResolveTarget:
    def test_by_uid(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        path = resolve_target({"uid": "person-a"}, index=index, registry_entities={})
        assert path is not None
        assert path.name == "p.md"

    def test_by_type_and_name(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "Alex Doe"})
        index = EntityIndex(wiki)
        path = resolve_target(
            {"type": "person", "name": "Alex Doe"}, index=index, registry_entities={}
        )
        assert path is not None

    def test_cross_type_rejected(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "Alex Doe"})
        index = EntityIndex(wiki)
        path = resolve_target(
            {"type": "company", "name": "Alex Doe"}, index=index, registry_entities={}
        )
        assert path is None

    def test_zero_matches_returns_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        assert resolve_target({"uid": "no-such"}, index=index, registry_entities={}) is None

    def test_handle_resolution(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "Alex Doe"})
        index = EntityIndex(wiki)
        registry = {"person-a": {"type": "person", "handles": {"alt_emails": ["alex@example.org"]}}}
        path = resolve_target(
            {"type": "person", "handle": {"alt_emails": "alex@example.org"}},
            index=index,
            registry_entities=registry,
        )
        assert path is not None

    def test_handle_ambiguous_returns_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "a.md", {"uid": "person-a", "type": "person", "name": "A"})
        _write_page(wiki, "b.md", {"uid": "person-b", "type": "person", "name": "B"})
        index = EntityIndex(wiki)
        registry = {
            "person-a": {"type": "person", "handles": {"alt_emails": ["x@example.org"]}},
            "person-b": {"type": "person", "handles": {"alt_emails": ["x@example.org"]}},
        }
        path = resolve_target(
            {"type": "person", "handle": {"alt_emails": "x@example.org"}},
            index=index,
            registry_entities=registry,
        )
        assert path is None

    def test_handle_key_not_in_allowlist_returns_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        path = resolve_target(
            {"type": "person", "handle": {"not_a_real_key": "x"}},
            index=index,
            registry_entities={},
        )
        assert path is None


# ---------------------------------------------------------------------------
# §6.3 allowlist empty by default
# ---------------------------------------------------------------------------


class TestAllowlistEmptyByDefault:
    def test_no_config_raises_tier(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        raw_record = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "set",
            "field": "current_title",
            "value": "VP Engineering",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            raw_record,
            _envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=None,
        )
        assert result.disposition == "raised-tier"
        assert "allowlist" in result.reason


# ---------------------------------------------------------------------------
# §4/§5.1/§6.2 the applier -- scalar set, delta gate, precedence
# ---------------------------------------------------------------------------


class TestScalarSetApplier:
    def _config(self) -> dict:
        return _fields_config(
            current_title={"shape": "scalar", "writers": ["enrichment-service"]}
        )

    def _record(self, **overrides) -> dict:
        rec = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "set",
            "field": "current_title",
            "value": "VP Engineering",
            "source": "api:enrichment-vendor",
            "observed_at": "2026-08-06T05:58:40Z",
        }
        rec.update(overrides)
        return rec

    def test_applies_and_writes_field_sources(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(),
            _envelope(submitter="enrichment-service"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "applied"
        text = page.read_text()
        assert "current_title: VP Engineering" in text
        assert "api:enrichment-vendor" in text

    def test_reapply_is_byte_for_byte_noop(self, tmp_path: Path) -> None:
        """Re-applying the same batch must be byte-for-byte a no-op --
        no rewrite, no `updated` bump."""
        wiki = tmp_path / "wiki"
        page = _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        cfg = self._config()
        envelope = _envelope(submitter="enrichment-service")
        first = process_correction_record(
            self._record(),
            envelope,
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert first.disposition == "applied"
        after_first = page.read_text()

        second = process_correction_record(
            self._record(),
            envelope,
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert second.disposition == "noop"
        assert page.read_text() == after_first

    def test_lower_precedence_defers(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(
            wiki,
            "p.md",
            {"uid": "person-a", "type": "person", "name": "A", "current_title": "CTO"},
        )
        page.write_text(
            page.read_text().replace(
                "current_title: CTO",
                "current_title: CTO\nfield_sources:\n  current_title: 'user:conv-1'",
            )
        )
        index = EntityIndex(wiki)
        before = page.read_text()
        result = process_correction_record(
            self._record(),
            _envelope(submitter="enrichment-service"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "deferred-lower-precedence"
        assert page.read_text() == before

    def test_unparseable_source_raises_tier_not_rank_9(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(source="not-a-valid-source-shape!!"),
            _envelope(submitter="enrichment-service"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "raised-tier"
        assert "source" in result.reason

    def test_writer_not_permitted_raises_tier(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(),
            _envelope(submitter="some-other-writer"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "raised-tier"
        assert "not permitted" in result.reason

    def test_target_unresolved_raises_tier(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(target={"uid": "does-not-exist"}),
            _envelope(submitter="enrichment-service"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "raised-tier"

    def test_bad_op_for_scalar_raises_tier(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(op="add"),
            _envelope(submitter="enrichment-service"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "raised-tier"

    def test_unknown_key_raises_tier(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        record = self._record()
        record["typo_key"] = "oops"
        result = process_correction_record(
            record,
            _envelope(submitter="enrichment-service"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "raised-tier"
        assert "unknown key" in result.reason


# ---------------------------------------------------------------------------
# §4 add/remove list ops
# ---------------------------------------------------------------------------


class TestListOps:
    def _config(self) -> dict:
        return _fields_config(backlinks={"shape": "list", "writers": ["graph-writer"]})

    def test_add_new_value_applies(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        record = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "add",
            "field": "backlinks",
            "value": "company-b",
            "source": "script:graph-writer",
            "observed_at": "2026-08-06T03:00:00Z",
        }
        result = process_correction_record(
            record,
            _envelope(submitter="graph-writer"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "applied"
        assert "company-b" in page.read_text()

    def test_add_existing_value_is_noop(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(
            wiki,
            "p.md",
            {"uid": "person-a", "type": "person", "name": "A", "backlinks": ["company-b"]},
        )
        index = EntityIndex(wiki)
        before = page.read_text()
        record = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "add",
            "field": "backlinks",
            "value": "company-b",
            "source": "script:graph-writer",
            "observed_at": "2026-08-06T03:00:00Z",
        }
        result = process_correction_record(
            record,
            _envelope(submitter="graph-writer"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "noop"
        assert page.read_text() == before

    def test_remove_absent_value_is_noop(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(
            wiki,
            "p.md",
            {"uid": "person-a", "type": "person", "name": "A", "backlinks": ["company-c"]},
        )
        before = page.read_text()
        index = EntityIndex(wiki)
        record = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "remove",
            "field": "backlinks",
            "value": "company-b",
            "source": "script:graph-writer",
            "observed_at": "2026-08-06T03:00:00Z",
        }
        result = process_correction_record(
            record,
            _envelope(submitter="graph-writer"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "noop"
        assert page.read_text() == before

    def test_remove_present_value_applies(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(
            wiki,
            "p.md",
            {"uid": "person-a", "type": "person", "name": "A", "backlinks": ["company-b"]},
        )
        index = EntityIndex(wiki)
        record = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "remove",
            "field": "backlinks",
            "value": "company-b",
            "source": "script:graph-writer",
            "observed_at": "2026-08-06T03:00:00Z",
        }
        result = process_correction_record(
            record,
            _envelope(submitter="graph-writer"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "applied"
        assert "company-b" not in page.read_text()


# ---------------------------------------------------------------------------
# §6.3 monotone suppression
# ---------------------------------------------------------------------------


class TestMonotone:
    def _config(self) -> dict:
        return _fields_config(
            bounced={"shape": "scalar", "writers": ["delivery-monitor"], "monotone": True}
        )

    def test_any_permitted_writer_can_set(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        page.write_text(
            page.read_text().replace(
                "name: A", "name: A\nfield_sources:\n  bounced: 'user:conv-1'"
            )
        )
        index = EntityIndex(wiki)
        record = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "set",
            "field": "bounced",
            "value": "2026-08-06",
            "source": "script:delivery-monitor",
            "observed_at": "2026-08-06T14:01:55Z",
        }
        result = process_correction_record(
            record,
            _envelope(submitter="delivery-monitor"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "applied"
        assert result.monotone is True

    def test_unset_requires_user_tier(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        meta = {"uid": "person-a", "type": "person", "name": "A", "bounced": "2026-08-06"}
        page = _write_page(wiki, "p.md", meta)
        before = page.read_text()
        index = EntityIndex(wiki)
        record = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "set",
            "field": "bounced",
            "value": "",
            "source": "script:delivery-monitor",
            "observed_at": "2026-08-06T14:01:55Z",
        }
        result = process_correction_record(
            record,
            _envelope(submitter="delivery-monitor"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "deferred-lower-precedence"
        assert page.read_text() == before

        record["source"] = "user:conv-2026"
        result2 = process_correction_record(
            record,
            _envelope(submitter="delivery-monitor"),
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result2.disposition == "applied"

    def test_monotone_apply_is_logged_distinctly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§6.3: "every monotone apply is logged distinctly so the rule is
        auditable" — not just correctly applied, but actually logged."""
        wiki = tmp_path / "wiki"
        page = _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        page.write_text(
            page.read_text().replace(
                "name: A", "name: A\nfield_sources:\n  bounced: 'user:conv-1'"
            )
        )
        record = {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "set",
            "field": "bounced",
            "value": "2026-08-06",
            "source": "script:delivery-monitor",
            "observed_at": "2026-08-06T14:01:55Z",
        }
        with caplog.at_level("INFO", logger="athenaeum.corrections"):
            result = process_correction_record(
                record,
                _envelope(submitter="delivery-monitor"),
                index=EntityIndex(wiki),
                knowledge_root=tmp_path,
                registry_entities={},
                config=self._config(),
            )
        assert result.disposition == "applied"
        monotone_lines = [r.message for r in caplog.records if "monotone apply" in r.message]
        assert len(monotone_lines) == 1
        assert "bounced" in monotone_lines[0]


# ---------------------------------------------------------------------------
# §6.2 equal-rank tie-break on observed_at (dated branches; the undated ->
# escalate branch is covered end-to-end by TestEscalationDedup)
# ---------------------------------------------------------------------------


class TestDecideVerdictDatedTies:
    def _kwargs(self, **overrides) -> dict:
        base = dict(
            existing_source="api:apollo",
            incoming_source="api:enrichment-vendor",
            existing_value="CTO",
            incoming_value="VP Engineering",
            observed_at="2026-08-06T00:00:00Z",
            monotone=False,
            op="set",
            existing_updated="2026-08-01",
        )
        base.update(overrides)
        return base

    def test_newer_observed_at_wins(self) -> None:
        verdict, reason = decide_verdict(
            **self._kwargs(observed_at="2026-08-06", existing_updated="2026-08-01")
        )
        assert verdict == "apply"
        assert "newer" in reason

    def test_older_observed_at_defers(self) -> None:
        verdict, reason = decide_verdict(
            **self._kwargs(observed_at="2026-08-01", existing_updated="2026-08-06")
        )
        assert verdict == "defer"
        assert "newer" in reason  # "existing is newer"

    def test_indistinguishable_dates_escalate(self) -> None:
        verdict, reason = decide_verdict(
            **self._kwargs(observed_at="2026-08-06", existing_updated="2026-08-06")
        )
        assert verdict == "escalate"


# ---------------------------------------------------------------------------
# §7.1 sensitivity routing (and §11.1 cross-product with monotone)
# ---------------------------------------------------------------------------


class TestSensitivityRouting:
    def _config(self) -> dict:
        return {
            "librarian": {
                "corrections": {
                    "fields": {
                        "bounced": {
                            "shape": "scalar",
                            "writers": ["delivery-monitor"],
                            "monotone": True,
                        }
                    },
                    "sensitive_fields": {"bounced": "pii"},
                }
            },
            "storage": {"mapping": {"pii": "excluded"}},
        }

    def _record(self) -> dict:
        return {
            "record": "correction",
            "target": {"type": "person", "handle": {"alt_emails": "alex@example.org"}},
            "op": "set",
            "field": "bounced",
            "value": "2026-08-06",
            "source": "api:delivery-monitor:2026-08-06",
            "observed_at": "2026-08-06T14:01:55Z",
            "note": "permanent delivery failure",
        }

    def test_routes_to_sensitive_surface_not_entity_page(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        meta = {"uid": "person-alex", "type": "person", "name": "Alex Doe"}
        page = _write_page(wiki, "p.md", meta)
        before = page.read_text()
        index = EntityIndex(wiki)
        registry = {
            "person-alex": {"type": "person", "handles": {"alt_emails": ["alex@example.org"]}}
        }
        result = process_correction_record(
            self._record(),
            _envelope(submitter="delivery-monitor"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=self._config(),
        )
        # Cross-product AC: this is simultaneously the monotone case (§6.3)
        # AND the sensitivity-routed case (§7.1) -- both must hold together.
        assert result.disposition == "routed-elsewhere"
        assert result.monotone is True
        # The entity page named by the correction is UNTOUCHED.
        assert page.read_text() == before
        # The fact actually landed on the excluded/PII surface.
        excluded_root = tmp_path / "excluded"
        surface_file = excluded_root / "person-alex.json"
        assert surface_file.exists()
        surface_data = json.loads(surface_file.read_text())
        assert surface_data["bounced"] == "2026-08-06"

    def test_reapply_to_sensitive_surface_is_noop(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-alex", "type": "person", "name": "Alex Doe"})
        registry = {
            "person-alex": {"type": "person", "handles": {"alt_emails": ["alex@example.org"]}}
        }
        cfg = self._config()
        first = process_correction_record(
            self._record(),
            _envelope(submitter="delivery-monitor"),
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert first.disposition == "routed-elsewhere"
        second = process_correction_record(
            self._record(),
            _envelope(submitter="delivery-monitor"),
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert second.disposition == "noop"


# ---------------------------------------------------------------------------
# §7.2 schema evolution
# ---------------------------------------------------------------------------


class TestSchemaEvolution:
    def _base_config(self, slot: dict) -> dict:
        return {
            "librarian": {
                "corrections": {
                    "fields": {
                        "custom_attr": {"shape": "scalar", "writers": ["enrichment-service"]}
                    },
                    "schema_slots": {"custom_attr": slot},
                }
            }
        }

    def _record(self) -> dict:
        return {
            "record": "correction",
            "target": {"uid": "person-a"},
            "op": "set",
            "field": "custom_attr",
            "value": "some-value",
            "source": "api:enrichment-vendor",
            "observed_at": "2026-08-06T00:00:00Z",
        }

    def test_alias_of_routes_to_existing_slot(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(),
            _envelope(submitter="enrichment-service"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._base_config({"alias_of": "current_title"}),
        )
        assert result.disposition == "applied"
        assert "current_title: some-value" in page.read_text()

    def test_propose_amendment_is_held_schema_proposal(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        before = page.read_text()
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(),
            _envelope(submitter="enrichment-service"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._base_config({"propose_amendment": True}),
        )
        assert result.disposition == "held-schema-proposal"
        assert page.read_text() == before

    def test_prose_is_recorded_as_prose(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        result = process_correction_record(
            self._record(),
            _envelope(submitter="enrichment-service"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._base_config({"prose": True}),
        )
        assert result.disposition == "recorded-as-prose"
        assert "some-value" in page.read_text()
        # Frontmatter's own custom_attr key was NOT written -- it went to
        # body prose, not the field.
        assert "custom_attr:" not in page.read_text()


# ---------------------------------------------------------------------------
# §5.3 audit ledger denominator
# ---------------------------------------------------------------------------


class TestLedgerDenominator:
    def test_dispositions_sum_to_records_total(self, tmp_path: Path) -> None:
        outcome = BatchOutcome(
            path=tmp_path / "b.jsonl",
            source="graph-writer",
            envelope=_envelope(),
            records_total=2,
            results=[
                CorrectionRecordResult(correction_id="a", disposition="applied"),
                CorrectionRecordResult(correction_id="b", disposition="noop"),
            ],
        )
        record = build_ledger_record(outcome)
        assert record["records_total"] == 2
        assert sum(record["dispositions"].values()) == 2

    def test_mismatch_raises_loudly(self, tmp_path: Path) -> None:
        outcome = BatchOutcome(
            path=tmp_path / "b.jsonl",
            source="graph-writer",
            envelope=_envelope(),
            records_total=5,  # claims 5, only 1 result -- simulated truncation
            results=[CorrectionRecordResult(correction_id="a", disposition="applied")],
        )
        with pytest.raises(AssertionError):
            build_ledger_record(outcome)

    def test_final_line_without_trailing_newline(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        raw = tmp_path / "raw" / "graph-writer"
        raw.mkdir(parents=True)
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        batch = raw / "20260806T030000Z-1a2b3c4d.jsonl"
        lines = [
            json.dumps(
                _envelope(
                    submitter="graph-writer",
                    defaults={"op": "add", "field": "backlinks", "source": "script:graph-writer"},
                )
            ),
            json.dumps(
                {
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "value": "company-b",
                    "observed_at": "2026-08-06T03:00:00Z",
                }
            ),
        ]
        # No trailing newline on the final line.
        batch.write_text("\n".join(lines))
        index = EntityIndex(wiki)
        outcome = process_batch_file(
            batch,
            _envelope(
                submitter="graph-writer",
                defaults={"op": "add", "field": "backlinks", "source": "script:graph-writer"},
            ),
            "graph-writer",
            index=index,
            knowledge_root=tmp_path,
            config=_fields_config(backlinks={"shape": "list", "writers": ["graph-writer"]}),
        )
        assert outcome.records_total == 1
        record = build_ledger_record(outcome)  # must not raise
        assert record["records_total"] == 1


# ---------------------------------------------------------------------------
# §8/§8.1 fallthrough + handoff
# ---------------------------------------------------------------------------


class TestFallthroughHandoff:
    def test_malformed_records_reach_ordinary_intake_via_handoff(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        raw = tmp_path / "raw" / "graph-writer"
        raw.mkdir(parents=True)
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        batch_path = raw / "20260806T030000Z-1a2b3c4d.jsonl"
        env = _envelope(submitter="graph-writer")
        lines = [
            json.dumps(env),
            "not json at all",
            json.dumps(
                {
                    "record": "correction",
                    "target": {"uid": "no-such-entity"},
                    "op": "set",
                    "field": "current_title",
                    "value": "x",
                    "source": "api:apollo",
                    "observed_at": "2026-08-06T00:00:00Z",
                }
            ),
        ]
        batch_path.write_text("\n".join(lines) + "\n")
        index = EntityIndex(wiki)
        outcome = process_batch_file(
            batch_path,
            env,
            "graph-writer",
            index=index,
            knowledge_root=tmp_path,
            config=_fields_config(current_title={"shape": "scalar", "writers": ["graph-writer"]}),
        )
        raised = [r for r in outcome.results if r.disposition == "raised-tier"]
        assert len(raised) == 2
        handoff_path = write_correction_handoff(outcome, raised, raw_root=tmp_path / "raw")
        assert handoff_path.exists()
        text = handoff_path.read_text()
        assert env["batch_id"] in text
        assert "not json at all" in text or "reason" in text


# ---------------------------------------------------------------------------
# §5.4 batch retirement
# ---------------------------------------------------------------------------


class TestRetirement:
    def test_retire_without_git_unlinks(self, tmp_path: Path) -> None:
        batch = tmp_path / "batch.jsonl"
        batch.write_text("x\n")
        assert retire_batch(tmp_path, batch) is True
        assert not batch.exists()

    def test_retire_with_git_removes_and_commits(self, tmp_path: Path) -> None:
        import subprocess

        subprocess.run(["git", "init", "-q", "-b", "develop", str(tmp_path)], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
        )
        batch = tmp_path / "raw" / "graph-writer" / "b.jsonl"
        batch.parent.mkdir(parents=True)
        batch.write_text("x\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "-A"], check=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"],
            check=True,
            env={"ALLOW_PROTECTED_BRANCH_COMMIT": "1", "PATH": "/usr/bin:/bin"},
        )
        assert retire_batch(tmp_path, batch) is True
        assert not batch.exists()
        log = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--oneline"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "retired" in log


# ---------------------------------------------------------------------------
# §3.1/§10.2 batch discovery + FIFO ordering
# ---------------------------------------------------------------------------


class TestFindCorrectionBatches:
    def test_finds_valid_batches_fifo(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        (raw / "a").mkdir(parents=True)
        (raw / "b").mkdir(parents=True)
        env1 = _envelope(batch_id="1", submitter="a")
        env2 = _envelope(batch_id="2", submitter="b")
        (raw / "a" / "20260806T030000Z-11111111.jsonl").write_text(json.dumps(env1) + "\n")
        (raw / "b" / "20260806T020000Z-22222222.jsonl").write_text(json.dumps(env2) + "\n")
        found = find_correction_batches(raw)
        assert len(found) == 2
        # FIFO by filename -- the earlier timestamp comes first.
        assert found[0][0].name == "20260806T020000Z-22222222.jsonl"

    def test_malformed_batch_not_returned(self, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        (raw / "a").mkdir(parents=True)
        (raw / "a" / "20260806T030000Z-11111111.jsonl").write_text("not json\n")
        found = find_correction_batches(raw)
        assert found == []


def test_load_registry_missing_returns_empty(tmp_path: Path) -> None:
    assert load_registry(tmp_path) == {}


def test_load_registry_reads_entities(tmp_path: Path) -> None:
    (tmp_path / "registry.json").write_text(
        json.dumps({"entities": {"person-a": {"type": "person"}}})
    )
    reg = load_registry(tmp_path)
    assert "person-a" in reg


# ---------------------------------------------------------------------------
# §10.2 volume bounds -- an over-bound batch is deferred WHOLE, never
# refused (AC14). `run_correction_phase` is the §10.1 orchestrator these
# bounds gate.
# ---------------------------------------------------------------------------


def _corrections_batch(*records: dict, submitter: str, **envelope_overrides) -> str:
    envelope = {
        "record": "batch",
        "schema_version": 1,
        "submitter": submitter,
        "batch_id": "20260806T030000Z-1a2b3c4d",
        "created_at": "2026-08-06T03:00:00Z",
    }
    envelope.update(envelope_overrides)
    lines = [json.dumps(envelope)]
    lines.extend(json.dumps(r) for r in records)
    return "\n".join(lines) + "\n"


class TestVolumeBounds:
    def _noop_escalate(self, result: object, outcome: object) -> bool:
        return True

    def test_over_max_records_per_batch_deferred_whole(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        raw = tmp_path / "raw" / "graph-writer"
        raw.mkdir(parents=True)
        batch = raw / "20260806T030000Z-1a2b3c4d.jsonl"
        batch.write_text(
            _corrections_batch(
                {
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "value": "company-b",
                },
                {
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "value": "company-c",
                },
                submitter="graph-writer",
                defaults={
                    "op": "add",
                    "field": "backlinks",
                    "source": "script:graph-writer",
                    "observed_at": "2026-08-06T03:00:00Z",
                },
            )
        )
        config = {
            "librarian": {
                "corrections": {
                    "fields": {"backlinks": {"shape": "list", "writers": ["graph-writer"]}},
                    "max_records_per_batch": 1,  # batch carries 2 -> over bound
                }
            }
        }
        summary = run_correction_phase(
            raw_root=tmp_path / "raw",
            wiki_root=wiki,
            knowledge_root=tmp_path,
            index=EntityIndex(wiki),
            config=config,
            escalate_one=self._noop_escalate,
        )
        assert summary["batches_processed"] == 0
        assert summary["batches_carried_over"] == 1
        assert batch.exists()  # untouched, not deferred/mangled
        assert not (wiki / "_corrections_applied.jsonl").exists()

    def test_over_max_batch_bytes_deferred_whole(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        raw = tmp_path / "raw" / "graph-writer"
        raw.mkdir(parents=True)
        batch = raw / "20260806T030000Z-1a2b3c4d.jsonl"
        batch.write_text(
            _corrections_batch(
                {
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "value": "company-b",
                },
                submitter="graph-writer",
                defaults={
                    "op": "add",
                    "field": "backlinks",
                    "source": "script:graph-writer",
                    "observed_at": "2026-08-06T03:00:00Z",
                },
            )
        )
        config = {
            "librarian": {
                "corrections": {
                    "fields": {"backlinks": {"shape": "list", "writers": ["graph-writer"]}},
                    "max_batch_bytes": 4,  # the file is obviously bigger
                }
            }
        }
        summary = run_correction_phase(
            raw_root=tmp_path / "raw",
            wiki_root=wiki,
            knowledge_root=tmp_path,
            index=EntityIndex(wiki),
            config=config,
            escalate_one=self._noop_escalate,
        )
        assert summary["batches_processed"] == 0
        assert summary["batches_carried_over"] == 1
        assert batch.exists()

    def test_over_max_records_per_run_carries_second_batch(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        _write_page(wiki, "q.md", {"uid": "person-b", "type": "person", "name": "B"})
        raw = tmp_path / "raw" / "graph-writer"
        raw.mkdir(parents=True)
        # Two separate, individually-conformant batches sorted FIFO by
        # filename (§10.2).
        batch1 = raw / "20260806T030000Z-11111111.jsonl"
        batch1.write_text(
            _corrections_batch(
                {"record": "correction", "target": {"uid": "person-a"}, "value": "company-b"},
                submitter="graph-writer",
                batch_id="20260806T030000Z-11111111",
                defaults={
                    "op": "add",
                    "field": "backlinks",
                    "source": "script:graph-writer",
                    "observed_at": "2026-08-06T03:00:00Z",
                },
            )
        )
        batch2 = raw / "20260806T040000Z-22222222.jsonl"
        batch2.write_text(
            _corrections_batch(
                {"record": "correction", "target": {"uid": "person-b"}, "value": "company-c"},
                submitter="graph-writer",
                batch_id="20260806T040000Z-22222222",
                defaults={
                    "op": "add",
                    "field": "backlinks",
                    "source": "script:graph-writer",
                    "observed_at": "2026-08-06T04:00:00Z",
                },
            )
        )
        config = {
            "librarian": {
                "corrections": {
                    "fields": {"backlinks": {"shape": "list", "writers": ["graph-writer"]}},
                    "max_records_per_run": 1,  # batch1 alone exhausts the run budget
                }
            }
        }
        summary = run_correction_phase(
            raw_root=tmp_path / "raw",
            wiki_root=wiki,
            knowledge_root=tmp_path,
            index=EntityIndex(wiki),
            config=config,
            escalate_one=self._noop_escalate,
        )
        assert summary["batches_processed"] == 1
        assert summary["batches_carried_over"] == 1
        assert not batch1.exists()  # applied and retired
        assert batch2.exists()  # carried over whole, untouched

        page_a = (wiki / "p.md").read_text()
        page_b = (wiki / "q.md").read_text()
        assert "company-b" in page_a
        assert "company-c" not in page_b
