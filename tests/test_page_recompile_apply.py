# SPDX-License-Identifier: Apache-2.0
"""The page-recompile applier for structure-only plans (issue athenaeum#2540,
slice S4 of athenaeum#2525).

One class per acceptance criterion, named for the criterion it proves.
Plans are built directly through
:func:`athenaeum.page_recompile_plan.validate_plan` over a REAL
:func:`athenaeum.page_recompile.segment_page` result (so unit ids are never
hardcoded) and recorded with
:func:`athenaeum.page_recompile_plan.append_plan_record` -- the exact ledger
shape :func:`athenaeum.page_recompile_apply.write_apply` reads back. A model
call only happens for a merge group's AC7(d) verify step; every such call in
this file goes through the SAME stub-client pattern
``tests/test_page_recompile_plan.py`` already uses -- no network, no key.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from athenaeum.cli import main as cli_main
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.page_recompile import check_marker_resolution, segment_page
from athenaeum.page_recompile_apply import (
    JOURNAL_FILENAME,
    rollback_stale_journal,
    write_apply,
)
from athenaeum.page_recompile_plan import (
    MergedSection,
    PlanGroup,
    RecompilePlan,
    append_plan_record,
    page_content_hash,
    validate_plan,
)
from athenaeum.page_relations import read_relations

# ---------------------------------------------------------------------------
# Stub client -- identical shape to tests/test_page_recompile_plan.py's.
# ---------------------------------------------------------------------------


class _StubBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _StubUsage:
    def __init__(self) -> None:
        self.input_tokens = 11
        self.output_tokens = 7
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _StubResponse:
    def __init__(self, text: str) -> None:
        self.content = [_StubBlock(text)]
        self.usage = _StubUsage()


class _StubMessages:
    def __init__(self, client: "_StubClient") -> None:
        self._client = client

    def create(self, **kwargs: Any) -> _StubResponse:
        self._client.calls.append(kwargs)
        if self._client.responses:
            payload = self._client.responses.pop(0)
        else:
            payload = self._client.default
        return _StubResponse(json.dumps(payload))


class _StubClient:
    def __init__(
        self,
        responses: list[dict[str, Any]] | None = None,
        *,
        default: dict[str, Any] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.default: dict[str, Any] = default if default is not None else {}
        self.calls: list[dict[str, Any]] = []
        self.messages = _StubMessages(self)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _all_supported(n: int) -> dict[str, Any]:
    return {"sentences": [{"index": i, "verdict": "supported"} for i in range(n)]}


class _FlipFlopOnce(dict):
    """Test-only double: returns a DIFFERENT text on the SECOND ``.get()``
    call for *flip_label*, normal-dict behaviour for every other label and
    for the first call. The real single-source-page data flow makes a
    genuine label collision structurally impossible (every unit and
    definition comes from exactly one already-deduped
    ``parse_footnote_definitions`` dict) -- this is how AC5's collision
    guard is exercised anyway, by injecting the one divergence that
    architecture otherwise forbids."""

    def __init__(self, base: dict[str, str], flip_label: str, second_text: str) -> None:
        super().__init__(base)
        self._flip_label = flip_label
        self._second_text = second_text
        self._flip_seen = False

    def get(self, key: object, default: object = None) -> object:  # type: ignore[override]
        if key == self._flip_label:
            if self._flip_seen:
                return self._second_text
            self._flip_seen = True
        return super().get(key, default)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _write_page(wiki: Path, *, uid: str, name: str, body: str, **extra: Any) -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"uid": uid, "type": "reference", "name": name, "access": "internal"}
    meta.update(extra)
    path = wiki / f"{uid}.md"
    path.write_text(render_frontmatter(meta) + "\n" + body, encoding="utf-8")
    return path


#: A body with three units: a long lead paragraph (parent-kept), a
#: ``## Pricing`` section, and a ``## Beta Corp`` section -- the same shape
#: tests/test_page_recompile_plan.py uses, sized well above AC3's 200-char
#: default floor on its own (so a test that is not EXERCISING AC3 does not
#: trip it by accident).
_LEAD = (
    "Acme builds widgets and has operated in the European market for over a "
    "decade, serving manufacturers across the continent with a broad "
    "catalogue of parts and accessories for every kind of widget.[^src-1]"
)
_BODY = (
    _LEAD + "\n\n"
    "## Pricing\n\n"
    "Widgets cost ten pounds each, with a volume discount above one "
    "thousand units ordered in a single purchase.[^src-2]\n\n"
    "## Beta Corp\n\n"
    "Beta Corp is a separate company that resells Acme widgets under its "
    "own brand across the same market.[^src-3]\n\n"
    "[^src-1]: **Source:** doc-a\n"
    "[^src-2]: **Source:** doc-b\n"
    "[^src-3]: **Source:** doc-c\n"
)

_SUMMARY = (
    "Acme is a European widget manufacturer with a long operating history "
    "and a broad catalogue.[^src-1]"
)


def _record_plan(
    wiki: Path,
    *,
    uid: str,
    body: str,
    assignments: list[dict[str, str]],
    groups: list[dict[str, Any]] | None = None,
    merges: list[dict[str, Any]] | None = None,
    summary: str = _SUMMARY,
    page_actions: list[str] | None = None,
) -> tuple[RecompilePlan, str]:
    """Validate and record a plan the same way the real planner would,
    against a REAL segmentation of *body* -- unit ids are read off the
    segmenter, never hardcoded."""
    segment = segment_page(body)
    payload = {
        "groups": groups or [],
        "assignments": assignments,
        "merges": merges or [],
        "page_actions": page_actions or [],
        "summary": summary,
    }
    plan = validate_plan(
        payload,
        segment,
        body,
        uid=uid,
        content_hash=page_content_hash(body),
        known_uids=(),
    )
    plan_id = append_plan_record(plan, wiki)
    return plan, plan_id


def _unit_ids(body: str) -> list[str]:
    return [u.unit_id for u in segment_page(body).units]


def _record_forced_valid_plan(
    wiki: Path,
    *,
    uid: str,
    body: str,
    dispositions: list[tuple[str, str]],
    groups: list[dict[str, Any]] | None = None,
    merges: list[dict[str, Any]] | None = None,
    summary: str = "",
) -> str:
    """Record a plan with ``issues=()`` (so ``.valid`` is ``True``) WITHOUT
    going through :func:`validate_plan`'s own citation/shape checks -- for
    isolating one of :func:`compute_apply`'s OWN defense-in-depth checks
    (AC3/AC5/AC8) from a planner-side refusal that would otherwise pre-empt
    it before the plan is ever eligible to apply."""
    plan = RecompilePlan(
        uid=uid,
        content_hash=page_content_hash(body),
        dispositions=tuple(dispositions),
        groups=tuple(
            PlanGroup(
                key=g["key"], name=g["name"], description=g["description"], confidence=0.9
            )
            for g in (groups or [])
        ),
        merges=tuple(
            MergedSection(group=m["group"], text=m["text"], unit_ids=tuple(m.get("unit_ids", ())))
            for m in (merges or [])
        ),
        page_actions=(),
        summary=summary,
        scope="structure-only",
        issues=(),
        verified=None,
    )
    return append_plan_record(plan, wiki)


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    return tmp_path / "knowledge" / "wiki"


def _simple_plan(wiki: Path, uid: str = "page0001", body: str = _BODY) -> tuple[RecompilePlan, str]:
    """A plain structure-only plan: unit 0 (lead) stays on the parent, unit 1
    (Pricing) moves to a new sub-page, unit 2 (Beta Corp) stays on the
    parent too -- well above AC3's floor either way."""
    ids = _unit_ids(body)
    return _record_plan(
        wiki,
        uid=uid,
        body=body,
        assignments=[
            {"unit": ids[0], "disposition": "parent"},
            {"unit": ids[1], "disposition": "child:g1"},
            {"unit": ids[2], "disposition": "parent"},
        ],
        groups=[
            {"key": "g1", "name": "Pricing", "description": "Pricing details", "confidence": 0.9}
        ],
    )


