# SPDX-License-Identifier: Apache-2.0
"""Undo an applied page-recompile plan (issue athenaeum#2541, slice S5 of
athenaeum#2525).

One class per acceptance criterion, named for the criterion it proves --
the same shape ``tests/test_page_recompile_apply.py`` uses, with the same
helpers: plans are built through
:func:`athenaeum.page_recompile_plan.validate_plan` over a REAL
:func:`athenaeum.page_recompile.segment_page` result (so unit ids are never
hardcoded) and recorded with
:func:`athenaeum.page_recompile_plan.append_plan_record`, and every apply
runs the real :func:`athenaeum.page_recompile_apply.write_apply`. A model
call only ever happens for a merge group's verify step, through the same
stub-client pattern -- no network, no key.

**Every wiki in this module is a REAL git repository with a REAL pre-apply
commit**, because that is the only thing that makes a pre-image blob
readable: undo restores pages by ``git cat-file blob <id>`` against the
knowledge store's own object store.
``TestAC3ChangedPageQueuesADiff`` holds both controls for that -- the
positive one (the blob IS readable after the apply's own later snapshot
commit, which is what makes every direct-undo test here meaningful) and the
negative one (a wiki with no pre-apply commit queues instead of writing).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from athenaeum.cli import main as cli_main
from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.page_recompile import segment_page
from athenaeum.page_recompile_apply import write_apply
from athenaeum.page_recompile_plan import (
    UNDO_KIND,
    append_plan_record,
    page_content_hash,
    plan_page,
    undone_page_skip_reason,
    validate_plan,
)
from athenaeum.page_recompile_undo import (
    REASON_ALREADY_UNDONE,
    REASON_NO_APPLY_RECORD,
    REASON_PAGE_CHANGED,
    REASON_PAGE_MISSING,
    REASON_PRE_IMAGE_UNREADABLE,
    UNDO_DIRECT,
    UNDO_QUEUED,
    UNDO_REFUSED,
    compute_undo,
    find_apply_record,
    find_inbound_links,
    last_action_for_plan,
    read_git_blob,
    write_undo,
)
from athenaeum.page_relations import read_relations

# ---------------------------------------------------------------------------
# Stub client -- identical shape to tests/test_page_recompile_apply.py's.
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
        payload = self._client.responses.pop(0) if self._client.responses else self._client.default
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


# ---------------------------------------------------------------------------
# Fixtures: the same page shape the applier's own tests use.
# ---------------------------------------------------------------------------

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

#: A body whose two ``## Pricing`` sections are near-duplicates -- the AC2
#: merge shape. After an apply the originals are live on NO page, which is
#: exactly what makes "back byte for byte" a real claim.
_MERGE_LEAD = _LEAD
_PRICING_A = "Widgets cost ten pounds each across the whole catalogue.[^src-2]"
_PRICING_B = "Widget pricing is twelve euros per unit in the euro zone.[^src-3]"
_MERGE_BODY = (
    _MERGE_LEAD + "\n\n"
    "## Pricing A\n\n" + _PRICING_A + "\n\n"
    "## Pricing B\n\n" + _PRICING_B + "\n\n"
    "[^src-1]: **Source:** doc-a\n"
    "[^src-2]: **Source:** doc-b\n"
    "[^src-3]: **Source:** doc-c\n"
)


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


def _write_page_non_canonically(wiki: Path, *, uid: str, name: str, body: str) -> Path:
    """Write a page with NO blank line after the closing ``---``.

    The real corpus's other shape, and the common one: a seeded 600-page
    sample of a live 26,660-page wiki found 6.0% of pages written this way.
    It parses to the same frontmatter and the same body as the canonical
    form -- so the same plan applies to it -- but its BYTES differ, which is
    the whole point: a pre-image recorded by re-rendering the parsed
    frontmatter would not match them.
    """
    wiki.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"uid": uid, "type": "reference", "name": name, "access": "internal"}
    path = wiki / f"{uid}.md"
    path.write_text(render_frontmatter(meta) + body, encoding="utf-8")
    assert path.read_text(encoding="utf-8") != render_frontmatter(meta) + "\n" + body
    return path


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _page_bytes(wiki: Path) -> dict[str, bytes]:
    """Every wiki PAGE's exact bytes.

    Pages only, by ``athenaeum.page_relations._discover_wiki_pages``'s own
    convention (``*.md``, ``_``-prefixed infra files excluded) -- not the
    whole directory. The ledger (``_page_recompiles.jsonl``) is append-only
    by design and gains the ``undo`` record AC6 requires, and the queue file
    (``_pending_questions.md``) is where AC3's decision item lands, so a
    byte-identity claim over either would contradict another criterion. Each
    is asserted on its own terms instead: see
    :func:`_infra_files` and the AC3 tests.
    """
    return {
        p.name: p.read_bytes() for p in sorted(wiki.glob("*.md")) if not p.name.startswith("_")
    }


def _infra_files(wiki: Path) -> set[str]:
    """Every non-page file in the wiki -- the ledger, the queue, a leftover
    journal. Named so a test can state exactly which of them it expects."""
    return {
        p.name
        for p in wiki.iterdir()
        if p.is_file() and (p.name.startswith("_") or not p.name.endswith(".md"))
    }


def _unit_ids(body: str) -> list[str]:
    return [u.unit_id for u in segment_page(body).units]


def _record_plan(
    wiki: Path,
    *,
    uid: str,
    body: str,
    assignments: list[dict[str, str]],
    groups: list[dict[str, Any]] | None = None,
    merges: list[dict[str, Any]] | None = None,
    summary: str = _SUMMARY,
) -> str:
    segment = segment_page(body)
    plan = validate_plan(
        {
            "groups": groups or [],
            "assignments": assignments,
            "merges": merges or [],
            "page_actions": [],
            "summary": summary,
        },
        segment,
        body,
        uid=uid,
        content_hash=page_content_hash(body),
        known_uids=(),
    )
    assert plan.valid, plan.issues
    return append_plan_record(plan, wiki)


def _simple_plan(wiki: Path, *, uid: str = "page0001", body: str = _BODY) -> str:
    """Unit 0 (lead) and unit 2 stay on the parent; unit 1 (Pricing) moves to
    one new sub-page -- the applier's own ``_simple_plan`` shape."""
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


