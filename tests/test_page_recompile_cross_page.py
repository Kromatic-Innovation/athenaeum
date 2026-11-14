# SPDX-License-Identifier: Apache-2.0
"""Cross-page recompile actions (issue athenaeum#2548, slice S7 of
athenaeum#2525): move misfiled sections onto an existing page, split them off
into a standalone page, attach an existing page, and move an alias.

One class per acceptance criterion, named for the criterion it proves -- the
same shape ``tests/test_page_recompile_apply.py`` and
``tests/test_page_recompile_undo.py`` use, with the same discipline:

* plans are built through
  :func:`athenaeum.page_recompile_plan.validate_plan` over a REAL
  :func:`athenaeum.page_recompile.segment_page` result, so unit ids are
  never hardcoded;
* every apply runs the real
  :func:`athenaeum.page_recompile_apply.write_apply`, and every plan that
  carries a cross-page action is RAISED as a real decision item first
  (:func:`athenaeum.page_recompile_queue.queue_plan_for_approval`), because
  AC8 makes that the precondition for applying one at all;
* the wikis in :class:`TestAC11UndoCoversTheseActions` are REAL git
  repositories with a REAL pre-apply commit, which is the only thing that
  makes a pre-image blob readable (see
  ``tests/test_page_recompile_undo.py``'s own module docstring).

No model call happens anywhere in this file: none of these actions has a
merge group, which is the only thing the applier ever calls a client for.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from athenaeum.models import EntityIndex, parse_frontmatter, render_frontmatter
from athenaeum.page_recompile import segment_page
from athenaeum.page_recompile_apply import (
    access_widening_reason,
    relocation_note,
    write_apply,
)
from athenaeum.page_recompile_nightly import run_nightly_page_recompile
from athenaeum.page_recompile_plan import (
    ALIAS_MOVE_VERB,
    PAGE_ACTIONS,
    SCOPE_CROSS_PAGE,
    SCOPE_STRUCTURE_ONLY,
    STANDALONE_NAME_INACTIVE_CODE,
    STANDALONE_NAME_IS_SOURCE_CODE,
    RecompilePlan,
    append_plan_record,
    classify_plan_scope,
    page_content_hash,
    parse_alias_move,
    resolve_standalone_groups,
    validate_plan,
)
from athenaeum.page_recompile_queue import (
    VERDICT_APPROVE,
    VERDICT_APPROVE_STRUCTURE_ONLY,
    build_plan_outline,
    list_pending_page_recompile_plans,
    queue_plan_for_approval,
)
from athenaeum.page_recompile_undo import (
    REASON_PAGE_CHANGED,
    UNDO_DIRECT,
    UNDO_QUEUED,
    find_apply_record,
    write_undo,
)
from athenaeum.page_relations import read_relations

# ---------------------------------------------------------------------------
# Fixtures. The source page has FOUR units, one of which carries no citation
# at all -- AC3 needs both directions and a body where every unit is cited
# could only ever prove one of them.
# ---------------------------------------------------------------------------

_LEAD = (
    "Acme builds widgets and has operated in the European market for over a "
    "decade, serving manufacturers across the continent with a broad "
    "catalogue of parts and accessories.[^src-1]"
)
_PRICING = "Widgets cost ten pounds each, with a volume discount above one thousand units.[^src-2]"
_NOTES = "A dated note about an unrelated incident, carrying no citation of any kind."
_BETA = "Beta Corp is a separate company that resells Acme widgets under its own brand.[^src-3]"
_BODY = (
    _LEAD + "\n\n"
    "## Pricing\n\n" + _PRICING + "\n\n"
    "## Session Notes\n\n" + _NOTES + "\n\n"
    "## Beta Corp\n\n" + _BETA + "\n\n"
    "[^src-1]: **Source:** doc-a\n"
    "[^src-2]: **Source:** doc-b\n"
    "[^src-3]: **Source:** doc-c\n"
)
_SUMMARY = (
    "Acme is a European widget manufacturer with a long operating history "
    "and a broad catalogue.[^src-1]"
)

#: The move target. It defines ``src-2`` ITSELF, with different text from
#: the source page's ``src-2`` -- which is what makes AC1's "renumbered on
#: collision" clause reachable at all.
_TARGET_BODY = (
    "Beta Corp resells widgets in several European markets.[^src-2]\n\n"
    "[^src-2]: **Source:** target-doc\n"
)

#: The applier's AC3 parent-prose floor, out of the way of every test here
#: that is not about it (the same pin ``tests/test_page_recompile_nightly.py``
#: uses, and for the same reason).
_CONFIG: dict[str, Any] = {"librarian": {"page_recompile": {"min_parent_chars": 1}}}


def _unit_ids(body: str = _BODY) -> list[str]:
    return [u.unit_id for u in segment_page(body).units]


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


def _seed(
    wiki: Path,
    *,
    source_extra: dict[str, Any] | None = None,
    target_extra: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path]:
    """The three pages every test here needs: the recompiled SOURCE, an
    existing move/attach TARGET, and a third unrelated page."""
    source = _write_page(
        wiki,
        uid="page0001",
        name="Acme",
        body=_BODY,
        aliases=["Acme Corp", "Beta Metrics"],
        **(source_extra or {}),
    )
    target = _write_page(
        wiki,
        uid="target01",
        name="Beta Corp",
        body=_TARGET_BODY,
        aliases=["BC Ltd"],
        **(target_extra or {}),
    )
    other = _write_page(
        wiki, uid="other001", name="Gamma Ltd", body="Gamma Ltd is a distributor.\n"
    )
    return source, target, other


def _record_plan(
    wiki: Path,
    *,
    assignments: list[dict[str, str]],
    groups: list[dict[str, Any]] | None = None,
    page_actions: list[str] | None = None,
    queue: bool = True,
) -> tuple[RecompilePlan, str]:
    """Validate, record and (by default) RAISE a plan for approval.

    *queue* is a parameter rather than always-on because AC8's second half
    is exactly "an approving verdict over a plan nobody raised is still
    refused", and that case needs a recorded-but-unqueued plan.
    """
    segment = segment_page(_BODY)
    payload = {
        "groups": groups or [],
        "assignments": assignments,
        "merges": [],
        "page_actions": page_actions or [],
        "summary": _SUMMARY,
    }
    plan = validate_plan(
        payload,
        segment,
        _BODY,
        uid="page0001",
        content_hash=page_content_hash(_BODY),
        known_uids=["target01", "other001"],
    )
    plan_id = append_plan_record(plan, wiki)
    if queue:
        queue_plan_for_approval(
            wiki,
            plan_id=plan_id,
            plan=plan,
            outline=build_plan_outline(
                plan, segment.units, page_name="Acme", page_chars=len(_BODY)
            ),
        )
    return plan, plan_id


def _move_one_section(wiki: Path, **kwargs: Any) -> tuple[RecompilePlan, str]:
    """The canonical shape under test: the ``## Pricing`` section moves onto
    the existing ``Beta Corp`` page, everything else stays."""
    ids = _unit_ids()
    return _record_plan(
        wiki,
        assignments=[
            {"unit": ids[0], "disposition": "parent"},
            {"unit": ids[1], "disposition": "move:target01"},
            {"unit": ids[2], "disposition": "parent"},
            {"unit": ids[3], "disposition": "parent"},
        ],
        **kwargs,
    )


class _VerifyStubClient:
    """The minimum the applier's merge re-verification needs: one canned
    "every sentence supported" answer. The same stub shape
    ``tests/test_page_recompile_apply.py`` uses -- no network, no key."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = _VerifyStubMessages(self)