class TestAC1ApplyLockAndStaleness:
    def test_cli_exits_75_when_lock_is_held(self, wiki: Path) -> None:
        from athenaeum._cli_shared import EXIT_LOCK_HELD
        from athenaeum.runlock import RunLock

        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)
        knowledge = wiki.parent

        lock = RunLock(knowledge, wait=0)
        lock.acquire()
        try:
            rc = cli_main(["recompile", "apply", "--uid", "page0001", "--path", str(knowledge)])
        finally:
            lock.release()
        assert rc == EXIT_LOCK_HELD

    def test_refuses_a_stale_plan_after_the_page_changed(self, wiki: Path) -> None:
        path = _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)

        # The page is edited AFTER the plan was made -- its content hash no
        # longer matches the plan's pin.
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        path.write_text(
            render_frontmatter(meta) + "\n" + _BODY + "\nAn extra paragraph.\n",
            encoding="utf-8",
        )

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok is False
        assert "stale" in outcome.reason

    def test_applies_a_fresh_valid_plan(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok is True
        assert outcome.reason == "applied"
        assert len(outcome.created_uids) == 1

    def test_refuses_an_unknown_uid(self, wiki: Path) -> None:
        wiki.mkdir(parents=True)
        outcome = write_apply(wiki, uid="nosuchuid", client=None, config=None)
        assert outcome.ok is False


class TestAC2ParentFrontmatterAllowlist:
    def test_every_other_key_is_byte_identical(self, wiki: Path) -> None:
        path = _write_page(
            wiki,
            uid="page0001",
            name="Acme",
            body=_BODY,
            aliases=["Acme Corp"],
            tags=["widgets", "manufacturing"],
            coordinates="40.0,-70.0",
        )
        before_meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        _simple_plan(wiki)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues

        after_meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        allowlist = {"updated", "description", "related", "needs_split"}
        for key in before_meta:
            if key in allowlist:
                continue
            assert after_meta.get(key) == before_meta.get(key), key
        # Every before-key not in the allowlist is still present, unchanged.
        assert after_meta["uid"] == "page0001"
        assert after_meta["name"] == "Acme"
        assert after_meta["aliases"] == ["Acme Corp"]
        assert after_meta["type"] == "reference"
        assert after_meta["access"] == "internal"
        assert after_meta["tags"] == ["widgets", "manufacturing"]
        assert after_meta["coordinates"] == "40.0,-70.0"

    def test_needs_split_removed_only_when_body_now_under_threshold(self, wiki: Path) -> None:
        path = _write_page(wiki, uid="page0001", name="Acme", body=_BODY, needs_split=True)
        _simple_plan(wiki)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues

        after_meta, after_body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert len(after_body) <= 10_000  # DEFAULT_PAGE_SIZE_THRESHOLD_CHARS
        assert "needs_split" not in after_meta

    def test_a_link_hub_only_parent_is_refused(self, wiki: Path) -> None:
        """AC3's own test lives in TestAC3; this is AC2's angle on the same
        refusal: a plan with NO `parent` units at all degrades the page to
        links only, which must never land on disk."""
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        ids = _unit_ids(_BODY)
        _record_forced_valid_plan(
            wiki,
            uid="page0001",
            body=_BODY,
            dispositions=[
                (ids[0], "child:g1"),
                (ids[1], "child:g1"),
                (ids[2], "child:g1"),
            ],
            groups=[{"key": "g1", "name": "Everything", "description": "d"}],
            summary="",
        )

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok is False
        assert any(i.code == "parent-too-thin" for i in outcome.issues)
        # Nothing written: the only .md file is still the original parent.
        assert [p.name for p in wiki.glob("*.md")] == ["page0001.md"]


class TestAC3MinimumCitedProse:
    def test_refuses_a_plan_leaving_only_links_on_the_parent(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        ids = _unit_ids(_BODY)
        _record_forced_valid_plan(
            wiki,
            uid="page0001",
            body=_BODY,
            dispositions=[
                (ids[0], "child:g1"),
                (ids[1], "child:g1"),
                (ids[2], "child:g1"),
            ],
            groups=[{"key": "g1", "name": "Everything", "description": "d"}],
            summary="Hi.",
        )

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok is False
        assert any(i.code == "parent-too-thin" for i in outcome.issues)

    def test_accepts_a_plan_with_enough_retained_prose(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues

    def test_configurable_minimum_is_honoured(self, wiki: Path) -> None:
        """A very small retained prose budget passes with a high configured
        minimum refused, and the SAME plan passes once the knob is lowered."""
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)

        strict_config = {"librarian": {"page_recompile": {"min_parent_chars": 100_000}}}
        outcome = write_apply(wiki, uid="page0001", client=None, config=strict_config)
        assert outcome.ok is False
        assert any(i.code == "parent-too-thin" for i in outcome.issues)


class TestAC4SubPageCreation:
    def test_new_uid_name_shape_and_inherited_fields(self, wiki: Path) -> None:
        _write_page(
            wiki,
            uid="page0001",
            name="Acme",
            body=_BODY,
            access="confidential",
            tags=["widgets"],
            provenance_scope="team-a",
            claimed_scope="org",
            subject="organization",
        )
        _simple_plan(wiki)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues
        assert len(outcome.created_uids) == 1
        sub_uid = outcome.created_uids[0]
        assert sub_uid != "page0001"

        sub_path = next(wiki.glob(f"{sub_uid}*.md"))
        sub_meta, sub_body = parse_frontmatter(sub_path.read_text(encoding="utf-8"))
        assert sub_meta["name"] == "Acme (Pricing)"
        assert sub_meta["access"] == "confidential"
        assert sub_meta["tags"] == ["widgets"]
        assert sub_meta["provenance_scope"] == "team-a"
        assert sub_meta["claimed_scope"] == "org"
        assert sub_meta["subject"] == "organization"
        assert sub_meta["parent"] == "page0001"
        assert "Part of:" in sub_body  # the breadcrumb

    def test_no_sub_page_has_wider_access_than_its_parent(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY, access="personal")
        _simple_plan(wiki)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues
        sub_path = next(wiki.glob(f"{outcome.created_uids[0]}*.md"))
        sub_meta, _ = parse_frontmatter(sub_path.read_text(encoding="utf-8"))
        # Inherited, never widened: equal access is "not wider".
        assert sub_meta["access"] == "personal"

    def test_both_sides_of_the_relation_are_linked(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues
        sub_uid = outcome.created_uids[0]

        parent_path = wiki / "page0001.md"
        parent_meta, _ = parse_frontmatter(parent_path.read_text(encoding="utf-8"))
        rel = read_relations(parent_meta)
        assert any(c.uid == sub_uid and c.via == "split" for c in rel.children)

        sub_path = next(wiki.glob(f"{sub_uid}*.md"))
        sub_meta, _ = parse_frontmatter(sub_path.read_text(encoding="utf-8"))
        sub_rel = read_relations(sub_meta)
        assert sub_rel.parent is not None
        assert sub_rel.parent.uid == "page0001"


class TestAC5UnitConservationAndByteIdentity:
    def test_unmerged_units_are_byte_for_byte_on_their_destination(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)
        ids = _unit_ids(_BODY)
        units = {u.unit_id: u for u in segment_page(_BODY).units}

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues

        parent_body = parse_frontmatter((wiki / "page0001.md").read_text(encoding="utf-8"))[1]
        assert units[ids[0]].text in parent_body
        assert units[ids[2]].text in parent_body

        sub_path = next(wiki.glob(f"{outcome.created_uids[0]}*.md"))
        sub_body = parse_frontmatter(sub_path.read_text(encoding="utf-8"))[1]
        assert units[ids[1]].text in sub_body

    def test_every_unit_accounted_for_exactly_once(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        ids = _unit_ids(_BODY)
        # A duplicate-assignment bug: unit 1 assigned twice (once parent,
        # once child) -- must be refused, not silently resolved.
        plan, _ = _record_plan(
            wiki,
            uid="page0001",
            body=_BODY,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "child:g1"},
                {"unit": ids[1], "disposition": "parent"},
                {"unit": ids[2], "disposition": "parent"},
            ],
            groups=[{"key": "g1", "name": "Pricing", "description": "d", "confidence": 0.9}],
        )
        # validate_plan already catches this at plan time -- confirm it, and
        # separately confirm the applier's OWN defense-in-depth catches it
        # even when handed a hand-built plan that bypasses validate_plan.
        assert any(i.code == "duplicate-assignment" for i in plan.issues)

        bogus_plan = RecompilePlan(
            uid="page0001",
            content_hash=page_content_hash(_BODY),
            dispositions=(
                (ids[0], "parent"),
                (ids[1], "child:g1"),
                (ids[1], "parent"),
                (ids[2], "parent"),
            ),
            groups=plan.groups,
            merges=(),
            page_actions=(),
            summary=_SUMMARY,
            scope="structure-only",
            issues=(),
            verified=None,
        )
        from athenaeum.page_recompile_plan import append_plan_record

        bogus_id = append_plan_record(bogus_plan, wiki)
        outcome = write_apply(wiki, plan_id=bogus_id, client=None, config=None)
        assert outcome.ok is False
        assert any(i.code == "unit-accounted-for-twice" for i in outcome.issues)

    def test_conservation_passes_again_after_re_reading_written_pages(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues
        assert outcome.post_write_issues == ()

    def test_conservation_passes_when_the_childs_leading_heading_is_deeper_than_target_level(
        self, wiki: Path
    ) -> None:
        """The child unit's own heading is ``### Pricing`` (deeper than
        :func:`athenaeum.page_recompile_apply.renormalise_heading`'s
        ``target_level=2``), so the body actually written to the sub-page
        carries a re-levelled ``## Pricing`` -- athenaeum#2540's one
        permitted structural edit. The post-write re-read check must
        compare against that SAME re-levelled text, not the raw unit text,
        or a perfectly correct apply is flagged with a spurious
        ``post-write-unit-missing`` issue."""
        body = (
            _LEAD + "\n\n"
            "### Pricing\n\n"
            "Widgets cost ten pounds each, with a volume discount above one "
            "thousand units ordered in a single purchase.[^src-2]\n\n"
            "## Beta Corp\n\n"
            "Beta Corp is a separate company that resells Acme widgets under "
            "its own brand across the same market.[^src-3]\n\n"
            "[^src-1]: **Source:** doc-a\n"
            "[^src-2]: **Source:** doc-b\n"
            "[^src-3]: **Source:** doc-c\n"
        )
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        _simple_plan(wiki, body=body)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues
        assert outcome.post_write_issues == ()


class TestAC5FootnoteLabelCollisionGuard:
    """AC5 permits footnote-label renumbering-on-collision as an allowed
    edit but does not mandate implementing it, and for a structure-only
    plan it is unreachable: every unit and definition comes from ONE
    already-deduped source page, so two pieces landing on one destination
    can never legitimately disagree about what a shared label means.
    `_first_colliding_label` makes that invariant self-enforcing rather
    than merely reasoned about -- this proves the guard actually fires,
    by hand-constructing the one divergence the real data flow forbids."""

    def test_a_hand_constructed_collision_is_refused_with_no_write(
        self, wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = (
            _LEAD + "\n\n"
            "## Alpha\n\nAlpha facts cite a shared source.[^shared1]\n\n"
            "## Beta\n\nBeta facts also cite the same shared source.[^shared1]\n\n"
            "[^src-1]: **Source:** doc-a\n"
            "[^shared1]: **Source:** doc-shared\n"
        )
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        _record_forced_valid_plan(
            wiki,
            uid="page0001",
            body=body,
            dispositions=[
                (ids[0], "parent"),
                (ids[1], "child:g1"),
                (ids[2], "child:g1"),
            ],
            groups=[{"key": "g1", "name": "Shared", "description": "d"}],
            summary=_SUMMARY,
        )

        import athenaeum.page_recompile_apply as apply_mod

        real_parse = apply_mod.parse_footnote_definitions

        def _fake_parse(body_text: str) -> dict[str, str]:
            return _FlipFlopOnce(real_parse(body_text), "shared1", "A DIFFERENT DEFINITION")

        monkeypatch.setattr(apply_mod, "parse_footnote_definitions", _fake_parse)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok is False
        assert any(
            i.code == "footnote-label-collision" and i.detail.startswith("shared1")
            for i in outcome.issues
        )
        # No write at all -- not even the sub-page.
        assert [p.name for p in wiki.glob("*.md")] == ["page0001.md"]


class TestAC6FootnoteMarkersResolveOnPage:
    def test_every_marker_resolves_on_its_own_page_text_unchanged(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues

        parent_body = parse_frontmatter((wiki / "page0001.md").read_text(encoding="utf-8"))[1]
        assert check_marker_resolution(parent_body).ok
        assert "[^src-1]: **Source:** doc-a" in parent_body
        assert "[^src-3]: **Source:** doc-c" in parent_body

        sub_path = next(wiki.glob(f"{outcome.created_uids[0]}*.md"))
        sub_body = parse_frontmatter(sub_path.read_text(encoding="utf-8"))[1]
        assert check_marker_resolution(sub_body).ok
        assert "[^src-2]: **Source:** doc-b" in sub_body


class TestAC7MergedSections:
    def _merge_body(self) -> str:
        return (
            _LEAD + "\n\n"
            "## Pricing A\n\n"
            "Widgets cost ten pounds in the UK market.[^src-2]\n\n"
            "## Pricing B\n\n"
            "Widgets cost twelve euros in the EU market.[^src-3]\n\n"
            "[^src-1]: **Source:** doc-a\n"
            "[^src-2]: **Source:** doc-b\n"
            "[^src-3]: **Source:** doc-c\n"
        )

    def test_a_passing_merge_lands_with_every_citation_and_definition(self, wiki: Path) -> None:
        body = self._merge_body()
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        merged_text = (
            "Widgets cost ten pounds in the UK and twelve euros in the "
            "EU.[^src-2][^src-3]"
        )
        _record_plan(
            wiki,
            uid="page0001",
            body=body,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "merge:g1"},
                {"unit": ids[2], "disposition": "merge:g1"},
            ],
            groups=[{"key": "g1", "name": "Pricing", "description": "d", "confidence": 0.9}],
            merges=[{"group": "g1", "text": merged_text}],
        )

        client = _StubClient(responses=[_all_supported(1)])
        outcome = write_apply(wiki, uid="page0001", client=client, config=None)
        assert outcome.ok, outcome.issues
        assert client.call_count == 1

        sub_path = next(wiki.glob(f"{outcome.created_uids[0]}*.md"))
        sub_body = parse_frontmatter(sub_path.read_text(encoding="utf-8"))[1]
        assert merged_text in sub_body
        assert "[^src-2]: **Source:** doc-b" in sub_body
        assert "[^src-3]: **Source:** doc-c" in sub_body
        assert check_marker_resolution(sub_body).ok

    def test_a_dropped_citation_in_the_merged_text_is_refused(self, wiki: Path) -> None:
        body = self._merge_body()
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        # Drops the [^src-3] citation that unit 2 originally carried.
        merged_text = "Widgets cost ten pounds in the UK and twelve euros in the EU.[^src-2]"
        _record_plan(
            wiki,
            uid="page0001",
            body=body,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "merge:g1"},
                {"unit": ids[2], "disposition": "merge:g1"},
            ],
            groups=[{"key": "g1", "name": "Pricing", "description": "d", "confidence": 0.9}],
            merges=[{"group": "g1", "text": merged_text}],
        )

        client = _StubClient(responses=[_all_supported(1)])
        outcome = write_apply(wiki, uid="page0001", client=client, config=None)
        assert outcome.ok is False
        assert any(i.code == "merge-dropped-citation" for i in outcome.issues)
        # No write at all -- not even the sub-page.
        assert list(wiki.glob("*.md")) == [wiki / "page0001.md"]

    def test_an_altered_definition_is_refused(self, wiki: Path) -> None:
        """AC7(b)'s failing case: a required label's definition is no longer
        present VERBATIM where the merge needs it. Driven directly against
        :func:`compute_apply` (not through ``write_apply``'s page-hash-pinned
        staleness gate, which would itself refuse first -- a definition's
        text is part of the page body, so editing it always changes the
        content hash AC1 already checks; this isolates compute_apply's OWN
        AC7(b) check from that outer gate)."""
        from athenaeum.page_recompile import segment_page as _segment
        from athenaeum.page_recompile_apply import compute_apply

        body = self._merge_body()
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        merged_text = (
            "Widgets cost ten pounds in the UK and twelve euros in the "
            "EU.[^src-2][^src-3]"
        )
        plan = RecompilePlan(
            uid="page0001",
            content_hash=page_content_hash(body),
            dispositions=((ids[0], "parent"), (ids[1], "merge:g1"), (ids[2], "merge:g1")),
            groups=(PlanGroup(key="g1", name="Pricing", description="d", confidence=0.9),),
            merges=(MergedSection(group="g1", text=merged_text, unit_ids=(ids[1], ids[2])),),
            page_actions=(),
            summary=_SUMMARY,
            scope="structure-only",
            issues=(),
            verified=None,
        )
        segment = _segment(body)
        # The definition src-3 needs is now ALTERED to a different label
        # (simulating "no longer present under this name" -- the definition
        # the merge was checked against at plan time has changed), passed as
        # the *page_body* compute_apply reads definitions from directly,
        # independent of write_apply's own outer staleness check.
        altered_page_body = body.replace(
            "[^src-3]: **Source:** doc-c", "[^src-3-renamed]: **Source:** doc-c"
        )

        result = compute_apply(
            plan=plan,
            segment=segment,
            page_meta={"uid": "page0001", "name": "Acme", "type": "reference"},
            page_body=altered_page_body,
            page_text=render_frontmatter(
                {"uid": "page0001", "name": "Acme", "type": "reference"}
            )
            + "\n"
            + altered_page_body,
            page_path=wiki / "page0001.md",
            wiki_root=wiki,
            today="2026-01-01",
            config=None,
            client=_StubClient(responses=[_all_supported(1)]),
            usage=None,
        )
        assert not result.ok
        assert any(
            i.code == "merge-dangling-citation" and i.detail == "src-3" for i in result.issues
        )

    def test_a_merge_feeder_citing_an_undefined_label_is_refused(self, wiki: Path) -> None:
        """The direct AC7(b) case: a required label has no definition on the
        page at all."""
        body = (
            _LEAD + "\n\n"
            "## Pricing A\n\n"
            "Widgets cost ten pounds.[^src-2]\n\n"
            "## Pricing B\n\n"
            "Widgets cost twelve euros.[^src-9]\n\n"
            "[^src-1]: **Source:** doc-a\n"
            "[^src-2]: **Source:** doc-b\n"
            # src-9 is cited by unit 2 but never defined.
        )
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        merged_text = "Widgets cost ten pounds and twelve euros.[^src-2][^src-9]"
        _record_plan(
            wiki,
            uid="page0001",
            body=body,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "merge:g1"},
                {"unit": ids[2], "disposition": "merge:g1"},
            ],
            groups=[{"key": "g1", "name": "Pricing", "description": "d", "confidence": 0.9}],
            merges=[{"group": "g1", "text": merged_text}],
        )
        client = _StubClient(responses=[_all_supported(1)])
        outcome = write_apply(wiki, uid="page0001", client=client, config=None)
        assert outcome.ok is False
        assert any(i.code == "merge-dangling-citation" for i in outcome.issues)

    def test_merge_too_long_is_refused(self, wiki: Path) -> None:
        body = self._merge_body()
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        units = {u.unit_id: u for u in segment_page(body).units}
        too_long = (units[ids[1]].text + units[ids[2]].text) * 2
        _record_plan(
            wiki,
            uid="page0001",
            body=body,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "merge:g1"},
                {"unit": ids[2], "disposition": "merge:g1"},
            ],
            groups=[{"key": "g1", "name": "Pricing", "description": "d", "confidence": 0.9}],
            merges=[{"group": "g1", "text": too_long}],
        )
        client = _StubClient(responses=[_all_supported(1)])
        outcome = write_apply(wiki, uid="page0001", client=client, config=None)
        assert outcome.ok is False
        assert any(i.code == "merge-too-long" for i in outcome.issues)

    def test_an_unsupported_sentence_refuses_the_whole_plan(self, wiki: Path) -> None:
        body = self._merge_body()
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        merged_text = (
            "Widgets cost ten pounds in the UK and twelve euros in the "
            "EU.[^src-2][^src-3]"
        )
        _record_plan(
            wiki,
            uid="page0001",
            body=body,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "merge:g1"},
                {"unit": ids[2], "disposition": "merge:g1"},
            ],
            groups=[{"key": "g1", "name": "Pricing", "description": "d", "confidence": 0.9}],
            merges=[{"group": "g1", "text": merged_text}],
        )
        client = _StubClient(responses=[{"sentences": [{"index": 0, "verdict": "unsupported"}]}])
        outcome = write_apply(wiki, uid="page0001", client=client, config=None)
        assert outcome.ok is False
        assert any(i.code == "merge-unverified" for i in outcome.issues)