def _merge_plan(wiki: Path, *, uid: str = "page0001") -> str:
    """Both ``## Pricing`` units are fed into ONE merged sub-page section, so
    neither original survives anywhere on disk after the apply."""
    ids = _unit_ids(_MERGE_BODY)
    return _record_plan(
        wiki,
        uid=uid,
        body=_MERGE_BODY,
        assignments=[
            {"unit": ids[0], "disposition": "parent"},
            {"unit": ids[1], "disposition": "merge:g1"},
            {"unit": ids[2], "disposition": "merge:g1"},
        ],
        groups=[{"key": "g1", "name": "Pricing", "description": "Pricing", "confidence": 0.9}],
        merges=[
            {
                "group": "g1",
                "text": "Widgets cost ten pounds, or twelve euros in the euro "
                "zone.[^src-2][^src-3]",
            }
        ],
    )


def _applied(
    wiki: Path,
    *,
    body: str = _BODY,
    uid: str = "page0001",
    merge: bool = False,
) -> tuple[str, dict[str, bytes], str]:
    """Seed a REAL git repo, commit the pre-apply state, apply a plan.

    Returns ``(plan_id, pre-apply page bytes, the apply's snapshot sha)``.
    """
    knowledge = wiki.parent
    _init_git_repo(knowledge)
    _write_page(wiki, uid=uid, name="Acme", body=body)
    _commit_all(knowledge, "seed")
    before = _page_bytes(wiki)

    if merge:
        plan_id = _merge_plan(wiki, uid=uid)
        client: Any = _StubClient(responses=[_all_supported(1)])
    else:
        plan_id = _simple_plan(wiki, uid=uid, body=body)
        client = None
    outcome = write_apply(wiki, plan_id=plan_id, client=client, config=None)
    assert outcome.ok, (outcome.reason, outcome.issues)
    assert not outcome.post_write_issues, outcome.post_write_issues
    assert outcome.snapshot_sha
    return plan_id, before, outcome.snapshot_sha


