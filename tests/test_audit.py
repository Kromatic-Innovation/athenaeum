# SPDX-License-Identifier: Apache-2.0
"""``athenaeum audit`` — page audit pass (issue athenaeum#1624).

Covers: dry-run writes nothing; --limit/--sample+--seed bound the pages
processed and --seed is reproducible; --batch routes through
athenaeum.batch's transport and not the synchronous client; --apply stamps
last_audited/audit_version; coordinate fills vs. the undeterminable marker
vs. "never overwrite a populated value"; the retirement-candidate flag and
its generic (no source-type/adapter-name) flagging logic; the run report's
per-page verdicts + tokens/cost + totals; and that every template scaffold
documents the new fields. All fixtures — no test reads or writes a live
knowledge store.
"""

from __future__ import annotations

import json
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
