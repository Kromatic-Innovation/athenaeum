# SPDX-License-Identifier: Apache-2.0
"""Integrate mode for the pages repository (athenaeum#2935, slice S7 of the sync design).

Sections 3.3, 4, 5 and 6.3 of ``docs/design/federation-multi-writer-sync.md``.

A store declares ``sync.mode: integrate`` in its ``_store/access.yaml``; one
install may narrow that locally (``sync_mode: fast_forward``) and may never
widen it. :func:`resolve_sync_mode` is the whole rule. For a repository in
integrate mode the run does:

* the repository lease (:mod:`athenaeum.sync_lease`), taken by
  :func:`sync_before_compile` and dropped by :func:`push_after_compile`;
* :func:`sync_before_compile` -- snapshot uncommitted work as a local commit,
  fetch, then integrate (step 2 to 4 of section 4);
* the normal compile, which commits as today;
* :func:`push_after_compile` -- push without force; on a rejection fetch,
  integrate and push again, at most three attempts with a short random wait
  between them (step 6 and 7). After the last the commits stay local and the
  outcome is ``push: failed, reason: contended``.

:func:`integrate_remote` is section 4.1. Nothing is rebased: the result is one
merge commit with two parents, built by the store seam
(:func:`athenaeum.store.write_merge_commit`) all-or-nothing. A path changed on
one side only takes that side. A path changed on both sides is merged by its
kind (section 5): a page by :func:`athenaeum.page_merge.merge_page`, the pending
merges list by :func:`athenaeum.record_merge.merge_records`, a regenerated file
by taking the remote side (the compile regenerates it), and **everything else
keeps the remote version and is reported held** -- every file under ``_store/``
and any path nothing has a rule for. A contested page unit also keeps the
remote value (``merge_page`` does that). Nothing here calls a model. The held
list is returned and written into the merge commit (section 6.3), and each held
*page* is filed as a ``held-page`` decision in the owning store's queue
(athenaeum#2995, :func:`make_held_filer`) before that commit is written, so its
``Athenaeum-Page`` line can carry the decision id. A person's answer is applied
as an ordinary new commit by :func:`answer_held_decision`.

Before the merge commit is written (section 4.3), every page changed on both
sides is checked: frontmatter parses with ``uid``, ``type`` and ``name``, no
conflict marker, and no link from a more widely shared surface to a more
private one. A page that fails is held and keeps the remote version. One uid on
two of the pages either side changed is handled by section 7.4
(athenaeum#2996): added on both sides with the same ``type`` and normalised ``name``
it is one page merged against an empty ancestor; with a different ``type`` or ``name``
it is a collision and the local, unpublished page is re-minted (new 16-hex uid from
:func:`athenaeum.models.generate_uid`, file renamed, references made in the same
unpublished commits rewritten) and recorded as ``Athenaeum-Uid-Reminted``. Any other
shape (the uid is already in the common ancestor, or more than two pages carry it)
still blocks the integrate and nothing is merged or pushed.

The merge commit message carries ``Athenaeum-Sync: 1`` and one
``Athenaeum-Page:`` line per page changed on both sides, with git blob ids and
reason codes only. No page text and no path is written into it.

Layering: L4. Imports the store seam (:mod:`athenaeum.store`), the pure merges
(:mod:`athenaeum.page_merge`, :mod:`athenaeum.record_merge`), the declaration
(:mod:`athenaeum.store_access`) and the link rule
(:mod:`athenaeum.store_boundary`, :mod:`athenaeum.store_links`) and the per-store
decision queue (:mod:`athenaeum.store_decisions`, L2). It runs no git
command itself and imports nothing from :mod:`athenaeum.librarian`.
"""

from __future__ import annotations

import logging
import random
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from athenaeum.config import (
    StructuredUpsertSpec,
    resolve_push_branch,
    resolve_push_remote,
    resolve_structured_upsert,
)
from athenaeum.erasure import erasure_commit_line, erasure_marks
from athenaeum.models import generate_uid, parse_frontmatter
from athenaeum.page_merge import merge_page
from athenaeum.record_merge import KIND_PENDING_MERGES, merge_derived, merge_records
from athenaeum.storage import available_adapters, open_store, surface_visibility
from athenaeum.store import (
    PushFailureReason,
    PushResult,
    WorkingTreeNotCleanError,
    blob_id,
    blob_id_at_revision,
    blob_id_of,
    changed_paths,
    commit_paths,
    commit_repo,
    common_ancestor,
    current_branch,
    deletion_commit_messages,
    fast_forward_branch,
    fetch_remote_branch,
    is_ancestor,
    list_files_at_revision,
    push_branch,
    read_blob,
    read_file_at_revision,
    repo_head,
    write_merge_commit,
)
from athenaeum.store_access import (
    StoreDeclaration,
    StoreDeclarationInvalid,
    load_store_declaration,
)
from athenaeum.store_access_evidence import record_fetch, record_push
from athenaeum.store_boundary import link_forbidden
from athenaeum.store_decisions import (
    PageQuote,
    StoreDecisionRefused,
    _rights,
    file_decision,
    load_queue,
    resolve_decision,
)
from athenaeum.store_links import extract_link_refs
from athenaeum.sync_lease import LEASE_HELD, LEASE_TAKEN, acquire_repo_lease, release_repo_lease
from athenaeum.sync_machine_local import check_repo as _check_machine_local

log = logging.getLogger(__name__)

__all__ = [
    "DECISION_KIND_HELD_PAGE",
    "MAX_PUSH_ATTEMPTS",
    "HeldDecisionRefused",
    "HeldItem",
    "IntegrateResult",
    "PageRoot",
    "RepoSyncPlan",
    "PushOutcome",
    "SyncModeDecision",
    "SyncSettings",
    "SyncStep",
    "SyncReport",
    "answer_held_decision",
    "integrate_remote",
    "make_held_filer",
    "plan_repo_syncs",
    "push_after_compile",
    "resolve_sync_mode",
    "sync_before_compile",
    "validate_page_text",
]

MODE_FAST_FORWARD = "fast_forward"
MODE_INTEGRATE = "integrate"

#: Pushes tried in one run before the commits stay local (design note §4.2).
MAX_PUSH_ATTEMPTS = 3
#: The short random wait, in seconds, between two push attempts.
RETRY_WAIT_RANGE = (0.2, 1.5)

REASON_CONTENDED = "contended"
REASON_INTEGRATE_BLOCKED = "integrate-blocked"
REASON_LEASE_HELD = "lease-held"

# Why an integrate was held or blocked.
HELD_STORE_FILE = "store-file"
HELD_UNREGISTERED = "unregistered-path"
HELD_CONTESTED = "contested"
HELD_RECORD = "record"
BLOCK_UNRELATED = "unrelated-histories"
BLOCK_UID_COLLISION = "uid-collision"
BLOCK_FETCH = "fetch-failed"
BLOCK_DIRTY = "uncommitted-changes"