class TestAC1DirectUndoRestoresTheWiki:
    """AC1: a direct undo restores every modified page from its pre-image
    blob, removes the created pages, and removes the relation rows -- the
    round trip leaves the wiki byte-identical."""

    def test_apply_then_undo_leaves_the_wiki_byte_identical(self, wiki: Path) -> None:
        plan_id, before, _snapshot = _applied(wiki)
        assert _page_bytes(wiki) != before  # the apply really did change it

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert outcome.restored_uids == ("page0001",)
        assert len(outcome.deleted_uids) == 1
        assert _page_bytes(wiki) == before
        # The ledger is the only non-page file, and it is append-only by
        # design -- nothing else was left behind.
        assert _infra_files(wiki) == {"_page_recompiles.jsonl"}

    def test_a_page_written_non_canonically_still_undoes_directly(self, wiki: Path) -> None:
        """AC1 on the corpus's OTHER page shape (no blank line after the
        closing ``---``), which is ~6% of a real wiki.

        The canonical-fixture round-trip test above cannot catch this: it
        passes whenever the pre-image is recorded by RE-RENDERING the parsed
        frontmatter, because for a canonically-written page the re-render
        happens to reproduce the bytes. For this page it does not -- so a
        re-rendered pre-image names a git blob that was never written, the
        lookup misses, and a perfectly undoable apply degrades to queued.
        This test is the one that fails in that case.
        """
        knowledge = wiki.parent
        _init_git_repo(knowledge)
        page = _write_page_non_canonically(wiki, uid="page0001", name="Acme", body=_BODY)
        _commit_all(knowledge, "seed")
        before = _page_bytes(wiki)
        raw_before = page.read_bytes()

        plan_id = _simple_plan(wiki)
        assert write_apply(wiki, plan_id=plan_id, client=None, config=None).ok

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert _page_bytes(wiki) == before
        assert page.read_bytes() == raw_before

    def test_the_created_sub_page_file_is_gone(self, wiki: Path) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        created = record["created_uids"]
        assert created
        created_paths = [
            Path(row["path"]) for row in record["pages"] if row["uid"] in set(created)
        ]
        assert all(p.is_file() for p in created_paths)

        write_undo(wiki, plan_id=plan_id, config=None)

        assert not any(p.exists() for p in created_paths)

    def test_the_parents_relation_rows_are_removed(self, wiki: Path) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        parent = wiki / "page0001.md"
        meta_after_apply, _ = parse_frontmatter(parent.read_text(encoding="utf-8"))
        assert read_relations(meta_after_apply).children, "the apply should have linked a child"

        write_undo(wiki, plan_id=plan_id, config=None)

        meta_after_undo, body_after_undo = parse_frontmatter(parent.read_text(encoding="utf-8"))
        assert read_relations(meta_after_undo).children == ()
        assert "related" not in meta_after_undo
        assert "Sub-pages" not in body_after_undo

    def test_a_pre_existing_child_relation_survives_the_undo(self, wiki: Path) -> None:
        """The pre-image carries whatever relation rows the parent held
        BEFORE the apply, so a child that predates the recompile is restored
        along with everything else rather than dropped."""
        knowledge = wiki.parent
        _init_git_repo(knowledge)
        _write_page(
            wiki,
            uid="page0001",
            name="Acme",
            body=_BODY,
            related=[{"uid": "old00001", "role": "child", "via": "split"}],
        )
        _write_page(wiki, uid="old00001", name="Acme (Legacy)", body="Legacy.\n", parent="page0001")
        _commit_all(knowledge, "seed")
        before = _page_bytes(wiki)

        plan_id = _simple_plan(wiki)
        assert write_apply(wiki, plan_id=plan_id, client=None, config=None).ok

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert _page_bytes(wiki) == before
        meta, _ = parse_frontmatter((wiki / "page0001.md").read_text(encoding="utf-8"))
        assert [c.uid for c in read_relations(meta).children] == ["old00001"]
        assert (wiki / "old00001.md").is_file()


class TestAC2MergedSectionComesBackByteForByte:
    """AC2: the round trip includes a plan with a merged section -- after
    undo the original unmerged units are back, byte for byte."""

    def test_the_merged_originals_are_gone_after_the_apply(self, wiki: Path) -> None:
        """The control AC2 needs: without it, "back byte for byte" could pass
        on a corpus where they never left."""
        _plan_id, _before, _snapshot = _applied(wiki, body=_MERGE_BODY, merge=True)
        live = "".join(p.read_text(encoding="utf-8") for p in sorted(wiki.glob("*.md")))
        assert _PRICING_A not in live
        assert _PRICING_B not in live

    def test_undo_brings_each_original_unit_back_byte_for_byte(self, wiki: Path) -> None:
        plan_id, before, _snapshot = _applied(wiki, body=_MERGE_BODY, merge=True)

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        restored = (wiki / "page0001.md").read_text(encoding="utf-8")
        assert _PRICING_A in restored
        assert _PRICING_B in restored
        assert "## Pricing A\n\n" + _PRICING_A in restored
        assert "## Pricing B\n\n" + _PRICING_B in restored
        assert _page_bytes(wiki) == before