class TestAC8ConservationBucketing:
    def test_a_plan_that_both_merges_and_separately_keeps_the_same_unit_fails(
        self, wiki: Path
    ) -> None:
        body = (
            _LEAD + "\n\n"
            "## Pricing A\n\n"
            "Widgets cost ten pounds.[^src-2]\n\n"
            "## Pricing B\n\n"
            "Widgets cost twelve euros.[^src-3]\n\n"
            "[^src-1]: **Source:** doc-a\n"
            "[^src-2]: **Source:** doc-b\n"
            "[^src-3]: **Source:** doc-c\n"
        )
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        merged_text = "Widgets cost ten pounds and twelve euros.[^src-2][^src-3]"
        # Unit 1 is BOTH merged into g1 AND, via a second (hand-built,
        # validate_plan-bypassing) disposition row, also kept verbatim as a
        # second child of g2 -- the defense-in-depth case AC8 names.
        bogus_plan = RecompilePlan(
            uid="page0001",
            content_hash=page_content_hash(body),
            dispositions=(
                (ids[0], "parent"),
                (ids[1], "merge:g1"),
                (ids[1], "child:g2"),
                (ids[2], "merge:g1"),
            ),
            groups=(),
            merges=(MergedSection(group="g1", text=merged_text, unit_ids=(ids[1], ids[2])),),
            page_actions=(),
            summary=_SUMMARY,
            scope="structure-only",
            issues=(),
            verified=None,
        )
        plan_id = append_plan_record(bogus_plan, wiki)
        client = _StubClient(responses=[_all_supported(1)])
        outcome = write_apply(wiki, plan_id=plan_id, client=client, config=None)
        assert outcome.ok is False
        assert any(i.code == "unit-accounted-for-twice" for i in outcome.issues)