_NONE_BLOB = "none"

#: ``kind`` of the decision a held page opens in the store's decision queue.
DECISION_KIND_HELD_PAGE = "held-page"
#: A held page with this reason is not filed: the next run tries it again (§6.4).
REASON_DEFERRED = "deferred"

#: The answers a held-page decision takes, in the order they are offered.
ANSWER_KEEP_REMOTE = "keep-remote"
ANSWER_TAKE_LOCAL = "take-local"
ANSWER_MERGED = "merged"
HELD_ANSWERS = (ANSWER_KEEP_REMOTE, ANSWER_TAKE_LOCAL, ANSWER_MERGED)


# ---------------------------------------------------------------------------
# Section 3.3: which mode applies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncModeDecision:
    """The mode one install acts on for one store, and why.

    ``local_ignored`` is true when the local setting asked for more than the
    store declares (a local ``integrate`` on a store that does not declare it);
    the caller reports that. A local setting may only narrow.
    """

    mode: str
    declared: str  # what the store declares; fast_forward when it declares nothing
    local: str | None
    local_ignored: bool = False
    reason: str = ""

    @property
    def integrate(self) -> bool:
        return self.mode == MODE_INTEGRATE


def resolve_sync_mode(
    declaration: StoreDeclaration | StoreDeclarationInvalid | None,
    local_mode: str | None = None,
) -> SyncModeDecision:
    """Apply design note §3.3: the store's declaration, narrowed locally.

    No declaration, or one that does not parse, is ``fast_forward`` (a declaration
    that cannot be read never widens anything). A local ``fast_forward`` narrows a
    declared ``integrate``. A local ``integrate`` never widens: on a store that does
    not declare it, it is ignored and ``local_ignored`` is set. A local value that is
    neither mode is ignored the same way.
    """
    if declaration is None:
        declared, why = MODE_FAST_FORWARD, "no-declaration"
    elif isinstance(declaration, StoreDeclarationInvalid):
        declared, why = MODE_FAST_FORWARD, "declaration-invalid"
    else:
        declared = declaration.sync.mode
        why = "declared"
    if local_mode is None:
        return SyncModeDecision(declared, declared, None, False, why)
    if local_mode not in (MODE_FAST_FORWARD, MODE_INTEGRATE):
        return SyncModeDecision(declared, declared, local_mode, True, why)
    if local_mode == MODE_INTEGRATE and declared != MODE_INTEGRATE:
        return SyncModeDecision(MODE_FAST_FORWARD, declared, local_mode, True, why)
    if local_mode == MODE_FAST_FORWARD and declared == MODE_INTEGRATE:
        return SyncModeDecision(MODE_FAST_FORWARD, declared, local_mode, False, "narrowed-locally")
    return SyncModeDecision(declared, declared, local_mode, False, why)


# ---------------------------------------------------------------------------
# Settings and result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageRoot:
    """A directory of the repository that holds one surface's pages."""

    rel: str  # path from the repository root; "" for the root itself
    surface: str = "wiki"


#: ``(held page, its title, its gist) -> decision id``; see :func:`make_held_filer`.
HeldFiler = Callable[["HeldItem", str, str], str | None]


@dataclass(frozen=True)
class SyncSettings:
    """What the sync needs to know about one repository; every field has a default."""

    page_roots: tuple[PageRoot, ...] = (PageRoot("wiki"),)
    config: Mapping[str, Any] | None = None
    remote: str = "origin"
    max_attempts: int = MAX_PUSH_ATTEMPTS
    wait_range: tuple[float, float] = RETRY_WAIT_RANGE
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    #: Files one held page as a decision and returns its id (``None`` when it could
    #: not be). ``None`` here means no decision is filed (athenaeum#2995).
    file_held: HeldFiler | None = None


@dataclass(frozen=True)
class HeldItem:
    """One file, page or page unit that waits for a person; the remote version stays.

    ``uid`` is empty for a file that is not a page. Blob ids only, never text.
    ``path`` and ``units`` (the contested or held unit names) are set for a page;
    ``decision`` is the id of the decision filed for it (athenaeum#2995), empty
    when none was.
    """

    kind: str
    reasons: tuple[str, ...]
    uid: str = ""
    base: str = _NONE_BLOB
    local: str = _NONE_BLOB
    remote: str = _NONE_BLOB
    path: str = ""
    units: tuple[str, ...] = ()
    decision: str = ""


@dataclass(frozen=True)
class IntegrateResult:
    """What :func:`integrate_remote` did.

    ``status`` is ``up-to-date``, ``fast-forward``, ``merged``, ``blocked`` or
    ``failed``. ``pages_changed_both`` is the uid of each page changed on both sides.
    """

    status: str
    commit: str | None = None
    held: tuple[HeldItem, ...] = ()
    pages_changed_both: tuple[str, ...] = ()
    reason: str = ""
    #: The remote tip this integrate fetched (``None`` when the fetch failed): the
    #: remote's value before a push that follows (athenaeum#2928).
    remote_tip: str | None = None
    #: ``(old uid, new uid)`` of each local page re-minted by this integrate (§7.4).
    reminted: tuple[tuple[str, str], ...] = ()
    #: The uid of each page deleted in this integrate because a side's deletion
    #: commit carried the erasure mark (§5.1). Uids only.
    erased: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in ("up-to-date", "fast-forward", "merged")


@dataclass(frozen=True)
class PushOutcome:
    """Outcome of :func:`push_after_compile`: ``reason`` is set exactly when not ``ok``."""

    ok: bool
    attempts: int
    reason: str | None = None
    detail: str = ""
    #: The remote tip as it stood just before the push that succeeded, and the commit
    #: that push published. Both ``None`` unless the push succeeded and the tip was
    #: known; the filing checks after a push read them (athenaeum#2928).
    remote_before: str | None = None
    pushed: str | None = None
    #: Local pages re-minted by the integrates this push needed (athenaeum#2996).
    reminted: int = 0


@dataclass(frozen=True)
class SyncStep:
    """One row of the run summary: a step, how it went, and why when it did not."""

    step: str  # snapshot | fetch | integrate | push
    outcome: str  # ok | failed | skipped
    reason: str = ""


@dataclass
class SyncReport:
    """Everything one repository's sync did, one :class:`SyncStep` per step."""

    mode: str = MODE_INTEGRATE
    steps: list[SyncStep] = field(default_factory=list)
    integrate: IntegrateResult | None = None
    push: PushOutcome | None = None

    def add(self, step: str, outcome: str, reason: str = "") -> None:
        self.steps.append(SyncStep(step, outcome, reason))


# ---------------------------------------------------------------------------
# Section 4: the sequence
# ---------------------------------------------------------------------------