class TestAC3ChangedPageQueuesADiff:
    """AC3: if any touched page changed after the apply, undo writes nothing
    and raises a decision item carrying a diff of current against pre-image
    for each changed page."""

    def test_the_pre_image_blob_is_readable_after_the_applys_own_snapshot(
        self, wiki: Path
    ) -> None:
        """The positive control for every direct-undo test in this module: a
        pre-image blob really is reachable from the knowledge store's object
        store AFTER the apply has rewritten the page and committed its own
        snapshot, and its bytes hash back to the recorded id."""
        plan_id, before, _snapshot = _applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        row = next(r for r in record["pages"] if r["uid"] == "page0001")
        blob_id = row["pre_blob_id"]
        assert blob_id

        blob = read_git_blob(wiki.parent, blob_id)

        assert blob is not None
        assert blob == before["page0001.md"]
        assert hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest() == blob_id

    def test_an_unreadable_blob_id_reads_as_none(self, wiki: Path) -> None:
        _plan_id, _before, _snapshot = _applied(wiki)
        assert read_git_blob(wiki.parent, "0" * 40) is None
        assert read_git_blob(wiki.parent, "") is None

    def test_an_abbreviated_blob_id_is_rejected_rather_than_trusted(self, wiki: Path) -> None:
        """``git cat-file`` resolves an abbreviated id happily, so a malformed
        ledger field could otherwise restore a page from bytes that are not
        the recorded pre-image. The hash-back check is what refuses it."""
        plan_id, _before, _snapshot = _applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        full_id = next(r["pre_blob_id"] for r in record["pages"] if r["uid"] == "page0001")
        abbreviated = full_id[:12]
        # The control: git itself DOES resolve the abbreviation.
        assert _git(wiki.parent, "cat-file", "-t", abbreviated) == "blob"

        assert read_git_blob(wiki.parent, full_id) is not None
        assert read_git_blob(wiki.parent, abbreviated) is None

    def test_a_missing_page_blocks_the_undo_with_its_own_reason(self, wiki: Path) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        (wiki / "page0001.md").unlink()
        pending = wiki / "_pending_questions.md"

        outcome = write_undo(wiki, plan_id=plan_id, config=None, pending_path=pending)

        assert outcome.action == UNDO_QUEUED
        assert outcome.reason == REASON_PAGE_MISSING
        assert [b.uid for b in outcome.blocked] == ["page0001"]
        assert REASON_PAGE_MISSING in pending.read_text(encoding="utf-8")

    def test_a_changed_page_whose_pre_image_is_also_gone_says_so_in_the_diff(
        self, wiki: Path
    ) -> None:
        """Both blockers at once. The diff has to say the pre-image is absent,
        or a whole-file addition would read as "this page was created here"."""
        knowledge = wiki.parent
        _init_git_repo(knowledge)
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        plan_id = _simple_plan(wiki)
        assert write_apply(wiki, plan_id=plan_id, client=None, config=None).ok
        parent = wiki / "page0001.md"
        meta, body = parse_frontmatter(parent.read_text(encoding="utf-8"))
        parent.write_text(render_frontmatter(meta) + "\n" + body + "\nEdited.\n", encoding="utf-8")

        outcome = write_undo(wiki, plan_id=plan_id, config=None, dry_run=True)

        assert outcome.action == UNDO_QUEUED
        assert outcome.reason == REASON_PAGE_CHANGED
        diff = outcome.blocked[0].diff
        assert "is NOT\nreadable either" in diff or "is NOT readable either" in diff
        assert "+Edited." in diff

    def test_a_page_changed_after_the_apply_writes_nothing_and_queues_a_diff(
        self, wiki: Path
    ) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        parent = wiki / "page0001.md"
        meta, body = parse_frontmatter(parent.read_text(encoding="utf-8"))
        parent.write_text(
            render_frontmatter(meta) + "\n" + body + "\nA later human edit.\n",
            encoding="utf-8",
        )
        after_edit = _page_bytes(wiki)
        pending = wiki / "_pending_questions.md"

        outcome = write_undo(wiki, plan_id=plan_id, config=None, pending_path=pending)

        assert outcome.action == UNDO_QUEUED
        assert outcome.reason == REASON_PAGE_CHANGED
        assert [b.uid for b in outcome.blocked] == ["page0001"]
        # Nothing written: the wiki is exactly as the human left it.
        assert _page_bytes(wiki) == after_edit
        # The decision item carries a REAL diff against the pre-image.
        assert pending.is_file()
        text = pending.read_text(encoding="utf-8")
        assert "```diff" in text
        assert "page0001" in text
        assert REASON_PAGE_CHANGED in text
        # A real REMOVAL line, i.e. content the pre-image has and the current
        # page does not: the apply relocated the `## Pricing` unit onto a
        # sub-page, so it appears in the diff only because the pre-image is
        # genuinely being compared against, not merely echoed.
        assert "-## Pricing" in text
        assert "-Widgets cost ten pounds each" in text
        assert "+A later human edit." in text

    def test_a_changed_created_sub_page_also_blocks_the_undo(self, wiki: Path) -> None:
        """A created sub-page is a touched page too. Its pre-image is the
        empty string by construction, so its diff shows the whole page as an
        addition -- what undo would delete."""
        plan_id, _before, _snapshot = _applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        created_uid = record["created_uids"][0]
        child = Path(next(r["path"] for r in record["pages"] if r["uid"] == created_uid))
        meta, body = parse_frontmatter(child.read_text(encoding="utf-8"))
        child.write_text(
            render_frontmatter(meta) + "\n" + body + "\nHand-written addition.\n",
            encoding="utf-8",
        )
        after_edit = _page_bytes(wiki)
        pending = wiki / "_pending_questions.md"

        outcome = write_undo(wiki, plan_id=plan_id, config=None, pending_path=pending)

        assert outcome.action == UNDO_QUEUED
        assert [b.uid for b in outcome.blocked] == [created_uid]
        assert _page_bytes(wiki) == after_edit
        diff = outcome.blocked[0].diff
        assert "+Hand-written addition." in diff
        assert "---" in diff and "+++" in diff

    def test_a_missing_pre_image_blob_queues_instead_of_writing(self, wiki: Path) -> None:
        """The lane's reversible default: an unreadable pre-image is treated
        exactly like a changed page -- nothing is written and a decision item
        names the unreadable blob. Reproduced the way it really happens: the
        page is created and recompiled with no pre-apply commit, so its
        earlier bytes were never stored in any git object."""
        knowledge = wiki.parent
        _init_git_repo(knowledge)
        _write_page(wiki, uid="page0001", name="Acme", body=_BODY)
        plan_id = _simple_plan(wiki)
        assert write_apply(wiki, plan_id=plan_id, client=None, config=None).ok
        after_apply = _page_bytes(wiki)
        pending = wiki / "_pending_questions.md"

        outcome = write_undo(wiki, plan_id=plan_id, config=None, pending_path=pending)

        assert outcome.action == UNDO_QUEUED
        assert outcome.reason == REASON_PRE_IMAGE_UNREADABLE
        assert _page_bytes(wiki) == after_apply
        text = pending.read_text(encoding="utf-8")
        assert REASON_PRE_IMAGE_UNREADABLE in text
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        blob_id = next(r["pre_blob_id"] for r in record["pages"] if r["uid"] == "page0001")
        assert blob_id in text
        assert "git revert" in text

    def test_no_apply_record_is_refused_not_queued(self, wiki: Path) -> None:
        wiki.mkdir(parents=True)
        outcome = write_undo(wiki, plan_id="nosuchplan", config=None)
        assert outcome.action == UNDO_REFUSED
        assert outcome.reason == REASON_NO_APPLY_RECORD
        assert outcome.ok is False