class _VerifyStubMessages:
    def __init__(self, client: _VerifyStubClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        self._client.calls.append(kwargs)
        return _VerifyStubResponse(
            json.dumps({"sentences": [{"index": 0, "verdict": "supported"}]})
        )


class _VerifyStubUsage:
    input_tokens = 11
    output_tokens = 7
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _VerifyStubBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _VerifyStubResponse:
    def __init__(self, text: str) -> None:
        self.content = [_VerifyStubBlock(text)]
        self.usage = _VerifyStubUsage()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


def _page_bytes(wiki: Path) -> dict[str, bytes]:
    """Every wiki PAGE's exact bytes -- ``*.md``, ``_``-prefixed infra files
    excluded (the ledger is append-only by design and gains records on both
    the apply and the undo, so a byte-identity claim over it would
    contradict AC10)."""
    return {p.name: p.read_bytes() for p in sorted(wiki.glob("*.md")) if not p.name.startswith("_")}


# ---------------------------------------------------------------------------
# AC1: `move:<existing uid>`.
# ---------------------------------------------------------------------------


class TestAC1MoveInsertsVerbatimBeforeTheFootnoteBlock:
    def test_the_unit_lands_verbatim_under_its_own_heading(self, wiki: Path) -> None:
        _source, target, _other = _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)
        assert not outcome.post_write_issues, outcome.post_write_issues

        _meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))
        # Its own heading, and ONLY its own heading: no wrapper heading was
        # added above it (the only `##` headings on the target are the ones
        # the moved unit itself brought).
        assert "## Pricing\n" in body
        assert [line for line in body.splitlines() if line.startswith("#")] == ["## Pricing"]
        # Verbatim apart from the renumbered marker, which AC1 permits.
        assert _PRICING.replace("[^src-2]", "[^src-2-2]") in body

    def test_it_is_inserted_before_the_targets_footnote_block(self, wiki: Path) -> None:
        _source, target, _other = _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok
        _meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))
        lines = body.splitlines()
        heading_at = lines.index("## Pricing")
        first_definition_at = next(
            index for index, line in enumerate(lines) if line.startswith("[^")
        )
        assert heading_at < first_definition_at

    def test_the_targets_frontmatter_identity_is_unchanged(self, wiki: Path) -> None:
        _source, target, _other = _seed(wiki)
        before_meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        _plan, plan_id = _move_one_section(wiki)

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok
        after_meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))

        # `updated` is bookkeeping and `needs_split` is AC4; every identity
        # field is byte-identical, and no key was invented or dropped.
        allowlist = {"updated", "needs_split"}
        for key in set(before_meta) | set(after_meta):
            if key in allowlist:
                continue
            assert after_meta.get(key) == before_meta.get(key), key
        assert after_meta["uid"] == "target01"
        assert after_meta["name"] == "Beta Corp"
        assert after_meta["type"] == "reference"
        assert after_meta["access"] == "internal"
        assert after_meta["aliases"] == ["BC Ltd"]

    def test_footnote_definitions_travel_and_are_renumbered_on_collision(
        self, wiki: Path
    ) -> None:
        _source, target, _other = _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok
        _meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))

        # The target's own `src-2` keeps its own definition...
        assert "[^src-2]: **Source:** target-doc" in body
        # ...and the incoming one travelled under a free label instead of
        # overwriting it or silently resolving to the wrong source.
        assert "[^src-2-2]: **Source:** doc-b" in body
        assert body.count("**Source:** doc-b") == 1

    def test_a_non_colliding_label_travels_unchanged(self, wiki: Path) -> None:
        """The other direction of the same clause: nothing is renumbered when
        nothing collides, so a moved unit's bytes are untouched."""
        _source, target, _other = _seed(wiki)
        ids = _unit_ids()
        _plan, plan_id = _record_plan(
            wiki,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "parent"},
                {"unit": ids[2], "disposition": "parent"},
                # `src-3` is free on the target.
                {"unit": ids[3], "disposition": "move:target01"},
            ],
        )

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok
        _meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert _BETA in body
        assert "[^src-3]: **Source:** doc-c" in body
        assert "src-3-2" not in body

    def test_nothing_already_on_the_target_is_rewritten(self, wiki: Path) -> None:
        """The issue's own out-of-scope boundary: "rewriting a target page's
        existing content"."""
        _source, target, _other = _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok
        _meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert "Beta Corp resells widgets in several European markets.[^src-2]" in body


# ---------------------------------------------------------------------------
# AC2: a wider target refuses the move.
# ---------------------------------------------------------------------------


class TestAC2WiderTargetAccessRefusesTheMove:
    def test_a_world_readable_target_is_refused(self, wiki: Path) -> None:
        _source, target, _other = _seed(wiki, target_extra={"access": "open"})
        before = target.read_bytes()
        _plan, plan_id = _move_one_section(wiki)

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["move-target-access-wider"]
        # A HARD refusal, not a warning: nothing was written anywhere.
        assert target.read_bytes() == before

    def test_a_coarser_access_level_is_refused(self, wiki: Path) -> None:
        _source, target, _other = _seed(
            wiki, source_extra={"access": "confidential"}, target_extra={"access": "internal"}
        )
        _plan, plan_id = _move_one_section(wiki)

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["move-target-access-wider"]

    def test_an_audience_role_the_source_lacks_is_refused(self, wiki: Path) -> None:
        """The fine-grained half a coarse `access:` comparison alone misses:
        both pages are `internal`, but the target grants a role."""
        _source, target, _other = _seed(wiki, target_extra={"audience": ["partner"]})
        _plan, plan_id = _move_one_section(wiki)

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["move-target-access-wider"]

    def test_an_equal_target_is_allowed(self, wiki: Path) -> None:
        """Positive control, without which every assertion above would also
        pass on a function that refused every move."""
        _source, _target, _other = _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)
        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

    def test_a_narrower_target_is_allowed(self, wiki: Path) -> None:
        _source, _target, _other = _seed(wiki, target_extra={"access": "confidential"})
        _plan, plan_id = _move_one_section(wiki)
        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

    def test_an_unreadable_target_access_fails_closed(self) -> None:
        """A target whose `access:` cannot be established is treated as
        WIDEST, so the move is refused rather than assumed safe."""
        assert access_widening_reason({"access": "internal"}, {}) != ""
        assert access_widening_reason({"access": "internal"}, {"access": ["internal"]}) != ""


# ---------------------------------------------------------------------------
# AC3: the relocation footnote, both directions.
# ---------------------------------------------------------------------------


