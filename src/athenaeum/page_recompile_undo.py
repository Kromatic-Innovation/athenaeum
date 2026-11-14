# SPDX-License-Identifier: Apache-2.0
"""Page recompile: undo an applied plan (issue athenaeum#2541, slice S5 of
the page-recompile design, athenaeum#2525).

:mod:`athenaeum.page_recompile_apply` (S4, athenaeum#2540) is the write
side: it rewrites a parent page and creates sub-pages for one
``structure-only`` plan, then records what it did -- pre/post content hash
AND the git blob id of every pre-image, the created uids, and each merged
group's original unit ids -- as one ``apply`` record in the ledger
:mod:`athenaeum.page_recompile_plan` owns. THIS module is that write's
compensating operation: given a plan id, it either restores every touched
page from its recorded pre-image and deletes the created sub-pages
(DIRECT), or queues a human-adjudicated decision item carrying a diff
(QUEUED), or refuses outright (REFUSED).

**Why structure-only plans may apply unattended at all (operator ruling on
athenaeum#2525).** They apply without approval from day one *because* this
path exists. A merged section's original units are no longer live on any
page after an apply, and are reachable only through the apply's own
snapshot commit -- so "the pass can run unattended" and "undo works" are
one claim, not two.

**The direct/queued split is a CONTENT-HASH comparison**, exactly
:mod:`athenaeum.unfold`'s (``can_unfold_directly``): every touched page's
CURRENT body hash (:func:`athenaeum.verdicts.content_hash_for_path`, via
:func:`athenaeum.page_recompile_plan.page_content_hash` on the parsed body)
must still equal the ``post_content_hash`` the apply recorded. Equal ->
nothing has edited the page since the apply -> restore directly. Unequal
-> queued, carrying a real unified diff per changed page (AC3).

**The gap :mod:`athenaeum.unfold` recorded, and why this slice does not
repeat it.** That module's docstring names it plainly: its fold record
pinned a content HASH, not a text snapshot, so no reader of that ledger --
itself included -- can reconstruct the pre-image to diff against or
restore from, and its queued proposal has to ship a labelled best-effort
substitute. The applier closed that gap deliberately: it records
``pre_blob_id``, the git blob id of each pre-image
(:func:`athenaeum.page_recompile_apply._git_blob_id`, verified here to
equal what ``git hash-object`` produces for the same bytes). A blob id is
a content-addressed REFERENCE to the bytes themselves, so this module
restores the pre-image byte for byte (``git cat-file blob <id>``) and
diffs against it for real.

**Reversible default when a pre-image blob is NOT readable (athenaeum#2541
lane decision).** The applier takes its snapshot AFTER the write, so a
pre-image lives in that commit's PARENT -- but nothing guarantees a
pre-apply commit ever happened (a page created and recompiled between two
snapshots was never staged, so its pre-image bytes exist in no git
object). The blob can therefore be legitimately absent, and an absent
pre-image is indistinguishable, from here, from a corrupted one. This
module treats an unreadable pre-image EXACTLY like a page that changed
after the apply: it writes nothing and queues a decision item naming the
unreadable blob id. That is conservative (no page is written from a
guess), reversible (the operator can still ``git revert`` the apply's
snapshot commit, which the docs name as the coarse fallback), and needs no
new ledger field.

**"Reverses recorded link rewrites" (AC1) is satisfied INHERENTLY, because
the applier records no link rewrites.** Verified against the applier
rather than assumed: it has no ``link_rewrite``-shaped ledger field and no
inbound-link rewriter at all. Every relation it creates lives *inside a
page it writes* -- ``related`` rows with ``role: "child"`` plus a
"Sub-pages" section in the PARENT, and a top-level ``parent:`` key plus a
breadcrumb line in each CREATED sub-page. Restoring the parent from its
pre-image therefore removes the parent-side rows and the Sub-pages
section byte-exactly, and deleting a created sub-page removes the
child-side row with the file. :func:`athenaeum.page_relations.remove_relation`
is deliberately NOT called: it REWRITES the parent (to re-render its
Sub-pages section), which would undo the byte-exact restore this module
just made and defeat AC1's own round-trip test. This module invents no
rewrite-record format the applier never writes.

**Why "a later page links to a created sub-page" (AC4) is checkable at
all.** A created sub-page's uid did not exist before the apply, so any
reference to it from a page OTHER than the recompiled parent (and other
than the created sub-pages' own cross-links) was necessarily added after
the apply. The scan is therefore "any inbound reference from a third
page", with no timestamp heuristic anywhere: ``related`` rows naming a
created uid, a ``parent:`` key naming one, and ``[[uid|...]]`` body
wikilinks. Any hit refuses the undo and reports the COUNT -- deleting a
page something else points at would leave a dangling link, which no diff
a human adjudicates can repair.

**Order of writes on the direct path: parents FIRST, created pages
deleted LAST** -- the mirror of the applier's "sub-pages first, parent
last". A kill between the two leaves created pages on disk that nothing
links to any more (orphans a later undo re-reads as changed, so it queues
rather than guessing); the reverse order would leave the parent pointing
at pages that no longer exist. No journal of its own, unlike the applier:
an orphan is visible and harmless, a dangling link is neither.

**Ledger and snapshot (AC5/AC6).** One ``undo`` record naming the plan id,
appended to the SAME ledger file, then exactly ONE
:meth:`athenaeum.store.FilesystemStore.snapshot` commit -- ledger append
first so the record itself lands in that commit, the same order
:func:`athenaeum.page_recompile_apply.write_apply` uses. The planner's own
skip is NOT here: it reads this record from the module that owns the
ledger (:func:`athenaeum.page_recompile_plan.undone_page_skip_reason`),
because the reverse import would make the planner and this module a cycle.

**Out of scope (per the issue).** Undo of moves to other pages and of
standalone pages (a sibling slice adds those with the actions themselves),
and any time-based expiry of undo. A plan whose scope is not
``structure-only`` is refused here, the same refusal the applier makes --
there is nothing this module could have applied.

Layering: L4 domain/pipeline, the same layer as its siblings
:mod:`athenaeum.page_recompile`, :mod:`athenaeum.page_recompile_plan` and
:mod:`athenaeum.page_recompile_apply` (same-layer imports are permitted by
``tests/test_layer_boundary.py``). Imports :mod:`athenaeum.page_relations`
(L3) for the page-discovery and relation-row primitives it reuses rather
than duplicates, :mod:`athenaeum.models` (L1),
:mod:`athenaeum.atomic_io` (L0) and :mod:`athenaeum.store` (L0/L1);
:func:`athenaeum.answers.raise_pending_question` (L4) is imported
function-locally in :func:`_queue_undo_proposal`, mirroring
:mod:`athenaeum.unfold`'s own deferred-import style for the same call.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter
from athenaeum.page_recompile_apply import APPLY_KIND, _git_blob_id
from athenaeum.page_recompile_plan import (
    PAGE_RECOMPILE_LEDGER_VERSION,
    SCOPE_STRUCTURE_ONLY,
    UNDO_KIND,
    _append_record,
    default_page_recompile_ledger_path,
    find_plan_record,
    page_content_hash,
    read_page_recompile_ledger,
)
from athenaeum.page_relations import (
    _discover_wiki_pages,
    _index_pages_by_uid,
    _related_rows,
    read_relations,
)
from athenaeum.store import FilesystemStore, now_iso

#: :attr:`UndoOutcome.action` values. ``direct`` and ``queued`` are
#: :mod:`athenaeum.unfold`'s own two, named identically on purpose -- this is
#: the same split over the same kind of record. ``refused`` is this module's
#: third: AC4's inbound-link case is not adjudicable by a diff, so it is
#: neither done nor queued.
UNDO_DIRECT = "direct"
UNDO_QUEUED = "queued"
UNDO_REFUSED = "refused"

#: The one refusal that is an operator MISTAKE rather than a finding about
#: the corpus: there is no ``apply`` record for the plan id given. Named as a
#: constant because the CLI's exit code turns on it -- every other refusal is
#: a legitimate report (and ``--dry-run``'s whole deliverable), while this one
#: means the command had nothing to look at.
REASON_NO_APPLY_RECORD = "no_apply_record"

#: Same wikilink grammar as :data:`athenaeum.page_decompose.WIKILINK_RE` /
#: ``athenaeum.resolutions._WIKILINK_RE`` (Obsidian-style ``[[target]]`` /
#: ``[[target|alias]]``). Restated rather than imported, the convention every
#: other reader of this grammar in the tree already follows (five modules,
#: including :mod:`athenaeum.unfold`): a two-line stable primitive is cheaper
#: to state than a module-scope import of an unrelated L4 pipeline.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|\n]+?)(?:\|[^\[\]\n]*)?\]\]")


# ---------------------------------------------------------------------------
# Reading the apply record this undo compensates for.
# ---------------------------------------------------------------------------


def find_apply_record(
    plan_id: str, wiki_root: Path, *, ledger_path: Path | None = None
) -> dict[str, Any] | None:
    """The LAST ``apply`` record for *plan_id*, or ``None``.

    Last, not first, for the same reason
    :func:`athenaeum.page_recompile_plan.find_plan_record` reads the last
    ``plan`` record: the ledger is append-only, so a re-applied plan's
    newer record is the current one -- and it is the newer apply this undo
    has to reverse.
    """
    match: dict[str, Any] | None = None
    for record in read_page_recompile_ledger(wiki_root, ledger_path=ledger_path):
        if record.get("kind") == APPLY_KIND and str(record.get("id")) == plan_id:
            match = record
    return match


def _record_pages(record: dict[str, Any]) -> list[dict[str, Any]]:
    raw = record.get("pages")
    return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


def _created_uids(record: dict[str, Any]) -> list[str]:
    raw = record.get("created_uids")
    if not isinstance(raw, list):
        return []
    return [uid for uid in raw if isinstance(uid, str) and uid]


def read_git_blob(knowledge_root: Path, blob_id: str) -> bytes | None:
    """The bytes of git blob *blob_id* in *knowledge_root*'s object store,
    or ``None`` when it cannot be read.

    ``None`` covers every "no usable pre-image" case as one outcome, because
    the caller's response to all of them is identical (see the module
    docstring's reversible default): an empty id, a knowledge root that is
    not a git repository, a blob that was never committed, a pruned object,
    and -- caught explicitly here -- an object whose bytes do NOT hash back
    to the id that was asked for. That last check costs one sha1 over
    content already in memory and closes the one failure mode a bare
    ``cat-file`` cannot: git resolves an ABBREVIATED or otherwise
    non-literal id happily, so without it a malformed ledger field could
    restore a page from the wrong bytes.
    """
    if not blob_id:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(knowledge_root), "cat-file", "blob", blob_id],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    if _git_blob_id(proc.stdout) != blob_id:
        return None
    return proc.stdout


# ---------------------------------------------------------------------------
# AC4: inbound links to a created sub-page.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundLink:
    """One reference to a created sub-page from a page undo would not touch."""

    #: Path of the page holding the reference, as a string.
    path: str
    #: The created sub-page uid being referenced.
    uid: str
    #: ``related-row`` / ``parent-key`` / ``wikilink`` -- where it was found.
    kind: str


def find_inbound_links(
    wiki_root: Path, created_uids: list[str], *, exclude: set[Path]
) -> tuple[InboundLink, ...]:
    """Every reference to a uid in *created_uids* from a page not in
    *exclude* (AC4).

    *exclude* is the recompiled parent plus the created sub-pages
    themselves -- the parent's rows are removed by the pre-image restore
    and a created page's own rows go with its file, so neither is a reason
    to refuse. Page discovery is
    :func:`athenaeum.page_relations._discover_wiki_pages`, the existing
    primitive, not a second corpus walk.
    """
    wanted = set(created_uids)
    if not wanted:
        return ()
    excluded = {p.resolve() for p in exclude}
    found: list[InboundLink] = []
    for path in _discover_wiki_pages(Path(wiki_root)):
        try:
            if path.resolve() in excluded:
                continue
        except OSError:  # pragma: no cover - defensive
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, body = parse_frontmatter(text)
        meta_dict = meta if isinstance(meta, dict) else {}
        for row in _related_rows(meta_dict):
            if isinstance(row, dict):
                uid = row.get("uid")
                if isinstance(uid, str) and uid in wanted:
                    found.append(InboundLink(path=str(path), uid=uid, kind="related-row"))
        parent = read_relations(meta_dict).parent
        if parent is not None and parent.uid in wanted:
            found.append(InboundLink(path=str(path), uid=parent.uid, kind="parent-key"))
        for match in _WIKILINK_RE.finditer(body):
            target = match.group(1).strip()
            if target in wanted:
                found.append(InboundLink(path=str(path), uid=target, kind="wikilink"))
    return tuple(found)


# ---------------------------------------------------------------------------
# AC3: what blocks a direct undo, and the diff that goes with it.
# ---------------------------------------------------------------------------

#: :attr:`BlockedPage.reason` values, each with its own explanation in
#: :data:`_REASON_EXPLANATIONS` (the "no silent no-ops" discipline
#: :mod:`athenaeum.unfold` documents for its own result type).
REASON_PAGE_CHANGED = "page_changed_since_apply"
REASON_PAGE_MISSING = "page_missing"
REASON_PAGE_UNREADABLE = "page_unreadable"
REASON_PRE_IMAGE_UNREADABLE = "pre_image_blob_unreadable"

_REASON_EXPLANATIONS: dict[str, str] = {
    REASON_PAGE_CHANGED: (
        "The page has changed since the recompile was applied (its content "
        "hash no longer matches the one the apply recorded) -- restoring "
        "the pre-image would silently discard those later edits."
    ),
    REASON_PAGE_MISSING: (
        "The page the apply wrote is no longer on disk, so there is nothing "
        "to restore the pre-image over; a human should decide where it "
        "belongs now."
    ),
    REASON_PAGE_UNREADABLE: (
        "The page the apply wrote exists but could not be read, so whether "
        "it still matches the apply cannot be established."
    ),
    REASON_PRE_IMAGE_UNREADABLE: (
        "The page itself is untouched, but its pre-image is not readable "
        "from the knowledge store's git objects -- the apply records the "
        "pre-image's blob id, and that blob is absent (most likely the page "
        "was never committed before the apply, so its earlier bytes were "
        "never stored) or does not hash back to the recorded id. There is "
        "no pre-image to restore or to diff against: use `git revert` of "
        "the apply's own snapshot commit instead."
    ),
}


@dataclass(frozen=True)
class BlockedPage:
    """One page that blocks a direct undo, with the diff AC3 asks for."""

    uid: str
    path: str
    reason: str
    #: Unified diff of the recorded pre-image against the page's current
    #: text. Empty only when there is no pre-image AND no current text to
    #: diff (see :data:`REASON_PRE_IMAGE_UNREADABLE`).
    diff: str = ""


def _unified_diff(pre_text: str, current_text: str, *, label: str) -> str:
    """Pre-image against current, for one page.

    A created sub-page's pre-image is the EMPTY string, deliberately and
    not as a fallback: the page did not exist before the apply, so "current
    against pre-image" for it is the whole file as an addition -- which is
    exactly what a human adjudicating needs to see, since undo would delete
    it.
    """
    return "".join(
        difflib.unified_diff(
            pre_text.splitlines(keepends=True),
            current_text.splitlines(keepends=True),
            fromfile=f"{label} (pre-image: what undo would restore)",
            tofile=f"{label} (current, on disk now)",
        )
    )


# ---------------------------------------------------------------------------
# The compute step: everything decided before anything is written.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageRestore:
    """One page's pre-image, already read out of the git object store."""

    uid: str
    path: Path
    #: The pre-image bytes, decoded -- what gets written back verbatim.
    text: str
    #: The pre-image's BODY content hash, as the apply recorded it. Carried
    #: onto the ``undo`` ledger record because the planner's own skip (AC6)
    #: compares the page's live hash against exactly this value.
    pre_content_hash: str