class TestAC4RefusedWhenALaterPageLinksToACreatedSubPage:
    """AC4: if a later page links to a created sub-page, undo is refused with
    the reason and names the count of such links."""

    def _link_source(self, wiki: Path, body: str) -> None:
        _write_page(wiki, uid="later001", name="Later Page", body=body)

    def test_a_wikilink_from_a_later_page_refuses_with_a_count(self, wiki: Path) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        created_uid = record["created_uids"][0]
        self._link_source(wiki, f"Written later, citing [[{created_uid}|Acme Pricing]].\n")
        after_link = _page_bytes(wiki)

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_REFUSED
        assert outcome.reason == "created_sub_page_has_inbound_links"
        assert outcome.inbound_link_count == 1
        assert outcome.inbound_links[0].uid == created_uid
        assert outcome.inbound_links[0].kind == "wikilink"
        assert _page_bytes(wiki) == after_link

    def test_the_count_adds_up_across_kinds_and_occurrences(self, wiki: Path) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        created_uid = record["created_uids"][0]
        _write_page(
            wiki,
            uid="later001",
            name="Later Page",
            body=f"See [[{created_uid}]] and again [[{created_uid}|Pricing]].\n",
            related=[{"uid": created_uid, "role": "see-also"}],
        )

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_REFUSED
        assert outcome.inbound_link_count == 3
        assert {link.kind for link in outcome.inbound_links} == {"wikilink", "related-row"}

    def test_a_parent_key_on_a_later_page_counts_too(self, wiki: Path) -> None:
        """Not only wikilinks: a later page that adopts a created sub-page as
        its own parent is just as broken by deleting it."""
        plan_id, _before, _snapshot = _applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        created_uid = record["created_uids"][0]
        _write_page(
            wiki,
            uid="later001",
            name="Later Page",
            body="No wikilink here at all.\n",
            parent=created_uid,
        )

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_REFUSED
        assert outcome.inbound_link_count == 1
        assert outcome.inbound_links[0].kind == "parent-key"

    def test_an_apply_that_created_nothing_has_no_links_to_scan(self, wiki: Path) -> None:
        """The scan short-circuits on an empty created set rather than
        walking the corpus to find nothing."""
        _plan_id, _before, _snapshot = _applied(wiki)
        assert find_inbound_links(wiki, [], exclude=set()) == ()

    def test_the_parents_own_links_to_its_children_are_not_counted(self, wiki: Path) -> None:
        """The control: the recompiled parent links to every sub-page it
        created, in a ``related`` row AND a Sub-pages wikilink. Counting
        those would refuse every undo there is."""
        plan_id, _before, _snapshot = _applied(wiki)
        parent_text = (wiki / "page0001.md").read_text(encoding="utf-8")
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        assert record["created_uids"][0] in parent_text  # the links really are there

        computed = compute_undo(wiki, plan_id=plan_id)

        assert computed.inbound_links == ()
        assert computed.action == UNDO_DIRECT