class TestAC3RelocationFootnoteOnlyForUncitedUnits:
    def test_an_uncited_unit_gains_exactly_one_relocation_footnote(self, wiki: Path) -> None:
        source, target, _other = _seed(wiki)
        ids = _unit_ids()
        _plan, plan_id = _record_plan(
            wiki,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "parent"},
                # `## Session Notes` carries no citation at all.
                {"unit": ids[2], "disposition": "move:target01"},
                {"unit": ids[3], "disposition": "parent"},
            ],
        )

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok
        _meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))

        label = f"reloc-{plan_id}"
        assert body.count(f"[^{label}]") == 2  # one marker, one definition
        definition = f"[^{label}]: {relocation_note('Acme', 'page0001', plan_id)}"
        assert definition in body
        # It names the page it came from AND the plan id, not one or the other.
        assert "page0001" in definition and "Acme" in definition and plan_id in definition
        assert source.exists()

    def test_a_cited_unit_gains_nothing(self, wiki: Path) -> None:
        _source, target, _other = _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok
        _meta, body = parse_frontmatter(target.read_text(encoding="utf-8"))

        assert "reloc-" not in body
        assert "Relocated from" not in body

    def test_two_uncited_units_on_one_destination_share_one_definition(
        self, wiki: Path
    ) -> None:
        """Each unit still carries exactly one added marker; the fact being
        recorded is identical for both, so it is defined once."""
        body = (
            _LEAD + "\n\n"
            "## Note One\n\nAn uncited dated note.\n\n"
            "## Note Two\n\nA second uncited dated note.\n\n"
            "[^src-1]: **Source:** doc-a\n"
        )
        _write_page(wiki, uid="page0002", name="Acme Two", body=body)
        target = _write_page(wiki, uid="target02", name="Notes", body="Notes live here.\n")
        segment = segment_page(body)
        ids = [u.unit_id for u in segment.units]
        plan = validate_plan(
            {
                "groups": [],
                "assignments": [
                    {"unit": ids[0], "disposition": "parent"},
                    {"unit": ids[1], "disposition": "move:target02"},
                    {"unit": ids[2], "disposition": "move:target02"},
                ],
                "merges": [],
                "page_actions": [],
                "summary": _SUMMARY,
            },
            segment,
            body,
            uid="page0002",
            content_hash=page_content_hash(body),
            known_uids=["target02"],
        )
        plan_id = append_plan_record(plan, wiki)
        queue_plan_for_approval(
            wiki,
            plan_id=plan_id,
            plan=plan,
            outline=build_plan_outline(
                plan, segment.units, page_name="Acme Two", page_chars=len(body)
            ),
        )

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok
        _meta, target_body = parse_frontmatter(target.read_text(encoding="utf-8"))
        label = f"reloc-{plan_id}"
        assert target_body.count(f"[^{label}]:") == 1
        assert target_body.count(f"[^{label}]") == 3  # two markers, one definition


# ---------------------------------------------------------------------------
# AC4: an over-threshold target is flagged, never refused.
# ---------------------------------------------------------------------------


class TestAC4OversizedTargetIsFlaggedNotRefused:
    #: Below the size the target reaches after the move, above its size
    #: before -- so the flag is caused by THIS move and not by the fixture.
    _SMALL_THRESHOLD: dict[str, Any] = {
        "librarian": {
            "page_size_threshold_chars": 120,
            "page_recompile": {"min_parent_chars": 1},
        }
    }

    def test_the_move_is_not_refused_for_size(self, wiki: Path) -> None:
        _source, _target, _other = _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)

        outcome = write_apply(
            wiki, plan_id=plan_id, config=self._SMALL_THRESHOLD, verdict=VERDICT_APPROVE
        )

        assert outcome.ok is True, (outcome.reason, outcome.issues)

    def test_the_target_is_flagged_needs_split(self, wiki: Path) -> None:
        _source, target, _other = _seed(wiki)
        before_meta, before_body = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert "needs_split" not in before_meta
        assert len(before_body) < 120  # under the threshold before the move
        _plan, plan_id = _move_one_section(wiki)

        assert write_apply(
            wiki, plan_id=plan_id, config=self._SMALL_THRESHOLD, verdict=VERDICT_APPROVE
        ).ok

        after_meta, after_body = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert len(after_body) > 120
        assert after_meta["needs_split"] is True

    def test_the_flagged_target_becomes_a_candidate(self, wiki: Path) -> None:
        """"becomes a candidate" is a claim about the SCAN, so the scan is
        what answers it."""
        from athenaeum.page_recompile import find_recompile_candidates

        _source, _target, _other = _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)
        assert write_apply(
            wiki, plan_id=plan_id, config=self._SMALL_THRESHOLD, verdict=VERDICT_APPROVE
        ).ok

        scan = find_recompile_candidates(wiki, self._SMALL_THRESHOLD)
        found = {c.uid: c for c in scan.candidates}
        assert "target01" in found
        assert found["target01"].flagged is True

    def test_a_target_that_stays_under_the_threshold_is_not_flagged(self, wiki: Path) -> None:
        _source, target, _other = _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)
        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

        after_meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert "needs_split" not in after_meta


# ---------------------------------------------------------------------------
# AC5: `standalone:<group>` and the name-resolution conversion.
# ---------------------------------------------------------------------------


def _standalone_plan(wiki: Path, *, name: str, queue: bool = True) -> tuple[RecompilePlan, str]:
    ids = _unit_ids()
    return _record_plan(
        wiki,
        assignments=[
            {"unit": ids[0], "disposition": "parent"},
            {"unit": ids[1], "disposition": "parent"},
            {"unit": ids[2], "disposition": "standalone:g1"},
            {"unit": ids[3], "disposition": "parent"},
        ],
        groups=[
            {"key": "g1", "name": name, "description": "An unrelated incident.", "confidence": 0.8}
        ],
        queue=queue,
    )


def _page_by_uid(wiki: Path, uid: str) -> Path:
    path = EntityIndex(wiki).get_by_uid(uid)
    assert path is not None, uid
    return path


class TestAC5StandalonePageCreation:
    def test_it_creates_a_page_with_a_new_uid_a_name_and_a_type(self, wiki: Path) -> None:
        source, _target, _other = _seed(wiki)
        _plan, plan_id = _standalone_plan(wiki, name="Widget Logistics")

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)
        assert len(outcome.created_uids) == 1

        created_uid = outcome.created_uids[0]
        assert created_uid not in {"page0001", "target01", "other001"}
        meta, body = parse_frontmatter(
            _page_by_uid(wiki, created_uid).read_text(encoding="utf-8")
        )
        assert meta["name"] == "Widget Logistics"
        assert meta["type"] == "reference"
        assert _NOTES in body
        assert source.exists()

    def test_it_has_no_relation_to_the_source_page(self, wiki: Path) -> None:
        source, _target, _other = _seed(wiki)
        _plan, plan_id = _standalone_plan(wiki, name="Widget Logistics")
        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)

        created = _page_by_uid(wiki, outcome.created_uids[0])
        meta, body = parse_frontmatter(created.read_text(encoding="utf-8"))
        assert "parent" not in meta
        assert read_relations(meta).parent is None
        assert "page-relations:breadcrumb" not in body

        # And the source does not claim it as a child either -- a standalone
        # page is not a sub-page.
        source_meta, source_body = parse_frontmatter(source.read_text(encoding="utf-8"))
        assert [c.uid for c in read_relations(source_meta).children] == []
        assert "Sub-pages" not in source_body

    def test_its_type_is_validated_against_the_deployment_schema(self, wiki: Path) -> None:
        _source, _target, _other = _seed(wiki, source_extra={"type": "not-a-declared-type"})
        _plan, plan_id = _standalone_plan(wiki, name="Widget Logistics")

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["standalone-type-not-declared"]

    def test_a_type_declared_only_by_the_deployment_is_accepted(self, wiki: Path) -> None:
        """The registry half of "the deployment schema": a type absent from
        ``KNOWN_TYPES`` but present in ``wiki/_schema/types.md``."""
        _source, _target, _other = _seed(wiki, source_extra={"type": "widget-dossier"})
        schema = wiki / "_schema"
        schema.mkdir(parents=True, exist_ok=True)
        (schema / "types.md").write_text(
            "| type | notes |\n|---|---|\n| widget-dossier | A widget dossier |\n",
            encoding="utf-8",
        )
        _plan, plan_id = _standalone_plan(wiki, name="Widget Logistics")

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is True, (outcome.reason, outcome.issues)

    def test_a_group_with_no_name_is_refused(self, wiki: Path) -> None:
        _source, _target, _other = _seed(wiki)
        ids = _unit_ids()
        plan = RecompilePlan(
            uid="page0001",
            content_hash=page_content_hash(_BODY),
            dispositions=(
                (ids[0], "parent"),
                (ids[1], "parent"),
                (ids[2], "standalone:g1"),
                (ids[3], "parent"),
            ),
            groups=(),
            merges=(),
            page_actions=(),
            summary=_SUMMARY,
            scope=SCOPE_CROSS_PAGE,
            issues=(),
            verified=None,
        )
        plan_id = append_plan_record(plan, wiki)
        queue_plan_for_approval(
            wiki, plan_id=plan_id, plan=plan, outline={"page_name": "Acme"}
        )

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["standalone-missing-name"]