def sync_before_compile(
    repo: Path, branch: str, settings: SyncSettings | None = None
) -> SyncReport:
    """Steps 2 to 4: snapshot, fetch, integrate. The caller holds the run lock (step 1).

    Never raises for a git or network failure; each is reported as a failed step and
    the run proceeds against the local tree, as it does today.
    """
    report = SyncReport()
    if not _take_lease(repo, report):
        return report
    try:
        return _sync_before_compile(repo, branch, settings, report)
    except BaseException:
        # An exception here would otherwise leave the lease held until the process
        # ends; a normal return keeps it, because the compile and push follow.
        release_repo_lease(repo)
        raise


def _sync_before_compile(
    repo: Path, branch: str, settings: SyncSettings | None, report: SyncReport
) -> SyncReport:
    cfg = settings or SyncSettings()
    _report_machine_local(repo, report)
    try:
        sha = commit_repo(repo, "athenaeum sync: snapshot before integrate")
    except subprocess.CalledProcessError as exc:
        report.add("snapshot", "failed", "commit-failed")
        log.warning("athenaeum sync: snapshot failed in %s: %s", repo, exc)
        report.add("integrate", "skipped", BLOCK_DIRTY)
        return report
    report.add("snapshot", "ok" if sha else "skipped", "" if sha else "nothing-to-commit")
    result = integrate_remote(repo, branch, cfg)
    record_fetch(repo, cfg.remote, not (result.status == "failed" and result.reason == BLOCK_FETCH))
    report.integrate = result
    if result.status == "failed" and result.reason == BLOCK_FETCH:
        report.add("fetch", "failed", result.reason)
        report.add("integrate", "skipped", result.reason)
        return report
    report.add("fetch", "ok")
    report.add(
        "integrate", "ok" if result.ok else "failed", result.status if result.ok else result.reason
    )
    return report


def _take_lease(repo: Path, report: SyncReport) -> bool:
    """Section 9: the repository lease inside the git directory; ``False`` to skip.

    Another process holds it: the repository is skipped and reported, never
    integrated concurrently. Where no lease can be taken (no ``flock``) the sync goes
    on unguarded, as the run lock does.
    """
    status, reason = acquire_repo_lease(repo)
    if status == LEASE_HELD:
        report.add("lease", "skipped", reason)
        report.add("integrate", "skipped", reason)
        return False
    report.add("lease", "ok" if status == LEASE_TAKEN else "skipped", reason)
    return True


def _report_machine_local(repo: Path, report: SyncReport) -> None:
    """Section 5, S6b: a tracked machine-local file is a ``machine-local-files`` row.

    Read-only and run before the snapshot, so a file the snapshot is about to add
    is not counted. It only reports; the sync goes on. Paths go to the log, the
    summary row carries the count.
    """
    paths, error = _check_machine_local(repo)
    if error:
        report.add("machine-local-files", "skipped", error)
    elif paths:
        log.warning(
            "athenaeum sync: machine-local file(s) tracked in %s: %s", repo, ", ".join(paths)
        )
        report.add("machine-local-files", "failed", f"tracked={len(paths)}")
    else:
        report.add("machine-local-files", "ok")


def push_after_compile(
    repo: Path,
    branch: str,
    settings: SyncSettings | None = None,
    report: SyncReport | None = None,
) -> PushOutcome:
    """Steps 6 and 7 under the repository lease (section 9); see :func:`_push_after_compile`.

    The lease is taken here when the run did not already hold it, and dropped on the
    way out: the push ends the sequence. A lease held by another process means no
    push: ``reason=lease-held``.
    """
    status, reason = acquire_repo_lease(repo)
    if status == LEASE_HELD:
        outcome = PushOutcome(False, 0, REASON_LEASE_HELD, reason)
        if report is not None:
            report.push = outcome
            report.add("push", "failed", REASON_LEASE_HELD)
        return outcome
    try:
        return _push_after_compile(repo, branch, settings, report)
    finally:
        release_repo_lease(repo)


def _push_after_compile(
    repo: Path,
    branch: str,
    settings: SyncSettings | None = None,
    report: SyncReport | None = None,
) -> PushOutcome:
    """Steps 6 and 7: push without force; on a rejection integrate and push again.

    Up to ``settings.max_attempts`` pushes, with a short random wait between them. A
    rejection is the only thing retried: any other failure (no network, no permission)
    is reported at once. After the last rejection the commits stay local and the outcome
    is ``reason=contended``. Records one ``push`` step on *report* when given.
    """
    cfg = settings or SyncSettings()
    try:
        commit_repo(repo, "athenaeum sync: snapshot before push")
    except subprocess.CalledProcessError:
        outcome = PushOutcome(False, 0, "other", "snapshot-failed")
    else:
        outcome = _push_loop(repo, branch, cfg, _remote_tip_or_none(repo, branch, cfg))
        record_push(repo, cfg.remote, outcome.ok, outcome.detail or outcome.reason or "")
    if report is not None:
        report.push = outcome
        report.add("push", "ok" if outcome.ok else "failed", outcome.reason or "")
    return outcome


def _remote_tip_or_none(repo: Path, branch: str, cfg: SyncSettings) -> str | None:
    """The remote tip right now, for the filing checks; ``None`` on any failure.

    The fetch adds objects only. A failure here never stops the push: without the
    tip the checks after it simply do not run.
    """
    try:
        return fetch_remote_branch(repo, branch, cfg.remote)
    except subprocess.CalledProcessError:
        return None


def _push_loop(
    repo: Path, branch: str, cfg: SyncSettings, remote_before: str | None = None
) -> PushOutcome:
    attempts = 0
    detail = ""
    reminted = 0
    while attempts < max(1, cfg.max_attempts):
        attempts += 1
        result: PushResult = push_branch(repo, branch, cfg.remote)
        if result.ok:
            return PushOutcome(
                True,
                attempts,
                remote_before=remote_before,
                pushed=repo_head(repo),
                reminted=reminted,
            )
        detail = result.detail
        if result.reason is not PushFailureReason.REJECTED_REMOTE_MOVED:
            return PushOutcome(False, attempts, "other", detail, reminted=reminted)
        if attempts >= max(1, cfg.max_attempts):
            break
        cfg.sleep(cfg.rng.uniform(*cfg.wait_range))
        try:
            commit_repo(repo, "athenaeum sync: snapshot before integrate")
        except subprocess.CalledProcessError:
            return PushOutcome(
                False, attempts, REASON_INTEGRATE_BLOCKED, BLOCK_DIRTY, reminted=reminted
            )
        merged = integrate_remote(repo, branch, cfg)
        if not merged.ok:
            return PushOutcome(
                False, attempts, REASON_INTEGRATE_BLOCKED, merged.reason, reminted=reminted
            )
        remote_before = merged.remote_tip
        reminted += len(merged.reminted)
    return PushOutcome(False, attempts, REASON_CONTENDED, detail, reminted=reminted)


# ---------------------------------------------------------------------------
# Section 4.1: integrate
# ---------------------------------------------------------------------------