@dataclass(frozen=True)
class PageDeletion:
    """One created sub-page undo will delete (a pure addition at apply time)."""

    uid: str
    path: Path


@dataclass
class ComputedUndo:
    """Everything :func:`write_undo` needs, computed before any write."""

    action: str = UNDO_REFUSED
    reason: str = ""
    plan_id: str = ""
    uid: str = ""
    restores: tuple[PageRestore, ...] = ()
    #: One per created sub-page to delete. A created page already gone from
    #: disk is not listed -- there is nothing left to delete.
    deletions: tuple[PageDeletion, ...] = ()
    blocked: tuple[BlockedPage, ...] = ()
    inbound_links: tuple[InboundLink, ...] = ()


def compute_undo(
    wiki_root: Path,
    *,
    plan_id: str,
    ledger_path: Path | None = None,
) -> ComputedUndo:
    """Decide direct / queued / refused for *plan_id*, and read every
    pre-image needed -- writing nothing.

    This is the whole decision, and the ``--dry-run`` surface (AC7) is
    exactly this function with no write step after it: a dry run and a real
    run can never disagree about what would happen, because the real run
    has no second opinion to form.
    """
    record = find_apply_record(plan_id, wiki_root, ledger_path=ledger_path)
    if record is None:
        return ComputedUndo(
            action=UNDO_REFUSED,
            reason=REASON_NO_APPLY_RECORD,
            plan_id=plan_id,
        )

    plan_record = find_plan_record(plan_id, wiki_root, ledger_path=ledger_path)
    plan_dict = plan_record.get("plan") if isinstance(plan_record, dict) else None
    if isinstance(plan_dict, dict):
        scope = str(plan_dict.get("scope") or "")
        if scope and scope != SCOPE_STRUCTURE_ONLY:
            # The applier refuses a plan whose scope is not structure-only, so
            # an apply record for one should not exist -- but the ledger is
            # append-only and hand-editable, and this module must not be the
            # one that acts on a cross-page plan it cannot reverse.
            return ComputedUndo(
                action=UNDO_REFUSED,
                reason="plan_scope_not_structure_only",
                plan_id=plan_id,
                uid=str(record.get("uid") or ""),
            )

    uid = str(record.get("uid") or "")
    created = _created_uids(record)
    created_set = set(created)
    index = _index_pages_by_uid(Path(wiki_root))
    knowledge_root = Path(wiki_root).parent

    restores: list[PageRestore] = []
    deletions: list[PageDeletion] = []
    blocked: list[BlockedPage] = []
    touched_paths: set[Path] = set()

    for row in _record_pages(record):
        page_uid = str(row.get("uid") or "")
        recorded_path = str(row.get("path") or "")
        post_hash = str(row.get("post_content_hash") or "")
        pre_hash = str(row.get("pre_content_hash") or "")
        pre_blob = str(row.get("pre_blob_id") or "")
        is_created = page_uid in created_set

        path = Path(recorded_path) if recorded_path else None
        if path is None or not path.is_file():
            # A moved or renamed page is still findable by uid; the recorded
            # path is only the first guess.
            path = index.get(page_uid)
        label = page_uid or (path.name if path is not None else recorded_path)

        if path is None or not path.is_file():
            if is_created:
                # Already gone: undo wanted to delete it and someone did.
                # Nothing to write, nothing to adjudicate.
                continue
            blocked.append(
                BlockedPage(uid=page_uid, path=recorded_path, reason=REASON_PAGE_MISSING)
            )
            continue
        touched_paths.add(path)

        try:
            current_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            blocked.append(
                BlockedPage(uid=page_uid, path=str(path), reason=REASON_PAGE_UNREADABLE)
            )
            continue
        _, current_body = parse_frontmatter(current_text)

        if post_hash and page_content_hash(current_body) != post_hash:
            # AC3: write nothing, carry a diff. A created sub-page's
            # pre-image is the empty string by construction, so only a
            # MODIFIED page needs its blob read -- and when that blob is
            # unreadable too, the diff says so rather than letting a
            # whole-file addition imply the page was created here.
            pre_text = ""
            note = ""
            if not is_created:
                blob = read_git_blob(knowledge_root, pre_blob)
                if blob is None:
                    note = (
                        f"(the pre-image blob {pre_blob or '(none recorded)'} is NOT "
                        "readable either; the diff below shows the current page "
                        "in full, not a comparison against it)\n"
                    )
                else:
                    pre_text = blob.decode("utf-8", errors="replace")
            blocked.append(
                BlockedPage(
                    uid=page_uid,
                    path=str(path),
                    reason=REASON_PAGE_CHANGED,
                    diff=note + _unified_diff(pre_text, current_text, label=label),
                )
            )
            continue

        if is_created:
            deletions.append(PageDeletion(uid=page_uid, path=path))
            continue

        blob = read_git_blob(knowledge_root, pre_blob)
        if blob is None:
            blocked.append(
                BlockedPage(
                    uid=page_uid,
                    path=str(path),
                    reason=REASON_PRE_IMAGE_UNREADABLE,
                    diff=f"(no pre-image: blob {pre_blob or '(none recorded)'} is not readable)\n",
                )
            )
            continue
        restores.append(
            PageRestore(
                uid=page_uid,
                path=path,
                text=blob.decode("utf-8"),
                pre_content_hash=pre_hash,
            )
        )

    inbound = find_inbound_links(
        Path(wiki_root),
        created,
        exclude=touched_paths | {d.path for d in deletions},
    )

    # AC4 before AC3, deliberately: an inbound link is not adjudicable by a
    # diff. Queueing "here is what changed, undo anyway?" for a plan whose
    # undo would be refused regardless asks a human a question whose answer
    # cannot be carried out.
    if inbound:
        return ComputedUndo(
            action=UNDO_REFUSED,
            reason="created_sub_page_has_inbound_links",
            plan_id=plan_id,
            uid=uid,
            inbound_links=inbound,
        )
    if blocked:
        return ComputedUndo(
            action=UNDO_QUEUED,
            reason=blocked[0].reason,
            plan_id=plan_id,
            uid=uid,
            blocked=tuple(blocked),
        )
    return ComputedUndo(
        action=UNDO_DIRECT,
        reason="restorable",
        plan_id=plan_id,
        uid=uid,
        restores=tuple(restores),
        deletions=tuple(deletions),
    )


