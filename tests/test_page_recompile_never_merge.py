"""Never-merge rulings survive page splits (issue athenaeum#2651).

* AC1 -- a sub-page created by a split inherits every ``merge_rejected_with``
  entry its parent held, so the wiki-page clusterer refuses to cluster it
  with the page the parent was ruled distinct from.
* AC2 -- a queued plan that moves units onto / attaches a page ruled distinct
  from the source shows that ruling in the ``page-recompile`` decision item
  and stays approvable (a flag, not a refusal).
* AC3 -- undoing the split removes the sub-pages together with the inherited
  links (round trip).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from athenaeum.decisions import page_recompile_plan_to_decision
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.page_recompile import segment_page
from athenaeum.page_recompile_apply import find_never_merge_rulings, write_apply
from athenaeum.page_recompile_plan import append_plan_record, page_content_hash, validate_plan
from athenaeum.page_recompile_queue import build_plan_outline, queue_plan_for_approval
from athenaeum.page_recompile_undo import write_undo
from athenaeum.wiki_dedupe import find_wiki_page_clusters

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
_GROUPS = [{"key": "g1", "name": "Pricing", "description": "Pricing details", "confidence": 0.9}]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "wiki").mkdir()
    return root / "wiki"


def _write_page(wiki: Path, *, uid: str, name: str, body: str, **extra: Any) -> Path:
    meta: dict[str, Any] = {"uid": uid, "type": "reference", "name": name, "access": "internal"}
    meta.update(extra)
    path = wiki / f"{uid}.md"
    path.write_text(render_frontmatter(meta) + "\n" + body, encoding="utf-8")
    return path


def _commit_all(wiki: Path) -> None:
    _git(wiki.parent, "add", "-A")
    _git(wiki.parent, "commit", "-m", "seed")


def _validated(
    *,
    uid: str,
    assignments: list[dict[str, str]],
    page_actions: list[str] | None = None,
    known_uids: tuple[str, ...] = (),
):
    segment = segment_page(_BODY)
    plan = validate_plan(
        {
            "groups": _GROUPS,
            "assignments": assignments,
            "merges": [],
            "page_actions": page_actions or [],
            "summary": _SUMMARY,
        },
        segment,
        _BODY,
        uid=uid,
        content_hash=page_content_hash(_BODY),
        known_uids=known_uids,
    )
    assert plan.valid, plan.issues
    return plan


def _split_assignments() -> list[dict[str, str]]:
    ids = [u.unit_id for u in segment_page(_BODY).units]
    return [
        {"unit": ids[0], "disposition": "parent"},
        {"unit": ids[1], "disposition": "child:g1"},
        {"unit": ids[2], "disposition": "parent"},
    ]


def _split(wiki: Path, **parent_extra: Any) -> Any:
    _write_page(wiki, uid="page0001", name="Acme", body=_BODY, **parent_extra)
    _write_page(wiki, uid="page0002", name="Acme Holdings", body="Unrelated holding company.")
    _commit_all(wiki)
    plan_id = append_plan_record(_validated(uid="page0001", assignments=_split_assignments()), wiki)
    outcome = write_apply(wiki, plan_id=plan_id, client=None, config=None)
    assert outcome.ok, (outcome.reason, outcome.issues)
    return plan_id, outcome


def _sub_paths(wiki: Path, outcome: Any) -> list[Path]:
    created = set(outcome.created_uids)
    return [
        p
        for p in wiki.glob("*.md")
        if parse_frontmatter(p.read_text(encoding="utf-8"))[0].get("uid") in created
    ]


def _same_vector(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0, 0.0] for _ in texts]


class TestAC1SubPagesInheritTheRuling:
    def test_every_sub_page_carries_the_parents_rejections(self, wiki: Path) -> None:
        _plan_id, outcome = _split(wiki, merge_rejected_with=["Acme Holdings", "Other Co"])
        subs = _sub_paths(wiki, outcome)
        assert subs
        for path in subs:
            meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            assert meta["merge_rejected_with"] == ["Acme Holdings", "Other Co"]
        parent_meta, _ = parse_frontmatter((wiki / "page0001.md").read_text(encoding="utf-8"))
        assert parent_meta["merge_rejected_with"] == ["Acme Holdings", "Other Co"]

    def test_a_parent_with_no_rulings_gives_sub_pages_none(self, wiki: Path) -> None:
        _plan_id, outcome = _split(wiki)
        for path in _sub_paths(wiki, outcome):
            meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            assert "merge_rejected_with" not in meta

    def test_the_clusterer_refuses_to_cluster_a_sub_page_with_the_ruled_page(
        self, wiki: Path
    ) -> None:
        _plan_id, outcome = _split(wiki, merge_rejected_with=["Acme Holdings"])
        subs = {p.name for p in _sub_paths(wiki, outcome)}
        assert subs
        clusters = find_wiki_page_clusters(wiki, threshold=0.9, embedding_provider=_same_vector)
        for cluster in clusters:
            members = {Path(m).name for m in cluster.member_paths}
            assert not (members & subs and "page0002.md" in members), members

    def test_positive_control_without_the_ruling_the_sub_page_clusters(self, wiki: Path) -> None:
        _plan_id, outcome = _split(wiki)
        subs = {p.name for p in _sub_paths(wiki, outcome)}
        clusters = find_wiki_page_clusters(wiki, threshold=0.9, embedding_provider=_same_vector)
        assert any(
            {Path(m).name for m in c.member_paths} & subs
            and "page0002.md" in {Path(m).name for m in c.member_paths}
            for c in clusters
        )


class TestAC2CrossPageMovesAreFlagged:
    def _stub_plan(self, wiki: Path):
        ids = [u.unit_id for u in segment_page(_BODY).units]
        return _validated(
            uid="page0001",
            assignments=[
                {"unit": ids[0], "disposition": "parent"},
                {"unit": ids[1], "disposition": "move:page0002"},
                {"unit": ids[2], "disposition": "parent"},
            ],
            page_actions=["attach:page0003"],
            known_uids=("page0002", "page0003"),
        )

    def test_a_ruled_target_is_flagged_in_the_decision_item_and_stays_approvable(
        self, wiki: Path
    ) -> None:
        source = _write_page(
            wiki, uid="page0001", name="Acme", body=_BODY, merge_rejected_with=["Acme Holdings"]
        )
        _write_page(wiki, uid="page0002", name="Acme Holdings", body="Holding company.")
        _write_page(wiki, uid="page0003", name="Widget Guide", body="A guide.")
        plan = self._stub_plan(wiki)
        meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))

        rulings = find_never_merge_rulings(plan, wiki, meta)
        assert [r["target_uid"] for r in rulings] == ["page0002"]
        assert rulings[0]["target_name"] == "Acme Holdings"
        assert rulings[0]["via"] == ["move"]

        outline = build_plan_outline(plan, segment_page(_BODY).units, never_merge_rulings=rulings)
        record = queue_plan_for_approval(wiki, plan_id="plan-x", plan=plan, outline=outline)
        item = page_recompile_plan_to_decision(record)

        assert item["payload"]["outline"]["never_merge_rulings"] == rulings
        assert "Acme Holdings" in item["summary"]
        assert "never-merge" in item["summary"]
        # Flag, not refusal: the item is a normal approvable page-recompile item.
        assert item["type"] == "page-recompile"

    def test_the_ruling_is_found_from_the_targets_side_and_for_attach(self, wiki: Path) -> None:
        source = _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _write_page(wiki, uid="page0002", name="Acme Holdings", body="x")
        _write_page(
            wiki, uid="page0003", name="Widget Guide", body="y", merge_rejected_with=["Acme"]
        )
        plan = self._stub_plan(wiki)
        meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        rulings = find_never_merge_rulings(plan, wiki, meta)
        assert [(r["target_uid"], r["via"]) for r in rulings] == [("page0003", ["attach"])]

    def test_no_ruling_means_no_flag(self, wiki: Path) -> None:
        source = _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        _write_page(wiki, uid="page0002", name="Acme Holdings", body="x")
        _write_page(wiki, uid="page0003", name="Widget Guide", body="y")
        meta, _ = parse_frontmatter(source.read_text(encoding="utf-8"))
        assert find_never_merge_rulings(self._stub_plan(wiki), wiki, meta) == []
        outline = build_plan_outline(self._stub_plan(wiki), segment_page(_BODY).units)
        assert outline["never_merge_rulings"] == []


class TestAC3UndoRemovesInheritedLinks:
    def test_undo_removes_sub_pages_and_their_inherited_links(self, wiki: Path) -> None:
        plan_id, outcome = _split(wiki, merge_rejected_with=["Acme Holdings"])
        subs = _sub_paths(wiki, outcome)
        assert subs and all(p.is_file() for p in subs)
        parent_before_undo = (wiki / "page0001.md").read_text(encoding="utf-8")
        assert "Acme Holdings" in subs[0].read_text(encoding="utf-8")

        undone = write_undo(wiki, plan_id=plan_id, config=None)
        assert undone.action == "direct", (undone.reason, undone.blocked)

        assert not any(p.exists() for p in subs)
        for path in wiki.glob("*.md"):
            uid = parse_frontmatter(path.read_text(encoding="utf-8"))[0].get("uid")
            assert uid in {"page0001", "page0002"}
        # The parent keeps its own ruling after the round trip.
        parent_meta, _ = parse_frontmatter((wiki / "page0001.md").read_text(encoding="utf-8"))
        assert parent_meta["merge_rejected_with"] == ["Acme Holdings"]
        assert (wiki / "page0001.md").read_text(encoding="utf-8") != parent_before_undo