def integrate_remote(
    repo: Path, branch: str, settings: SyncSettings | None = None
) -> IntegrateResult:
    """Fetch the remote branch and bring it into local *branch* (design note §4.1)."""
    cfg = settings or SyncSettings()
    try:
        tip = fetch_remote_branch(repo, branch, cfg.remote)
    except subprocess.CalledProcessError as exc:
        log.warning("athenaeum sync: fetch failed in %s: %s", repo, exc)
        return IntegrateResult("failed", reason=BLOCK_FETCH)
    return replace(_integrate_at(repo, branch, tip, cfg), remote_tip=tip)


def _integrate_at(repo: Path, branch: str, tip: str, cfg: SyncSettings) -> IntegrateResult:
    local = repo_head(repo)
    if local is None:
        return IntegrateResult("failed", reason="no-local-commit")
    if is_ancestor(repo, tip, local):
        return IntegrateResult("up-to-date")
    if is_ancestor(repo, local, tip):
        try:
            fast_forward_branch(repo, branch, tip)
        except subprocess.CalledProcessError:
            return IntegrateResult("blocked", reason=BLOCK_DIRTY)
        return IntegrateResult("fast-forward", commit=tip)
    ancestor = common_ancestor(repo, local, tip)
    if ancestor is None:
        return IntegrateResult("blocked", reason=BLOCK_UNRELATED)
    return _merge(repo, branch, ancestor, local, tip, cfg)


def _classify(path: str, roots: tuple[PageRoot, ...]) -> tuple[str, PageRoot | None]:
    """The kind of file *path* is (design note §5): page, record, derived, store or unregistered."""
    pure = PurePosixPath(path)
    if "_store" in pure.parts[:-1]:
        return "store", None
    parent = "" if str(pure.parent) == "." else str(pure.parent)
    root = next((r for r in roots if r.rel.strip("/") == parent), None)
    if root is None:
        return "unregistered", None
    if pure.name == "_pending_merges.md":
        return "record", root
    if pure.name == "_index.md":
        return "derived", root
    if pure.suffix == ".md" and not pure.name.startswith("_"):
        return "page", root
    return "unregistered", root


def _text(data: bytes | None) -> tuple[str | None, bool]:
    """``(text, readable)``: ``None`` data is an absent file and readable."""
    if data is None:
        return None, True
    try:
        return data.decode("utf-8"), True
    except UnicodeDecodeError:
        return None, False


def _uid_of(*texts: str | None) -> str:
    for text in texts:
        if not text:
            continue
        meta, _ = parse_frontmatter(text)
        if isinstance(meta, dict) and meta.get("uid"):
            return str(meta["uid"])
    return ""


def _merge(
    repo: Path, branch: str, ancestor: str, local: str, tip: str, cfg: SyncSettings
) -> IntegrateResult:
    local_changed = set(changed_paths(repo, ancestor, local))
    remote_changed = set(changed_paths(repo, ancestor, tip))
    specs = resolve_structured_upsert(dict(cfg.config) if cfg.config is not None else None)

    final: dict[str, bytes | None] = {}  # merged content of every path either side changed
    held: list[HeldItem] = []
    page_lines: list[tuple[str, str]] = []  # (uid, rest) of a Page line, in path order
    erased: list[str] = []  # uids of pages deleted as an erasure (§5.1)
    both_pages: dict[str, tuple[str, bytes | None, str | None, str | None]] = {}
    roots = cfg.page_roots

    for path in sorted(local_changed | remote_changed):
        mine = read_file_at_revision(repo, local, path)
        theirs = read_file_at_revision(repo, tip, path)
        if path not in remote_changed:
            final[path] = mine
            continue
        if path not in local_changed:
            final[path] = theirs
            continue
        base_bytes = read_file_at_revision(repo, ancestor, path)
        kind, root = _classify(path, roots)
        blobs = {
            "base": blob_id_at_revision(repo, ancestor, path) or _NONE_BLOB,
            "local": blob_id_at_revision(repo, local, path) or _NONE_BLOB,
            "remote": blob_id_at_revision(repo, tip, path) or _NONE_BLOB,
        }
        if mine == theirs:
            final[path] = theirs
            if kind == "page":
                uid = _uid_of(_text(theirs)[0])
                both_pages[path] = (uid, theirs, None, None)
                line = (
                    f"resolver=rule base={blobs['base']} local={blobs['local']} "
                    f"remote={blobs['remote']} result={blobs['remote']}"
                )
                page_lines.append((uid, line))
            continue
        base_t, base_ok = _text(base_bytes)
        mine_t, mine_ok = _text(mine)
        theirs_t, theirs_ok = _text(theirs)
        readable = base_ok and mine_ok and theirs_ok
        if kind == "store":
            final[path] = theirs
            held.append(_held_item(HELD_STORE_FILE, ("config-never-merged",), blobs))
        elif kind == "page" and root is not None and _erased_uid(
            repo, (ancestor, local, tip), path, (base_t, mine_t, theirs_t)
        ):
            erased.append(_uid_of(base_t, mine_t, theirs_t))
            final[path] = None
        elif kind == "page" and root is not None:
            _merge_one_page(
                path,
                base_t,
                mine_t,
                theirs_t,
                readable,
                theirs,
                specs,
                blobs,
                repo,
                final,
                held,
                page_lines,
                both_pages,
            )
        elif kind == "record" and readable and mine_t is not None and theirs_t is not None:
            rec = merge_records(KIND_PENDING_MERGES, mine_t, theirs_t)
            final[path] = rec.text.encode("utf-8")
            if rec.held:
                reasons = tuple(sorted({h.reason for h in rec.held}))
                held.append(_held_item(HELD_RECORD, reasons, blobs))
        elif kind == "derived" and readable:
            der = merge_derived(mine_t, theirs_t, take="remote")
            final[path] = der.text.encode("utf-8") if der.text is not None else None
        else:
            # A record or regenerated file missing on one side, or any path no rule names.
            final[path] = theirs
            held.append(_held_item(HELD_UNREGISTERED, ("no-rule-for-path",), blobs))

    reminted = _resolve_uid_collisions(
        repo,
        (ancestor, local, tip),
        (local_changed, remote_changed),
        specs,
        roots,
        final,
        held,
        page_lines,
        both_pages,
    )

    _validate(repo, local, tip, final, both_pages, held, page_lines, roots, cfg.config)

    # One uid still on two pages either side changed (§7.4 could not settle it: the uid
    # is in the common ancestor, or more than two pages carry it) blocks the integrate.
    seen: dict[str, str] = {}
    for path in sorted(final):
        kind, _ = _classify(path, roots)
        text, _ok = _text(final[path])
        if kind != "page" or text is None:
            continue
        uid = _uid_of(text)
        if uid and seen.setdefault(uid, path) != path:
            return IntegrateResult("blocked", reason=BLOCK_UID_COLLISION)

    held = _file_held_pages(repo, tip, held, page_lines, cfg.file_held)

    changes = {
        p: data for p, data in final.items() if data != read_file_at_revision(repo, local, p)
    }
    message = _message(tip, page_lines, held, reminted, erased)
    try:
        commit = write_merge_commit(repo, branch, tip, changes, message)
    except WorkingTreeNotCleanError:
        return IntegrateResult("blocked", reason=BLOCK_DIRTY)
    except subprocess.CalledProcessError as exc:
        log.warning("athenaeum sync: merge commit failed in %s: %s", repo, exc)
        return IntegrateResult("failed", reason="merge-commit-failed")
    uids = tuple(uid for uid, _ in page_lines)
    return IntegrateResult(
        "merged",
        commit=commit,
        held=tuple(held),
        pages_changed_both=uids,
        reminted=tuple(reminted),
        erased=tuple(erased),
    )


