# SPDX-License-Identifier: Apache-2.0
"""Shared content gets one page of its own (issue athenaeum#2589).

The grammar under test here -- the `shared:<group>` disposition and the
`shared-subject:<group>:<uid>` page action -- is deliberately NOT
model-facing (mirrors `ALIAS_MOVE_VERB`/`parse_alias_move`'s own precedent
in `athenaeum.page_recompile_plan`): the packaged `page_recompile_plan.md`
prompt teaches neither, so a model-authored plan can never carry one. Every
plan here is therefore constructed directly as a `RecompilePlan` dataclass,
the same "operator-authored or later-slice plan record" shape
`tests/test_page_recompile_cross_page.py::TestAC10ConservationAndTheLedger`
already uses for its own hand-built plans, rather than through
`validate_plan`'s model-payload parser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from athenaeum.config import resolve_shared_content_page_type
from athenaeum.mcp_server import _LinkNameIndex, _render_relation_lines
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.page_recompile import segment_page
from athenaeum.page_recompile_apply import write_apply
from athenaeum.page_recompile_plan import (
    SCOPE_CROSS_PAGE,
    PlanGroup,
    RecompilePlan,
    append_plan_record,
    page_content_hash,
)
from athenaeum.page_recompile_queue import VERDICT_APPROVE, queue_plan_for_approval
from athenaeum.page_relations import read_relations

_LEAD = "Acme builds widgets and has operated for over a decade.[^src-1]"
_SHARED = (
    "Rule A and Rule B are sequential, not competing: Rule A applies first, "
    "and Rule B only once Rule A's conditions are met.[^src-2]"
)
_BODY = (
    _LEAD
    + "\n\n## Relationship\n\n"
    + _SHARED
    + "\n\n[^src-1]: **Source:** doc-a\n[^src-2]: **Source:** doc-b\n"
)
_SUMMARY = "Acme is a widget maker with a long operating history.[^src-1]"

#: Keeps the AC3 parent-prose floor out of the way, same pin
#: `test_page_recompile_cross_page.py` uses.
_MIN_PARENT_CHARS: dict[str, Any] = {"min_parent_chars": 1}


def _config(*, shared_type: str | None) -> dict[str, Any]:
    librarian: dict[str, Any] = {"page_recompile": _MIN_PARENT_CHARS}
    if shared_type is not None:
        librarian["shared_content_page_type"] = shared_type
    return {"librarian": librarian}


def _unit_ids() -> list[str]:
    return [u.unit_id for u in segment_page(_BODY).units]


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    return tmp_path / "knowledge" / "wiki"


def _write_page(wiki: Path, *, uid: str, name: str, body: str, **extra: Any) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"uid": uid, "type": "reference", "name": name, "access": "internal"}
    meta.update(extra)
    path = wiki / f"{uid}.md"
    path.write_text(render_frontmatter(meta) + "\n" + body, encoding="utf-8")
    return path


def _seed(wiki: Path) -> tuple[Path, Path, Path]:
    """The recompiled SOURCE page and its two existing SUBJECT pages."""
    source = _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
    subject_a = _write_page(
        wiki, uid="subjecta1", name="Rule A", body="Rule A is a pricing rule.\n"
    )
    subject_b = _write_page(
        wiki, uid="subjectb1", name="Rule B", body="Rule B is a discount rule.\n"
    )
    return source, subject_a, subject_b


def _shared_plan(
    wiki: Path, *, subject_uids: list[str], group_key: str = "g1"
) -> tuple[RecompilePlan, str]:
    ids = _unit_ids()
    plan = RecompilePlan(
        uid="page0001",
        content_hash=page_content_hash(_BODY),
        dispositions=(
            (ids[0], "parent"),
            (ids[1], f"shared:{group_key}"),
        ),
        groups=(
            PlanGroup(
                key=group_key,
                name="Rule A / Rule B Relationship",
                description="How Rule A and Rule B relate to each other.",
                confidence=0.9,
            ),
        ),
        merges=(),
        page_actions=tuple(f"shared-subject:{group_key}:{uid}" for uid in subject_uids),
        summary=_SUMMARY,
        scope=SCOPE_CROSS_PAGE,
        issues=(),
        verified=None,
    )
    plan_id = append_plan_record(plan, wiki)
    queue_plan_for_approval(wiki, plan_id=plan_id, plan=plan, outline={})
    return plan, plan_id


def _read_meta(path: Path) -> dict[str, Any]:
    meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert isinstance(meta, dict)
    return meta


# ---------------------------------------------------------------------------
# AC2: the config resolver itself. Mechanism, not policy -- nothing
# declared leaves the value empty, which every caller treats as "off".
# ---------------------------------------------------------------------------


class TestResolveSharedContentPageType:
    def test_nothing_declared_is_empty(self) -> None:
        assert resolve_shared_content_page_type(None) == ""
        assert resolve_shared_content_page_type({}) == ""
        assert resolve_shared_content_page_type({"librarian": {}}) == ""

    def test_a_declared_type_is_read(self) -> None:
        config = {"librarian": {"shared_content_page_type": "relationship"}}
        assert resolve_shared_content_page_type(config) == "relationship"

    def test_whitespace_is_stripped(self) -> None:
        config = {"librarian": {"shared_content_page_type": "  relationship  "}}
        assert resolve_shared_content_page_type(config) == "relationship"

    def test_malformed_values_fail_to_off(self) -> None:
        assert resolve_shared_content_page_type({"librarian": "not-a-dict"}) == ""
        bad_type_config = {"librarian": {"shared_content_page_type": 7}}
        assert resolve_shared_content_page_type(bad_type_config) == ""
        assert resolve_shared_content_page_type("not-a-dict") == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC1: the mechanism -- content shared by two or more distinct subjects
# routes to one new page, which records those subjects, and each subject
# page gets a link to it.
# ---------------------------------------------------------------------------


class TestAC1SharedPageRouting:
    def test_one_new_page_is_created_and_both_subjects_link_to_it(self, wiki: Path) -> None:
        _source, subject_a, subject_b = _seed(wiki)
        _plan, plan_id = _shared_plan(wiki, subject_uids=["subjecta1", "subjectb1"])

        outcome = write_apply(
            wiki, plan_id=plan_id, config=_config(shared_type="reference"), verdict=VERDICT_APPROVE
        )
        assert outcome.ok, (outcome.reason, outcome.issues)
        assert not outcome.post_write_issues, outcome.post_write_issues
        assert len(outcome.created_uids) == 1
        shared_uid = outcome.created_uids[0]
        assert shared_uid not in {"page0001", "subjecta1", "subjectb1"}

        shared_meta = _read_meta(
            next(p for p in wiki.glob("*.md") if _read_meta(p).get("uid") == shared_uid)
        )
        assert shared_meta["type"] == "reference"
        assert shared_meta["name"] == "Rule A / Rule B Relationship"

        meta_a = _read_meta(subject_a)
        meta_b = _read_meta(subject_b)
        rel_a = read_relations(meta_a)
        rel_b = read_relations(meta_b)
        assert rel_a.parent is not None and rel_a.parent.uid == shared_uid
        assert rel_b.parent is not None and rel_b.parent.uid == shared_uid

        # The shared page itself records BOTH subjects, via the parent/child
        # relation issue athenaeum#2535 shipped, via="fold".
        shared_related = shared_meta.get("related") or []
        shared_children = {row["uid"]: row["via"] for row in shared_related}
        assert shared_children == {"subjecta1": "fold", "subjectb1": "fold"}

    def test_the_shared_text_is_not_copied_onto_either_subject_page(self, wiki: Path) -> None:
        _source, subject_a, subject_b = _seed(wiki)
        _plan, plan_id = _shared_plan(wiki, subject_uids=["subjecta1", "subjectb1"])

        outcome = write_apply(
            wiki, plan_id=plan_id, config=_config(shared_type="reference"), verdict=VERDICT_APPROVE
        )
        assert outcome.ok, (outcome.reason, outcome.issues)

        _meta_a, body_a = parse_frontmatter(subject_a.read_text(encoding="utf-8"))
        _meta_b, body_b = parse_frontmatter(subject_b.read_text(encoding="utf-8"))
        assert "sequential, not competing" not in body_a
        assert "sequential, not competing" not in body_b

        shared_uid = outcome.created_uids[0]
        shared_path = next(p for p in wiki.glob("*.md") if _read_meta(p).get("uid") == shared_uid)
        _shared_meta, shared_body = parse_frontmatter(shared_path.read_text(encoding="utf-8"))
        assert "sequential, not competing" in shared_body


# ---------------------------------------------------------------------------
# AC2: mechanism, not policy -- with nothing declared, behaviour is
# unchanged. The SAME plan that mints a shared page above instead leaves
# its content on the recompiled page when the knob is unset.
# ---------------------------------------------------------------------------


class TestAC2DefaultOffLeavesBehaviourUnchanged:
    def test_nothing_declared_downgrades_to_an_ordinary_child_group(self, wiki: Path) -> None:
        """With the knob unset, `shared:<group>` downgrades to `child:<group>`
        -- the SAME "re-planned without it" fallback `merge:<group>` already
        gets when `librarian.page_recompile.merge_near_duplicates` is off.
        That means a normal split sub-page of the SOURCE page is created
        (`child:<group>` has always meant that, with or without this
        issue), but neither subject page is touched at all: no relation is
        set on subject A or subject B, and neither carries the shared
        text. The thing AC2 actually guards against -- a plan silently
        linking the two UNRELATED subject pages together, or minting a
        page typed by a config value nobody set -- never happens."""
        _source, subject_a, subject_b = _seed(wiki)
        _plan, plan_id = _shared_plan(wiki, subject_uids=["subjecta1", "subjectb1"])

        outcome = write_apply(
            wiki, plan_id=plan_id, config=_config(shared_type=None), verdict=VERDICT_APPROVE
        )
        assert outcome.ok, (outcome.reason, outcome.issues)
        assert len(outcome.created_uids) == 1
        split_uid = outcome.created_uids[0]

        split_path = next(p for p in wiki.glob("*.md") if _read_meta(p).get("uid") == split_uid)
        split_meta = _read_meta(split_path)
        _split_meta, split_body = parse_frontmatter(split_path.read_text(encoding="utf-8"))
        assert "sequential, not competing" in split_body
        # A plain split child of the SOURCE page, via="split" -- not a
        # shared page, and not linked to either subject.
        assert read_relations(split_meta).parent is not None
        assert read_relations(split_meta).parent.uid == "page0001"

        meta_a = _read_meta(subject_a)
        meta_b = _read_meta(subject_b)
        assert read_relations(meta_a).parent is None
        assert read_relations(meta_b).parent is None
        _meta_a, body_a = parse_frontmatter(subject_a.read_text(encoding="utf-8"))
        _meta_b, body_b = parse_frontmatter(subject_b.read_text(encoding="utf-8"))
        assert "sequential, not competing" not in body_a
        assert "sequential, not competing" not in body_b


# ---------------------------------------------------------------------------
# AC3: recall and listings show the link -- through the EXISTING surface
# issue athenaeum#2538 built for the parent/child relation generally, not a
# new rendering path.
# ---------------------------------------------------------------------------


class TestAC3RecallShowsTheLink:
    def test_the_subject_hit_renders_a_parent_line_naming_the_shared_page(
        self, wiki: Path
    ) -> None:
        _source, subject_a, _subject_b = _seed(wiki)
        _plan, plan_id = _shared_plan(wiki, subject_uids=["subjecta1", "subjectb1"])
        outcome = write_apply(
            wiki, plan_id=plan_id, config=_config(shared_type="reference"), verdict=VERDICT_APPROVE
        )
        assert outcome.ok, (outcome.reason, outcome.issues)
        shared_uid = outcome.created_uids[0]

        fm_a = _read_meta(subject_a)
        lines, rendered_uids = _render_relation_lines(
            fm_a,
            names=_LinkNameIndex(wiki),
            caller_audience=None,
            enforce_recallable=False,
            config=None,
            sub_pages_cap=10,
        )
        assert f"**Parent:** {shared_uid}" in lines
        assert shared_uid in rendered_uids

    def test_the_shared_page_hit_renders_a_sub_pages_line_naming_both_subjects(
        self, wiki: Path
    ) -> None:
        _source, _subject_a, _subject_b = _seed(wiki)
        _plan, plan_id = _shared_plan(wiki, subject_uids=["subjecta1", "subjectb1"])
        outcome = write_apply(
            wiki, plan_id=plan_id, config=_config(shared_type="reference"), verdict=VERDICT_APPROVE
        )
        assert outcome.ok, (outcome.reason, outcome.issues)
        shared_uid = outcome.created_uids[0]
        shared_path = next(p for p in wiki.glob("*.md") if _read_meta(p).get("uid") == shared_uid)

        fm_shared = _read_meta(shared_path)
        lines, rendered_uids = _render_relation_lines(
            fm_shared,
            names=_LinkNameIndex(wiki),
            caller_audience=None,
            enforce_recallable=False,
            config=None,
            sub_pages_cap=10,
        )
        assert "**Sub-pages:**" in lines
        assert "subjecta1" in rendered_uids
        assert "subjectb1" in rendered_uids


# ---------------------------------------------------------------------------
# AC4: the required counter-example -- content concerning only ONE subject
# stays on that subject's page, never a shared page of its own.
# ---------------------------------------------------------------------------


class TestAC4SingleSubjectCounterExample:
    def test_a_lone_named_subject_refuses_rather_than_minting_a_page(self, wiki: Path) -> None:
        _source, _subject_a, _subject_b = _seed(wiki)
        _plan, plan_id = _shared_plan(wiki, subject_uids=["subjecta1"])

        outcome = write_apply(
            wiki, plan_id=plan_id, config=_config(shared_type="reference"), verdict=VERDICT_APPROVE
        )
        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["shared-group-too-few-subjects"]

    def test_ordinary_single_subject_content_still_just_moves(self, wiki: Path) -> None:
        """The existing, unaffected path: content naming one subject uses
        `move:<uid>` (never `shared:<group>`) and lands on that subject's
        own page exactly as it did before this issue -- the shared-content
        mechanism never enters the picture."""
        _source, subject_a, _subject_b = _seed(wiki)
        ids = _unit_ids()
        plan = RecompilePlan(
            uid="page0001",
            content_hash=page_content_hash(_BODY),
            dispositions=((ids[0], "parent"), (ids[1], "move:subjecta1")),
            groups=(),
            merges=(),
            page_actions=(),
            summary=_SUMMARY,
            scope=SCOPE_CROSS_PAGE,
            issues=(),
            verified=None,
        )
        plan_id = append_plan_record(plan, wiki)
        queue_plan_for_approval(wiki, plan_id=plan_id, plan=plan, outline={})

        outcome = write_apply(
            wiki, plan_id=plan_id, config=_config(shared_type="reference"), verdict=VERDICT_APPROVE
        )
        assert outcome.ok, (outcome.reason, outcome.issues)
        assert outcome.created_uids == ()
        _meta_a, body_a = parse_frontmatter(subject_a.read_text(encoding="utf-8"))
        assert "sequential, not competing" in body_a
        assert read_relations(_read_meta(subject_a)).parent is None