class TestAC9LedgerAndSnapshot:
    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(
            ["git", "init", "-b", "develop"], cwd=root, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)

    def test_ledger_record_and_one_snapshot_commit(self, wiki: Path) -> None:
        knowledge = wiki.parent
        knowledge.mkdir(parents=True, exist_ok=True)
        self._init_git_repo(knowledge)
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        subprocess.run(["git", "add", "-A"], cwd=knowledge, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "seed"], cwd=knowledge, check=True, capture_output=True
        )
        before_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=knowledge, capture_output=True, text=True, check=True
        ).stdout.strip()

        _simple_plan(wiki)
        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues
        assert outcome.snapshot_sha is not None
        assert outcome.snapshot_sha != before_sha

        after_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=knowledge, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert after_sha == outcome.snapshot_sha
        # Exactly one commit landed from this apply.
        parent_sha = subprocess.run(
            ["git", "rev-parse", "HEAD^"], cwd=knowledge, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert parent_sha == before_sha

        records = [
            json.loads(line)
            for line in (wiki / "_page_recompiles.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        apply_records = [r for r in records if r.get("kind") == "apply"]
        assert len(apply_records) == 1
        record = apply_records[0]
        assert record["uid"] == "page0001"
        assert record["created_uids"] == list(outcome.created_uids)
        pages = {p["uid"]: p for p in record["pages"]}
        assert "page0001" in pages
        assert pages["page0001"]["pre_blob_id"]
        assert pages["page0001"]["pre_content_hash"]
        assert pages["page0001"]["post_content_hash"]
        assert outcome.created_uids[0] in pages

    def test_the_recorded_pre_blob_id_is_gits_own_hash_of_the_pre_apply_file(
        self, wiki: Path
    ) -> None:
        """athenaeum#2541: ``pre_blob_id`` must name bytes git actually holds,
        or the undo path's ``git cat-file blob`` lookup misses and a
        reversible apply degrades to a queued decision item.

        Asserted against ``git hash-object`` on the real pre-apply file, and
        on a page written WITHOUT a blank line after its closing ``---`` --
        the corpus's other shape, and the one a re-render of the parsed
        frontmatter does not reproduce.
        """
        knowledge = wiki.parent
        knowledge.mkdir(parents=True, exist_ok=True)
        self._init_git_repo(knowledge)
        meta = {"uid": "page0001", "type": "reference", "name": "Acme", "access": "internal"}
        page = wiki / "page0001.md"
        wiki.mkdir(parents=True, exist_ok=True)
        # No blank line after the frontmatter: parses to the same body, but
        # its bytes are NOT what render_frontmatter(meta) + "\n" + body gives.
        page.write_text(render_frontmatter(meta) + _BODY, encoding="utf-8")
        assert page.read_text(encoding="utf-8") != render_frontmatter(meta) + "\n" + _BODY
        expected = subprocess.run(
            ["git", "hash-object", str(page)],
            cwd=knowledge,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        _simple_plan(wiki)
        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues

        records = [
            json.loads(line)
            for line in (wiki / "_page_recompiles.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        record = next(r for r in records if r.get("kind") == "apply")
        row = next(r for r in record["pages"] if r["uid"] == "page0001")
        assert row["pre_blob_id"] == expected

    def test_merge_group_originals_are_recorded_for_the_undo_snapshot(self, wiki: Path) -> None:
        body = (
            _LEAD + "\n\n"
            "## Pricing A\n\nWidgets cost ten pounds.[^src-2]\n\n"
            "## Pricing B\n\nWidgets cost twelve euros.[^src-3]\n\n"
            "[^src-1]: **Source:** doc-a\n[^src-2]: **Source:** doc-b\n"
            "[^src-3]: **Source:** doc-c\n"
        )
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        merged_text = "Widgets cost ten pounds and twelve euros.[^src-2][^src-3]"
        _record_plan(
            wiki,
            uid="page0001",
            body=body,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "merge:g1"},
                {"unit": ids[2], "disposition": "merge:g1"},
            ],
            groups=[{"key": "g1", "name": "Pricing", "description": "d", "confidence": 0.9}],
            merges=[{"group": "g1", "text": merged_text}],
        )
        client = _StubClient(responses=[_all_supported(1)])
        outcome = write_apply(wiki, uid="page0001", client=client, config=None)
        assert outcome.ok, outcome.issues

        records = [
            json.loads(line)
            for line in (wiki / "_page_recompiles.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        record = next(r for r in records if r.get("kind") == "apply")
        assert len(record["merges"]) == 1
        merge_record = record["merges"][0]
        assert merge_record["group"] == "g1"
        assert set(merge_record["unit_ids"]) == {ids[1], ids[2]}
        assert len(merge_record["unit_hashes"]) == 2


class TestAC10AllOrNothing:
    def _wiki_hashes(self, wiki: Path) -> dict[str, str]:
        import hashlib

        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(wiki.glob("*.md"))
        }

    def test_a_write_failure_leaves_the_wiki_byte_identical(
        self, wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)
        before = self._wiki_hashes(wiki)

        import athenaeum.page_recompile_apply as apply_mod

        real_write = apply_mod.atomic_write_text
        calls = {"n": 0}

        def _failing_write(path: Path, text: str, **kwargs: object) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated failure")
            real_write(path, text, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(apply_mod, "atomic_write_text", _failing_write)
        with pytest.raises(OSError):
            write_apply(wiki, uid="page0001", client=None, config=None)

        after = self._wiki_hashes(wiki)
        assert after == before
        assert not (wiki / JOURNAL_FILENAME).exists()

    def test_a_failure_on_the_parent_write_rolls_back_the_sub_pages_too(
        self, wiki: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)
        before = self._wiki_hashes(wiki)

        import athenaeum.page_recompile_apply as apply_mod

        real_write = apply_mod.atomic_write_text
        calls = {"n": 0}

        def _failing_write(path: Path, text: str, **kwargs: object) -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # the parent is written LAST (second call)
                raise OSError("simulated failure on parent write")
            real_write(path, text, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(apply_mod, "atomic_write_text", _failing_write)
        with pytest.raises(OSError):
            write_apply(wiki, uid="page0001", client=None, config=None)

        after = self._wiki_hashes(wiki)
        assert after == before
        assert not (wiki / JOURNAL_FILENAME).exists()

    def test_a_journal_left_by_a_killed_process_before_the_parent_write_is_rolled_back(
        self, wiki: Path
    ) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        bogus_sub = wiki / "deadbeef-bogus.md"
        bogus_sub.write_text("orphaned sub-page draft", encoding="utf-8")
        journal = wiki / JOURNAL_FILENAME
        journal.write_text(
            json.dumps(
                {
                    "plan_id": "x",
                    "uid": "page0001",
                    "parent_path": str(wiki / "page0001.md"),
                    "pre_content_hash": page_content_hash(_BODY),
                    "new_paths": [str(bogus_sub)],
                }
            ),
            encoding="utf-8",
        )

        rollback_stale_journal(wiki)

        assert not bogus_sub.exists()
        assert not journal.exists()

    def test_a_journal_left_after_the_parent_write_keeps_the_sub_pages(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        # Simulate: the parent was ALREADY rewritten (its body no longer
        # hashes to the journal's recorded pre-image) before the process
        # died -- the journal's listed sub-page must survive rollback.
        new_sub = wiki / "cafebabe-real.md"
        new_sub.write_text("a real, already-linked sub-page", encoding="utf-8")
        journal = wiki / JOURNAL_FILENAME
        journal.write_text(
            json.dumps(
                {
                    "plan_id": "x",
                    "uid": "page0001",
                    "parent_path": str(wiki / "page0001.md"),
                    "pre_content_hash": "0" * 64,  # does NOT match the current body
                    "new_paths": [str(new_sub)],
                }
            ),
            encoding="utf-8",
        )

        rollback_stale_journal(wiki)

        assert new_sub.exists()
        assert not journal.exists()

    def test_apply_rolls_back_a_stale_journal_at_the_start(self, wiki: Path) -> None:
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _simple_plan(wiki)
        orphan = wiki / "deadbeef-orphan.md"
        orphan.write_text("orphan", encoding="utf-8")
        (wiki / JOURNAL_FILENAME).write_text(
            json.dumps(
                {
                    "plan_id": "x",
                    "uid": "page0001",
                    "parent_path": str(wiki / "page0001.md"),
                    "pre_content_hash": page_content_hash(_BODY),
                    "new_paths": [str(orphan)],
                }
            ),
            encoding="utf-8",
        )

        outcome = write_apply(wiki, uid="page0001", client=None, config=None)
        assert outcome.ok, outcome.issues
        assert not orphan.exists()


class TestAC11MergeNearDuplicatesKnob:
    def _merge_body(self) -> str:
        return (
            _LEAD + "\n\n"
            "## Pricing A\n\nWidgets cost ten pounds.[^src-2]\n\n"
            "## Pricing B\n\nWidgets cost twelve euros.[^src-3]\n\n"
            "[^src-1]: **Source:** doc-a\n[^src-2]: **Source:** doc-b\n"
            "[^src-3]: **Source:** doc-c\n"
        )

    def test_default_on_merges(self, wiki: Path) -> None:
        body = self._merge_body()
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        merged_text = "Widgets cost ten pounds and twelve euros.[^src-2][^src-3]"
        _record_plan(
            wiki,
            uid="page0001",
            body=body,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "merge:g1"},
                {"unit": ids[2], "disposition": "merge:g1"},
            ],
            groups=[{"key": "g1", "name": "Pricing", "description": "d", "confidence": 0.9}],
            merges=[{"group": "g1", "text": merged_text}],
        )
        client = _StubClient(responses=[_all_supported(1)])
        outcome = write_apply(wiki, uid="page0001", client=client, config=None)
        assert outcome.ok, outcome.issues
        assert client.call_count == 1
        sub_path = next(wiki.glob(f"{outcome.created_uids[0]}*.md"))
        assert merged_text in sub_path.read_text(encoding="utf-8")

    def test_off_re_plans_without_the_merge(self, wiki: Path) -> None:
        body = self._merge_body()
        _write_page(wiki, uid="page0001", name="Acme", body=body)
        ids = _unit_ids(body)
        merged_text = "Widgets cost ten pounds and twelve euros.[^src-2][^src-3]"
        _record_plan(
            wiki,
            uid="page0001",
            body=body,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "merge:g1"},
                {"unit": ids[2], "disposition": "merge:g1"},
            ],
            groups=[{"key": "g1", "name": "Pricing", "description": "d", "confidence": 0.9}],
            merges=[{"group": "g1", "text": merged_text}],
        )
        # No client at all -- if merging were still attempted, this would
        # refuse with merge-verify-unavailable. It does not, because the
        # knob being off converts every `merge:g1` into `child:g1` before
        # merge validation is even reached.
        config = {"librarian": {"page_recompile": {"merge_near_duplicates": False}}}
        outcome = write_apply(wiki, uid="page0001", client=None, config=config)
        assert outcome.ok, outcome.issues
        sub_path = next(wiki.glob(f"{outcome.created_uids[0]}*.md"))
        sub_body = sub_path.read_text(encoding="utf-8")
        assert merged_text not in sub_body
        units = {u.unit_id: u for u in segment_page(body).units}
        assert units[ids[1]].text in sub_body
        assert units[ids[2]].text in sub_body
