# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1667 — constrain date fills, redefine retirement duds,
per-type sampling floor, auto-memory selection.

Kept as a separate module from ``tests/test_audit.py`` (the athenaeum#1624
foundation suite) so each issue's acceptance criteria map to one file. All
fixtures live under ``tmp_path``; nothing here reads or writes a live
knowledge store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum.audit import (
    AUDIT_SYSTEM,
    AUDIT_VERSION,
    apply_audit_report,
    build_audit_report,
    identity_key,
    parse_audit_response,
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


def _client(responder) -> FakeLLMClient:
    def wrapped(**params: Any) -> Any:
        return make_llm_response(responder(params), usage=make_llm_usage(10, 5))

    return FakeLLMClient(responder=wrapped)


# --- AUDIT_VERSION bump ------------------------------------------------


def test_audit_version_is_v2() -> None:
    assert AUDIT_VERSION == "audit-v2"


# --- Prompt names the excluded date classes -----------------------------


class TestPromptNamesExcludedDateClasses:
    @pytest.mark.parametrize(
        "phrase",
        [
            "connect",
            "first-contact",
            "last-contact",
            "last-email",
            "meeting",
            "updated-timestamp",
            "ingestion",
        ],
    )
    def test_excluded_class_named_in_prompt(self, phrase: str) -> None:
        assert phrase in AUDIT_SYSTEM


# --- Wrong-date class 1: connect date ------------------------------------


def _connect_date_page(root: Path) -> Path:
    return _page(
        root,
        "connect.md",
        "uid: connect1\ntype: person\nname: Connect Person\n" "linkedin_connected_on: 2010-12-17\n",
        "Joined the company as an engineer in 2016.[^1]\n\n"
        "[^1]: [[src-apollo|Apollo]] 2016-01-01\n",
    )


class TestExcludedConnectDate:
    def test_connect_date_is_refused(self, wiki: Path) -> None:
        path = _connect_date_page(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "valid_from": {"value": "2010-12-17"},
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "valid_from" not in meta
        assert "linkedin_connected_on" in meta["audit_findings"]["valid_from"]
        assert "contact" in meta["audit_findings"]["valid_from"]

    def test_stated_role_start_on_same_page_is_filled(self, wiki: Path) -> None:
        path = _connect_date_page(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "valid_from": {"value": "2016-01-01"},
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["valid_from"] == "2016-01-01"


# --- Wrong-date class 2: note / updated: date ----------------------------


class TestExcludedUpdatedDate:
    def test_updated_date_is_refused(self, wiki: Path) -> None:
        path = _page(
            wiki,
            "noted.md",
            "uid: noted1\ntype: concept\nname: Noted Thing\nupdated: 2026-09-05\n",
            "A concept page with no stated validity window.\n",
        )

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "valid_until": {"value": "2026-09-05"},
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "valid_until" not in meta
        assert "updated" in meta["audit_findings"]["valid_until"]


# --- Wrong-date class 3: CRM contact dates --------------------------------


class TestExcludedCrmContactDates:
    def test_crm_first_contact_and_last_email_are_both_refused(self, wiki: Path) -> None:
        path = _page(
            wiki,
            "crm.md",
            "uid: crm1\ntype: company\nname: CRM Co\n"
            "crm_first_contact: 2021-08-30\ncrm_last_email: 2024-12-23\n",
            "A company with no stated engagement window.\n",
        )

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "valid_from": {"value": "2021-08-30"},
                    "valid_until": {"value": "2024-12-23"},
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "valid_from" not in meta
        assert "valid_until" not in meta
        assert "crm_first_contact" in meta["audit_findings"]["valid_from"]
        assert "crm_last_email" in meta["audit_findings"]["valid_until"]


# --- Fallback switch: audit.date_fill = off -------------------------------


class TestDateFillFallbackSwitch:
    def test_off_mode_fills_no_dates_but_still_fills_claimed_scope(self, wiki: Path) -> None:
        path = _page(
            wiki,
            "offmode.md",
            "uid: offmode1\ntype: concept\nname: Off Mode Thing\n",
            "This ran from 2026-01-01 to 2026-06-30, per the signed SOW.[^1]\n\n"
            "[^1]: [[src-sow|SOW]] 2026-01-01\n",
        )

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "valid_from": {"value": "2026-01-01"},
                    "valid_until": {"value": "2026-06-30"},
                    "claimed_scope": {"value": "team:acme"},
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )

        report = build_audit_report(
            wiki,
            client=_client(responder),
            model="m",
            config={"audit": {"date_fill": "off"}},
        )
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "valid_from" not in meta
        assert "valid_until" not in meta
        assert meta["claimed_scope"] == "team:acme"
        findings = meta["audit_findings"]
        assert (
            findings["valid_from"] == "undeterminable: date filling disabled (audit.date_fill=off)"
        )
        assert (
            findings["valid_until"] == "undeterminable: date filling disabled (audit.date_fill=off)"
        )

    def test_constrained_is_the_default_when_key_absent(self, wiki: Path) -> None:
        path = _page(
            wiki,
            "defaultmode.md",
            "uid: defaultmode1\ntype: concept\nname: Default Mode Thing\n",
            "This ran from 2026-01-01 to 2026-06-30, per the signed SOW.[^1]\n\n"
            "[^1]: [[src-sow|SOW]] 2026-01-01\n",
        )

        def responder(params: dict[str, Any]) -> str:
            assert "valid_from" in params["messages"][0]["content"]
            return json.dumps(
                {
                    "valid_from": {"value": "2026-01-01"},
                    "valid_until": {"value": "2026-06-30"},
                    "retirement_candidate": False,
                    "retirement_reason": "",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m", config={})
        apply_audit_report(report, wiki)

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["valid_from"] == "2026-01-01"
        assert meta["valid_until"] == "2026-06-30"


# --- Retirement redefinition: no-claims-or-duplicate, sources not light --


class TestPromptRetirementSection:
    def test_prompt_states_no_claims_or_duplicate_only(self) -> None:
        assert "no claim at all" in AUDIT_SYSTEM
        assert "duplicates another page" in AUDIT_SYSTEM

    def test_prompt_says_restating_is_not_a_criterion(self) -> None:
        assert "NOT, on its own, a reason" in AUDIT_SYSTEM

    def test_prompt_says_light_source_page_never_flagged(self) -> None:
        assert "light BY DESIGN" in AUDIT_SYSTEM
        assert "Never" in AUDIT_SYSTEM and "flag such a page" in AUDIT_SYSTEM


def _principle_page(root: Path) -> Path:
    return _page(
        root,
        "principle.md",
        "uid: principle1\ntype: concept\nname: A Principle\n",
        "## Statement\nDeploys happen only on green CI.\n\n"
        "## Evidence\nThree incidents traced to red-CI deploys.[^1]\n\n"
        "## Exceptions\nHotfixes may bypass with two-person sign-off.\n\n"
        "[^1]: [[src-incidents|Incident log]]\n",
    )


def _light_source_page_with_summary(root: Path) -> Path:
    return _page(
        root,
        "source-good.md",
        "uid: source1\ntype: source\nname: Board Source\nvalid_from: 2026-01-01\n",
        "Summary: the board records engagement decisions and staffing "
        "changes for the account, reviewed monthly. Valid from the board's "
        "creation date; superseded pages are archived, not deleted.\n",
    )


def _light_source_page_without_summary(root: Path) -> Path:
    return _page(
        root,
        "source-bare.md",
        "uid: source2\ntype: source\nname: Bare Source\n",
        "# Bare Source\n",
    )


def _stub_page(root: Path) -> Path:
    return _page(
        root,
        "stub.md",
        "uid: stub1\ntype: concept\nname: Stub Thing\n",
        "# Stub Thing\n",
    )


class TestRetirementFixtures:
    def test_principle_page_is_not_flagged(self, wiki: Path) -> None:
        _principle_page(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps({"retirement_candidate": False, "retirement_reason": ""})

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is False

    def test_light_source_page_with_summary_is_not_flagged(self, wiki: Path) -> None:
        _light_source_page_with_summary(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps({"retirement_candidate": False, "retirement_reason": ""})

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is False

    def test_light_source_page_without_summary_is_not_flagged_but_gets_finding(
        self, wiki: Path
    ) -> None:
        _light_source_page_without_summary(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "retirement_candidate": False,
                    "retirement_reason": "",
                    "source_summary_missing": "no summary of the source is given",
                }
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is False
        assert v.audit_findings["source_summary_missing"]

    def test_stub_page_is_flagged_with_a_reason(self, wiki: Path) -> None:
        _stub_page(wiki)

        def responder(params: dict[str, Any]) -> str:
            return json.dumps(
                {"retirement_candidate": True, "retirement_reason": "no claims, name heading only"}
            )

        report = build_audit_report(wiki, client=_client(responder), model="m")
        [v] = report.verdicts
        assert v.retirement_candidate is True
        assert v.retirement_reason


# --- Duplicate detection: deterministic, in code --------------------------


class TestDuplicateDetection:
    def test_two_identical_pages_are_both_flagged_as_duplicates(self, wiki: Path) -> None:
        _page(
            wiki,
            "dupe-a.md",
            "uid: dupea\ntype: concept\nname: Dupe A\n",
            "This is the exact same content, word for word, on two pages.\n",
        )
        _page(
            wiki,
            "dupe-b.md",
            "uid: dupeb\ntype: concept\nname: Dupe B\n",
            "This is the exact same content, word for word, on two pages.\n",
        )
        _page(
            wiki,
            "distinct.md",
            "uid: distinct1\ntype: concept\nname: Distinct\n",
            "This is the exact same content, word for word, on two pages, "
            "except this one differs by a trailing clause.\n",
        )

        def responder(params: dict[str, Any]) -> str:
            return json.dumps({"retirement_candidate": False, "retirement_reason": ""})

        report = build_audit_report(wiki, client=_client(responder), model="m")
        by_uid = {v.uid: v for v in report.verdicts}

        assert by_uid["dupea"].retirement_candidate is True
        assert by_uid["dupea"].retirement_reason == "duplicate of dupeb"
        assert by_uid["dupeb"].retirement_candidate is True
        assert by_uid["dupeb"].retirement_reason == "duplicate of dupea"
        assert by_uid["distinct1"].retirement_candidate is False


# --- Per-type sampling floor -----------------------------------------------


class TestPerTypeSamplingFloor:
    def test_small_types_get_at_least_one_page(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        for i in range(40):
            _page(wiki_root, f"person{i}.md", f"uid: person{i}\ntype: person\nname: P{i}\n", "B.\n")
        for i in range(3):
            _page(wiki_root, f"source{i}.md", f"uid: source{i}\ntype: source\nname: S{i}\n", "B.\n")
        _page(wiki_root, "pref0.md", "uid: pref0\ntype: preference\nname: Pref\n", "B.\n")

        selected = select_audit_pages(wiki_root, sample=10, seed=3)
        types = [m["type"] for _p, m, _b in selected]
        assert len(selected) == 10
        assert types.count("source") >= 1
        assert types.count("preference") >= 1

    def test_degenerate_case_fewer_slots_than_types(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        sizes = {"a": 5, "b": 4, "c": 3, "d": 2, "e": 1}
        for type_name, count in sizes.items():
            for i in range(count):
                _page(
                    wiki_root,
                    f"{type_name}{i}.md",
                    f"uid: {type_name}{i}\ntype: {type_name}\nname: N{i}\n",
                    "B.\n",
                )

        selected = select_audit_pages(wiki_root, sample=3, seed=9)
        types = {m["type"] for _p, m, _b in selected}
        assert len(selected) == 3
        assert types == {"a", "b", "c"}  # 3 largest, ties by name

        again = select_audit_pages(wiki_root, sample=3, seed=9)
        assert [str(p) for p, _m, _b in selected] == [str(p) for p, _m, _b in again]


# --- Auto-memory selection --------------------------------------------------


def _auto_memory_page(
    root: Path, name: str, *, retired: bool = False, cluster_id: str = "c1"
) -> Path:
    retired_line = "retired: true\n" if retired else ""
    return _page(
        root,
        name,
        f"type: auto-memory\nname: Cluster {name}\ncluster_id: {cluster_id}\n{retired_line}",
        "Some clustered memory content.\n",
    )


class TestAutoMemorySelection:
    def test_uid_less_auto_memory_page_is_selected_and_reported_by_path(self, wiki: Path) -> None:
        path = _auto_memory_page(wiki, "auto-abc123-20260101-1.md")

        def responder(params: dict[str, Any]) -> str:
            return json.dumps({"retirement_candidate": False, "retirement_reason": ""})

        report = build_audit_report(wiki, client=_client(responder), model="m")
        assert report.scanned == 1
        [v] = report.verdicts
        assert v.uid == "auto-abc123-20260101-1.md"
        assert v.path == path

    def test_retired_auto_memory_page_is_not_selected(self, wiki: Path) -> None:
        _auto_memory_page(wiki, "auto-retired-20260101-1.md", retired=True)
        selected = select_audit_pages(wiki)
        assert selected == []

    def test_wrong_type_glob_match_is_not_selected(self, wiki: Path) -> None:
        _page(
            wiki,
            "auto-company-20260101-1.md",
            "type: company\nname: Not Auto Memory\n",
            "A company page whose filename happens to match the glob.\n",
        )
        selected = select_audit_pages(wiki)
        assert selected == []

    def test_uids_selector_matches_path_identity(self, wiki: Path) -> None:
        _auto_memory_page(wiki, "auto-xyz-20260101-1.md")
        selected = select_audit_pages(wiki, uids=["auto-xyz-20260101-1.md"])
        assert len(selected) == 1


# --- Apply identity re-check for uid-less pages ----------------------------


class TestApplyIdentityRecheckForAutoMemory:
    def test_unchanged_cluster_id_is_written(self, wiki: Path) -> None:
        path = _auto_memory_page(wiki, "auto-stable-20260101-1.md")

        def responder(params: dict[str, Any]) -> str:
            return json.dumps({"retirement_candidate": False, "retirement_reason": ""})

        report = build_audit_report(wiki, client=_client(responder), model="m")
        changed = apply_audit_report(report, wiki)
        assert changed == 1

        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["audit_version"] == AUDIT_VERSION
        assert "last_audited" in meta

    def test_cluster_id_change_between_scan_and_apply_blocks_the_write(self, wiki: Path) -> None:
        path = _auto_memory_page(wiki, "auto-shifting-20260101-1.md", cluster_id="c1")

        def responder(params: dict[str, Any]) -> str:
            return json.dumps({"retirement_candidate": False, "retirement_reason": ""})

        report = build_audit_report(wiki, client=_client(responder), model="m")

        # Simulate a recluster between scan and apply.
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        meta["cluster_id"] = "c2-different"
        from athenaeum.models import render_frontmatter

        path.write_text(render_frontmatter(meta) + "\n" + body, encoding="utf-8")

        changed = apply_audit_report(report, wiki)
        assert changed == 0

        meta_after, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "last_audited" not in meta_after


# --- identity_key helper ----------------------------------------------------


class TestIdentityKey:
    def test_uid_page_uses_its_uid(self, wiki: Path) -> None:
        path = _page(wiki, "u.md", "uid: u1\ntype: concept\nname: U\n", "B.\n")
        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert identity_key(path, meta, wiki) == "u1"

    def test_uid_less_page_uses_wiki_relative_path(self, wiki: Path) -> None:
        path = _auto_memory_page(wiki, "auto-relpath-20260101-1.md")
        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert identity_key(path, meta, wiki) == "auto-relpath-20260101-1.md"


# --- parse_audit_response: meta-gated exclusion, direct unit coverage -----


class TestParseAuditResponseExclusionGuard:
    def test_excluded_value_becomes_a_finding_not_a_fill(self) -> None:
        fills, findings, _rc, _reason = parse_audit_response(
            json.dumps({"valid_from": {"value": "2010-12-17"}}),
            ["valid_from"],
            meta={"linkedin_connected_on": "2010-12-17"},
        )
        assert fills == {}
        assert "linkedin_connected_on" in findings["valid_from"]

    def test_without_meta_the_guard_is_skipped(self) -> None:
        fills, _findings, _rc, _reason = parse_audit_response(
            json.dumps({"valid_from": {"value": "2010-12-17"}}), ["valid_from"]
        )
        assert fills == {"valid_from": "2010-12-17"}