class TestAC5NameResolutionConvertsToAMove:
    """"The proposed name is resolved against existing names and aliases
    first; a hit converts the action to a move." The conversion lives in the
    PLANNER, so the recorded plan -- and the decision item an operator
    answers -- already states the action that will be taken."""

    def _resolved(self, wiki: Path, name: str) -> RecompilePlan:
        segment = segment_page(_BODY)
        ids = [u.unit_id for u in segment.units]
        plan = validate_plan(
            {
                "groups": [
                    {"key": "g1", "name": name, "description": "D.", "confidence": 0.8}
                ],
                "assignments": [
                    {"unit": ids[0], "disposition": "parent"},
                    {"unit": ids[1], "disposition": "standalone:g1"},
                    {"unit": ids[2], "disposition": "parent"},
                    {"unit": ids[3], "disposition": "parent"},
                ],
                "merges": [],
                "page_actions": [],
                "summary": _SUMMARY,
            },
            segment,
            _BODY,
            uid="page0001",
            content_hash=page_content_hash(_BODY),
            known_uids=["target01"],
        )
        return resolve_standalone_groups(plan, EntityIndex(wiki))

    def test_a_name_hit_becomes_a_move(self, wiki: Path) -> None:
        _seed(wiki)
        resolved = self._resolved(wiki, "Beta Corp")
        assert [d for _u, d in resolved.dispositions][1] == "move:target01"
        assert resolved.scope == SCOPE_CROSS_PAGE
        assert resolved.issues == ()

    def test_an_alias_hit_becomes_a_move(self, wiki: Path) -> None:
        _seed(wiki)
        resolved = self._resolved(wiki, "BC Ltd")
        assert [d for _u, d in resolved.dispositions][1] == "move:target01"

    def test_a_miss_stays_standalone(self, wiki: Path) -> None:
        _seed(wiki)
        resolved = self._resolved(wiki, "Widget Logistics")
        assert [d for _u, d in resolved.dispositions][1] == "standalone:g1"
        assert resolved.issues == ()

    def test_a_name_that_resolves_to_the_source_page_is_refused(self, wiki: Path) -> None:
        _seed(wiki)
        resolved = self._resolved(wiki, "Acme")
        assert [d for _u, d in resolved.dispositions][1] == "standalone:g1"
        assert [i.code for i in resolved.issues] == [STANDALONE_NAME_IS_SOURCE_CODE]
        assert resolved.valid is False

    def test_a_name_that_resolves_to_an_inactive_page_is_refused(self, wiki: Path) -> None:
        _seed(wiki)
        _write_page(
            wiki,
            uid="dead0001",
            name="Retired Topic",
            body="Nothing here.\n",
            retired=True,
        )
        resolved = self._resolved(wiki, "Retired Topic")
        assert [i.code for i in resolved.issues] == [STANDALONE_NAME_INACTIVE_CODE]
        assert resolved.valid is False

    def test_the_applier_refuses_rather_than_converting_a_second_time(
        self, wiki: Path
    ) -> None:
        """An operator approved "create a new page". If that name now
        resolves, the applier must not quietly write onto the existing page
        instead -- that is a different action from the one approved."""
        _seed(wiki)
        _plan, plan_id = _standalone_plan(wiki, name="Beta Corp")

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["standalone-name-already-exists"]


# ---------------------------------------------------------------------------
# AC6: `attach:` and `attach-self-under:`.
# ---------------------------------------------------------------------------


def _all_parent_plan(wiki: Path, *, page_actions: list[str]) -> tuple[RecompilePlan, str]:
    return _record_plan(
        wiki,
        assignments=[{"unit": unit_id, "disposition": "parent"} for unit_id in _unit_ids()],
        page_actions=page_actions,
    )


class TestAC6AttachSetsTheRelationThroughTheWriter:
    def test_attach_makes_the_named_page_a_subpage_with_via_attach(self, wiki: Path) -> None:
        source, _target, other = _seed(wiki)
        _plan, plan_id = _all_parent_plan(wiki, page_actions=["attach:other001"])

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)
        assert not outcome.post_write_issues, outcome.post_write_issues

        other_meta, _ = parse_frontmatter(other.read_text(encoding="utf-8"))
        source_meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        assert read_relations(other_meta).parent is not None
        assert read_relations(other_meta).parent.uid == "page0001"  # type: ignore[union-attr]
        children = read_relations(source_meta).children
        assert [(c.uid, c.via) for c in children] == [("other001", "attach")]

    def test_attach_self_under_makes_this_page_a_subpage(self, wiki: Path) -> None:
        source, _target, other = _seed(wiki)
        _plan, plan_id = _all_parent_plan(wiki, page_actions=["attach-self-under:other001"])

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)

        source_meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        other_meta, _ = parse_frontmatter(other.read_text(encoding="utf-8"))
        assert read_relations(source_meta).parent.uid == "other001"  # type: ignore[union-attr]
        assert [(c.uid, c.via) for c in read_relations(other_meta).children] == [
            ("page0001", "attach")
        ]

    def test_it_changes_no_body_text_beyond_the_breadcrumb(self, wiki: Path) -> None:
        _source, _target, other = _seed(wiki)
        _meta, before_body = parse_frontmatter(other.read_text(encoding="utf-8"))
        _plan, plan_id = _all_parent_plan(wiki, page_actions=["attach:other001"])

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

        _meta, after_body = parse_frontmatter(other.read_text(encoding="utf-8"))
        from athenaeum.page_relations import strip_breadcrumb

        assert "page-relations:breadcrumb" in after_body
        assert strip_breadcrumb(after_body) == before_body

    def test_the_attached_page_is_not_renamed(self, wiki: Path) -> None:
        """The issue's out-of-scope boundary, asserted rather than assumed."""
        _source, _target, other = _seed(wiki)
        before_meta, _ = parse_frontmatter(other.read_text(encoding="utf-8"))
        _plan, plan_id = _all_parent_plan(wiki, page_actions=["attach:other001"])

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

        after_meta, _ = parse_frontmatter(other.read_text(encoding="utf-8"))
        assert after_meta["name"] == before_meta["name"] == "Gamma Ltd"
        assert after_meta["uid"] == "other001"

    def test_the_source_page_renders_a_sub_pages_section(self, wiki: Path) -> None:
        source, _target, _other = _seed(wiki)
        _plan, plan_id = _all_parent_plan(wiki, page_actions=["attach:other001"])
        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

        _meta, body = parse_frontmatter(source.read_text(encoding="utf-8"))
        assert "## Sub-pages" in body
        assert "[[other001|Gamma Ltd]]" in body

    def test_an_attach_onto_an_inactive_page_is_refused_before_any_write(
        self, wiki: Path
    ) -> None:
        source, _target, _other = _seed(wiki)
        _write_page(
            wiki, uid="dead0001", name="Retired", body="Gone.\n", retired=True
        )
        before = source.read_bytes()
        _plan, plan_id = _all_parent_plan(wiki, page_actions=["attach-self-under:dead0001"])

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["attach-parent-inactive"]
        assert source.read_bytes() == before

    def test_an_attach_self_under_while_also_splitting_is_refused(self, wiki: Path) -> None:
        """Three nesting levels: the pre-check has to see the children this
        same apply is about to create, which disk cannot yet show."""
        source, _target, _other = _seed(wiki)
        before = source.read_bytes()
        ids = _unit_ids()
        _plan, plan_id = _record_plan(
            wiki,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "child:g1"},
                {"unit": ids[2], "disposition": "parent"},
                {"unit": ids[3], "disposition": "parent"},
            ],
            groups=[
                {"key": "g1", "name": "Pricing", "description": "Prices.", "confidence": 0.9}
            ],
            page_actions=["attach-self-under:other001"],
        )

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["attach-would-nest-three-levels"]
        assert source.read_bytes() == before

    def test_an_attach_naming_an_unknown_page_is_refused(self, wiki: Path) -> None:
        _seed(wiki)
        ids = _unit_ids()
        plan = RecompilePlan(
            uid="page0001",
            content_hash=page_content_hash(_BODY),
            dispositions=tuple((unit_id, "parent") for unit_id in ids),
            groups=(),
            merges=(),
            page_actions=("attach:nosuchuid",),
            summary=_SUMMARY,
            scope=SCOPE_CROSS_PAGE,
            issues=(),
            verified=None,
        )
        plan_id = append_plan_record(plan, wiki)
        queue_plan_for_approval(wiki, plan_id=plan_id, plan=plan, outline={})

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["attach-unknown-page"]


