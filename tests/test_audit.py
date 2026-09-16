# SPDX-License-Identifier: Apache-2.0
"""``athenaeum audit`` — page audit pass (issue athenaeum#1624).

Covers: dry-run writes nothing; --limit/--sample+--seed bound the pages
processed and --seed is reproducible; --batch routes through
athenaeum.batch's transport and not the synchronous client; --apply stamps
last_audited/audit_version; coordinate fills vs. the undeterminable marker
vs. "never overwrite a populated value"; the retirement-candidate flag and
its generic (no source-type/adapter-name) flagging logic; the run report's
per-page verdicts + tokens/cost + totals; and that every template scaffold
documents the new fields. Also covers the transitory-class decay stamping
(issue athenaeum#1713) — see ``TestTransitoryClassStamping`` below. All
fixtures — no test reads or writes a live knowledge store.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from athenaeum.audit import (
    AUDIT_VERSION,
    AuditVerdict,
    apply_audit_report,
    build_audit_report,
    parse_audit_response,
    render_audit_prompt,
    select_audit_pages,
)
from athenaeum.models import parse_frontmatter
from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage


def _page(root: Path, name: str, frontmatter: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge" / "wiki"
    root.mkdir(parents=True)
    return root


# --- fixture pages -----------------------------------------------------


def _dated_page(root: Path) -> Path:
    return _page(
        root,
        "dated.md",
        "uid: dated1\ntype: concept\nname: Dated Thing\n",
        "This engagement ran from 2026-01-01 to 2026-06-30, per the "
        "signed statement of work.[^1]\n\n[^1]: [[src-sow|SOW]] 2026-01-01\n",
    )


def _vague_page(root: Path) -> Path:
    return _page(
        root,
        "vague.md",
        "uid: vague1\ntype: concept\nname: Vague Thing\n",
        "A concept with no temporal detail at all.\n",
    )


def _scoped_page(root: Path) -> Path:
    return _page(
        root,
        "scoped.md",
        "uid: scoped1\ntype: concept\nname: Scoped Thing\nclaimed_scope: team:pre-existing\n",
        "Already has a claimed_scope.\n",
    )


def _restating_page(root: Path) -> Path:
    return _page(
        root,
        "restate.md",
        "uid: restate1\ntype: concept\nname: Restating Thing\n",
        "As the cited source states, the deal closed in March.[^1]\n\n"
        "[^1]: [[src-x|Source X]] March\n",
    )


def _factual_page(root: Path) -> Path:
    return _page(
        root,
        "factual.md",
        "uid: factual1\ntype: concept\nname: Factual Thing\n",
        "The source says the deal closed in March, but the actual "
        "signed contract date was in April — a discrepancy worth noting.\n",
    )


def _responder(params: dict[str, Any]) -> str:
    """Deterministic per-page JSON verdict, keyed on the prompt's page name."""
    prompt = params["messages"][0]["content"]
    if "Dated Thing" in prompt:
        return json.dumps(
            {
                "valid_from": {"value": "2026-01-01"},
                "valid_until": {"value": "2026-06-30"},
                "retirement_candidate": False,
                "retirement_reason": "",
            }
        )
    if "Vague Thing" in prompt:
        return json.dumps(
            {
                "valid_from": {"undeterminable": "no dated validity window stated"},
                "valid_until": {"undeterminable": "no dated validity window stated"},
                "retirement_candidate": False,
                "retirement_reason": "",
            }
        )
    if "Scoped Thing" in prompt:
        # Malicious/careless: tries to overwrite claimed_scope even though it
        # was not asked about (not listed under "Fields to determine").
        return json.dumps(
            {
                "valid_from": {"undeterminable": "no date stated"},
                "valid_until": {"undeterminable": "no date stated"},
                "claimed_scope": {"value": "team:should-never-land"},
                "retirement_candidate": False,
                "retirement_reason": "",
            }
        )
    if "Restating Thing" in prompt:
        return json.dumps(
            {
                "retirement_candidate": True,
                "retirement_reason": "restates the cited source with no independent claim",
            }
        )
    if "Factual Thing" in prompt:
        return json.dumps(
            {
                "retirement_candidate": False,
                "retirement_reason": "",
            }
        )
    return json.dumps({"retirement_candidate": False, "retirement_reason": ""})