def _erased_uid(
    repo: Path,
    revs: tuple[str, str, str],
    path: str,
    texts: tuple[str | None, str | None, str | None],
) -> bool:
    """Whether *path* was deleted on a side by a commit marked as an erasure (§5.1).

    Reads the history between the common ancestor and the side that deleted the
    page, through the store seam. A deletion without the mark is an ordinary
    retire and is left to the page merge.
    """
    ancestor, local, tip = revs
    base_t, mine_t, theirs_t = texts
    uid = _uid_of(base_t, mine_t, theirs_t)
    if not uid:
        return False
    for side_rev, side_text in ((local, mine_t), (tip, theirs_t)):
        if side_text is not None:
            continue  # this side did not delete the page
        for message in deletion_commit_messages(repo, ancestor, side_rev, path):
            if uid in erasure_marks(message):
                return True
    return False


def _merge_one_page(
    path: str,
    base_t: str | None,
    mine_t: str | None,
    theirs_t: str | None,
    readable: bool,
    theirs: bytes | None,
    specs: Mapping[str, StructuredUpsertSpec],
    blobs: dict[str, str],
    repo: Path,
    final: dict[str, bytes | None],
    held: list[HeldItem],
    page_lines: list[tuple[str, str]],
    both_pages: dict[str, tuple[str, bytes | None, str | None, str | None]],
) -> None:
    uid = _uid_of(theirs_t, mine_t, base_t)
    if not readable:
        final[path] = theirs
        held.append(_held_page(("unreadable",), uid, blobs, path))
        page_lines.append((uid or "unknown", _held_line(blobs, ("unreadable",), blobs["remote"])))
        return
    ptype = ""
    for text in (theirs_t, mine_t, base_t):
        meta, _ = parse_frontmatter(text) if text else ({}, "")
        if isinstance(meta, dict) and meta.get("type"):
            ptype = str(meta["type"])
            break
    merged = merge_page(base_t, mine_t, theirs_t, specs.get(ptype))
    data = merged.text.encode("utf-8") if merged.text is not None else None
    final[path] = data
    result_blob = blob_id_of(repo, data) if data is not None else _NONE_BLOB
    reasons = tuple(
        dict.fromkeys([HELD_CONTESTED] * bool(merged.contested) + [h.reason for h in merged.held])
    )
    both_pages[path] = (uid, data, mine_t, theirs_t)
    if reasons:
        units = tuple(dict.fromkeys([*merged.contested, *(h.unit for h in merged.held)]))
        held.append(_held_page(reasons, uid, blobs, path, units))
        page_lines.append((uid or "unknown", _held_line(blobs, reasons, result_blob)))
    else:
        page_lines.append(
            (
                uid or "unknown",
                f"resolver=rule base={blobs['base']} local={blobs['local']} "
                f"remote={blobs['remote']} result={result_blob}",
            )
        )


def _held_item(
    kind: str,
    reasons: tuple[str, ...],
    blobs: Mapping[str, str],
    uid: str = "",
    path: str = "",
    units: tuple[str, ...] = (),
) -> HeldItem:
    return HeldItem(kind, reasons, uid, blobs["base"], blobs["local"], blobs["remote"], path, units)


def _held_page(
    reasons: tuple[str, ...],
    uid: str,
    blobs: Mapping[str, str],
    path: str,
    units: tuple[str, ...] = (),
) -> HeldItem:
    return _held_item("page", reasons, blobs, uid, path, units)


def _held_line(blobs: Mapping[str, str], reasons: tuple[str, ...], result: str) -> str:
    return (
        f"resolver=held reason={','.join(reasons)} base={blobs['base']} "
        f"local={blobs['local']} remote={blobs['remote']} result={result}"
    )