# ---------------------------------------------------------------------------
# AC7: an attach-self-under-only plan leaves needs_split alone.
# ---------------------------------------------------------------------------


class TestAC7AttachSelfUnderOnlyLeavesNeedsSplitAsItWas:
    def test_a_flagged_page_stays_flagged(self, wiki: Path) -> None:
        source, _target, _other = _seed(wiki, source_extra={"needs_split": True})
        _plan, plan_id = _all_parent_plan(wiki, page_actions=["attach-self-under:other001"])

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)

        after_meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        assert after_meta["needs_split"] is True

    def test_an_unflagged_page_stays_unflagged(self, wiki: Path) -> None:
        source, _target, _other = _seed(wiki)
        _plan, plan_id = _all_parent_plan(wiki, page_actions=["attach-self-under:other001"])
        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

        after_meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        assert "needs_split" not in after_meta

    def test_a_plan_that_does_route_units_away_still_clears_the_flag(
        self, wiki: Path
    ) -> None:
        """The other direction: `needs_split` is re-evaluated exactly when
        content LEFT the page, which is what the flag is about."""
        source, _target, _other = _seed(wiki, source_extra={"needs_split": True})
        _plan, plan_id = _move_one_section(wiki)
        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

        after_meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        assert "needs_split" not in after_meta


# ---------------------------------------------------------------------------
# AC8: cross-page classing, and no apply without an approved item, in every
# mode.
# ---------------------------------------------------------------------------


class TestAC8CrossPageClassingAndTheApprovalInterlock:
    @pytest.mark.parametrize(
        ("dispositions", "page_actions"),
        [
            ([("u", "move:target01")], []),
            ([("u", "standalone:g1")], []),
            ([("u", "parent")], ["attach:target01"]),
            ([("u", "parent")], ["attach-self-under:target01"]),
            ([("u", "parent")], [f"{ALIAS_MOVE_VERB}:target01:Beta Metrics"]),
        ],
    )
    def test_each_of_the_four_actions_and_an_alias_move_is_cross_page(
        self, dispositions: list[tuple[str, str]], page_actions: list[str]
    ) -> None:
        assert classify_plan_scope(dispositions, page_actions) == SCOPE_CROSS_PAGE

    def test_a_structure_only_plan_is_still_structure_only(self) -> None:
        """Positive control: the classifier has not simply started saying
        cross-page to everything."""
        assert (
            classify_plan_scope(
                [("a", "parent"), ("b", "child:g1"), ("c", "merge:g1"), ("d", "duplicate:a")],
                [],
            )
            == SCOPE_STRUCTURE_ONLY
        )

    def test_the_alias_move_is_not_in_the_model_facing_vocabulary(self) -> None:
        """A model cannot propose an alias move: the packaged prompt does not
        teach it and ``validate_plan`` does not accept it, so AC12's default
        ("aliases stay on the parent") is the only behaviour the nightly pass
        can produce. The grammar exists for an operator-authored plan."""
        assert ALIAS_MOVE_VERB not in PAGE_ACTIONS
        prompt = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "athenaeum"
            / "prompts"
            / "page_recompile_plan.md"
        ).read_text(encoding="utf-8")
        assert ALIAS_MOVE_VERB not in prompt

    def test_parse_alias_move_reads_the_uid_and_the_alias(self) -> None:
        assert parse_alias_move(f"{ALIAS_MOVE_VERB}:abc12345:Beta Metrics") == (
            "abc12345",
            "Beta Metrics",
        )
        # A uid is 8 hex chars and never contains a colon, so an alias that
        # does still parses.
        assert parse_alias_move(f"{ALIAS_MOVE_VERB}:abc12345:Q3: metrics") == (
            "abc12345",
            "Q3: metrics",
        )
        assert parse_alias_move(f"{ALIAS_MOVE_VERB}:abc12345") is None
        assert parse_alias_move("attach:abc12345") is None

    @pytest.mark.parametrize("mode", ["off", "shadow", "queue", "auto"])
    def test_no_verdict_refuses_the_apply_in_every_mode(self, wiki: Path, mode: str) -> None:
        """The run mode has no say over whether an ANSWERED plan may be
        written, so the refusal must hold under all four -- `auto` included.
        """
        _source, target, _other = _seed(wiki)
        before = _page_bytes(wiki)
        _plan, plan_id = _move_one_section(wiki)
        config = {
            "librarian": {"page_recompile": {"min_parent_chars": 1, "mode": mode}}
        }

        outcome = write_apply(wiki, plan_id=plan_id, config=config)

        assert outcome.ok is False
        assert "approved page-recompile decision item" in outcome.reason
        assert _page_bytes(wiki) == before
        assert target.read_bytes() == before[target.name]

    def test_a_reject_verdict_refuses_the_apply(self, wiki: Path) -> None:
        _seed(wiki)
        before = _page_bytes(wiki)
        _plan, plan_id = _move_one_section(wiki)

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict="reject")

        assert outcome.ok is False
        assert _page_bytes(wiki) == before

    def test_an_approving_verdict_over_an_unraised_plan_is_refused(self, wiki: Path) -> None:
        """The second half of the interlock: a caller holding a verdict for
        a plan nobody was ever asked about still writes nothing."""
        _seed(wiki)
        before = _page_bytes(wiki)
        _plan, plan_id = _move_one_section(wiki, queue=False)

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert "no page-recompile decision item was ever raised" in outcome.reason
        assert _page_bytes(wiki) == before

    def test_the_cli_cannot_apply_a_cross_page_plan(self, wiki: Path) -> None:
        """``athenaeum recompile apply`` passes no verdict, which is how an
        operator naming a uid directly is kept from going behind the queue."""
        from athenaeum.cli import main as cli_main

        _seed(wiki)
        before = _page_bytes(wiki)
        _move_one_section(wiki)

        rc = cli_main(["recompile", "apply", "--uid", "page0001", "--path", str(wiki.parent)])

        assert rc == 1
        assert _page_bytes(wiki) == before

    def test_a_structure_only_plan_needs_no_verdict(self, wiki: Path) -> None:
        """Positive control for the whole interlock: it gates cross-page
        plans only, so the athenaeum#2540 path is untouched."""
        _seed(wiki)
        ids = _unit_ids()
        _plan, plan_id = _record_plan(
            wiki,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "child:g1"},
                {"unit": ids[2], "disposition": "parent"},
                {"unit": ids[3], "disposition": "parent"},
            ],
            groups=[
                {"key": "g1", "name": "Pricing", "description": "Prices.", "confidence": 0.9}
            ],
            queue=False,
        )

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG).ok