# ---------------------------------------------------------------------------
# The queued-proposal path (AC3), through unfold.py's own queueing call.
# ---------------------------------------------------------------------------


def _queue_undo_proposal(
    computed: ComputedUndo,
    *,
    wiki_root: Path,
    pending_path: Path | None,
) -> dict[str, Any]:
    """Raise ONE decision item for a plan whose undo is not directly safe.

    Reuses :func:`athenaeum.answers.raise_pending_question` end to end,
    exactly as :mod:`athenaeum.unfold`'s ``_queue_unfold_proposal`` does
    (the issue asks for that queueing call by name) -- no new queue type and
    no new payload shape. One item per PLAN, not per changed page: the
    decision a human makes is "undo this recompile or not", and a per-page
    item would ask them the same question several times with no way to
    answer it once.
    """
    from athenaeum.answers import raise_pending_question

    target = pending_path or (Path(wiki_root) / "_pending_questions.md")
    question = (
        f"Undo recompile plan {computed.plan_id} on page {computed.uid} "
        "even though it is no longer directly reversible?"
    )
    lines = [
        f"Recompile plan: {computed.plan_id}",
        f"Recompiled page uid: {computed.uid}",
        f"Not directly undoable: {computed.reason}.",
        "",
        "Nothing was written. Each page below blocks a direct undo, with a "
        "diff of the pre-image undo would have restored against what is on "
        "disk now. A created sub-page has no pre-image by definition (it did "
        "not exist before the apply), so its diff shows the whole page as an "
        "addition -- undo would delete it.",
    ]
    for page in computed.blocked:
        lines += [
            "",
            f"### {page.uid} ({page.path})",
            f"Reason: {page.reason} -- {_REASON_EXPLANATIONS.get(page.reason, '')}",
        ]
        if page.diff:
            lines += ["", "```diff", page.diff.rstrip("\n"), "```"]
    lines += [
        "",
        "Coarse fallback if the answer is yes: `git revert` the apply's own "
        "snapshot commit in the knowledge store, which reverses the whole "
        "apply including any merged section's originals.",
    ]
    return raise_pending_question(
        target,
        question,
        "\n".join(lines),
        entity=computed.uid or "(page recompile)",
        source=f"page-recompile-undo:{computed.plan_id}",
        kind="question",
    )