class TestAC5LockAndOneSnapshotCommit:
    """AC5: undo runs under the run lock, exits 75 when it is held, and makes
    one snapshot commit of its own."""

    def test_cli_exits_75_when_the_lock_is_held(self, wiki: Path) -> None:
        from athenaeum._cli_shared import EXIT_LOCK_HELD
        from athenaeum.runlock import RunLock

        plan_id, _before, _snapshot = _applied(wiki)
        knowledge = wiki.parent

        lock = RunLock(knowledge, wait=0)
        lock.acquire()
        try:
            rc = cli_main(["recompile", "undo", plan_id, "--path", str(knowledge)])
        finally:
            lock.release()

        assert rc == EXIT_LOCK_HELD

    def test_dry_run_exits_75_while_the_lock_is_held_too(self, wiki: Path) -> None:
        from athenaeum._cli_shared import EXIT_LOCK_HELD
        from athenaeum.runlock import RunLock

        plan_id, _before, _snapshot = _applied(wiki)
        knowledge = wiki.parent

        lock = RunLock(knowledge, wait=0)
        lock.acquire()
        try:
            rc = cli_main(
                ["recompile", "undo", plan_id, "--dry-run", "--path", str(knowledge)]
            )
        finally:
            lock.release()

        assert rc == EXIT_LOCK_HELD

    def test_exactly_one_snapshot_commit_lands_on_top_of_the_applys(self, wiki: Path) -> None:
        plan_id, _before, apply_sha = _applied(wiki)
        knowledge = wiki.parent

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert outcome.snapshot_sha
        assert _git(knowledge, "rev-parse", "HEAD") == outcome.snapshot_sha
        assert _git(knowledge, "rev-parse", "HEAD^") == apply_sha
        subject = _git(knowledge, "log", "-1", "--format=%s")
        assert subject == f"page-recompile undo page0001 ({plan_id})"

    def test_a_queued_undo_makes_no_commit_at_all(self, wiki: Path) -> None:
        plan_id, _before, apply_sha = _applied(wiki)
        parent = wiki / "page0001.md"
        meta, body = parse_frontmatter(parent.read_text(encoding="utf-8"))
        parent.write_text(render_frontmatter(meta) + "\n" + body + "\nEdited.\n", encoding="utf-8")

        outcome = write_undo(
            wiki, plan_id=plan_id, config=None, pending_path=wiki / "_pending_questions.md"
        )

        assert outcome.action == UNDO_QUEUED
        assert outcome.snapshot_sha is None
        assert _git(wiki.parent, "rev-parse", "HEAD") == apply_sha

    def test_the_cli_undoes_and_exits_zero(self, wiki: Path) -> None:
        plan_id, before, _snapshot = _applied(wiki)
        knowledge = wiki.parent

        rc = cli_main(["recompile", "undo", plan_id, "--path", str(knowledge)])

        assert rc == 0
        assert _page_bytes(wiki) == before

    def test_the_cli_exits_one_when_the_undo_is_queued(self, wiki: Path) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        parent = wiki / "page0001.md"
        meta, body = parse_frontmatter(parent.read_text(encoding="utf-8"))
        parent.write_text(render_frontmatter(meta) + "\n" + body + "\nEdited.\n", encoding="utf-8")

        rc = cli_main(["recompile", "undo", plan_id, "--path", str(wiki.parent)])

        assert rc == 1


class TestAC6LedgerRecordAndThePlannerSkip:
    """AC6: the ledger gains an ``undo`` record referencing the plan id, and
    a plan that was undone is not re-applied automatically -- the page is
    skipped until its content hash differs from the pre-image."""

    def _undo_records(self, wiki: Path) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in (wiki / "_page_recompiles.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("kind") == UNDO_KIND
        ]

    def test_the_undo_record_references_the_plan_id(self, wiki: Path) -> None:
        plan_id, before, _snapshot = _applied(wiki)

        write_undo(wiki, plan_id=plan_id, config=None)

        records = self._undo_records(wiki)
        assert len(records) == 1
        record = records[0]
        assert record["plan_id"] == plan_id
        assert record["id"] == plan_id
        assert record["uid"] == "page0001"
        assert [row["uid"] for row in record["restored"]] == ["page0001"]
        assert len(record["deleted"]) == 1
        # The restored row pins the hash the planner's own skip compares
        # against -- the pre-image's BODY hash, not the whole file's.
        _meta, pre_body = parse_frontmatter(before["page0001.md"].decode("utf-8"))
        assert record["restored"][0]["pre_content_hash"] == page_content_hash(pre_body)

    def test_the_next_planning_pass_does_not_draft_the_same_plan(self, wiki: Path) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        assert write_undo(wiki, plan_id=plan_id, config=None).action == UNDO_DIRECT

        client = _StubClient(default={})
        result = plan_page(wiki_root=wiki, uid="page0001", client=client, config=None)

        assert result.plan is None
        assert "undo" in result.error
        assert plan_id in result.error
        # And it cost nothing: the skip is checked before the labelling pass.
        assert client.call_count == 0

    def test_the_skip_lifts_once_the_page_changes_again(self, wiki: Path) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        assert write_undo(wiki, plan_id=plan_id, config=None).action == UNDO_DIRECT
        restored_body = parse_frontmatter((wiki / "page0001.md").read_text(encoding="utf-8"))[1]

        assert undone_page_skip_reason(wiki, uid="page0001", body=restored_body)
        assert not undone_page_skip_reason(
            wiki, uid="page0001", body=restored_body + "\nA later edit.\n"
        )
        # Another page's planning is never affected.
        assert not undone_page_skip_reason(wiki, uid="other001", body=restored_body)

    def test_no_undo_record_means_no_skip(self, wiki: Path) -> None:
        _plan_id, _before, _snapshot = _applied(wiki)
        body = parse_frontmatter((wiki / "page0001.md").read_text(encoding="utf-8"))[1]
        assert not undone_page_skip_reason(wiki, uid="page0001", body=body)