class TestAC8TheNightlyPhaseInModeAutoRaisesAndWritesNothing:
    """The explicit "test in mode ``auto``" the AC asks for, driven through
    the REAL nightly phase rather than a direct applier call: the phase is
    the only thing that runs unattended, so it is the only place a
    cross-page plan could ever be applied without an answer."""

    _THRESHOLD = 200

    def _config(self, mode: str) -> dict[str, Any]:
        return {
            "librarian": {
                "page_size_threshold_chars": self._THRESHOLD,
                "oversize_hard_ceiling_chars": 100_000,
                "page_recompile": {"min_parent_chars": 1, "mode": mode},
            }
        }

    @staticmethod
    def _client() -> Any:
        ids = _unit_ids()

        class _Block:
            type = "text"

            def __init__(self, text: str) -> None:
                self.text = text

        class _Usage:
            input_tokens = 11
            output_tokens = 7
            cache_creation_input_tokens = 0
            cache_read_input_tokens = 0

        class _Response:
            def __init__(self, text: str) -> None:
                self.content = [_Block(text)]
                self.usage = _Usage()

        labels = {
            "labels": [
                {
                    "unit": unit_id,
                    "topic": "widgets",
                    # The shortlist is built from these strings with no model
                    # call, so this is what puts `target01` in reach of a
                    # `move`.
                    "about": "Beta Corp",
                    "belongs": "elsewhere",
                    "kind": "fact",
                }
                for unit_id in ids
            ]
        }
        plan = {
            "groups": [],
            "assignments": [
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "move:target01"},
                {"unit": ids[2], "disposition": "parent"},
                {"unit": ids[3], "disposition": "parent"},
            ],
            "merges": [],
            "page_actions": [],
            "summary": _SUMMARY,
        }
        verify = {"sentences": [{"index": 0, "verdict": "supported"}]}

        class _Messages:
            def __init__(self, client: Any) -> None:
                self._client = client

            def create(self, **kwargs: Any) -> _Response:
                system = str(kwargs.get("system") or "")
                self._client.calls.append(kwargs)
                if "label" in system.lower()[:400] and "disposition" not in system:
                    payload: dict[str, Any] = labels
                elif "sentence" in system.lower() and "supported" in system.lower():
                    payload = verify
                else:
                    payload = plan
                return _Response(json.dumps(payload))

        class _Client:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []
                self.messages = _Messages(self)

        return _Client()

    def test_mode_auto_queues_the_cross_page_plan_and_writes_no_page(
        self, wiki: Path
    ) -> None:
        _seed(wiki)
        before = _page_bytes(wiki)

        summary = run_nightly_page_recompile(
            wiki, client=self._client(), config=self._config("auto")
        )

        assert summary.mode == "auto"
        assert summary.queued >= 1, (summary.to_dict(), summary.errors)
        assert len(list_pending_page_recompile_plans(wiki)) >= 1
        # The whole point: an item was raised and NOTHING was written.
        assert _page_bytes(wiki) == before

    def test_the_plan_it_recorded_is_classed_cross_page(self, wiki: Path) -> None:
        _seed(wiki)
        run_nightly_page_recompile(wiki, client=self._client(), config=self._config("auto"))

        items = list_pending_page_recompile_plans(wiki)
        assert items
        assert all(item.get("scope") == SCOPE_CROSS_PAGE for item in items)


# ---------------------------------------------------------------------------
# AC9: approve-structure-only.
# ---------------------------------------------------------------------------


class TestAC9ApproveStructureOnlyAppliesOnlyTheStructurePart:
    def _mixed_plan(self, wiki: Path) -> tuple[RecompilePlan, str]:
        ids = _unit_ids()
        return _record_plan(
            wiki,
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "child:g1"},
                {"unit": ids[2], "disposition": "move:target01"},
                {"unit": ids[3], "disposition": "standalone:g2"},
            ],
            groups=[
                {"key": "g1", "name": "Pricing", "description": "Prices.", "confidence": 0.9},
                {"key": "g2", "name": "Logistics", "description": "Shipping.", "confidence": 0.8},
            ],
            page_actions=["attach:other001"],
        )

    def test_the_in_page_split_is_applied(self, wiki: Path) -> None:
        _source, _target, _other = _seed(wiki)
        _plan, plan_id = self._mixed_plan(wiki)

        outcome = write_apply(
            wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE_STRUCTURE_ONLY
        )

        assert outcome.ok, (outcome.reason, outcome.issues)
        assert len(outcome.created_uids) == 1  # the `child:g1` sub-page only
        created_meta, _ = parse_frontmatter(
            _page_by_uid(wiki, outcome.created_uids[0]).read_text(encoding="utf-8")
        )
        assert created_meta["name"] == "Acme (Pricing)"

    def test_the_move_target_is_untouched(self, wiki: Path) -> None:
        _source, target, _other = _seed(wiki)
        before = target.read_bytes()
        _plan, plan_id = self._mixed_plan(wiki)

        assert write_apply(
            wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE_STRUCTURE_ONLY
        ).ok

        assert target.read_bytes() == before

    def test_the_other_units_are_left_on_the_parent(self, wiki: Path) -> None:
        source, _target, _other = _seed(wiki)
        _plan, plan_id = self._mixed_plan(wiki)

        assert write_apply(
            wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE_STRUCTURE_ONLY
        ).ok

        _meta, body = parse_frontmatter(source.read_text(encoding="utf-8"))
        assert _NOTES in body  # the `move:` unit stayed
        assert _BETA in body  # the `standalone:` unit stayed
        assert _PRICING not in body  # the `child:` unit left, as approved

    def test_the_attach_is_not_performed(self, wiki: Path) -> None:
        _source, _target, other = _seed(wiki)
        before = other.read_bytes()
        _plan, plan_id = self._mixed_plan(wiki)

        assert write_apply(
            wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE_STRUCTURE_ONLY
        ).ok

        assert other.read_bytes() == before

    def test_a_merge_is_still_verified_under_approve_structure_only(
        self, wiki: Path
    ) -> None:
        """``client`` and ``verdict`` are orthogonal arguments on the same
        applier call, and both are load-bearing: the client re-verifies a
        ``merge`` group's model-written text (athenaeum#2547's
        ``_page_recompile_verify_client``), the verdict decides WHICH parts
        of the plan are applied. ``merge`` is a structure-only verb, so a
        plan that both merges and moves must still verify the merge under
        ``approve-structure-only`` -- routing THROUGH the verify seam, not
        around it -- while refusing the move.
        """
        merge_body = (
            _LEAD + "\n\n"
            "## Pricing A\n\nWidgets cost ten pounds each across the catalogue.[^src-2]\n\n"
            "## Pricing B\n\nWidget pricing is twelve euros per unit in the euro zone.[^src-3]\n\n"
            "## Session Notes\n\n" + _NOTES + "\n\n"
            "[^src-1]: **Source:** doc-a\n"
            "[^src-2]: **Source:** doc-b\n"
            "[^src-3]: **Source:** doc-c\n"
        )
        _write_page(wiki, uid="page0003", name="Acme Three", body=merge_body)
        target = _write_page(wiki, uid="target03", name="Notes", body="Notes live here.\n")
        segment = segment_page(merge_body)
        ids = [u.unit_id for u in segment.units]
        merged_text = "Widgets cost ten pounds, or twelve euros in the euro zone.[^src-2][^src-3]"
        plan = validate_plan(
            {
                "groups": [
                    {"key": "g1", "name": "Pricing", "description": "Prices.", "confidence": 0.9}
                ],
                "assignments": [
                    {"unit": ids[0], "disposition": "parent"},
                    {"unit": ids[1], "disposition": "merge:g1"},
                    {"unit": ids[2], "disposition": "merge:g1"},
                    {"unit": ids[3], "disposition": "move:target03"},
                ],
                "merges": [{"group": "g1", "text": merged_text}],
                "page_actions": [],
                "summary": _SUMMARY,
            },
            segment,
            merge_body,
            uid="page0003",
            content_hash=page_content_hash(merge_body),
            known_uids=["target03"],
        )
        assert plan.issues == ()
        plan_id = append_plan_record(plan, wiki)
        queue_plan_for_approval(
            wiki,
            plan_id=plan_id,
            plan=plan,
            outline=build_plan_outline(
                plan, segment.units, page_name="Acme Three", page_chars=len(merge_body)
            ),
        )
        target_before = target.read_bytes()
        client = _VerifyStubClient()

        outcome = write_apply(
            wiki,
            plan_id=plan_id,
            client=client,
            config=_CONFIG,
            verdict=VERDICT_APPROVE_STRUCTURE_ONLY,
        )

        assert outcome.ok, (outcome.reason, outcome.issues)
        # The verify call really was made -- with no client the applier
        # records `merge-verify-unavailable` and refuses the whole plan.
        assert client.calls
        assert len(outcome.created_uids) == 1
        _meta, created_body = parse_frontmatter(
            _page_by_uid(wiki, outcome.created_uids[0]).read_text(encoding="utf-8")
        )
        assert merged_text in created_body
        # ...and the move was still refused: the target is untouched and the
        # unit stayed on the parent.
        assert target.read_bytes() == target_before
        _meta, parent_body = parse_frontmatter(
            (wiki / "page0003.md").read_text(encoding="utf-8")
        )
        assert _NOTES in parent_body

    def test_plain_approve_does_apply_the_cross_page_part(self, wiki: Path) -> None:
        """The contrast that makes the verdict mean something."""
        _source, target, other = _seed(wiki)
        target_before = target.read_bytes()
        other_before = other.read_bytes()
        _plan, plan_id = self._mixed_plan(wiki)

        outcome = write_apply(
            wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE
        )

        assert outcome.ok, (outcome.reason, outcome.issues)
        assert len(outcome.created_uids) == 2  # sub-page AND standalone page
        assert target.read_bytes() != target_before
        assert other.read_bytes() != other_before