# ---------------------------------------------------------------------------
# The ledger record (AC6) and the orchestrator.
# ---------------------------------------------------------------------------


@dataclass
class UndoOutcome:
    """What :func:`write_undo` did, for the CLI to report."""

    action: str
    reason: str
    plan_id: str = ""
    uid: str = ""
    restored_uids: tuple[str, ...] = ()
    deleted_uids: tuple[str, ...] = ()
    blocked: tuple[BlockedPage, ...] = field(default_factory=tuple)
    inbound_links: tuple[InboundLink, ...] = field(default_factory=tuple)
    snapshot_sha: str | None = None
    ledger_record_id: str = ""
    queued_item_id: str = ""
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        """True for a completed direct undo, and for a dry run that produced
        a real report.

        A dry run's deliverable IS the report (AC7: "reports direct or
        queued and why"), so ``queued``/``refused`` are successful dry-run
        outcomes -- with the one exception of
        :data:`REASON_NO_APPLY_RECORD`, where the plan id named nothing and
        there was no report to produce.
        """
        if self.dry_run:
            return self.reason != REASON_NO_APPLY_RECORD
        return self.action == UNDO_DIRECT

    @property
    def inbound_link_count(self) -> int:
        """AC4's count: how many references to a created sub-page a third
        page holds."""
        return len(self.inbound_links)