class TestAC7DryRunReportsAndWritesNothing:
    """AC7: ``recompile undo --dry-run <plan id>`` reports direct or queued
    and why, and writes nothing."""

    def test_a_dry_run_reports_direct_and_writes_nothing(self, wiki: Path) -> None:
        plan_id, _before, apply_sha = _applied(wiki)
        after_apply = _page_bytes(wiki)
        ledger_before = (wiki / "_page_recompiles.jsonl").read_bytes()

        outcome = write_undo(wiki, plan_id=plan_id, config=None, dry_run=True)

        assert outcome.dry_run is True
        assert outcome.action == UNDO_DIRECT
        assert outcome.reason == "restorable"
        assert outcome.ok is True
        assert outcome.restored_uids == ("page0001",)
        assert len(outcome.deleted_uids) == 1
        assert outcome.snapshot_sha is None
        assert _page_bytes(wiki) == after_apply
        assert (wiki / "_page_recompiles.jsonl").read_bytes() == ledger_before
        assert _git(wiki.parent, "rev-parse", "HEAD") == apply_sha

    def test_a_dry_run_reports_queued_and_why_without_raising_the_item(
        self, wiki: Path
    ) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        parent = wiki / "page0001.md"
        meta, body = parse_frontmatter(parent.read_text(encoding="utf-8"))
        parent.write_text(render_frontmatter(meta) + "\n" + body + "\nEdited.\n", encoding="utf-8")
        pending = wiki / "_pending_questions.md"

        outcome = write_undo(
            wiki, plan_id=plan_id, config=None, pending_path=pending, dry_run=True
        )

        assert outcome.action == UNDO_QUEUED
        assert outcome.reason == REASON_PAGE_CHANGED
        assert [b.uid for b in outcome.blocked] == ["page0001"]
        assert outcome.blocked[0].diff
        assert not pending.exists(), "a dry run must not raise the decision item"

    def test_a_dry_run_reports_refused_and_the_count(self, wiki: Path) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        created_uid = record["created_uids"][0]
        _write_page(wiki, uid="later001", name="Later", body=f"[[{created_uid}]]\n")

        outcome = write_undo(wiki, plan_id=plan_id, config=None, dry_run=True)

        assert outcome.action == UNDO_REFUSED
        assert outcome.inbound_link_count == 1

    def test_the_dry_run_cli_prints_json_and_exits_zero(
        self, wiki: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        after_apply = _page_bytes(wiki)

        rc = cli_main(
            [
                "recompile",
                "undo",
                plan_id,
                "--dry-run",
                "--json",
                "--path",
                str(wiki.parent),
            ]
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["action"] == UNDO_DIRECT
        assert payload["dry_run"] is True
        assert payload["plan_id"] == plan_id
        assert payload["reason"] == "restorable"
        assert payload["snapshot_sha"] is None
        assert _page_bytes(wiki) == after_apply

    def test_the_queued_cli_report_names_each_blocking_page(
        self, wiki: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """AC7 asks the report to say WHY, so the human-readable rendering of
        the queued outcome is part of the deliverable, not decoration."""
        plan_id, _before, _snapshot = _applied(wiki)
        parent = wiki / "page0001.md"
        meta, body = parse_frontmatter(parent.read_text(encoding="utf-8"))
        parent.write_text(render_frontmatter(meta) + "\n" + body + "\nEdited.\n", encoding="utf-8")

        rc = cli_main(
            ["recompile", "undo", plan_id, "--dry-run", "--path", str(wiki.parent)]
        )

        out = capsys.readouterr().out
        assert rc == 0
        assert f"outcome:     {UNDO_QUEUED} ({REASON_PAGE_CHANGED})" in out
        assert "page0001: " + REASON_PAGE_CHANGED in out
        assert "would raise one decision item" in out

    def test_the_refused_cli_report_prints_the_link_count(
        self, wiki: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        created_uid = record["created_uids"][0]
        _write_page(wiki, uid="later001", name="Later", body=f"[[{created_uid}]]\n")

        rc = cli_main(["recompile", "undo", plan_id, "--path", str(wiki.parent)])

        out = capsys.readouterr().out
        assert rc == 1
        assert f"outcome:     {UNDO_REFUSED} (created_sub_page_has_inbound_links)" in out
        assert "inbound links to created sub-pages: 1" in out
        assert f"wikilink -> {created_uid}" in out

    def test_the_queued_cli_json_exits_one_and_carries_the_diff(
        self, wiki: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plan_id, _before, _snapshot = _applied(wiki)
        parent = wiki / "page0001.md"
        meta, body = parse_frontmatter(parent.read_text(encoding="utf-8"))
        parent.write_text(render_frontmatter(meta) + "\n" + body + "\nEdited.\n", encoding="utf-8")

        rc = cli_main(["recompile", "undo", plan_id, "--json", "--path", str(wiki.parent)])

        payload = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert payload["ok"] is False
        assert payload["action"] == UNDO_QUEUED
        assert payload["blocked"][0]["uid"] == "page0001"
        assert "+Edited." in payload["blocked"][0]["diff"]
        assert payload["queued_item_id"]

    def test_the_cli_reports_a_missing_wiki_root(self, tmp_path: Path) -> None:
        rc = cli_main(["recompile", "undo", "anything", "--path", str(tmp_path / "nope")])
        assert rc == 1

    def test_a_dry_run_of_an_unknown_plan_id_exits_one(self, wiki: Path) -> None:
        wiki.mkdir(parents=True)
        rc = cli_main(
            ["recompile", "undo", "nosuchplan", "--dry-run", "--path", str(wiki.parent)]
        )
        assert rc == 1


class TestOutOfScopeAndDefensive:
    """The issue's out-of-scope boundary, and the ledger shapes this module
    must not act on."""

    def test_a_plan_whose_scope_is_not_structure_only_is_refused(self, wiki: Path) -> None:
        """The applier refuses a cross-page plan, so an apply record for one
        can only come from a hand-edited ledger -- and undo of cross-page
        moves is explicitly out of scope for this slice."""
        plan_id, _before, _snapshot = _applied(wiki)
        ledger = wiki / "_page_recompiles.jsonl"
        lines = ledger.read_text(encoding="utf-8").splitlines()
        rewritten = []
        for line in lines:
            record = json.loads(line)
            if record.get("kind") == "plan":
                record["plan"]["scope"] = "cross-page"
            rewritten.append(json.dumps(record, sort_keys=True))
        ledger.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        after_apply = _page_bytes(wiki)

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_REFUSED
        assert outcome.reason == "plan_scope_not_structure_only"
        assert _page_bytes(wiki) == after_apply

    def test_an_already_deleted_created_page_is_not_a_blocker(self, wiki: Path) -> None:
        """Undo wanted to delete it and someone already did: there is nothing
        left to write and nothing for a human to adjudicate."""
        plan_id, before, _snapshot = _applied(wiki)
        record = find_apply_record(plan_id, wiki)
        assert record is not None
        created_uid = record["created_uids"][0]
        Path(next(r["path"] for r in record["pages"] if r["uid"] == created_uid)).unlink()

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert outcome.deleted_uids == ()
        assert _page_bytes(wiki) == before

    def test_a_second_undo_of_the_same_plan_is_refused_as_already_undone(
        self, wiki: Path
    ) -> None:
        """Without this check the second call would queue a decision item
        whose diff is EMPTY (the page already holds the pre-image) under the
        false reason "someone edited this"."""
        plan_id, before, _snapshot = _applied(wiki)
        assert write_undo(wiki, plan_id=plan_id, config=None).action == UNDO_DIRECT
        pending = wiki / "_pending_questions.md"

        outcome = write_undo(wiki, plan_id=plan_id, config=None, pending_path=pending)

        assert outcome.action == UNDO_REFUSED
        assert outcome.reason == REASON_ALREADY_UNDONE
        assert outcome.ok is False
        assert not pending.exists(), "an already-undone plan must raise no decision item"
        assert _page_bytes(wiki) == before

    def test_a_re_applied_plan_can_be_undone_again(self, wiki: Path) -> None:
        """The ordering control for the check above: "already undone" is the
        plan's LAST action, not merely the existence of an undo record. A
        plan applied, undone, then re-applied is live work undo must still
        reverse -- and the restored page matches the plan's pinned hash
        again, so the re-apply really does go through."""
        plan_id, before, _snapshot = _applied(wiki)
        assert write_undo(wiki, plan_id=plan_id, config=None).action == UNDO_DIRECT
        assert last_action_for_plan(plan_id, wiki) == UNDO_KIND

        re_applied = write_apply(wiki, plan_id=plan_id, client=None, config=None)
        assert re_applied.ok, (re_applied.reason, re_applied.issues)
        assert last_action_for_plan(plan_id, wiki) == "apply"

        outcome = write_undo(wiki, plan_id=plan_id, config=None)

        assert outcome.action == UNDO_DIRECT, (outcome.reason, outcome.blocked)
        assert _page_bytes(wiki) == before

    def test_last_action_for_an_unknown_plan_id_is_empty(self, wiki: Path) -> None:
        _plan_id, _before, _snapshot = _applied(wiki)
        assert last_action_for_plan("nosuchplan", wiki) == ""

    def test_the_last_apply_record_for_a_plan_id_is_the_one_undone(self, wiki: Path) -> None:
        """The ledger is append-only, so a re-applied plan has two apply
        records; the newer one is what has to be reversed."""
        plan_id, _before, _snapshot = _applied(wiki)
        ledger = wiki / "_page_recompiles.jsonl"
        records = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        apply_record = next(r for r in records if r.get("kind") == "apply")
        stale = dict(apply_record)
        stale["pages"] = [dict(row, post_content_hash="stale") for row in apply_record["pages"]]
        # Insert the STALE copy FIRST, then the real one, and confirm the
        # real (last) one is the record that gets used.
        rebuilt = [json.dumps(stale, sort_keys=True)] + [
            json.dumps(r, sort_keys=True) for r in records
        ]
        ledger.write_text("\n".join(rebuilt) + "\n", encoding="utf-8")

        found = find_apply_record(plan_id, wiki)
        assert found is not None
        assert found["pages"][0]["post_content_hash"] != "stale"
        assert compute_undo(wiki, plan_id=plan_id).action == UNDO_DIRECT
