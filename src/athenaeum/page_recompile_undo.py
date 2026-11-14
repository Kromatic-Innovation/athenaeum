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

**That reference has to name the page's RAW bytes, and this slice had to
fix the applier so it does (athenaeum#2541).** It originally hashed
``render_frontmatter(parsed_meta) + "\n" + body`` -- a RE-RENDER, which
normalises the page. For a canonically-written page the re-render happens
to reproduce the bytes, so a canonical fixture cannot catch it; for a page
whose frontmatter is followed by no blank line (6.0% of a seeded 600-page
sample of a real 26,660-page wiki) it produces bytes that were never
written anywhere, naming a blob git does not have. Every such apply would
have degraded to a queued decision item with an unreadable-pre-image
reason, for no reason at all.
``TestAC1DirectUndoRestoresTheWiki::test_a_page_written_non_canonically_still_undoes_directly``
is the regression test; it fails against the re-rendered pre-image.

**Reversible default when a pre-image blob is NOT readable (athenaeum#2541
lane decision).** The applier takes its snapshot AFTER the write, so a
pre-image lives in that commit's PARENT -- but nothing guarantees a
pre-apply commit ever happened (a page created and recompiled between two
snapshots was never staged, so its pre-image bytes exist in no git
object). Since the applier was corrected to record the raw bytes, that is
the ONLY remaining reason a pre-image can be missing, and it is a real
one. The blob can therefore be legitimately absent, and an absent
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
child-side row with the file. For a SPLIT's own relations
:func:`athenaeum.page_relations.remove_relation` is deliberately NOT
called: it REWRITES the parent (to re-render its Sub-pages section), which
would undo the byte-exact restore this module just made and defeat AC1's
own round-trip test. This module invents no rewrite-record format the
applier never writes.

**An ATTACH is the one relation a pre-image restore does not name, and it
is undone by a detach (issue athenaeum#2548 AC11).** ``attach`` /
``attach-self-under`` relate the recompiled page to a page OUTSIDE the
apply, through :func:`athenaeum.page_relations.set_relation`, and the
applier records the pair on its ``attachments`` ledger rows. So for those
-- and only those -- :func:`write_undo` calls
:func:`athenaeum.page_relations.remove_relation` first, BEFORE the
pre-image restores, so the detach's own two-page rewrite is then
overwritten by the authoritative pre-images rather than the other way
round. Both attached pages are also ordinary ``pages`` rows, so an attach
onto a page someone has since edited blocks the undo like any other
changed page instead of being silently reverted.

**Why "a later page links to a created sub-page" (AC4) is checkable at
all.** A created sub-page's uid did not exist before the apply, so any
reference to it from a page OTHER than the recompiled parent (and other
than the created sub-pages' own cross-links) was necessarily added after
the apply. The scan is therefore "any inbound reference from a third
page", with no timestamp heuristic anywhere. FOUR shapes, two in the
frontmatter (a ``related`` row naming a created uid, and a ``parent:``
key naming one) and two in the body (``[[uid|Label]]`` wikilinks and
``[Label](uid-slug.md)`` markdown inline links) -- see
:func:`find_inbound_links` for why the markdown form is the one that
matters most in practice, and what counting only the others cost. Any hit
refuses the undo and reports the COUNT, de-duplicated per logical
reference because AC4 asks for that count by name -- deleting a page
something else points at would leave a dangling link, which no diff a
human adjudicates can repair.

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

**Cross-page applies, added by issue athenaeum#2548 AC11.** Moves to other
pages, created standalone pages and attaches were out of scope for
athenaeum#2541 because the applier could not perform them, and a plan whose
scope was not ``structure-only`` was refused here for that reason. Slice S7
makes them real, and they need almost nothing new from this module: a move
target is an ordinary ``pages`` row whose pre-image restore is exactly
"remove the moved units", a created standalone page is an ordinary
``created_uids`` deletion, and a target edited since the apply takes the
same queued path every other changed page does. The one addition is the
detach above. Still out of scope: any time-based expiry of undo.

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
import logging
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter
from athenaeum.page_recompile_apply import APPLY_KIND, _git_blob_id
from athenaeum.page_recompile_plan import (
    PAGE_RECOMPILE_LEDGER_VERSION,
    UNDO_KIND,
    _append_record,
    default_page_recompile_ledger_path,
    page_content_hash,
    read_page_recompile_ledger,
)
from athenaeum.page_relations import (
    _LEGACY_ROLE_CHILD_NAMES_PARENT,
    RelationError,
    _discover_wiki_pages,
    _index_pages_by_uid,
    _related_rows,
    read_relations,
    remove_relation,
)
from athenaeum.store import FilesystemStore, now_iso

log = logging.getLogger(__name__)

#: :attr:`UndoOutcome.action` values. ``direct`` and ``queued`` are
#: :mod:`athenaeum.unfold`'s own two, named identically on purpose -- this is
#: the same split over the same kind of record. ``refused`` is this module's
#: third: AC4's inbound-link case is not adjudicable by a diff, so it is
#: neither done nor queued.
UNDO_DIRECT = "direct"
UNDO_QUEUED = "queued"
UNDO_REFUSED = "refused"

#: Already undone, and not re-applied since. Its own refusal rather than a
#: queued "the page changed" item: after a direct undo the page genuinely no
#: longer matches the apply's post hash, so without this check a second
#: ``recompile undo`` of the same plan id would raise a decision item whose
#: diff is EMPTY (the page already holds the pre-image) and whose stated
#: reason -- "someone edited this" -- is false.
REASON_ALREADY_UNDONE = "already_undone"

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

#: A markdown INLINE link's target: ``[Label](34498e6a-learn-s-m-a-r-t.md)``.
#: Scanned alongside the wikilink form because it is the form the real corpus
#: overwhelmingly uses -- a census of all 26,649 live wiki pages found 167
#: markdown links resolving to a real page file against 8 uid-form wikilinks.
#: A scan of wikilinks alone would therefore cover ~5% of real inbound
#: references and miss ~95% of them, which for AC4 means refusing almost
#: nothing and deleting a page that other pages still point at.
_MD_LINK_RE = re.compile(r"\]\(\s*<?([^)<>\s]+)>?\s*\)")


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


def last_action_for_plan(
    plan_id: str, wiki_root: Path, *, ledger_path: Path | None = None
) -> str:
    """``"apply"`` / ``"undo"`` / ``""`` -- whichever of the two this plan id
    did LAST, by ledger order.

    Not "is there an undo record", which would be wrong: the ledger is
    append-only, so a plan that was applied, undone, and then re-applied
    carries both kinds, and the re-apply is live work an undo must still be
    able to reverse. Only an undo that is the plan's most recent action
    means "already undone".
    """
    last = ""
    for record in read_page_recompile_ledger(wiki_root, ledger_path=ledger_path):
        if str(record.get("id")) != plan_id:
            continue
        kind = record.get("kind")
        if kind in (APPLY_KIND, UNDO_KIND):
            last = str(kind)
    return last


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
    wiki_root: Path, created: Mapping[str, str], *, exclude: set[Path]
) -> tuple[InboundLink, ...]:
    """Every reference to a created sub-page from a page not in *exclude*
    (AC4).

    *created* maps each created sub-page's uid to its FILENAME
    (``<uid>-<slug>.md``), because a reference can name either: the
    frontmatter shapes name the uid, while both body link forms name the
    file. An empty filename is tolerated (the record is all this module has
    to go on) and simply narrows that page to its uid forms.

    *exclude* is the recompiled parent plus the created sub-pages
    themselves -- the parent's rows are removed by the pre-image restore and
    a created page's own rows go with its file, so neither is a reason to
    refuse. Page discovery is
    :func:`athenaeum.page_relations._discover_wiki_pages`, the existing
    primitive, not a second corpus walk.

    **Four reference shapes, not one.** Two in the frontmatter (a
    ``related`` row naming the uid, and the ``parent:`` key) and two in the
    body: ``[[uid|Label]]`` wikilinks and ``[Label](uid-slug.md)`` markdown
    inline links. The markdown form is the one that matters most in
    practice -- see :data:`_MD_LINK_RE` for the corpus census -- and it was
    the one originally missing, which made this scan refuse almost nothing.

    **One logical reference is counted once.** AC4 requires undo to name
    the COUNT of inbound links, so an inflated count is a wrong answer, not
    a cosmetic one. :func:`athenaeum.page_relations.read_relations`
    normalises a page's new-shape ``parent:`` key and its legacy
    ``split-from`` ``related`` row to ONE parent relation; counting both
    rows would report two references where a reader of that frontmatter
    sees one. The row that encodes the resolved parent is therefore skipped
    in favour of the single ``parent-key`` entry. Distinct SHAPES of a
    genuinely distinct reference (a ``see-also`` row AND a body link, say)
    are separate references and are each counted.
    """
    wanted = set(created)
    if not wanted:
        return ()
    names_to_uid: dict[str, str] = {}
    for uid, filename in created.items():
        for key in (filename, Path(filename).stem if filename else ""):
            if key:
                names_to_uid[key] = uid
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

        relations = read_relations(meta_dict)
        parent_uid = relations.parent.uid if relations.parent is not None else None
        for row in _related_rows(meta_dict):
            if not isinstance(row, dict):
                continue
            row_uid = row.get("uid")
            if not isinstance(row_uid, str) or row_uid not in wanted:
                continue
            if row_uid == parent_uid and row.get("role") == _LEGACY_ROLE_CHILD_NAMES_PARENT:
                # The legacy half of the SAME parent relation the
                # `parent-key` entry below already counts.
                continue
            found.append(InboundLink(path=str(path), uid=row_uid, kind="related-row"))
        if parent_uid is not None and parent_uid in wanted:
            found.append(InboundLink(path=str(path), uid=parent_uid, kind="parent-key"))

        for match in _WIKILINK_RE.finditer(body):
            target = match.group(1).strip()
            uid = target if target in wanted else names_to_uid.get(target, "")
            if uid:
                found.append(InboundLink(path=str(path), uid=uid, kind="wikilink"))
        for match in _MD_LINK_RE.finditer(body):
            uid = _md_target_uid(match.group(1), wanted, names_to_uid)
            if uid:
                found.append(InboundLink(path=str(path), uid=uid, kind="markdown-link"))
    return tuple(found)


def _md_target_uid(
    raw_target: str, wanted: set[str], names_to_uid: Mapping[str, str]
) -> str:
    """The created-sub-page uid a markdown link target names, or ``""``.

    Tolerates the shapes a hand-written or generated link actually uses: a
    bare filename, a ``./``-prefixed one, a ``#anchor`` suffix, and a
    percent-or-query tail. An absolute URL is never a wiki page reference
    and is rejected before any of that.
    """
    target = raw_target.strip()
    if not target or "://" in target or target.startswith("#"):
        return ""
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return ""
    name = PurePosixPath(target).name
    if name in names_to_uid:
        return names_to_uid[name]
    stem = PurePosixPath(name).stem
    if stem in names_to_uid:
        return names_to_uid[stem]
    if stem in wanted:
        return stem
    return ""


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
REASON_POST_HASH_MISSING = "apply_record_missing_post_hash"
REASON_PRE_HASH_MISSING = "apply_record_missing_pre_hash"

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
    REASON_POST_HASH_MISSING: (
        "The apply record does not say what this page's content hash was "
        "immediately after the apply, so there is no way to establish that "
        "the page has not been edited since -- and a direct restore would "
        "silently discard any edit there has been. Today's applier always "
        "records it, so this means the ledger record is truncated or was "
        "hand-edited."
    ),
    REASON_PRE_HASH_MISSING: (
        "The apply record does not say what this page's content hash was "
        "BEFORE the apply. The restore itself would work, but the undo "
        "record could not pin the hash the planner compares against to "
        "avoid re-drafting the same plan -- so the page would be recompiled "
        "again on the next pass, unattended, with nothing reporting why. "
        "Today's applier always records it, so this means the ledger record "
        "is truncated or was hand-edited."
    ),
    REASON_PRE_IMAGE_UNREADABLE: (
        "The page itself is untouched, but its pre-image is not readable "
        "from the knowledge store's git objects -- the apply records the "
        "pre-image's blob id, and that blob is absent (most likely the page "
        "was never committed before the apply, so its earlier bytes were "
        "never stored) or does not hash back to the recorded id. There is "
        "no pre-image to restore or to diff against: use `git revert` of "
        "the apply's own snapshot commit instead. The same reason covers a "
        "pre-image whose bytes are not valid UTF-8, which this module will "
        "not write back to a page it cannot read."
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


@dataclass(frozen=True)
class AttachmentUndo:
    """One ``attach`` relation to detach (issue athenaeum#2548 AC11)."""

    child_uid: str
    parent_uid: str


@dataclass
class ComputedUndo:
    """Everything :func:`write_undo` needs, computed before any write."""

    action: str = UNDO_REFUSED
    reason: str = ""
    plan_id: str = ""
    uid: str = ""
    restores: tuple[PageRestore, ...] = ()
    #: Relations an ``attach`` set, detached before the restores run.
    attachments: tuple[AttachmentUndo, ...] = ()
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

    if last_action_for_plan(plan_id, wiki_root, ledger_path=ledger_path) == UNDO_KIND:
        return ComputedUndo(
            action=UNDO_REFUSED,
            reason=REASON_ALREADY_UNDONE,
            plan_id=plan_id,
            uid=str(record.get("uid") or ""),
        )

    # Issue athenaeum#2548 AC11: a CROSS-PAGE apply is undoable, and this
    # function needs no new machinery to reverse it. The applier records
    # every page it touched -- move targets, created standalone pages and
    # the two pages an attach wrote -- as ordinary ``pages`` rows with a
    # pre-image blob id, so the loop below restores a move target's
    # pre-image (which is what REMOVES the moved units from it) and deletes
    # a created standalone page exactly as it does a created sub-page. The
    # one thing a pre-image restore does not NAME is the relation an attach
    # set, which :func:`write_undo` detaches through
    # :func:`athenaeum.page_relations.remove_relation`.
    #
    # The scope refusal this check replaced (``plan_scope_not_structure_only``,
    # issue athenaeum#2541) was correct while cross-page applies could not
    # happen: an apply record for one could then only come from a
    # hand-edited ledger. It is now a legitimate record, and refusing it
    # would leave exactly the applies that touch the most pages with no
    # undo at all.
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

        if not post_hash:
            # A3: fail CLOSED. Without the post hash there is no way to
            # establish the page is still what the apply wrote, and a
            # restore would silently discard any later edit. Same
            # hand-edited-ledger threat model as the scope check above.
            blocked.append(
                BlockedPage(uid=page_uid, path=str(path), reason=REASON_POST_HASH_MISSING)
            )
            continue

        if page_content_hash(current_body) != post_hash:
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

        if not pre_hash:
            # A4's sibling of the check above, failing closed for the same
            # reason: the restore would work, but the undo record could not
            # pin the hash the planner's AC6 skip compares against, so the
            # page would be re-planned and re-applied on the next pass with
            # nothing reporting why.
            blocked.append(
                BlockedPage(uid=page_uid, path=str(path), reason=REASON_PRE_HASH_MISSING)
            )
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
        try:
            pre_image_text = blob.decode("utf-8")
        except UnicodeDecodeError:
            # A pre-image this module cannot decode is a pre-image it must
            # not write back -- a lossy `errors="replace"` restore would
            # corrupt the page it is supposed to be repairing. Routed to the
            # same queue as an absent blob, which is what the contract
            # ("unreadable pre-image -> write nothing, queue") already says.
            blocked.append(
                BlockedPage(
                    uid=page_uid,
                    path=str(path),
                    reason=REASON_PRE_IMAGE_UNREADABLE,
                    diff=f"(pre-image blob {pre_blob} is not valid UTF-8; "
                    "it cannot be restored without corrupting the page)\n",
                )
            )
            continue
        restores.append(
            PageRestore(
                uid=page_uid,
                path=path,
                text=pre_image_text,
                pre_content_hash=pre_hash,
            )
        )

    # uid -> filename for every created sub-page, so the scan can recognise
    # the body link forms (which name the FILE) as well as the frontmatter
    # ones (which name the uid). Read off the apply record, so a created
    # page already deleted from disk is still matchable.
    # Built with a plain loop, NOT a comprehension with a walrus: a walrus
    # inside a comprehension binds in the ENCLOSING scope, which silently
    # rebound this function's own `uid` (the parent's) to the last created
    # sub-page's -- caught by the snapshot-subject and ledger-record tests.
    created_filenames: dict[str, str] = {uid: "" for uid in created}
    for row in _record_pages(record):
        row_uid = str(row.get("uid") or "")
        if row_uid in created_set:
            created_filenames[row_uid] = Path(str(row.get("path") or "")).name
    inbound = find_inbound_links(
        Path(wiki_root),
        created_filenames,
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
        attachments=_record_attachments(record),
    )


def _record_attachments(record: "Mapping[str, Any]") -> tuple[AttachmentUndo, ...]:
    """The ``attach`` pairs an apply record names (issue athenaeum#2548
    AC11). Tolerant of an absent or malformed field -- every apply record
    written before slice S7 has none."""
    raw = record.get("attachments")
    if not isinstance(raw, list):
        return ()
    out: list[AttachmentUndo] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        child = str(row.get("child") or "")
        parent = str(row.get("parent") or "")
        if child and parent:
            out.append(AttachmentUndo(child_uid=child, parent_uid=parent))
    return tuple(out)


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

    # AC11 of issue athenaeum#2548: "an attach is undone by a detach", and
    # it runs FIRST -- `remove_relation` rewrites both sides of the pair,
    # so the authoritative pre-image restores below must land on top of it,
    # not under it. A refusal here is never fatal: the restore that follows
    # reproduces the pre-image bytes regardless, and the only way
    # `remove_relation` refuses is that the relation is already gone.
    for attachment in computed.attachments:
        try:
            remove_relation(wiki_root, child_uid=attachment.child_uid)
        except RelationError as exc:
            log.debug(
                "page-recompile undo: detach of %s from %s skipped: %s",
                attachment.child_uid,
                attachment.parent_uid,
                exc,
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
    "REASON_ALREADY_UNDONE",
    "REASON_POST_HASH_MISSING",
    "REASON_PRE_HASH_MISSING",
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
    "last_action_for_plan",
    "read_git_blob",
    "write_undo",
]