def _file_held_pages(
    repo: Path,
    tip: str,
    held: list[HeldItem],
    page_lines: list[tuple[str, str]],
    file_held: HeldFiler | None,
) -> list[HeldItem]:
    """File one decision per held page and put its id on the page's line (§6.3, §6.4).

    One decision per page: a page held for two reasons (a contested unit and a failed
    check) is one item. A page with no uid cannot be named in a decision, and a
    ``deferred`` page is tried again on the next run; neither is filed. A filer that
    cannot file (returns ``None``) leaves the line as it was: the page is still held
    and the push still goes ahead. Returns *held* with ``decision`` set where filed.
    """
    if file_held is None:
        return held
    by_path: dict[str, HeldItem] = {}
    for item in held:
        if item.kind != "page" or not item.path or not item.uid:
            continue
        prior = by_path.get(item.path)
        if prior is None:
            by_path[item.path] = item
        else:
            by_path[item.path] = replace(
                prior,
                reasons=tuple(dict.fromkeys([*prior.reasons, *item.reasons])),
                units=tuple(dict.fromkeys([*prior.units, *item.units])),
            )
    ids: dict[str, str] = {}
    for path, item in by_path.items():
        if REASON_DEFERRED in item.reasons:
            continue
        text, _ok = _text(read_file_at_revision(repo, tip, path))
        meta, body = parse_frontmatter(text) if text else ({}, "")
        meta = meta if isinstance(meta, dict) else {}
        title = str(meta.get("name") or "")
        gist = str(meta.get("description") or "") or next(
            (ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")), ""
        )
        decision = file_held(item, title, gist)
        if decision:
            ids[path] = decision
    if not ids:
        return held
    by_uid = {by_path[p].uid: d for p, d in ids.items()}
    for i, (uid, rest) in enumerate(page_lines):
        if uid in by_uid and rest.startswith("resolver=held"):
            page_lines[i] = (uid, f"{rest} decision={by_uid[uid]}")
    return [
        replace(h, decision=ids[h.path]) if h.kind == "page" and h.path in ids else h for h in held
    ]


def _message(
    tip: str,
    page_lines: list[tuple[str, str]],
    held: list[HeldItem],
    reminted: list[tuple[str, str]] | None = None,
    erased: list[str] | None = None,
) -> str:
    lines = [f"athenaeum sync: integrate {tip[:12]}", "", "Athenaeum-Sync: 1"]
    lines += [f"Athenaeum-Page: {uid} {rest}" for uid, rest in page_lines]
    lines += [
        f"Athenaeum-Held: kind={h.kind} reason={','.join(h.reasons)} "
        f"base={h.base} local={h.local} remote={h.remote}"
        for h in held
        if not h.uid
    ]
    lines += [f"Athenaeum-Uid-Reminted: {old} {new}" for old, new in reminted or ()]
    lines += [erasure_commit_line(uid) for uid in erased or ()]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Section 7.4: when two installs minted the same uid (athenaeum#2996)
# ---------------------------------------------------------------------------


def _normalised_name(meta: Mapping[str, Any]) -> str:
    return " ".join(str(meta.get("name") or "").casefold().split())


def _identity(text: str | None) -> tuple[str, str]:
    """``(type, normalised name)`` of a page text: what §7.4 compares."""
    meta, _ = parse_frontmatter(text) if text else ({}, "")
    meta = meta if isinstance(meta, dict) else {}
    return str(meta.get("type") or "").strip(), _normalised_name(meta)


def _uid_in_ancestor(repo: Path, ancestor: str, roots: tuple[PageRoot, ...], uid: str) -> bool:
    for root in roots:
        for path in list_files_at_revision(repo, ancestor, root.rel):
            if _classify(path, roots)[0] != "page":
                continue
            if _uid_of(_text(read_file_at_revision(repo, ancestor, path))[0]) == uid:
                return True
    return False


def _token(old: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(old)}(?![A-Za-z0-9_-])")


def _rewrite(text: str, pairs: list[tuple[str, str]], keep: set[str]) -> str:
    """Replace each old token with its new one, on every line not in *keep*."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.rstrip("\r\n") not in keep:
            for old, new in pairs:
                line = _token(old).sub(new, line)
        out.append(line)
    return "".join(out)


def _remint(
    repo: Path,
    revs: tuple[str, str, str],
    local_changed: set[str],
    old: str,
    path: str,
    new: str,
    roots: tuple[PageRoot, ...],
    final: dict[str, bytes | None],
    both_pages: dict[str, tuple[str, bytes | None, str | None, str | None]],
) -> None:
    """Give the local page at *path* the uid *new*, rename it, rewrite local references."""
    ancestor, _local, tip = revs
    pure = PurePosixPath(path)
    renamed = path
    pairs = [(old, new)]
    if pure.stem == old or pure.stem.startswith(old + "-"):
        new_stem = new + pure.stem[len(old) :]
        renamed = str(pure.with_name(new_stem + pure.suffix))
        pairs.insert(0, (pure.stem, new_stem))
    data = final[path]
    assert data is not None
    final[path] = None
    final[renamed] = _rewrite(data.decode("utf-8"), pairs, set()).encode("utf-8")
    # References made in the same unpublished local commits: lines of a locally changed
    # page that neither the common ancestor nor the remote has. A line already published
    # (in the ancestor or on the remote) is never rewritten. A pending-merges record can
    # name a page by its ``<uid>-<slug>.md`` file, so records are rewritten like pages.
    for other in sorted(local_changed - {path}):
        current = final.get(other)
        if current is None or _classify(other, roots)[0] not in ("page", "record"):
            continue
        text, ok = _text(current)
        if text is None or not ok:
            continue
        published: set[str] = set()
        for rev in (ancestor, tip):
            theirs, _ok = _text(read_file_at_revision(repo, rev, other))
            published.update((theirs or "").splitlines())
        rewritten = _rewrite(text, pairs, published)
        if rewritten != text:
            final[other] = rewritten.encode("utf-8")
            if other in both_pages:
                uid, _data, mine_t, theirs_t = both_pages[other]
                both_pages[other] = (uid, final[other], mine_t, theirs_t)


def _resolve_uid_collisions(
    repo: Path,
    revs: tuple[str, str, str],
    changed: tuple[set[str], set[str]],
    specs: Mapping[str, StructuredUpsertSpec],
    roots: tuple[PageRoot, ...],
    final: dict[str, bytes | None],
    held: list[HeldItem],
    page_lines: list[tuple[str, str]],
    both_pages: dict[str, tuple[str, bytes | None, str | None, str | None]],
) -> list[tuple[str, str]]:
    """Settle each uid that two pages of the merged tree carry (design note §7.4).

    Reads each page's uid from the merged tree through the seam; no index. A uid added
    on both sides (one new local page, one new remote page, neither in the common
    ancestor) is, with the same ``type`` and normalised ``name``, one page merged
    against an empty ancestor; otherwise the local page is re-minted. Anything else is
    left in place and blocks the integrate. Returns ``(old, new)`` for each re-mint.
    """
    ancestor, local, tip = revs
    local_changed, remote_changed = changed
    by_uid: dict[str, list[str]] = {}
    for path in sorted(final):
        text, _ok = _text(final[path])
        if text is None or _classify(path, roots)[0] != "page":
            continue
        uid = _uid_of(text)
        if uid:
            by_uid.setdefault(uid, []).append(path)
    used = set(by_uid)
    reminted: list[tuple[str, str]] = []
    for uid, paths in by_uid.items():
        if len(paths) != 2:
            continue
        added = [read_file_at_revision(repo, ancestor, p) is None for p in paths]
        local_new = [
            p for p, new in zip(paths, added) if new and p in local_changed - remote_changed
        ]
        remote_new = [
            p for p, new in zip(paths, added) if new and p in remote_changed - local_changed
        ]
        if len(local_new) != 1 or len(remote_new) != 1:
            continue
        if _uid_in_ancestor(repo, ancestor, roots, uid):
            continue
        mine_path, theirs_path = local_new[0], remote_new[0]
        mine_t, _ = _text(read_file_at_revision(repo, local, mine_path))
        theirs_t, _ = _text(read_file_at_revision(repo, tip, theirs_path))
        if _identity(mine_t) == _identity(theirs_t):
            blobs = {
                "base": _NONE_BLOB,
                "local": blob_id_at_revision(repo, local, mine_path) or _NONE_BLOB,
                "remote": blob_id_at_revision(repo, tip, theirs_path) or _NONE_BLOB,
            }
            _merge_one_page(
                theirs_path,
                None,
                mine_t,
                theirs_t,
                True,
                read_file_at_revision(repo, tip, theirs_path),
                specs,
                blobs,
                repo,
                final,
                held,
                page_lines,
                both_pages,
            )
            final[mine_path] = None
            continue
        new = generate_uid()
        while new in used:
            new = generate_uid()
        used.add(new)
        _remint(repo, revs, local_changed, uid, mine_path, new, roots, final, both_pages)
        reminted.append((uid, new))
    return reminted


# ---------------------------------------------------------------------------
# Sections 6.3 and 6.4: the held-page decision (athenaeum#2995)
# ---------------------------------------------------------------------------


class HeldDecisionRefused(ValueError):
    """A held-page decision could not be answered; nothing was written.

    ``code`` is one of ``no_decision``, ``not_pending``, ``not_allowed``,
    ``bad_answer``, ``stale``, ``unavailable`` or ``invalid_text``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_BLOB_RE = re.compile(r"^[0-9a-f]{40,64}$")


def make_held_filer(
    repo: Path,
    roots: tuple[PageRoot, ...],
    *,
    knowledge_root: Path,
    store_id: str,
    mounts: Mapping[str, Path] | None = None,
    asserter: Mapping[str, object] | None = None,
) -> HeldFiler:
    """The filer that opens a ``held-page`` decision in *store_id*'s own queue.

    The decision names the uid, the contested units, the reason codes and the base,
    local and remote blob ids; it quotes only this store's page (title and gist of
    the version that stays published). Filing is idempotent: a pending decision for
    the same uid and the same local and remote blobs is returned, not duplicated.
    A refusal or a write error is logged and returns ``None``; it never stops a sync.
    """
    page_roots = {r.rel.strip("/") for r in roots}

    def relative_to_root(path: str) -> str:
        parent = _page_dir(path)
        if parent in page_roots and parent:
            return PurePosixPath(path).relative_to(parent).as_posix()
        return path

    def file(item: HeldItem, title: str, gist: str) -> str | None:
        try:
            for rec in load_queue(knowledge_root, store_id):
                d = rec.get("detail") or {}
                if (
                    rec.get("kind") == DECISION_KIND_HELD_PAGE
                    and rec.get("disposition") == "pending"
                    and d.get("uid") == item.uid
                    and d.get("local") == item.local
                    and d.get("remote") == item.remote
                ):
                    return str(rec["id"])
            stale = [
                str(rec["id"])
                for rec in load_queue(knowledge_root, store_id)
                if rec.get("kind") == DECISION_KIND_HELD_PAGE
                and rec.get("disposition") == "pending"
                and (rec.get("detail") or {}).get("uid") == item.uid
            ]
            record = file_decision(
                knowledge_root,
                store_id=store_id,
                question=(
                    f"Two installs changed the page {item.uid} and the librarian could not "
                    "settle it. The version already published stays on the page until you "
                    "answer. Keep the published version, take this install's version, or "
                    "supply a merged text."
                ),
                pages=[PageQuote(store_id, item.uid, title, gist, relative_to_root(item.path))],
                kind=DECISION_KIND_HELD_PAGE,
                mounts=mounts,
                asserter=asserter,
                detail={
                    "uid": item.uid,
                    "path": item.path,
                    "units": list(item.units),
                    "reasons": list(item.reasons),
                    "base": item.base,
                    "local": item.local,
                    "remote": item.remote,
                    "answers": list(HELD_ANSWERS),
                },
            )
            # The page was held again on a newer remote or local version: the earlier
            # pending decision for it can no longer be answered (it would be stale), so
            # the new one replaces it.
            for old in stale:
                resolve_decision(
                    knowledge_root, store_id=store_id, decision_id=old, disposition="superseded"
                )
            return str(record["id"])
        except (StoreDecisionRefused, OSError, KeyError) as exc:
            log.warning(
                "athenaeum sync: held page %s not filed as a decision: %s",
                item.uid,
                type(exc).__name__,
            )
            return None

    return file


def answer_held_decision(
    repo: Path,
    knowledge_root: Path,
    *,
    store_id: str,
    decision_id: str,
    answer: str,
    text: str | None = None,
    mounts: Mapping[str, Path] | None = None,
    asserter: Mapping[str, object] | None = None,
) -> str:
    """Apply a person's answer to a held page as one ordinary new commit; return its SHA.

    *answer* is ``keep-remote`` (the published version, as it already stands),
    ``take-local`` (this install's version, read from history by its blob id) or
    ``merged`` (the *text* supplied). The page must still be at the published
    version it was held at; otherwise the decision is ``stale`` and nothing is
    written. The commit carries the decision id and blob ids only, and the decision
    is closed after it. Raises :class:`HeldDecisionRefused` with nothing written.
    """
    rec = next(
        (r for r in load_queue(knowledge_root, store_id) if r.get("id") == decision_id), None
    )
    if rec is None:
        raise HeldDecisionRefused("no_decision", f"no decision {decision_id} in {store_id}'s queue")
    if rec.get("kind") != DECISION_KIND_HELD_PAGE or rec.get("disposition") != "pending":
        raise HeldDecisionRefused(
            "not_pending", f"{decision_id} is not a pending held-page decision"
        )
    if mounts is not None and "answer" not in _rights(store_id, mounts, asserter):
        raise HeldDecisionRefused("not_allowed", f"this install may not answer for {store_id}")
    if answer not in HELD_ANSWERS:
        raise HeldDecisionRefused("bad_answer", f"answer must be one of {', '.join(HELD_ANSWERS)}")
    detail = rec.get("detail") or {}
    rel = str(detail.get("path") or "")
    pure = PurePosixPath(rel)
    if not rel or pure.is_absolute() or ".." in pure.parts:
        raise HeldDecisionRefused("unavailable", "the decision names no usable page path")
    remote, local = str(detail.get("remote", "")), str(detail.get("local", ""))
    page = Path(repo) / rel
    if answer == ANSWER_MERGED:
        if text is None:
            raise HeldDecisionRefused("invalid_text", "the merged answer needs a text")
        why = validate_page_text(text)
        if why is not None or _uid_of(text) != detail.get("uid"):
            raise HeldDecisionRefused(
                "invalid_text", f"the merged text is not usable: {why or 'uid'}"
            )
        data = text.encode("utf-8")
    else:
        wanted = remote if answer == ANSWER_KEEP_REMOTE else local
        # Filtered (path=rel, athenaeum#3127): wanted is a committed page blob, and
        # `data` is compared with and written over the checkout file.
        found = read_blob(repo, wanted, path=rel) if _BLOB_RE.match(wanted) else None
        if found is None:
            raise HeldDecisionRefused("unavailable", f"the {answer} version is not in history")
        data = found
    current = blob_id(page) if page.is_file() else _NONE_BLOB
    if current != remote:
        if current == blob_id_of(repo, data):
            # A retry after the commit landed but closing the decision failed: the page
            # already holds the answered text, so only the closing is left.
            resolve_decision(
                knowledge_root,
                store_id=store_id,
                decision_id=decision_id,
                disposition=f"answered-{answer}",
            )
            return repo_head(repo) or ""
        raise HeldDecisionRefused(
            "stale", f"{rel} is no longer at the published version this decision was filed against"
        )
    before = page.read_bytes()
    page.write_bytes(data)
    message = (
        f"athenaeum sync: settle held page {detail.get('uid')} ({answer})\n\n"
        f"Athenaeum-Decision: {decision_id} answer={answer} "
        f"base={detail.get('base', _NONE_BLOB)} local={local} remote={remote} "
        f"result={blob_id_of(repo, data)}\n"
    )
    try:
        commit = commit_paths(Path(repo), [Path(rel)], message, allow_empty=True)
    except subprocess.CalledProcessError:
        page.write_bytes(before)
        raise
    if commit is None:
        page.write_bytes(before)
        raise HeldDecisionRefused("unavailable", "the pages are not in a git repository")
    resolve_decision(
        knowledge_root, store_id=store_id, decision_id=decision_id, disposition=f"answered-{answer}"
    )
    return commit


# ---------------------------------------------------------------------------
# Section 4.3: validation before the merge commit is written
# ---------------------------------------------------------------------------


def validate_page_text(text: str) -> str | None:
    """Why *text* may not be pushed as a page, or ``None`` when it may.

    The frontmatter must parse with ``uid``, ``type`` and ``name``, and no line may
    open or close a conflict marker.
    """
    meta, _body = parse_frontmatter(text)
    if not isinstance(meta, dict) or not meta:
        return "frontmatter-unparseable"
    for key in ("uid", "type", "name"):
        if not str(meta.get(key) or "").strip():
            return f"missing-{key}"
    for line in text.splitlines():
        if line.startswith(("<<<<<<<", ">>>>>>>")) and line[7:8] in ("", " "):
            return "conflict-marker"
    return None


def _validate(
    repo: Path,
    local: str,
    tip: str,
    final: dict[str, bytes | None],
    both_pages: dict[str, tuple[str, bytes | None, str | None, str | None]],
    held: list[HeldItem],
    page_lines: list[tuple[str, str]],
    roots: tuple[PageRoot, ...],
    config: Mapping[str, Any] | None,
) -> None:
    """Hold, with the remote version kept, every both-sides page that fails §4.3."""
    surfaces = {r.rel.strip("/"): r.surface for r in roots}
    ranked = any(surface_visibility(r.surface, dict(config) if config else None) > 0 for r in roots)
    index = _link_index(repo, local, tip, final, roots) if ranked else {}
    for path, (uid, data, _mine, theirs_t) in both_pages.items():
        if data is None or data == read_file_at_revision(repo, tip, path):
            continue  # nothing is added to what the remote already holds
        text = data.decode("utf-8", errors="replace")
        why = validate_page_text(text)
        if why is None and ranked:
            why = _boundary_violation(path, text, surfaces, index, config)
        if why is None:
            continue
        remote_bytes = read_file_at_revision(repo, tip, path)
        final[path] = remote_bytes
        reasons = (why,)
        blobs = {
            "base": _NONE_BLOB,
            "local": blob_id_at_revision(repo, local, path) or _NONE_BLOB,
            "remote": blob_id_at_revision(repo, tip, path) or _NONE_BLOB,
        }
        held.append(_held_page(reasons, uid, blobs, path))
        for i, (line_uid, _rest) in enumerate(page_lines):
            if line_uid == (uid or "unknown"):
                page_lines[i] = (line_uid, _held_line(blobs, reasons, blobs["remote"]))
                break


def _page_dir(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _link_index(
    repo: Path,
    local: str,
    tip: str,
    final: Mapping[str, bytes | None],
    roots: tuple[PageRoot, ...],
) -> dict[str, str]:
    """Link-target name (uid, file name, stem) to its page path, over the merged tree."""
    index: dict[str, str] = {}
    paths: set[str] = set()
    for root in roots:
        paths.update(p for p in list_files_at_revision(repo, local, root.rel) if p.endswith(".md"))
    paths.update(p for p, data in final.items() if data is not None and p.endswith(".md"))
    for path in sorted(paths):
        if path in final and final[path] is None:
            continue
        data = final[path] if path in final else read_file_at_revision(repo, local, path)
        text, _ok = _text(data)
        pure = PurePosixPath(path)
        for key in (pure.name, pure.stem, _uid_of(text)):
            if key:
                index.setdefault(key, path)
    return index


def _boundary_violation(
    path: str,
    text: str,
    surfaces: Mapping[str, str],
    index: Mapping[str, str],
    config: Mapping[str, Any] | None,
) -> str | None:
    cfg = dict(config) if config else None
    source = surfaces.get(_page_dir(path), "wiki")
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        return None
    for _kind, ref in extract_link_refs(meta, body):
        target = index.get(ref) or index.get(PurePosixPath(ref).stem)
        if target is None:
            continue
        if link_forbidden(source, surfaces.get(_page_dir(target), "wiki"), cfg):
            return "store-boundary-link"
    return None


# ---------------------------------------------------------------------------
# Which repositories of a deployment sync, and in which mode
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoSyncPlan:
    """One syncable git repository of the store, with the mode this install acts on."""

    repo: Path
    label: str  # the surface names held in it, joined by a comma
    branch: str | None
    decision: SyncModeDecision
    settings: SyncSettings


def plan_repo_syncs(knowledge_root: Path, config: dict[str, Any] | None) -> list[RepoSyncPlan]:
    """One plan per git repository that holds a writable surface and has a remote.

    The declaration is the first one found at a held surface's root
    (``_store/access.yaml``); the local narrowing is the most restrictive
    ``sync_mode`` among the repository's adapters. A repository with no
    declaration resolves to ``fast_forward``, so a caller acts only on the plans
    whose ``decision.integrate`` is true and reports ``decision.local_ignored``.
    """
    store = open_store(config, knowledge_root)
    adapters = available_adapters(config)
    # The same remote and branch the fast-forward sync uses (review on PR
    # athenaeum#2983): ``librarian.push_remote`` for every repository, and
    # ``librarian.push_branch`` for the knowledge repository only; a separate
    # surface repository syncs its own checked-out branch.
    remote = resolve_push_remote(config)
    knowledge_branch = resolve_push_branch(config)
    read_only = store.read_only_surfaces()
    plans: list[RepoSyncPlan] = []
    for repo, surfaces in store.versioned_surface_repos().items():
        writable = [s for s in surfaces if s not in read_only]
        if not writable or not store.capabilities_for(writable[0]).has_remote:
            continue
        roots: list[PageRoot] = []
        declaration: StoreDeclaration | StoreDeclarationInvalid | None = None
        local_modes: set[str] = set()
        for name in writable:
            adapter = adapters[name]
            root = adapter.resolve_root(knowledge_root)
            try:
                rel = root.resolve().relative_to(repo.resolve()).as_posix()
            except ValueError:
                continue
            roots.append(PageRoot("" if rel == "." else rel, name))
            if declaration is None:
                declaration = load_store_declaration(root)
            if adapter.sync_mode:
                local_modes.add(adapter.sync_mode)
        local = (
            MODE_FAST_FORWARD
            if MODE_FAST_FORWARD in local_modes
            else (MODE_INTEGRATE if local_modes else None)
        )
        decision = resolve_sync_mode(declaration, local)
        plans.append(
            RepoSyncPlan(
                repo=repo,
                label=",".join(writable),
                branch=(
                    knowledge_branch
                    if knowledge_branch and repo.resolve() == knowledge_root.resolve()
                    else current_branch(repo)
                ),
                decision=decision,
                settings=SyncSettings(page_roots=tuple(roots), config=config, remote=remote),
            )
        )
    return plans
