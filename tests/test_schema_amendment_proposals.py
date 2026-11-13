# SPDX-License-Identifier: Apache-2.0
"""Schema-amendment proposal ledger tests (issue athenaeum#2197).

Fixtures throughout are synthetic: a made-up ``person`` page type and a
made-up ``deal`` field group, mirroring the issue's own motivating CRM-import
example (``crm_deal_name``/``crm_deal_amount``) without naming any real
deployment's config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.decisions import list_pending_decisions, schema_amendment_proposal_to_decision
from athenaeum.models import EntityIndex, RawFile
from athenaeum.schema_amendment_proposals import (
    PROPOSAL_KIND,
    default_schema_amendment_proposals_ledger_path,
    default_schema_amendment_text,
    list_pending_schema_amendment_proposals,
    proposal_item_id,
    read_schema_amendment_proposals_ledger,
    record_field_group_misfits,
    record_schema_misfit,
)
from athenaeum.structured_upsert import tier0_structured_upsert

PERSON_TYPES = ["person", "company"]
PERSON_UID = "person-dana-reyes"

FIELD_GROUP_CONFIG: dict = {
    "librarian": {
        "structured_upsert": {
            "person": {
                "sections": [],
                "set_keys": ["crm_deal_name", "crm_deal_amount"],
            }
        },
        "field_groups": {
            "person": {
                "deal": ["crm_deal_name", "crm_deal_amount"],
            }
        },
    }
}

PERSON_PAGE = """\
---
uid: person-dana-reyes
type: person
name: Dana Reyes
access: personal
created: '2026-10-01'
updated: '2026-10-01'
crm_deal_name: Acme Renewal
crm_deal_amount: '5000'
---

# Dana Reyes
"""


def _second_deal_raw(name: str = "Beta Pilot", amount: str = "1200") -> str:
    return f"""\
---
uid: person-dana-reyes
type: person
name: Dana Reyes
access: public
created: '2026-11-08'
crm_deal_name: {name}
crm_deal_amount: '{amount}'
---