# ---------------------------------------------------------------------------
# AC10: conservation and the ledger.
# ---------------------------------------------------------------------------


class TestAC10ConservationAndTheLedger:
    def test_conservation_covers_a_move_target(self, wiki: Path) -> None:
        """A unit routed to a move target is accounted for THERE -- so a
        plan that forgets a different unit is still caught."""
        _seed(wiki)
        ids = _unit_ids()
        plan = RecompilePlan(
            uid="page0001",
            content_hash=page_content_hash(_BODY),
            dispositions=(
                (ids[0], "parent"),
                (ids[1], "move:target01"),
                (ids[2], "parent"),
                # ids[3] is never assigned.
            ),
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

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [(i.code, i.detail) for i in outcome.issues] == [
            ("unit-not-accounted-for", ids[3])
        ]

    def test_conservation_covers_a_standalone_page(self, wiki: Path) -> None:
        _seed(wiki)
        ids = _unit_ids()
        plan = RecompilePlan(
            uid="page0001",
            content_hash=page_content_hash(_BODY),
            dispositions=(
                (ids[0], "parent"),
                (ids[1], "standalone:g1"),
                (ids[2], "parent"),
                # The SAME unit twice across two destinations.
                (ids[1], "parent"),
            ),
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

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        codes = {i.code for i in outcome.issues}
        assert "unit-accounted-for-twice" in codes

    def test_a_conserving_cross_page_plan_is_accepted(self, wiki: Path) -> None:
        _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)
        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

    def test_the_ledger_records_every_target_page_with_pre_and_post_hash(
        self, wiki: Path
    ) -> None:
        _source, target, _other = _seed(wiki)
        _meta, target_body_before = parse_frontmatter(target.read_text(encoding="utf-8"))
        _plan, plan_id = _standalone_and_move_plan(wiki)

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)

        record = find_apply_record(plan_id, wiki)
        assert record is not None
        rows = {row["uid"]: row for row in record["pages"]}

        # The recompiled page, the move target and the created standalone
        # page are all present.
        assert "page0001" in rows
        assert "target01" in rows
        standalone_uid = next(
            uid for uid in outcome.created_uids if uid not in {"page0001", "target01"}
        )
        assert standalone_uid in rows

        target_row = rows["target01"]
        assert target_row["pre_content_hash"] == page_content_hash(target_body_before)
        _meta_after, target_body_after = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert target_row["post_content_hash"] == page_content_hash(target_body_after)
        assert target_row["pre_blob_id"]
        assert target_row["units"]

        created_row = rows[standalone_uid]
        assert created_row["pre_content_hash"] == ""
        assert created_row["post_content_hash"]
        assert standalone_uid in record["created_uids"]

    def test_the_recorded_pre_blob_id_is_what_git_hash_object_produces(
        self, wiki: Path
    ) -> None:
        """The blob id has to name bytes git actually holds, or an undo
        degrades to a queued item for no reason (athenaeum#2541's lesson,
        re-asserted for the new target rows)."""
        _source, target, _other = _seed(wiki)
        pre_bytes = target.read_bytes()
        _plan, plan_id = _move_one_section(wiki)
        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

        record = find_apply_record(plan_id, wiki)
        assert record is not None
        row = next(r for r in record["pages"] if r["uid"] == "target01")
        expected = subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=pre_bytes,
            capture_output=True,
            check=True,
        ).stdout.decode()
        assert row["pre_blob_id"] == expected.strip()

    def test_the_ledger_names_the_attachments_and_alias_moves(self, wiki: Path) -> None:
        _seed(wiki)
        _plan, plan_id = _all_parent_plan(
            wiki, page_actions=["attach:other001", f"{ALIAS_MOVE_VERB}:target01:Beta Metrics"]
        )
        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)

        record = find_apply_record(plan_id, wiki)
        assert record is not None
        assert record["attachments"] == [
            {
                "child": "other001",
                "parent": "page0001",
                "via": "attach",
                "action": "attach:other001",
            }
        ]
        assert record["alias_moves"] == [{"alias": "Beta Metrics", "target": "target01"}]
        # Both pages the attach wrote are recorded too, so an undo can see
        # whether either has been edited since.
        assert {"page0001", "other001", "target01"} <= {r["uid"] for r in record["pages"]}


def _standalone_and_move_plan(wiki: Path) -> tuple[RecompilePlan, str]:
    ids = _unit_ids()
    return _record_plan(
        wiki,
        assignments=[
            {"unit": ids[0], "disposition": "parent"},
            {"unit": ids[1], "disposition": "move:target01"},
            {"unit": ids[2], "disposition": "standalone:g1"},
            {"unit": ids[3], "disposition": "parent"},
        ],
        groups=[
            {"key": "g1", "name": "Widget Logistics", "description": "Shipping.", "confidence": 0.8}
        ],
    )


