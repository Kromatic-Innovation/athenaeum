# SPDX-License-Identifier: Apache-2.0
"""Tests for athenaeum.corrections (issue athenaeum#797, slices 1-4 of the
field-correction fast path, docs/field-corrections.md).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from athenaeum.corrections import (
    BatchOutcome,
    CorrectionRecordResult,
    TargetResolution,
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
    resolve_target_for_apply,
    retire_batch,
    run_correction_phase,
    write_correction_handoff,
)
from athenaeum.models import EntityIndex, parse_frontmatter

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
    )


def _git_init(root: Path) -> None:
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Corrections Test")


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

    def test_handle_resolution_apollo_organization_id(self, tmp_path: Path) -> None:
        """issue athenaeum#874: a provider-keyed company correction target
        resolves through registry.json on `apollo_organization_id`."""
        wiki = tmp_path / "wiki"
        _write_page(wiki, "c.md", {"uid": "company-acme", "type": "company", "name": "Acme"})
        index = EntityIndex(wiki)
        registry = {
            "company-acme": {
                "type": "company",
                "handles": {"apollo_organization_id": "5f1a2b3c"},
            }
        }
        path = resolve_target(
            {"type": "company", "handle": {"apollo_organization_id": "5f1a2b3c"}},
            index=index,
            registry_entities=registry,
        )
        assert path is not None
        assert path.name == "c.md"


# ---------------------------------------------------------------------------
# §3.3 create branch (issue athenaeum#865) -- resolve_target_for_apply's
# decision table in isolation, before any write path is involved.
# ---------------------------------------------------------------------------


class TestResolveTargetForApply:
    def test_existing_match_passes_through(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        index = EntityIndex(wiki)
        resolution = resolve_target_for_apply(
            {"uid": "person-a"}, index=index, registry_entities={}
        )
        assert resolution.kind == "existing"
        assert resolution.path is not None and resolution.path.name == "p.md"

    def test_zero_match_handle_is_creatable(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        resolution = resolve_target_for_apply(
            {"type": "company", "handle": {"domains": "acme.example"}},
            index=index,
            registry_entities={},
        )
        assert resolution == TargetResolution(
            kind="creatable",
            entity_type="company",
            handle_key="domains",
            handle_value="acme.example",
        )

    def test_zero_match_uid_stays_unresolvable(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        resolution = resolve_target_for_apply(
            {"uid": "no-such"}, index=index, registry_entities={}
        )
        assert resolution.kind == "unresolvable"

    def test_zero_match_type_name_stays_unresolvable(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        resolution = resolve_target_for_apply(
            {"type": "company", "name": "Acme Inc"}, index=index, registry_entities={}
        )
        assert resolution.kind == "unresolvable"

    def test_ambiguous_handle_stays_unresolvable(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "a.md", {"uid": "company-a", "type": "company", "name": "A"})
        _write_page(wiki, "b.md", {"uid": "company-b", "type": "company", "name": "B"})
        index = EntityIndex(wiki)
        registry = {
            "company-a": {"type": "company", "handles": {"domains": ["acme.example"]}},
            "company-b": {"type": "company", "handles": {"domains": ["acme.example"]}},
        }
        resolution = resolve_target_for_apply(
            {"type": "company", "handle": {"domains": "acme.example"}},
            index=index,
            registry_entities=registry,
        )
        assert resolution.kind == "unresolvable"

    def test_missing_type_stays_unresolvable(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        resolution = resolve_target_for_apply(
            {"handle": {"domains": "acme.example"}}, index=index, registry_entities={}
        )
        assert resolution.kind == "unresolvable"

    def test_blank_type_stays_unresolvable(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        resolution = resolve_target_for_apply(
            {"type": "  ", "handle": {"domains": "acme.example"}},
            index=index,
            registry_entities={},
        )
        assert resolution.kind == "unresolvable"

    def test_handle_key_not_in_allowlist_stays_unresolvable(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        resolution = resolve_target_for_apply(
            {"type": "company", "handle": {"not_a_real_key": "x"}},
            index=index,
            registry_entities={},
        )
        assert resolution.kind == "unresolvable"


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

    def test_absent_field_fills_despite_higher_ranked_page_source(
        self, tmp_path: Path
    ) -> None:
        """§6.2 arbitrates between an incoming claim and an INCUMBENT one. A
        field no one has ever set has no incumbent, so a lower-ranked source
        fills it rather than deferring -- the same reading §4 already gives
        `op: add` on a list ("new value, not a conflict").

        The page-level `source:` here is `user:` (rank 1), which outranks the
        correction's `api:` (rank 3). That must NOT defer: the page-level
        source is the attribution of the page's OWN fields, not a standing
        claim about a field it never carried. Contrast
        `test_lower_precedence_defers` above, where `user:` attribution sits on
        the field being written and the correction is correctly refused.
        """
        wiki = tmp_path / "wiki"
        page = _write_page(
            wiki,
            "p.md",
            {
                "uid": "person-a",
                "type": "person",
                "name": "A",
                "source": "user:conv-1",
            },
        )
        result = process_correction_record(
            self._record(),
            _envelope(submitter="enrichment-service"),
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "applied"
        assert "current_title: VP Engineering" in page.read_text()

    def test_absent_field_fills_despite_newer_page_updated_stamp(
        self, tmp_path: Path
    ) -> None:
        """The equal-rank branch of the same rule (issue athenaeum#865).

        Equal ranks send §6.2 to its `observed_at` tie-break, which compares
        against the page's `updated:` stamp. With the page stamped far later
        than the correction was observed, an absent field would lose that
        tie-break and defer -- despite there being no incumbent value to lose
        to. This is the shape that made the tier-0 create path's AC 3 (create
        and update are one path) unreachable: a freshly created page is always
        stamped `updated: <today>`, so the batch that created it lost to it.
        """
        wiki = tmp_path / "wiki"
        page = _write_page(
            wiki,
            "p.md",
            {
                "uid": "person-a",
                "type": "person",
                "name": "A",
                "source": "api:enrichment-vendor",  # equal rank to the incoming
                "updated": "2030-01-01",  # far newer than the record's observed_at
            },
        )
        result = process_correction_record(
            self._record(),
            _envelope(submitter="enrichment-service"),
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities={},
            config=self._config(),
        )
        assert result.disposition == "applied"
        assert "current_title: VP Engineering" in page.read_text()

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
# §3.3/§4/§5/§7/§8 tier-0 create-by-handle (issue athenaeum#865): a handle
# target with zero matches creates instead of raising, through the SAME
# applier path an update takes.
# ---------------------------------------------------------------------------


class TestCreateByHandle:
    def _envelope(self, **overrides: object) -> dict:
        return _envelope(submitter="employer-feed", **overrides)

    def test_create_when_absent(self, tmp_path: Path) -> None:
        """Handle target, zero matches -> entity created, carrying the key
        and its provenance."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        index = EntityIndex(wiki)
        registry: dict = {}
        cfg = _fields_config(
            industry={"shape": "scalar", "writers": ["employer-feed"]},
        )
        record = {
            "record": "correction",
            "target": {"type": "company", "handle": {"domains": "acme.example"}},
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert result.disposition == "applied"
        pages = list(wiki.glob("*.md"))
        assert len(pages) == 1
        meta, _ = parse_frontmatter(pages[0].read_text())
        assert meta["type"] == "company"
        assert meta["domains"] == ["acme.example"]
        assert meta["industry"] == "Software"
        assert meta["field_sources"]["domains"] == [
            {"value": "acme.example", "source": "api:apollo"}
        ]
        assert meta["field_sources"]["industry"] == "api:apollo"
        uid = meta["uid"]
        assert uid  # minted
        # in-run registry view updated so a later record can resolve to it
        assert registry[uid]["handles"]["domains"] == ["acme.example"]
        # AC 5: a human can see where it came from.
        assert meta["source"] == "api:apollo"

    def test_create_field_equals_handle_key_no_duplicate(self, tmp_path: Path) -> None:
        """The record's own field IS the handle key (the simplest possible
        create -- the correction both keys resolution AND asserts the
        handle value). The entity is still created and reported
        `applied`, not `noop`, and the value is not duplicated."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        index = EntityIndex(wiki)
        cfg = _fields_config(domains={"shape": "list", "writers": ["employer-feed"]})
        record = {
            "record": "correction",
            "target": {"type": "company", "handle": {"domains": "acme.example"}},
            "op": "add",
            "field": "domains",
            "value": "acme.example",
            "source": "script:employer-feed",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert result.disposition == "applied"
        pages = list(wiki.glob("*.md"))
        assert len(pages) == 1
        meta, _ = parse_frontmatter(pages[0].read_text())
        assert meta["domains"] == ["acme.example"]  # not duplicated
        assert meta["field_sources"]["domains"] == [
            {"value": "acme.example", "source": "script:employer-feed"}
        ]

    def test_idempotent_resubmit_same_batch_twice(self, tmp_path: Path) -> None:
        """AC: submitting the same batch twice yields one entity; the
        second pass is a delta-gated no-op, byte-for-byte stable."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        registry: dict = {}
        cfg = _fields_config(industry={"shape": "scalar", "writers": ["employer-feed"]})
        record = {
            "record": "correction",
            "target": {"type": "company", "handle": {"domains": "acme.example"}},
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        env = self._envelope()

        first = process_correction_record(
            record,
            env,
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert first.disposition == "applied"
        pages = list(wiki.glob("*.md"))
        assert len(pages) == 1
        after_first = pages[0].read_text()

        # Re-submit: a fresh EntityIndex (as a fresh process would build by
        # re-scanning the wiki tree), the SAME registry view a single
        # correction-phase run holds (see the athenaeum#865 completion
        # report for why cross-run staleness of registry.json itself is
        # out of scope).
        second = process_correction_record(
            record,
            env,
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert second.disposition == "noop"
        pages_after = list(wiki.glob("*.md"))
        assert len(pages_after) == 1
        assert pages_after[0].read_text() == after_first

    def test_update_existing_created_earlier(self, tmp_path: Path) -> None:
        """AC: a subsequent batch carrying the same key updates the entity
        created earlier -- create and update are one path."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        registry: dict = {}
        cfg = _fields_config(
            industry={"shape": "scalar", "writers": ["employer-feed"]},
            employee_count={"shape": "scalar", "writers": ["employer-feed"]},
        )
        target = {"type": "company", "handle": {"domains": "acme.example"}}
        env = self._envelope()

        create_record = {
            "record": "correction",
            "target": target,
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        first = process_correction_record(
            create_record,
            env,
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert first.disposition == "applied"
        assert len(list(wiki.glob("*.md"))) == 1

        update_record = {
            "record": "correction",
            "target": target,
            "op": "set",
            "field": "employee_count",
            "value": "500",
            "source": "api:apollo",
            "observed_at": "2026-08-07T00:00:00Z",
        }
        second = process_correction_record(
            update_record,
            env,
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert second.disposition == "applied"
        pages = list(wiki.glob("*.md"))
        assert len(pages) == 1  # still one entity, not a second create
        meta, _ = parse_frontmatter(pages[0].read_text())
        assert meta["industry"] == "Software"  # earlier field preserved
        assert meta["employee_count"] == "500"  # new field applied
        assert second.entity_path == first.entity_path  # same page, not a new one

    def test_create_by_apollo_organization_id_then_update(self, tmp_path: Path) -> None:
        """issue athenaeum#874: a zero-match `apollo_organization_id` handle
        target creates the company at tier 0 (the athenaeum#865 path), and a
        second submission carrying the same key updates that page rather
        than creating a duplicate."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        registry: dict = {}
        cfg = _fields_config(
            industry={"shape": "scalar", "writers": ["employer-feed"]},
            employee_count={"shape": "scalar", "writers": ["employer-feed"]},
        )
        target = {"type": "company", "handle": {"apollo_organization_id": "5f1a2b3c"}}
        env = self._envelope()

        create_record = {
            "record": "correction",
            "target": target,
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        first = process_correction_record(
            create_record,
            env,
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert first.disposition == "applied"
        pages = list(wiki.glob("*.md"))
        assert len(pages) == 1
        meta, _ = parse_frontmatter(pages[0].read_text())
        assert meta["apollo_organization_id"] == "5f1a2b3c"
        assert meta["field_sources"]["apollo_organization_id"] == "api:apollo"
        uid = meta["uid"]
        assert registry[uid]["handles"]["apollo_organization_id"] == "5f1a2b3c"

        update_record = {
            "record": "correction",
            "target": target,
            "op": "set",
            "field": "employee_count",
            "value": "500",
            "source": "api:apollo",
            "observed_at": "2026-08-07T00:00:00Z",
        }
        second = process_correction_record(
            update_record,
            env,
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert second.disposition == "applied"
        pages = list(wiki.glob("*.md"))
        assert len(pages) == 1  # still one entity, not a second create
        meta, _ = parse_frontmatter(pages[0].read_text())
        assert meta["industry"] == "Software"  # earlier field preserved
        assert meta["employee_count"] == "500"  # new field applied
        assert second.entity_path == first.entity_path  # same page, not a new one

    def test_no_key_no_create_uid_target(self, tmp_path: Path) -> None:
        """AC: no external key, behavior unchanged -- an unresolvable uid
        target still raises a tier. No name-only creation."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        index = EntityIndex(wiki)
        cfg = _fields_config(industry={"shape": "scalar", "writers": ["employer-feed"]})
        record = {
            "record": "correction",
            "target": {"uid": "does-not-exist"},
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert result.disposition == "raised-tier"
        assert list(wiki.glob("*.md")) == []

    def test_no_key_no_create_type_name_target(self, tmp_path: Path) -> None:
        """AC: no name-only creation -- a {type,name} target that resolves
        to nothing still raises, never creates."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        index = EntityIndex(wiki)
        cfg = _fields_config(industry={"shape": "scalar", "writers": ["employer-feed"]})
        record = {
            "record": "correction",
            "target": {"type": "company", "name": "Acme Inc"},
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert result.disposition == "raised-tier"
        assert list(wiki.glob("*.md")) == []

    def test_ambiguous_handle_still_raises(self, tmp_path: Path) -> None:
        """A handle resolving to >1 entity still raises -- creating here
        would manufacture a duplicate for an entity that already exists."""
        wiki = tmp_path / "wiki"
        _write_page(wiki, "a.md", {"uid": "company-a", "type": "company", "name": "A"})
        _write_page(wiki, "b.md", {"uid": "company-b", "type": "company", "name": "B"})
        index = EntityIndex(wiki)
        registry = {
            "company-a": {"type": "company", "handles": {"domains": ["acme.example"]}},
            "company-b": {"type": "company", "handles": {"domains": ["acme.example"]}},
        }
        cfg = _fields_config(industry={"shape": "scalar", "writers": ["employer-feed"]})
        record = {
            "record": "correction",
            "target": {"type": "company", "handle": {"domains": "acme.example"}},
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert result.disposition == "raised-tier"
        assert len(list(wiki.glob("*.md"))) == 2  # unchanged, no third page

    def test_missing_type_on_handle_target_raises(self, tmp_path: Path) -> None:
        """A handle target with no declared type cannot create -- the
        submitter must say what kind of entity to make."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        index = EntityIndex(wiki)
        cfg = _fields_config(industry={"shape": "scalar", "writers": ["employer-feed"]})
        record = {
            "record": "correction",
            "target": {"handle": {"domains": "acme.example"}},
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert result.disposition == "raised-tier"
        assert list(wiki.glob("*.md")) == []

    def test_schema_invalid_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A create that would violate the schema raises a tier; nothing
        is written."""

        class _Probe(BaseModel):
            x: int

        try:
            _Probe.model_validate({"x": "not-an-int"})
        except PydanticValidationError as exc:
            boom = exc
        else:  # pragma: no cover - defensive
            raise AssertionError("expected a ValidationError")

        def _always_fail(meta: dict) -> None:
            raise boom

        monkeypatch.setattr("athenaeum.corrections.validate_wiki_meta", _always_fail)

        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        index = EntityIndex(wiki)
        cfg = _fields_config(industry={"shape": "scalar", "writers": ["employer-feed"]})
        record = {
            "record": "correction",
            "target": {"type": "company", "handle": {"domains": "acme.example"}},
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=index,
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert result.disposition == "raised-tier"
        assert "schema" in result.reason.lower()
        assert list(wiki.glob("*.md")) == []

    def test_same_run_second_record_resolves_created_page(self, tmp_path: Path) -> None:
        """athenaeum#865 same-run hole: the registry snapshot is loaded once
        per `process_batch_file` call. A second record in the SAME batch,
        keyed on the same handle as the first, must resolve to the page
        the first record just created -- not create a second page."""
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        index = EntityIndex(wiki)
        env = self._envelope()
        cfg = _fields_config(
            domains={"shape": "list", "writers": ["employer-feed"]},
            industry={"shape": "scalar", "writers": ["employer-feed"]},
        )
        lines = [
            json.dumps(env),
            json.dumps(
                {
                    "record": "correction",
                    "target": {"type": "company", "handle": {"domains": "acme.example"}},
                    "op": "add",
                    "field": "domains",
                    "value": "acme.example",
                    "source": "script:employer-feed",
                    "observed_at": "2026-08-06T00:00:00Z",
                }
            ),
            json.dumps(
                {
                    "record": "correction",
                    "target": {"type": "company", "handle": {"domains": "acme.example"}},
                    "op": "set",
                    "field": "industry",
                    "value": "Software",
                    "source": "script:employer-feed",
                    "observed_at": "2026-08-06T00:00:00Z",
                }
            ),
        ]
        batch_path = tmp_path / "raw" / "employer-feed" / "b.jsonl"
        batch_path.parent.mkdir(parents=True)
        batch_path.write_text("\n".join(lines) + "\n")

        outcome = process_batch_file(
            batch_path,
            env,
            "employer-feed",
            index=index,
            knowledge_root=tmp_path,
            config=cfg,
            registry_entities={},
        )
        assert [r.disposition for r in outcome.results] == ["applied", "applied"]
        pages = list(wiki.glob("*.md"))
        assert len(pages) == 1  # NOT two pages from one batch
        meta, _ = parse_frontmatter(pages[0].read_text())
        assert meta["domains"] == ["acme.example"]
        assert meta["industry"] == "Software"

    def test_dry_run_agrees_with_real_run_same_run_create_then_update(
        self, tmp_path: Path
    ) -> None:
        """issue athenaeum#873: a `dry_run` batch that CREATES an entity (the
        athenaeum#865 tier-0 create-by-handle path) must preview the SAME
        outcome the real run produces -- one entity previewed, not two.
        `process_correction_record`'s create branch used to update the
        in-run `registry_entities` view (and `index`) only on the
        NON-dry-run path, so a second record in the same dry-run batch,
        keyed on the same handle as the first, did not resolve to the
        entity the first record notionally created and took the create
        branch again, minting a second `uid`.

        This test runs the IDENTICAL two-record batch (one `add domains`,
        one `set industry`, both keyed on ``handle: {domains:
        acme.example}`` -- mirroring
        ``test_same_run_second_record_resolves_created_page`` above) through
        both `dry_run=False` and `dry_run=True` and asserts the two AGREE on
        both dispositions and the number of distinct `entity_path` values,
        rather than hardcoding either side's expectation (the strongest
        form of this regression test)."""
        env = self._envelope()
        cfg = _fields_config(
            domains={"shape": "list", "writers": ["employer-feed"]},
            industry={"shape": "scalar", "writers": ["employer-feed"]},
        )
        lines = [
            json.dumps(env),
            json.dumps(
                {
                    "record": "correction",
                    "target": {"type": "company", "handle": {"domains": "acme.example"}},
                    "op": "add",
                    "field": "domains",
                    "value": "acme.example",
                    "source": "script:employer-feed",
                    "observed_at": "2026-08-06T00:00:00Z",
                }
            ),
            json.dumps(
                {
                    "record": "correction",
                    "target": {"type": "company", "handle": {"domains": "acme.example"}},
                    "op": "set",
                    "field": "industry",
                    "value": "Software",
                    "source": "script:employer-feed",
                    "observed_at": "2026-08-06T00:00:00Z",
                }
            ),
        ]
        batch_text = "\n".join(lines) + "\n"

        def _run(dry_run: bool, root: Path) -> tuple[list[str], list[Path | None]]:
            wiki = root / "wiki"
            wiki.mkdir(parents=True)
            batch_path = root / "raw" / "employer-feed" / "b.jsonl"
            batch_path.parent.mkdir(parents=True)
            batch_path.write_text(batch_text)
            outcome = process_batch_file(
                batch_path,
                env,
                "employer-feed",
                index=EntityIndex(wiki),
                knowledge_root=root,
                config=cfg,
                dry_run=dry_run,
                registry_entities={},
            )
            return (
                [r.disposition for r in outcome.results],
                [r.entity_path for r in outcome.results],
            )

        real_dispositions, real_paths = _run(False, tmp_path / "real")
        dry_dispositions, dry_paths = _run(True, tmp_path / "dry")

        assert dry_dispositions == real_dispositions
        assert len(set(dry_paths)) == len(set(real_paths)) == 1

        # Hard invariant: dry run still writes NOTHING to disk.
        assert list((tmp_path / "dry" / "wiki").glob("*.md")) == []
        assert not (tmp_path / "dry" / "registry.json").exists()

        # The real run, by contrast, actually wrote the one entity.
        assert len(list((tmp_path / "real" / "wiki").glob("*.md"))) == 1


class TestCreateTypeGate971:
    """Issue athenaeum#971 AC3: the create branch's declared ``type`` gets the
    same unknown-type handling as the two other deterministic (non-LLM)
    create/upsert paths (``intake.py`` tier0_passthrough,
    ``librarian.py`` tier0_handle_upsert) — reject-and-escalate
    (``disposition="raised-tier"``), never silently mint a page under an
    unrecognized or athenaeum#970-folded type.
    """

    def _envelope(self, **overrides: object) -> dict:
        return _envelope(submitter="employer-feed", **overrides)

    def test_unrecognized_type_raises_tier_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        cfg = _fields_config(
            industry={"shape": "scalar", "writers": ["employer-feed"]},
        )
        record = {
            "record": "correction",
            "target": {
                "type": "totally-bogus-type",
                "handle": {"domains": "acme.example"},
            },
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert result.disposition == "raised-tier"
        assert "totally-bogus-type" in (result.reason or "")
        assert list(wiki.glob("*.md")) == []

    def test_folded_type_user_raises_tier_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        # athenaeum#970's fold enforcement teeth: `type: user` on a NEW create is no
        # longer minted verbatim — it must raise a tier, exactly like a
        # never-declared type, so the folded value cannot recur.
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        cfg = _fields_config(
            industry={"shape": "scalar", "writers": ["employer-feed"]},
        )
        record = {
            "record": "correction",
            "target": {"type": "user", "handle": {"domains": "acme.example"}},
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert result.disposition == "raised-tier"
        assert list(wiki.glob("*.md")) == []

    def test_recognized_type_still_creates(self, tmp_path: Path) -> None:
        # Control: a currently-valid type (in KNOWN_TYPES, since this test's
        # tmp wiki has no `_schema/types.md`) is unaffected by the new gate.
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        cfg = _fields_config(
            industry={"shape": "scalar", "writers": ["employer-feed"]},
        )
        record = {
            "record": "correction",
            "target": {"type": "company", "handle": {"domains": "acme.example"}},
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert result.disposition == "applied"
        assert len(list(wiki.glob("*.md"))) == 1

    def test_declared_types_md_gates_over_the_known_types_fallback(
        self, tmp_path: Path
    ) -> None:
        # When a deployment DOES have a `_schema/types.md`, that declared
        # list — not the code-side KNOWN_TYPES fallback — is authoritative.
        # A type present in KNOWN_TYPES but ABSENT from this deployment's
        # types.md must still raise a tier.
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        schema_dir = wiki / "_schema"
        schema_dir.mkdir()
        (schema_dir / "types.md").write_text(
            "| Type | Description |\n|---|---|\n| company | ... |\n"
        )
        cfg = _fields_config(
            industry={"shape": "scalar", "writers": ["employer-feed"]},
        )
        record = {
            "record": "correction",
            "target": {"type": "principle", "handle": {"domains": "acme.example"}},
            "op": "set",
            "field": "industry",
            "value": "Software",
            "source": "api:apollo",
            "observed_at": "2026-08-06T00:00:00Z",
        }
        result = process_correction_record(
            record,
            self._envelope(),
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities={},
            config=cfg,
        )
        assert result.disposition == "raised-tier"
        assert list(wiki.glob("*.md")) == []


class TestMixedDispositionBatch:
    def test_conformant_record_still_applies_alongside_a_fallthrough_record(
        self, tmp_path: Path
    ) -> None:
        """§8: "A fallthrough is not a failure and never fails a batch:
        conformant records in the same batch apply normally." One malformed
        record and one conformant record in the SAME batch -- the malformed
        one raises a tier, the conformant one still writes."""
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-a", "type": "person", "name": "A"})
        env = _envelope(submitter="enrichment-service")
        lines = [
            json.dumps(env),
            json.dumps(
                {
                    # Unparseable source -> raised-tier (§8).
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "op": "set",
                    "field": "current_title",
                    "value": "x",
                    "source": "not a valid source ref",
                    "observed_at": "2026-08-06T00:00:00Z",
                }
            ),
            json.dumps(
                {
                    # Fully conformant.
                    "record": "correction",
                    "target": {"uid": "person-a"},
                    "op": "set",
                    "field": "current_title",
                    "value": "VP Engineering",
                    "source": "api:enrichment-vendor",
                    "observed_at": "2026-08-06T05:58:40Z",
                }
            ),
        ]
        batch_path = tmp_path / "raw" / "enrichment-service" / "b.jsonl"
        batch_path.parent.mkdir(parents=True)
        batch_path.write_text("\n".join(lines) + "\n")
        index = EntityIndex(wiki)
        outcome = process_batch_file(
            batch_path,
            env,
            "enrichment-service",
            index=index,
            knowledge_root=tmp_path,
            config=_fields_config(
                current_title={"shape": "scalar", "writers": ["enrichment-service"]}
            ),
        )
        dispositions = [r.disposition for r in outcome.results]
        assert dispositions.count("raised-tier") == 1
        assert dispositions.count("applied") == 1
        assert "current_title: VP Engineering" in (wiki / "p.md").read_text()


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
        # The fact actually landed on the excluded/PII surface -- as the SAME
        # markdown contact-record shape `classify_contact_value` /
        # `iter_contact_records` read and write (issue athenaeum#872), never a
        # parallel `{uid}.json` record only this router understood.
        excluded_root = tmp_path / "excluded"
        surface_file = excluded_root / "person-alex.md"
        assert surface_file.exists()
        surface_meta, _ = parse_frontmatter(surface_file.read_text())
        assert surface_meta["bounced"] == "2026-08-06"
        assert surface_meta["uid"] == "person-alex"
        # Reachable through pii's own contact-record surface scan, not just
        # at a filename this test happens to know.
        from athenaeum import pii

        assert pii.iter_contact_records(excluded_root) == [surface_file]

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

    def test_reads_through_a_legacy_json_record_and_migrates_on_write(
        self, tmp_path: Path
    ) -> None:
        """issue athenaeum#872 backward compatibility: a uid a PRE-FIX run wrote as
        ``{uid}.json`` (the shape `_write_surface_record` minted before this
        issue) is read through -- not silently orphaned -- and the very next
        write for that uid lands on the canonical ``.md`` record instead.
        """
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-alex", "type": "person", "name": "Alex Doe"})
        registry = {
            "person-alex": {"type": "person", "handles": {"alt_emails": ["alex@example.org"]}}
        }
        excluded_root = tmp_path / "excluded"
        excluded_root.mkdir(parents=True)
        legacy_file = excluded_root / "person-alex.json"
        legacy_file.write_text(
            json.dumps({"uid": "person-alex", "bounced": "2026-08-01", "note": "legacy"}),
            encoding="utf-8",
        )

        result = process_correction_record(
            self._record(),  # bounced=2026-08-06, monotone set -- applies over any prior value
            _envelope(submitter="delivery-monitor"),
            index=EntityIndex(wiki),
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=self._config(),
        )
        assert result.disposition == "routed-elsewhere"

        # The legacy file is untouched -- read-through, not delete-on-read.
        assert json.loads(legacy_file.read_text())["bounced"] == "2026-08-01"

        # The write landed on the canonical markdown record, carrying the
        # legacy data forward (the `note` key survives) plus the new value.
        canonical_file = excluded_root / "person-alex.md"
        assert canonical_file.exists()
        canonical_meta, _ = parse_frontmatter(canonical_file.read_text())
        assert canonical_meta["bounced"] == "2026-08-06"
        assert canonical_meta["note"] == "legacy"


# ---------------------------------------------------------------------------
# §7.1 usage-class declaration (issue athenaeum#872): a contact-value
# correction may carry the usage class of the value it writes, routed
# through the SAME store `classify_contact_value` (issue athenaeum#866) owns --
# these tests assert AGREEMENT between the correction path and that store,
# not incidental facts like a file extension.
# ---------------------------------------------------------------------------


class TestSensitivityRoutingUsageClass:
    def _config(self) -> dict:
        return {
            "librarian": {
                "corrections": {
                    "fields": {
                        "alt_emails": {"shape": "list", "writers": ["delivery-monitor"]}
                    },
                    "sensitive_fields": {"alt_emails": "pii"},
                }
            },
            "storage": {"mapping": {"pii": "excluded"}},
        }

    def _record(self, **overrides) -> dict:
        base = {
            "record": "correction",
            "target": {"uid": "person-alex"},
            "op": "add",
            "field": "alt_emails",
            "value": "alex@example.org",
            "source": "api:delivery-monitor:2026-08-06",
            "observed_at": "2026-08-06T14:01:55Z",
        }
        base.update(overrides)
        return base

    def _setup(self, tmp_path: Path) -> tuple[EntityIndex, dict, Path]:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "p.md", {"uid": "person-alex", "type": "person", "name": "Alex Doe"})
        return EntityIndex(wiki), {}, tmp_path / "excluded"

    def test_correction_carries_a_class_agrees_with_classify_contact_value(
        self, tmp_path: Path
    ) -> None:
        index, registry, excluded_root = self._setup(tmp_path)
        result = process_correction_record(
            self._record(usage_class="observed"),
            _envelope(submitter="delivery-monitor"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=self._config(),
        )
        assert result.disposition == "routed-elsewhere"

        from athenaeum import pii

        record_path = excluded_root / "person-alex.md"
        before = record_path.read_text()
        # The agreement assertion: calling `classify_contact_value` directly
        # with the SAME class/source/observed_at the correction declared
        # resolves to the SAME record and finds it already classified --
        # byte-identical, no second write. The correction path and
        # `classify_contact_value` converge on one classification, not two.
        again = pii.classify_contact_value(
            excluded_root,
            "alex@example.org",
            usage_class="observed",
            source="api:delivery-monitor:2026-08-06",
            observed_at="2026-08-06T14:01:55Z",
        )
        assert again == record_path
        assert record_path.read_text() == before
        assert (
            pii.is_outreach_eligible(pii.read_bounce_record(record_path), "alex@example.org")
            is True
        )

        # And readable through `read_person`, filterable by usage_classes,
        # exactly as a directly-classified value is.
        read = pii.read_person(
            tmp_path,
            self._config(),
            "person-alex",
            include_contact=True,
            usage_classes=[pii.USAGE_CLASS_OBSERVED],
        )
        assert read is not None
        assert read.contact["alt_emails"] == ["alex@example.org"]
        assert read.classifications["alt_emails"][0].usage_class == pii.USAGE_CLASS_OBSERVED

    def test_correction_without_a_class_stays_unclassified(self, tmp_path: Path) -> None:
        index, registry, excluded_root = self._setup(tmp_path)
        result = process_correction_record(
            self._record(),
            _envelope(submitter="delivery-monitor"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=self._config(),
        )
        assert result.disposition == "routed-elsewhere"

        from athenaeum import pii

        record_path = excluded_root / "person-alex.md"
        meta = pii.read_bounce_record(record_path)
        assert "alex@example.org" in meta.get("alt_emails", [])
        # No classification entry was written at all -- never defaulted to a
        # usable class.
        assert meta.get(pii.CONTACT_CLASSIFICATION_FIELD) is None
        classification = pii.classification_for_value(meta, "alex@example.org")
        assert classification.usage_class == pii.USAGE_CLASS_UNCLASSIFIED
        assert classification.outreach_eligible is False

        read = pii.read_person(
            tmp_path, self._config(), "person-alex", include_contact=True
        )
        assert read is not None
        assert read.classifications["alt_emails"][0].usage_class == pii.USAGE_CLASS_UNCLASSIFIED

    def test_correction_cannot_downgrade(self, tmp_path: Path) -> None:
        index, registry, excluded_root = self._setup(tmp_path)
        cfg = self._config()
        first = process_correction_record(
            self._record(usage_class="observed"),
            _envelope(submitter="delivery-monitor"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert first.disposition == "routed-elsewhere"

        # A second correction re-asserts the SAME (already-present) address
        # with the weaker `provider` class.
        second = process_correction_record(
            self._record(
                usage_class="provider",
                observed_at="2026-08-07T09:00:00Z",
                source="api:vendor-sync:2026-08-07",
            ),
            _envelope(submitter="delivery-monitor"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        # The field-value delta is zero (the address was already present);
        # the classification attempt is a separate, store-level question.
        assert second.disposition == "noop"

        from athenaeum import pii

        record_path = excluded_root / "person-alex.md"
        classification = pii.classification_for_value(
            pii.read_bounce_record(record_path), "alex@example.org"
        )
        # The no-downgrade rule -- enforced by
        # `pii._merge_contact_classification`, the SAME athenaeum#866 store rule,
        # not a second implementation here -- refused the provider assertion;
        # the stronger observed claim survives, provenance included.
        assert classification.usage_class == pii.USAGE_CLASS_OBSERVED
        assert classification.source == "api:delivery-monitor:2026-08-06"

    def test_invalid_usage_class_value_raises_tier(self, tmp_path: Path) -> None:
        index, registry, _ = self._setup(tmp_path)
        result = process_correction_record(
            self._record(usage_class="premium"),
            _envelope(submitter="delivery-monitor"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=self._config(),
        )
        assert result.disposition == "raised-tier"
        assert "usage_class" in result.reason

    def test_usage_class_on_non_identifier_field_raises_tier(self, tmp_path: Path) -> None:
        index, registry, _ = self._setup(tmp_path)
        cfg = self._config()
        cfg["librarian"]["corrections"]["fields"]["bounced"] = {
            "shape": "scalar",
            "writers": ["delivery-monitor"],
        }
        cfg["librarian"]["corrections"]["sensitive_fields"]["bounced"] = "pii"
        result = process_correction_record(
            {
                "record": "correction",
                "target": {"uid": "person-alex"},
                "op": "set",
                "field": "bounced",
                "value": "2026-08-06",
                "usage_class": "observed",
                "source": "api:delivery-monitor:2026-08-06",
                "observed_at": "2026-08-06T14:01:55Z",
            },
            _envelope(submitter="delivery-monitor"),
            index=index,
            knowledge_root=tmp_path,
            registry_entities=registry,
            config=cfg,
        )
        assert result.disposition == "raised-tier"
        assert "usage_class" in result.reason


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
    def test_retire_without_git_refuses(self, tmp_path: Path) -> None:
        """issue athenaeum#978 (S3, Tier B): the old silent-``unlink``
        fallback for a non-git *knowledge_root* is REMOVED (design note
        §4.4 R1) — retirement now refuses, leaving the batch in place,
        rather than silently discarding it unrecoverably."""
        batch = tmp_path / "batch.jsonl"
        batch.write_text("x\n")
        assert retire_batch(tmp_path, batch) is False
        assert batch.exists()

    def test_retire_refuses_against_fake_declaring_no_recovery_capability(
        self, tmp_path: Path
    ) -> None:
        """issue athenaeum#978 (S3, Tier B AC5): even with a REAL git repo
        present, an injected store fake declaring neither ``versioned`` nor
        ``purgeable`` (design note §4.4 R1) makes retirement refuse — proving
        the gate is driven by the declared capability, not by probing
        ``knowledge_root / ".git"`` directly."""
        from tests.store_fakes import NoRecoveryStore

        _git_init(tmp_path)
        batch = tmp_path / "batch.jsonl"
        batch.write_text("x\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", "seed")

        assert retire_batch(tmp_path, batch, store=NoRecoveryStore()) is False
        assert batch.exists()

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
        # issue athenaeum#978 (S3): retirement now refuses against a store
        # that is not versioned rather than falling back to a silent
        # unlink, so this needs a real git repo to observe batch1 retired.
        _git_init(tmp_path)
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