# Dana Reyes
"""


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    w = tmp_path / "wiki"
    w.mkdir()
    (w / f"{PERSON_UID}.md").write_text(PERSON_PAGE, encoding="utf-8")
    return w


def _raw(tmp_path: Path, content: str, name: str) -> RawFile:
    d = tmp_path / "raw" / "crm"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8")
    return RawFile(path=p, source="crm", timestamp="", uuid8="")


class TestProposalItemId:
    def test_deterministic_and_shape_only(self) -> None:
        """Same (page_type, field_group) -> same id, regardless of page/value."""
        a = proposal_item_id("person", "deal")
        b = proposal_item_id("person", "deal")
        assert a == b

    def test_different_shape_different_id(self) -> None:
        assert proposal_item_id("person", "deal") != proposal_item_id("company", "deal")
        assert proposal_item_id("person", "deal") != proposal_item_id("person", "address")


class TestRecordSchemaMisfit:
    def test_first_misfit_creates_one_proposal_with_count_one(self, tmp_path: Path) -> None:
        record = record_schema_misfit(
            tmp_path,
            page_type="person",
            field_group="deal",
            uid="person-a",
            proposed_amendment="add a deal entity type",
        )
        assert record["kind"] == PROPOSAL_KIND
        assert record["page_type"] == "person"
        assert record["field_group"] == "deal"
        assert record["affected_page_count"] == 1
        assert record["affected_uids"] == ["person-a"]

    def test_repeated_misfit_of_the_same_shape_adds_to_the_existing_proposal(
        self, tmp_path: Path
    ) -> None:
        """AC: repeated misfits of the same shape ADD to the existing proposal
        instead of creating new ones."""
        record_schema_misfit(
            tmp_path,
            page_type="person",
            field_group="deal",
            uid="person-a",
            proposed_amendment="add a deal entity type",
        )
        second = record_schema_misfit(
            tmp_path,
            page_type="person",
            field_group="deal",
            uid="person-b",
            proposed_amendment="add a deal entity type",
        )
        pending = list_pending_schema_amendment_proposals(tmp_path)
        assert len(pending) == 1, "one shape -> one proposal, never two"
        assert pending[0]["id"] == second["id"]
        assert pending[0]["affected_page_count"] == 2
        assert sorted(pending[0]["affected_uids"]) == ["person-a", "person-b"]

    def test_same_page_recorded_twice_does_not_inflate_the_count(self, tmp_path: Path) -> None:
        """Idempotent per (shape, uid): the SAME page misfiring twice (e.g. two
        colliding fields in the same group on one raw) counts as one page."""
        record_schema_misfit(
            tmp_path,
            page_type="person",
            field_group="deal",
            uid="person-a",
            proposed_amendment="add a deal entity type",
        )
        record_schema_misfit(
            tmp_path,
            page_type="person",
            field_group="deal",
            uid="person-a",
            proposed_amendment="add a deal entity type",
        )
        pending = list_pending_schema_amendment_proposals(tmp_path)
        assert len(pending) == 1
        assert pending[0]["affected_page_count"] == 1

    def test_different_shapes_never_collapse_into_one_proposal(self, tmp_path: Path) -> None:
        record_schema_misfit(
            tmp_path,
            page_type="person",
            field_group="deal",
            uid="person-a",
            proposed_amendment="add a deal entity type",
        )
        record_schema_misfit(
            tmp_path,
            page_type="company",
            field_group="deal",
            uid="company-a",
            proposed_amendment="add a deal entity type",
        )
        pending = list_pending_schema_amendment_proposals(tmp_path)
        assert len(pending) == 2
        shapes = {(r["page_type"], r["field_group"]) for r in pending}
        assert shapes == {("person", "deal"), ("company", "deal")}

    def test_ledger_is_append_only_jsonl(self, tmp_path: Path) -> None:
        record_schema_misfit(
            tmp_path,
            page_type="person",
            field_group="deal",
            uid="person-a",
            proposed_amendment="add a deal entity type",
        )
        record_schema_misfit(
            tmp_path,
            page_type="person",
            field_group="deal",
            uid="person-b",
            proposed_amendment="add a deal entity type",
        )
        ledger_path = default_schema_amendment_proposals_ledger_path(tmp_path)
        assert ledger_path.exists()
        lines = [ln for ln in ledger_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 2, "each occurrence appends a line; nothing rewritten in place"
        records = read_schema_amendment_proposals_ledger(tmp_path)
        assert len(records) == 2


class TestRecordFieldGroupMisfits:
    def test_two_fields_of_the_same_group_on_one_raw_record_the_page_once(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / f"{PERSON_UID}.md").write_text(PERSON_PAGE, encoding="utf-8")
        raw = _raw(tmp_path, _second_deal_raw(), "deal2.md")
        out = tier0_structured_upsert(
            raw, EntityIndex(wiki), wiki, PERSON_TYPES, config=FIELD_GROUP_CONFIG
        )
        assert out is not None and out.status == "refused"
        assert len(out.field_group_misfits) == 2  # both crm_deal_* fields collided

        record_field_group_misfits(out.field_group_misfits, wiki_root=wiki, uid=out.uid)

        pending = list_pending_schema_amendment_proposals(wiki)
        assert len(pending) == 1
        assert pending[0]["affected_page_count"] == 1


class TestListPendingDecisions:
    def test_proposal_appears_in_list_pending_decisions(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        record_schema_misfit(
            wiki_root,
            page_type="person",
            field_group="deal",
            uid="person-a",
            proposed_amendment=default_schema_amendment_text("person", "deal"),
        )
        items = list_pending_decisions(wiki_root)
        schema_items = [i for i in items if i["type"] == "schema-amendment-proposal"]
        assert len(schema_items) == 1
        assert schema_items[0]["payload"]["page_type"] == "person"
        assert schema_items[0]["payload"]["field_group"] == "deal"
        assert schema_items[0]["payload"]["affected_page_count"] == 1

    def test_decision_conversion_names_page_type_group_count_and_amendment(self) -> None:
        rec = {
            "id": "abc123",
            "created_at": "2026-11-13T00:00:00Z",
            "page_type": "person",
            "field_group": "deal",
            "affected_page_count": 3,
            "affected_uids": ["a", "b", "c"],
            "proposed_amendment": "add a deal entity type",
        }
        decision = schema_amendment_proposal_to_decision(rec)
        assert decision["type"] == "schema-amendment-proposal"
        assert "person" in decision["summary"]
        assert "deal" in decision["summary"]
        assert "3" in decision["summary"]
        assert "add a deal entity type" in decision["summary"]
        assert decision["payload"]["affected_page_count"] == 3


class TestHumanAdoptionOnly:
    def test_an_unresolved_proposal_changes_no_configuration(self, tmp_path: Path) -> None:
        """AC: nothing changes the deployment's schema configuration
        automatically -- a proposal is adopted only by a human resolution."""
        config_before = dict(FIELD_GROUP_CONFIG)
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()

        for i in range(3):
            record_schema_misfit(
                wiki_root,
                page_type="person",
                field_group="deal",
                uid=f"person-{i}",
                proposed_amendment=default_schema_amendment_text("person", "deal"),
            )

        pending = list_pending_schema_amendment_proposals(wiki_root)
        assert pending[0]["affected_page_count"] == 3

        # The config dict passed around by every caller is untouched -- this
        # module never reads OR writes `athenaeum.yaml`/the config dict at all.
        assert FIELD_GROUP_CONFIG == config_before
        assert "field_groups" in FIELD_GROUP_CONFIG["librarian"]
        assert FIELD_GROUP_CONFIG["librarian"]["field_groups"] == {
            "person": {"deal": ["crm_deal_name", "crm_deal_amount"]}
        }