# ---------------------------------------------------------------------------
# AC11: undo.
# ---------------------------------------------------------------------------


class TestAC11UndoCoversTheseActions:
    """Every wiki here is a REAL git repository with a REAL pre-apply commit
    -- the only thing that makes a pre-image blob readable."""

    def _applied(
        self, wiki: Path, *, page_actions: list[str] | None = None
    ) -> tuple[str, dict[str, bytes]]:
        _init_git_repo(wiki.parent)
        _seed(wiki)
        _git(wiki.parent, "add", "-A")
        _git(wiki.parent, "commit", "-m", "seed")
        before = _page_bytes(wiki)

        if page_actions is None:
            _plan, plan_id = _standalone_and_move_plan(wiki)
        else:
            _plan, plan_id = _all_parent_plan(wiki, page_actions=page_actions)
        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)
        assert not outcome.post_write_issues, outcome.post_write_issues
        assert outcome.snapshot_sha
        return plan_id, before

    def test_a_move_round_trips_byte_for_byte(self, wiki: Path) -> None:
        plan_id, before = self._applied(wiki)
        assert _page_bytes(wiki) != before

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert _page_bytes(wiki) == before

    def test_moved_units_are_removed_from_the_target(self, wiki: Path) -> None:
        plan_id, _before = self._applied(wiki)
        target = wiki / "target01.md"
        assert _PRICING.replace("[^src-2]", "[^src-2-2]") in target.read_text(encoding="utf-8")

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert "target01" in outcome.restored_uids
        body = target.read_text(encoding="utf-8")
        assert "## Pricing" not in body
        assert "src-2-2" not in body

    def test_a_created_standalone_page_is_removed(self, wiki: Path) -> None:
        plan_id, _before = self._applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        created = [uid for uid in record["created_uids"]]
        assert created
        created_paths = [
            Path(row["path"]) for row in record["pages"] if row["uid"] in created
        ]
        assert all(p.is_file() for p in created_paths)

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert set(outcome.deleted_uids) == set(created)
        assert not any(p.exists() for p in created_paths)

    def test_an_attach_is_undone_by_a_detach(self, wiki: Path) -> None:
        plan_id, before = self._applied(wiki, page_actions=["attach:other001"])
        other_meta, _ = parse_frontmatter((wiki / "other001.md").read_text(encoding="utf-8"))
        assert read_relations(other_meta).parent is not None

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        after_meta, after_body = parse_frontmatter(
            (wiki / "other001.md").read_text(encoding="utf-8")
        )
        assert read_relations(after_meta).parent is None
        assert "page-relations:breadcrumb" not in after_body
        source_meta, _ = parse_frontmatter((wiki / "page0001.md").read_text(encoding="utf-8"))
        assert [c.uid for c in read_relations(source_meta).children] == []
        assert _page_bytes(wiki) == before

    def test_an_alias_move_is_reversed(self, wiki: Path) -> None:
        plan_id, before = self._applied(
            wiki, page_actions=[f"{ALIAS_MOVE_VERB}:target01:Beta Metrics"]
        )
        source_meta, _ = parse_frontmatter((wiki / "page0001.md").read_text(encoding="utf-8"))
        assert source_meta["aliases"] == ["Acme Corp"]

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert _page_bytes(wiki) == before

    def test_a_target_edited_since_the_apply_goes_to_the_queued_path(
        self, wiki: Path
    ) -> None:
        plan_id, _before = self._applied(wiki)
        target = wiki / "target01.md"
        target.write_text(
            target.read_text(encoding="utf-8") + "\nA human edit.\n", encoding="utf-8"
        )
        after_edit = target.read_bytes()

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_QUEUED
        assert [b.uid for b in outcome.blocked] == ["target01"]
        assert outcome.blocked[0].reason == REASON_PAGE_CHANGED
        assert outcome.blocked[0].diff  # AC3 of athenaeum#2541: a real diff
        # Nothing was written, the edit included.
        assert target.read_bytes() == after_edit


# ---------------------------------------------------------------------------
# AC12: aliases.
# ---------------------------------------------------------------------------


class TestAC12AliasesStayOnTheParent:
    def test_a_move_leaves_every_alias_on_the_parent(self, wiki: Path) -> None:
        source, target, _other = _seed(wiki)
        _plan, plan_id = _move_one_section(wiki)

        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

        source_meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        target_meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert source_meta["aliases"] == ["Acme Corp", "Beta Metrics"]
        assert target_meta["aliases"] == ["BC Ltd"]

    def test_a_standalone_page_takes_no_alias(self, wiki: Path) -> None:
        source, _target, _other = _seed(wiki)
        _plan, plan_id = _standalone_plan(wiki, name="Widget Logistics")
        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)

        source_meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        created_meta, _ = parse_frontmatter(
            _page_by_uid(wiki, outcome.created_uids[0]).read_text(encoding="utf-8")
        )
        assert source_meta["aliases"] == ["Acme Corp", "Beta Metrics"]
        assert "aliases" not in created_meta

    def test_an_attach_leaves_every_alias_on_the_parent(self, wiki: Path) -> None:
        source, _target, _other = _seed(wiki)
        _plan, plan_id = _all_parent_plan(wiki, page_actions=["attach:other001"])
        assert write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE).ok

        source_meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        assert source_meta["aliases"] == ["Acme Corp", "Beta Metrics"]

    def test_a_listed_alias_move_does_move_it(self, wiki: Path) -> None:
        source, target, _other = _seed(wiki)
        _plan, plan_id = _all_parent_plan(
            wiki, page_actions=[f"{ALIAS_MOVE_VERB}:target01:Beta Metrics"]
        )

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)
        assert outcome.ok, (outcome.reason, outcome.issues)

        source_meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        target_meta, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
        assert source_meta["aliases"] == ["Acme Corp"]
        assert target_meta["aliases"] == ["BC Ltd", "Beta Metrics"]

    def test_an_alias_move_naming_an_alias_the_page_lacks_is_refused(
        self, wiki: Path
    ) -> None:
        source, _target, _other = _seed(wiki)
        before = source.read_bytes()
        _plan, plan_id = _all_parent_plan(
            wiki, page_actions=[f"{ALIAS_MOVE_VERB}:target01:Not An Alias"]
        )

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["alias-move-alias-not-on-the-page"]
        assert source.read_bytes() == before

    def test_an_alias_move_onto_a_wider_page_is_refused(self, wiki: Path) -> None:
        """The AC2 access property applies to an alias as much as to a unit:
        an alias is a way of REACHING content."""
        source, _target, _other = _seed(wiki, target_extra={"access": "open"})
        before = source.read_bytes()
        _plan, plan_id = _all_parent_plan(
            wiki, page_actions=[f"{ALIAS_MOVE_VERB}:target01:Beta Metrics"]
        )

        outcome = write_apply(wiki, plan_id=plan_id, config=_CONFIG, verdict=VERDICT_APPROVE)

        assert outcome.ok is False
        assert [i.code for i in outcome.issues] == ["alias-move-target-access-wider"]
        assert source.read_bytes() == before


# ---------------------------------------------------------------------------
# AC13: the CHANGELOG entry.
# ---------------------------------------------------------------------------


class TestAC13ChangelogEntry:
    def test_the_unreleased_section_names_this_slice(self) -> None:
        changelog = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(
            encoding="utf-8"
        )
        unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## ", 1)[0]
        assert "page recompile" in unreleased.lower()
        for phrase in ("move", "standalone", "attach"):
            assert phrase in unreleased.lower(), phrase
        assert "athenaeum#2548" in unreleased