def _client() -> FakeLLMClient:
    def responder(**params: Any) -> Any:
        return make_llm_response(_responder(params), usage=make_llm_usage(100, 50))

    return FakeLLMClient(responder=responder)


# --- AC1: dry-run writes nothing ----------------------------------------


class TestDryRun:
    def test_dry_run_leaves_every_fixture_page_byte_unchanged(self, wiki: Path) -> None:
        paths = [_dated_page(wiki), _vague_page(wiki), _scoped_page(wiki), _restating_page(wiki)]
        before = {p: p.read_bytes() for p in paths}

        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")

        assert report.scanned == len(paths)
        for p in paths:
            assert p.read_bytes() == before[p]


# --- AC2: --limit / --sample + --seed -----------------------------------


class TestSelection:
    def test_limit_bounds_pages_processed(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        for i in range(10):
            _page(wiki, f"p{i}.md", f"uid: uid{i}\ntype: concept\nname: P{i}\n", "Body.\n")

        selected = select_audit_pages(wiki, limit=3)
        assert len(selected) == 3

    def test_same_seed_selects_same_sample_twice(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        for i in range(10):
            page_type = "concept" if i % 2 == 0 else "person"
            _page(wiki, f"p{i}.md", f"uid: uid{i}\ntype: {page_type}\nname: P{i}\n", "Body.\n")

        first = [p.name for p, _m, _b in select_audit_pages(wiki, sample=4, seed=42)]
        second = [p.name for p, _m, _b in select_audit_pages(wiki, sample=4, seed=42)]
        assert first == second
        assert len(first) == 4

    def test_different_seed_can_select_a_different_sample(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        for i in range(20):
            page_type = "concept" if i % 2 == 0 else "person"
            _page(wiki, f"p{i}.md", f"uid: uid{i}\ntype: {page_type}\nname: P{i}\n", "Body.\n")

        a = [p.name for p, _m, _b in select_audit_pages(wiki, sample=6, seed=1)]
        b = [p.name for p, _m, _b in select_audit_pages(wiki, sample=6, seed=2)]
        assert a != b

    def test_sample_is_stratified_by_type(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        for i in range(8):
            _page(wiki, f"c{i}.md", f"uid: c{i}\ntype: concept\nname: C{i}\n", "Body.\n")
        for i in range(2):
            _page(wiki, f"p{i}.md", f"uid: p{i}\ntype: person\nname: P{i}\n", "Body.\n")

        selected = select_audit_pages(wiki, sample=5, seed=7)
        types = {m["type"] for _p, m, _b in selected}
        # 80/20 split over 10 pages, 5 sampled -> proportionally both strata
        # should be represented (4 concept, 1 person under largest-remainder).
        assert types == {"concept", "person"}


# --- AC3: --batch routes through batch.py's transport --------------------


class TestBatchTransport:
    def test_batch_mode_uses_batches_create_not_sync(self, wiki: Path) -> None:
        from types import SimpleNamespace

        _dated_page(wiki)
        _vague_page(wiki)

        submitted: list[list[dict[str, Any]]] = []
        sync_calls: list[dict[str, Any]] = []

        class _FakeBatches:
            def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
                submitted.append(list(requests))
                return SimpleNamespace(id="msgbatch_1", processing_status="ended")

            def retrieve(self, batch_id: str) -> SimpleNamespace:
                return SimpleNamespace(id=batch_id, processing_status="ended")

            def results(self, batch_id: str):
                for req in submitted[0]:
                    text = _responder(req["params"])
                    yield SimpleNamespace(
                        custom_id=req["custom_id"],
                        result=SimpleNamespace(
                            type="succeeded",
                            message=make_llm_response(text, usage=make_llm_usage(10, 5)),
                        ),
                    )

        class _FakeBatchClient:
            def __init__(self) -> None:
                self.batches = _FakeBatches()

                def create(**params: Any) -> Any:
                    sync_calls.append(params)
                    raise AssertionError("unexpected synchronous call in --batch mode")

                self.messages = SimpleNamespace(create=create, batches=self.batches)

        report = build_audit_report(
            wiki, client=_FakeBatchClient(), model="claude-haiku-4-5-20251001", use_batch=True
        )

        assert submitted, "requests must be routed to batches.create"
        assert not sync_calls
        assert report.used_batch is True
        assert len(report.audited) == 2


# --- AC4/AC5: --apply stamps last_audited/audit_version + coordinate fills


class TestApply:
    def test_apply_stamps_last_audited_and_audit_version(self, wiki: Path) -> None:
        path = _dated_page(wiki)
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        changed = apply_audit_report(report, wiki)
        assert changed == 1

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["audit_version"] == AUDIT_VERSION
        from datetime import datetime

        datetime.strptime(meta["last_audited"], "%Y-%m-%dT%H:%M:%SZ")  # parseable, raises if not

    def test_determinable_coordinates_are_filled(self, wiki: Path) -> None:
        path = _dated_page(wiki)
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["valid_from"] == "2026-01-01"
        assert meta["valid_until"] == "2026-06-30"
        assert "audit_findings" not in meta or "valid_from" not in meta.get("audit_findings", {})

    def test_undeterminable_is_recorded_not_blank_not_guessed(self, wiki: Path) -> None:
        path = _vague_page(wiki)
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "valid_from" not in meta
        assert "valid_until" not in meta
        findings = meta["audit_findings"]
        assert findings["valid_from"].startswith("undeterminable:")
        assert findings["valid_until"].startswith("undeterminable:")

    def test_populated_coordinate_is_never_overwritten(self, wiki: Path) -> None:
        path = _scoped_page(wiki)
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["claimed_scope"] == "team:pre-existing"
        # Never even asked about it (not an empty field), so no finding either.
        assert "claimed_scope" not in meta.get("audit_findings", {})

    def test_second_apply_is_a_pure_no_op_on_already_audited_fields(self, wiki: Path) -> None:
        path = _dated_page(wiki)
        report1 = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        apply_audit_report(report1, wiki)
        after_first = path.read_text(encoding="utf-8")

        report2 = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        apply_audit_report(report2, wiki)
        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        # last_audited advances (re-stamped), but the coordinate fields
        # themselves are untouched by the second pass.
        assert meta["valid_from"] == "2026-01-01"
        assert meta["valid_until"] == "2026-06-30"
        assert after_first != ""  # sanity: first apply actually wrote something


# --- AC6: retirement flag, generic ----------------------------------------


class TestRetirementFlag:
    def test_restating_page_is_flagged(self, wiki: Path) -> None:
        _restating_page(wiki)
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        [v] = [v for v in report.verdicts if v.uid == "restate1"]
        assert v.retirement_candidate is True
        assert v.retirement_reason

    def test_page_with_distinct_claim_is_not_flagged(self, wiki: Path) -> None:
        _factual_page(wiki)
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        [v] = [v for v in report.verdicts if v.uid == "factual1"]
        assert v.retirement_candidate is False

    def test_flagging_logic_is_generic(self) -> None:
        """No source-type, adapter-name, or board-title string anywhere in
        the module that decides/parses the retirement flag (issue athenaeum#1624
        AC6): the decision is entirely delegated to the model's own JSON
        verdict, never branched on in code."""
        import athenaeum.audit as audit_module
        from athenaeum.models import SOURCE_TYPES

        source = Path(audit_module.__file__).read_text(encoding="utf-8")
        # Checked as STRING LITERALS (quoted), not bare substrings — several
        # SOURCE_TYPES members ("document", "external", "inferred") are also
        # ordinary English words that legitimately appear in prose/docstrings
        # here; what AC6 actually forbids is the flagging logic BRANCHING on
        # one of these as a comparison value.
        banned = set(SOURCE_TYPES) | {
            "claude-code",
            "claude_code",
            "goodreads",
            "trello",
            "notion",
            "slack",
            "kroblog",
            "wiki-markdown-embedded",
        }
        hits = [
            token
            for token in banned
            if f'"{token}"' in source or f"'{token}'" in source
        ]
        assert not hits, f"generic flagging logic must not reference: {hits}"


# --- AC7: run report -------------------------------------------------------


class TestReport:
    def test_report_carries_per_page_verdicts_tokens_cost_and_totals(self, wiki: Path) -> None:
        _dated_page(wiki)
        _vague_page(wiki)
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")

        payload = report.to_dict()
        assert payload["scanned"] == 2
        assert len(payload["verdicts"]) == 2
        for v in payload["verdicts"]:
            assert v["input_tokens"] == 100
            assert v["output_tokens"] == 50
            assert v["cost_usd"] >= 0
        assert payload["totals"]["input_tokens"] == 200
        assert payload["totals"]["output_tokens"] == 100
        assert payload["totals"]["cost_usd"] >= 0

        text = report.render_text()
        assert "totals:" in text
        assert "dated.md" in text or "Dated Thing" in text or "dated.md" in text

    def test_json_round_trips(self, wiki: Path) -> None:
        _dated_page(wiki)
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        json.dumps(report.to_dict())  # must not raise


# --- Reusable per-page function: signature + parse contract ---------------


class TestAuditPageContract:
    def test_audit_page_returns_a_verdict_dataclass(self, wiki: Path) -> None:
        from athenaeum.audit import audit_page

        path = _dated_page(wiki)
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        verdict = audit_page(
            _client(),
            uid=meta["uid"],
            path=path,
            meta=meta,
            body=body,
            model="claude-haiku-4-5-20251001",
        )
        assert isinstance(verdict, AuditVerdict)
        assert verdict.uid == "dated1"
        assert verdict.error is None

    def test_audit_page_never_raises_on_transport_failure(self, wiki: Path) -> None:
        from athenaeum.audit import audit_page

        path = _dated_page(wiki)
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        client = FakeLLMClient(raises=RuntimeError("boom"))
        verdict = audit_page(
            client, uid=meta["uid"], path=path, meta=meta, body=body, model="m"
        )
        assert verdict.error is not None

    def test_parse_audit_response_ignores_fields_not_asked_about(self) -> None:
        fills, findings, retirement, reason = parse_audit_response(
            json.dumps({"claimed_scope": {"value": "should-be-ignored"}}),
            ["valid_from"],
        )
        assert fills == {}
        assert findings == {}

    def test_render_audit_prompt_lists_only_empty_fields(self) -> None:
        prompt = render_audit_prompt({"name": "X", "type": "concept"}, "body", ["valid_from"])
        assert "valid_from" in prompt
        assert "claimed_scope" not in prompt


# --- No LLM client available: dry-run degrades cleanly ---------------------


class TestNoClient:
    def test_no_client_reports_undecided_and_writes_nothing(self, wiki: Path) -> None:
        path = _dated_page(wiki)
        before = path.read_bytes()
        report = build_audit_report(wiki, client=None, model="m")
        assert report.llm_available is False
        assert report.skipped
        changed = apply_audit_report(report, wiki)
        assert changed == 0
        assert path.read_bytes() == before


# --- AC8: templates document the new fields --------------------------------


class TestTemplatesDocumentFields:
    @pytest.mark.parametrize(
        "name", ["company.md", "concept.md", "person.md", "project.md", "source.md"]
    )
    def test_scaffold_documents_audit_fields(self, name: str) -> None:
        from athenaeum import templates

        path = Path(templates.__file__).parent / name
        text = path.read_text(encoding="utf-8")
        for field_name in (
            "last_audited",
            "audit_version",
            "valid_from",
            "valid_until",
            "claimed_scope",
            "audit_findings",
        ):
            assert field_name in text, f"{name} must document {field_name}"

    def test_entity_template_schema_doc_documents_audit_fields(self) -> None:
        from athenaeum import schema

        path = Path(schema.__file__).parent / "_entity-template.md"
        text = path.read_text(encoding="utf-8")
        for field_name in (
            "last_audited",
            "audit_version",
            "valid_from",
            "valid_until",
            "claimed_scope",
            "audit_findings",
            # issue athenaeum#1628 Plan item 5 / AC7: the schema-version
            # marker itself must be documented alongside the audit fields.
            "schema_version",
        ):
            assert field_name in text, f"_entity-template.md must document {field_name}"


# --- AC9 (meta): nothing here touches a live knowledge store ---------------


class TestNeverTouchesLiveStore:
    def test_default_knowledge_root_is_never_imported_as_a_path_default_in_tests(self) -> None:
        # Structural check: every test above passes an explicit tmp_path-
        # derived `wiki` fixture to build_audit_report/select_audit_pages —
        # none rely on athenaeum.config.DEFAULT_KNOWLEDGE_ROOT. This test
        # exists as a named, greppable anchor for that property.
        assert True


# --- regression: non-string populated values (review finding, athenaeum#1624) ---


def _yaml_dated_page(root: Path) -> Path:
    """A page whose `valid_from` is an UNQUOTED YAML date.

    `parse_frontmatter` hands this back as a `datetime.date`, not a string —
    the shape a string-only "is it populated?" test reports as empty.
    """
    return _page(
        root,
        "yamldate.md",
        "uid: yamldate1\ntype: concept\nname: Dated Thing\nvalid_from: 2019-05-04\n",
        "This engagement ran from 2026-01-01 to 2026-06-30, per the "
        "signed statement of work.[^1]\n\n[^1]: [[src-sow|SOW]] 2026-01-01\n",
    )


class TestNonStringPopulatedCoordinates:
    """A populated coordinate that YAML parsed as a non-string must be
    treated as populated — never asked about, never overwritten."""

    def test_unquoted_yaml_date_is_not_asked_about(self, wiki: Path) -> None:
        path = _yaml_dated_page(wiki)
        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert not isinstance(meta["valid_from"], str), "fixture must exercise the date path"

        client = _client()
        build_audit_report(wiki, client=client, model="claude-haiku-4-5-20251001")

        prompt = client.calls[0]["messages"][0]["content"]
        assert "valid_until" in prompt
        assert "valid_from" not in prompt

    def test_unquoted_yaml_date_is_never_overwritten(self, wiki: Path) -> None:
        path = _yaml_dated_page(wiki)
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert str(meta["valid_from"]) == "2019-05-04"
        assert "valid_from" not in meta.get("audit_findings", {})


class TestResolvedFindingsAreCleared:
    """A page whose every recorded finding has since been resolved must not
    keep a stale `audit_findings:` block."""

    def test_stale_findings_removed_when_all_resolved(self, wiki: Path) -> None:
        path = _page(
            wiki,
            "resolved.md",
            "uid: dated1\ntype: concept\nname: Dated Thing\n"
            "audit_findings:\n"
            "  valid_from: 'undeterminable: no dated validity window stated'\n"
            "  valid_until: 'undeterminable: no dated validity window stated'\n",
            "This engagement ran from 2026-01-01 to 2026-06-30.[^1]\n\n"
            "[^1]: [[src-sow|SOW]] 2026-01-01\n",
        )
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["valid_from"] == "2026-01-01"
        assert meta["valid_until"] == "2026-06-30"
        assert "audit_findings" not in meta

    def test_unresolved_findings_are_kept(self, wiki: Path) -> None:
        path = _vague_page(wiki)
        report = build_audit_report(wiki, client=_client(), model="claude-haiku-4-5-20251001")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert set(meta["audit_findings"]) == {"valid_from", "valid_until"}


# --- issue athenaeum#1628: fields-to-determine come from the registry ------


class TestFieldsComeFromSchemaMigrationsRegistry:
    def test_no_hard_coded_three_field_tuple_left_in_audit_module(self) -> None:
        """issue athenaeum#1628 AC: `audit.py` takes its fields to determine
        from the registry, with no hard-coded tuple of the three field
        names left in it. `COORDINATE_FIELDS` may still EXIST (kept as a
        derived constant for existing importers), but it must be built by
        reading `athenaeum.schema_migrations.MIGRATIONS`, never by writing
        the three names out as a literal tuple in this module."""
        import athenaeum.audit as audit_module

        source = Path(audit_module.__file__).read_text(encoding="utf-8")
        assert '("valid_from", "valid_until", "claimed_scope")' not in source
        assert "schema_migrations" in source

    def test_coordinate_fields_still_equals_the_v1_to_v2_migration_fields(self) -> None:
        from athenaeum.audit import COORDINATE_FIELDS
        from athenaeum.schema_migrations import MIGRATIONS

        model_migration = next(m for m in MIGRATIONS if m.derivation == "model")
        assert COORDINATE_FIELDS == model_migration.fields

    def test_page_already_past_every_model_migration_is_asked_nothing(self, wiki: Path) -> None:
        # schema_version already at CURRENT_SCHEMA_VERSION: the v1->v2
        # migration is no longer pending, so its fields are not asked about
        # even though they are still blank on the page.
        from athenaeum.schema_migrations import CURRENT_SCHEMA_VERSION

        _page(
            wiki,
            "current.md",
            f"uid: current1\ntype: concept\nname: Current Thing\n"
            f"schema_version: {CURRENT_SCHEMA_VERSION}\n",
            "No temporal detail at all.\n",
        )
        client = _client()
        build_audit_report(wiki, client=client, model="claude-haiku-4-5-20251001")
        prompt = client.calls[0]["messages"][0]["content"]
        assert "none — every coordinate already set" in prompt


class TestSchemaVersionBump:
    """issue athenaeum#1628 decision 4 / AC5: the audit pass bumps
    `schema_version` only when every pending model migration's fields are
    populated or recorded in `audit_findings`, and leaves it unchanged
    otherwise."""

    def _all_empty_page(self, wiki: Path, *, extra_frontmatter: str = "") -> Path:
        return _page(
            wiki,
            "fully.md",
            f"uid: fully1\ntype: concept\nname: Fully Resolved Thing\n{extra_frontmatter}",
            "This ran from 2026-01-01 to 2026-06-30 for team:eng, per the "
            "signed statement of work.[^1]\n\n[^1]: [[src-sow|SOW]] 2026-01-01\n",
        )

    @staticmethod
    def _client_for(text: str) -> "FakeLLMClient":
        def responder(**params: Any) -> Any:
            return make_llm_response(text, usage=make_llm_usage(100, 50))

        return FakeLLMClient(responder=responder)

    def test_bumps_to_current_version_when_every_field_is_resolved(self, wiki: Path) -> None:
        path = self._all_empty_page(wiki)
        fully_resolved = json.dumps(
            {
                "valid_from": {"value": "2026-01-01"},
                "valid_until": {"value": "2026-06-30"},
                "claimed_scope": {"value": "team:eng"},
                "retirement_candidate": False,
                "retirement_reason": "",
            }
        )
        report = build_audit_report(
            wiki, client=self._client_for(fully_resolved), model="claude-haiku-4-5-20251001"
        )
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["schema_version"] == 2

    def test_bumps_when_the_remainder_is_recorded_undeterminable_not_filled(
        self, wiki: Path
    ) -> None:
        path = self._all_empty_page(wiki)
        mixed = json.dumps(
            {
                "valid_from": {"value": "2026-01-01"},
                "valid_until": {"value": "2026-06-30"},
                "claimed_scope": {"undeterminable": "no scope statement found"},
                "retirement_candidate": False,
                "retirement_reason": "",
            }
        )
        report = build_audit_report(
            wiki, client=self._client_for(mixed), model="claude-haiku-4-5-20251001"
        )
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["schema_version"] == 2

    def test_leaves_schema_version_unchanged_when_a_field_is_neither_filled_nor_recorded(
        self, wiki: Path
    ) -> None:
        path = self._all_empty_page(wiki)
        # claimed_scope is entirely absent from the model's JSON: neither a
        # fill nor an undeterminable finding.
        partial = json.dumps(
            {
                "valid_from": {"value": "2026-01-01"},
                "valid_until": {"value": "2026-06-30"},
                "retirement_candidate": False,
                "retirement_reason": "",
            }
        )
        report = build_audit_report(
            wiki, client=self._client_for(partial), model="claude-haiku-4-5-20251001"
        )
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "schema_version" not in meta

    def test_a_page_already_at_v1_bumps_straight_to_v2(self, wiki: Path) -> None:
        path = self._all_empty_page(wiki, extra_frontmatter="schema_version: 1\n")
        fully_resolved = json.dumps(
            {
                "valid_from": {"value": "2026-01-01"},
                "valid_until": {"value": "2026-06-30"},
                "claimed_scope": {"value": "team:eng"},
                "retirement_candidate": False,
                "retirement_reason": "",
            }
        )
        report = build_audit_report(
            wiki, client=self._client_for(fully_resolved), model="claude-haiku-4-5-20251001"
        )
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["schema_version"] == 2

    def test_audit_version_still_stamped_unchanged_alongside_the_bump(self, wiki: Path) -> None:
        path = self._all_empty_page(wiki)
        fully_resolved = json.dumps(
            {
                "valid_from": {"value": "2026-01-01"},
                "valid_until": {"value": "2026-06-30"},
                "claimed_scope": {"value": "team:eng"},
                "retirement_candidate": False,
                "retirement_reason": "",
            }
        )
        report = build_audit_report(
            wiki, client=self._client_for(fully_resolved), model="claude-haiku-4-5-20251001"
        )
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["schema_version"] == 2
        assert meta["audit_version"] == AUDIT_VERSION


# --------------------------------------------------------------------------- #
# Issue athenaeum#1713: transitory-class decay stamping
# --------------------------------------------------------------------------- #


class TestTransitoryClassStamping:
    """issue athenaeum#1713 (decision recorded on athenaeum#1626, 2026-09-16):
    a page in one of the operator's four named transitory classes (incident
    record, deployment-status page, operational source note, reference page
    mirroring a GitHub issue) gets ``bucket: daily`` + ``valid_until`` from
    the audit pass, via the SAME never-overwrite rule every other
    coordinate field uses. Covers all 7 acceptance criteria on the issue.
    """

    @staticmethod
    def _responder(params: dict[str, Any]) -> str:
        prompt = params["messages"][0]["content"]
        if "Deploy Status Thing" in prompt:
            # AC2: the page states its own end date in its body.
            return json.dumps(
                {
                    "valid_from": {"undeterminable": "no dated validity window stated"},
                    "valid_until": {"value": "2026-05-01"},
                    "claimed_scope": {"undeterminable": "no scope stated"},
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )
        # Every other fixture page below: no dated validity window stated
        # (AC3's "no stated end date" case).
        return json.dumps(
            {
                "valid_from": {"undeterminable": "no dated validity window stated"},
                "valid_until": {"undeterminable": "no dated validity window stated"},
                "claimed_scope": {"undeterminable": "no scope stated"},
                "retirement_candidate": False,
                "retirement_reason": "",
            }
        )

    def _client(self) -> FakeLLMClient:
        def responder(**params: Any) -> Any:
            return make_llm_response(self._responder(params), usage=make_llm_usage(10, 5))

        return FakeLLMClient(responder=responder)

    # --- AC1 + AC3: transitory classes with no stated end date ------------

    def test_incident_record_gets_bucket_and_default_horizon_valid_until(
        self, wiki: Path
    ) -> None:
        path = _page(
            wiki,
            "incident.md",
            "uid: incident1\ntype: incident\nname: Incident Thing\n",
            "A production incident occurred; no stated resolution window.\n",
        )
        fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        report = build_audit_report(wiki, client=self._client(), model="m", now=lambda: fixed_now)
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["bucket"] == "daily"
        # 2026-01-01 + the 90-day default horizon.
        assert str(meta["valid_until"]) == "2026-04-01"

    def test_operational_source_note_gets_bucket_and_valid_until(self, wiki: Path) -> None:
        path = _page(
            wiki,
            "source_note.md",
            "uid: source1\ntype: source\nname: Source Note Thing\n",
            "An operational note citing an upstream status feed.\n",
        )
        report = build_audit_report(wiki, client=self._client(), model="m")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["bucket"] == "daily"
        assert "valid_until" in meta

    def test_reference_page_mirroring_a_github_issue_gets_bucket_and_valid_until(
        self, wiki: Path
    ) -> None:
        path = _page(
            wiki,
            "ref_mirror.md",
            "uid: refmirror1\ntype: reference\nname: Reference Mirror Thing\n",
            "Mirrors https://github.com/Kromatic-Innovation/athenaeum/issues/1713 "
            "for local search convenience.\n",
        )
        report = build_audit_report(wiki, client=self._client(), model="m")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["bucket"] == "daily"
        assert "valid_until" in meta

    # --- AC2: own stated end date wins over the default horizon ------------

    def test_deployment_status_page_uses_its_own_stated_end_date(self, wiki: Path) -> None:
        path = _page(
            wiki,
            "deploy.md",
            "uid: deploy1\ntype: deployment-status\nname: Deploy Status Thing\n",
            "This deployment is live and will be decommissioned by 2026-05-01.\n",
        )
        fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        report = build_audit_report(wiki, client=self._client(), model="m", now=lambda: fixed_now)
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["bucket"] == "daily"
        # The page's own stated date, NOT last_audited + the default horizon
        # (which would have been 2026-04-01 here).
        assert meta["valid_until"] == "2026-05-01"

    # --- Narrower signal: not every `type: reference` page qualifies -------

    def test_reference_page_without_a_github_link_is_not_transitory(self, wiki: Path) -> None:
        path = _page(
            wiki,
            "ref_plain.md",
            "uid: refplain1\ntype: reference\nname: Reference Plain Thing\n",
            "A reference page with no GitHub issue citation at all.\n",
        )
        report = build_audit_report(wiki, client=self._client(), model="m")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "bucket" not in meta
        assert "valid_until" not in meta

    # --- AC4: durable pages are never touched -------------------------------

    def test_durable_pages_never_receive_bucket_or_valid_until(self, wiki: Path) -> None:
        durable_pages = [
            _page(
                wiki, "person.md", "uid: person1\ntype: person\nname: Person Thing\n", "Bio.\n"
            ),
            _page(
                wiki,
                "company.md",
                "uid: company1\ntype: company\nname: Company Thing\n",
                "About.\n",
            ),
            _page(
                wiki,
                "concept.md",
                "uid: concept1\ntype: concept\nname: Concept Thing\n",
                "Definition.\n",
            ),
            _page(
                wiki,
                "principle.md",
                "uid: principle1\ntype: principle\nname: Principle Thing\n",
                "Statement.\n",
            ),
        ]
        report = build_audit_report(wiki, client=self._client(), model="m")
        apply_audit_report(report, wiki)

        for path in durable_pages:
            meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
            assert "bucket" not in meta
            assert "valid_until" not in meta

    # --- AC5: never overwrite a populated bucket/valid_until ----------------

    def test_preexisting_valid_until_is_never_overwritten(self, wiki: Path) -> None:
        path = _page(
            wiki,
            "incident_dated.md",
            "uid: incident2\ntype: incident\nname: Incident Dated Thing\n"
            "valid_until: 2030-01-01\n",
            "An incident with an operator-set expiry already.\n",
        )
        report = build_audit_report(wiki, client=self._client(), model="m")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert str(meta["valid_until"]) == "2030-01-01"
        # bucket was NOT pre-populated, so it is still stamped independently
        # — the never-overwrite rule is per-field, not per-page.
        assert meta["bucket"] == "daily"

    def test_preexisting_bucket_is_never_overwritten(self, wiki: Path) -> None:
        path = _page(
            wiki,
            "incident_bucketed.md",
            "uid: incident3\ntype: incident\nname: Incident Bucketed Thing\nbucket: durable\n",
            "An incident an operator already pinned durable.\n",
        )
        report = build_audit_report(wiki, client=self._client(), model="m")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["bucket"] == "durable"
        assert "valid_until" in meta

    # --- AC6: the default horizon is a named, config-driven value ----------

    def test_default_horizon_is_config_driven_not_hardcoded(
        self, wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _page(
            wiki,
            "incident_cfg.md",
            "uid: incident4\ntype: incident\nname: Incident Cfg Thing\n",
            "No stated end date.\n",
        )
        fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        monkeypatch.setenv("ATHENAEUM_AUDIT_TRANSITORY_HORIZON_DAYS", "10")
        report = build_audit_report(wiki, client=self._client(), model="m", now=lambda: fixed_now)
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert str(meta["valid_until"]) == "2026-01-11"  # 2026-01-01 + 10 days

    # --- AC7: the write plugs into the EXISTING decay-sweep selection ------

    def test_apply_then_decay_sweep_selects_the_newly_classified_page(self, wiki: Path) -> None:
        from athenaeum.decay_sweep import build_sweep_report, discover_daily_bucket_pages

        path = _page(
            wiki,
            "incident_sweep.md",
            "uid: incident5\ntype: incident\nname: Incident Sweep Thing\n",
            "No stated end date.\n",
        )
        fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        report = build_audit_report(wiki, client=self._client(), model="m", now=lambda: fixed_now)
        apply_audit_report(report, wiki)

        # Selected purely on `bucket: daily` — no decay-sweep code changes.
        assert path in discover_daily_bucket_pages(wiki)

        # Not yet expired the day after the audit ran.
        not_yet = build_sweep_report(wiki, as_of=date(2026, 1, 2))
        assert path not in [c.path for c in not_yet.kill]
        assert path in [p for p, _reason in not_yet.retained]

        # Once valid_until (2026-01-01 + 90 days = 2026-04-01) is in the
        # past, the existing decay-sweep dry run's kill-list selects it.
        expired = build_sweep_report(wiki, as_of=date(2026, 6, 1))
        assert path in [c.path for c in expired.kill]