def _build_ledger_record(computed: ComputedUndo) -> dict[str, Any]:
    """The AC6 record: the plan id this undo reverses, which pages were
    restored (with the pre-image content hash the planner's own skip
    compares against), and which created pages were deleted."""
    return {
        "v": PAGE_RECOMPILE_LEDGER_VERSION,
        "kind": UNDO_KIND,
        # `id` is the plan id, the same convention the `apply` record uses
        # (its subject IS the plan); `plan_id` says so by name, so a reader
        # of this record never has to know that convention. AC6 asks for the
        # reference, not for which key carries it.
        "id": computed.plan_id,
        "plan_id": computed.plan_id,
        "ts": now_iso(),
        "uid": computed.uid,
        "restored": [
            {"uid": r.uid, "path": str(r.path), "pre_content_hash": r.pre_content_hash}
            for r in computed.restores
        ],
        "deleted": [{"uid": d.uid, "path": str(d.path)} for d in computed.deletions],
    }


def write_undo(
    wiki_root: Path,
    *,
    plan_id: str,
    config: dict[str, Any] | None = None,
    ledger_path: Path | None = None,
    pending_path: Path | None = None,
    dry_run: bool = False,
) -> UndoOutcome:
    """Undo ONE applied recompile plan end to end (issue athenaeum#2541).

    Computes the whole decision first (:func:`compute_undo`), then does
    exactly one of three things: restores every page from its pre-image and
    deletes the created sub-pages, appends the ``undo`` record and makes one
    snapshot commit (DIRECT); raises one decision item carrying a diff per
    blocking page and writes no page (QUEUED); or returns the refusal and
    its count (REFUSED). *dry_run* stops after the decision: nothing is
    written, not even the queue item (AC7).
    """
    computed = compute_undo(wiki_root, plan_id=plan_id, ledger_path=ledger_path)

    if dry_run:
        return UndoOutcome(
            action=computed.action,
            reason=computed.reason,
            plan_id=computed.plan_id,
            uid=computed.uid,
            restored_uids=tuple(r.uid for r in computed.restores),
            deleted_uids=tuple(d.uid for d in computed.deletions),
            blocked=computed.blocked,
            inbound_links=computed.inbound_links,
            dry_run=True,
        )

    if computed.action == UNDO_REFUSED:
        return UndoOutcome(
            action=UNDO_REFUSED,
            reason=computed.reason,
            plan_id=computed.plan_id,
            uid=computed.uid,
            inbound_links=computed.inbound_links,
        )

    if computed.action == UNDO_QUEUED:
        item = _queue_undo_proposal(computed, wiki_root=wiki_root, pending_path=pending_path)
        return UndoOutcome(
            action=UNDO_QUEUED,
            reason=computed.reason,
            plan_id=computed.plan_id,
            uid=computed.uid,
            blocked=computed.blocked,
            # `raise_pending_question` names its id `decision_id` on the
            # success path; `id` is accepted as a fallback so a future rename
            # degrades to an empty string rather than a KeyError.
            queued_item_id=str(item.get("decision_id") or item.get("id") or ""),
        )

    # Parents first, created pages last -- see the module docstring.
    for restore in computed.restores:
        atomic_write_text(restore.path, restore.text)
    for deletion in computed.deletions:
        try:
            deletion.path.unlink()
        except OSError:
            pass

    target_ledger = (
        ledger_path if ledger_path is not None else default_page_recompile_ledger_path(wiki_root)
    )
    _append_record(target_ledger, _build_ledger_record(computed))

    knowledge_root = Path(wiki_root).parent
    snapshot_sha = FilesystemStore(knowledge_root, config or {}).snapshot(
        f"page-recompile undo {computed.uid} ({computed.plan_id})"
    )

    return UndoOutcome(
        action=UNDO_DIRECT,
        reason="undone",
        plan_id=computed.plan_id,
        uid=computed.uid,
        restored_uids=tuple(r.uid for r in computed.restores),
        deleted_uids=tuple(d.uid for d in computed.deletions),
        snapshot_sha=snapshot_sha,
        ledger_record_id=computed.plan_id,
    )


__all__ = [
    "REASON_NO_APPLY_RECORD",
    "REASON_PAGE_CHANGED",
    "REASON_PAGE_MISSING",
    "REASON_PAGE_UNREADABLE",
    "REASON_PRE_IMAGE_UNREADABLE",
    "UNDO_DIRECT",
    "UNDO_QUEUED",
    "UNDO_REFUSED",
    "BlockedPage",
    "ComputedUndo",
    "InboundLink",
    "PageDeletion",
    "PageRestore",
    "UndoOutcome",
    "compute_undo",
    "find_apply_record",
    "find_inbound_links",
    "read_git_blob",
    "write_undo",
]
