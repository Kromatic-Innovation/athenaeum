# SPDX-License-Identifier: Apache-2.0
"""Integrate mode for the pages repository (athenaeum#2935, slice S7 of the sync design).

Sections 3.3, 4, 5 and 6.3 of ``docs/design/federation-multi-writer-sync.md``.

A store declares ``sync.mode: integrate`` in its ``_store/access.yaml``; one
install may narrow that locally (``sync_mode: fast_forward``) and may never
widen it. :func:`resolve_sync_mode` is the whole rule. For a repository in
integrate mode the run does:

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
remote value (``merge_page`` does that). Nothing here calls a model, and
nothing here raises a decision: the held list is returned and written into the
merge commit (section 6.3) for the held-page decision of a later slice.

Before the merge commit is written (section 4.3), every page changed on both
sides is checked: frontmatter parses with ``uid``, ``type`` and ``name``, no
conflict marker, and no link from a more widely shared surface to a more
private one. A page that fails is held and keeps the remote version. One uid on
two of the pages either side changed blocks the whole integrate (nothing is
merged or pushed); re-minting is a later slice.

The merge commit message carries ``Athenaeum-Sync: 1`` and one
``Athenaeum-Page:`` line per page changed on both sides, with git blob ids and
reason codes only. No page text and no path is written into it.

Layering: L4. Imports the store seam (:mod:`athenaeum.store`), the pure merges
(:mod:`athenaeum.page_merge`, :mod:`athenaeum.record_merge`), the declaration
(:mod:`athenaeum.store_access`) and the link rule
(:mod:`athenaeum.store_boundary`, :mod:`athenaeum.store_links`). It runs no git
command itself and imports nothing from :mod:`athenaeum.librarian`.
"""

from __future__ import annotations

import logging
import random
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
from athenaeum.models import parse_frontmatter
from athenaeum.page_merge import merge_page
from athenaeum.record_merge import KIND_PENDING_MERGES, merge_derived, merge_records
from athenaeum.storage import available_adapters, open_store, surface_visibility
from athenaeum.store import (
    PushFailureReason,
    PushResult,
    WorkingTreeNotCleanError,
    blob_id_at_revision,
    blob_id_of,
    changed_paths,
    commit_repo,
    common_ancestor,
    current_branch,
    fast_forward_branch,
    fetch_remote_branch,
    is_ancestor,
    list_files_at_revision,
    push_branch,
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
from athenaeum.store_links import extract_link_refs

log = logging.getLogger(__name__)

__all__ = [
    "MAX_PUSH_ATTEMPTS",
    "HeldItem",
    "IntegrateResult",
    "PageRoot",
    "RepoSyncPlan",
    "PushOutcome",
    "SyncModeDecision",
    "SyncSettings",
    "SyncStep",
    "SyncReport",
    "integrate_remote",
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


@dataclass(frozen=True)
class HeldItem:
    """One file, page or page unit that waits for a person; the remote version stays.

    ``uid`` is empty for a file that is not a page. Blob ids only, never text.
    """

    kind: str
    reasons: tuple[str, ...]
    uid: str = ""
    base: str = _NONE_BLOB
    local: str = _NONE_BLOB
    remote: str = _NONE_BLOB


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
    cfg = settings or SyncSettings()
    report = SyncReport()
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


def push_after_compile(
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
    while attempts < max(1, cfg.max_attempts):
        attempts += 1
        result: PushResult = push_branch(repo, branch, cfg.remote)
        if result.ok:
            return PushOutcome(True, attempts, remote_before=remote_before, pushed=repo_head(repo))
        detail = result.detail
        if result.reason is not PushFailureReason.REJECTED_REMOTE_MOVED:
            return PushOutcome(False, attempts, "other", detail)
        if attempts >= max(1, cfg.max_attempts):
            break
        cfg.sleep(cfg.rng.uniform(*cfg.wait_range))
        try:
            commit_repo(repo, "athenaeum sync: snapshot before integrate")
        except subprocess.CalledProcessError:
            return PushOutcome(False, attempts, REASON_INTEGRATE_BLOCKED, BLOCK_DIRTY)
        merged = integrate_remote(repo, branch, cfg)
        if not merged.ok:
            return PushOutcome(False, attempts, REASON_INTEGRATE_BLOCKED, merged.reason)
        remote_before = merged.remote_tip
    return PushOutcome(False, attempts, REASON_CONTENDED, detail)


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
            held.append(HeldItem(HELD_STORE_FILE, ("config-never-merged",), **blobs))
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
                held.append(HeldItem(HELD_RECORD, reasons, **blobs))
        elif kind == "derived" and readable:
            der = merge_derived(mine_t, theirs_t, take="remote")
            final[path] = der.text.encode("utf-8") if der.text is not None else None
        else:
            # A record or regenerated file missing on one side, or any path no rule names.
            final[path] = theirs
            held.append(HeldItem(HELD_UNREGISTERED, ("no-rule-for-path",), **blobs))

    _validate(repo, local, tip, final, both_pages, held, page_lines, roots, cfg.config)

    # One uid on two pages that either side changed blocks the integrate (section 7.4
    # re-minting is a later slice).
    seen: dict[str, str] = {}
    for path in sorted(final):
        kind, _ = _classify(path, roots)
        text, _ok = _text(final[path])
        if kind != "page" or text is None:
            continue
        uid = _uid_of(text)
        if uid and seen.setdefault(uid, path) != path:
            return IntegrateResult("blocked", reason=BLOCK_UID_COLLISION)

    changes = {
        p: data for p, data in final.items() if data != read_file_at_revision(repo, local, p)
    }
    message = _message(tip, page_lines, held)
    try:
        commit = write_merge_commit(repo, branch, tip, changes, message)
    except WorkingTreeNotCleanError:
        return IntegrateResult("blocked", reason=BLOCK_DIRTY)
    except subprocess.CalledProcessError as exc:
        log.warning("athenaeum sync: merge commit failed in %s: %s", repo, exc)
        return IntegrateResult("failed", reason="merge-commit-failed")
    uids = tuple(uid for uid, _ in page_lines)
    return IntegrateResult("merged", commit=commit, held=tuple(held), pages_changed_both=uids)


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
        held.append(HeldItem("page", ("unreadable",), uid, **blobs))
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
        held.append(HeldItem("page", reasons, uid, **blobs))
        page_lines.append((uid or "unknown", _held_line(blobs, reasons, result_blob)))
    else:
        page_lines.append(
            (
                uid or "unknown",
                f"resolver=rule base={blobs['base']} local={blobs['local']} "
                f"remote={blobs['remote']} result={result_blob}",
            )
        )


def _held_line(blobs: Mapping[str, str], reasons: tuple[str, ...], result: str) -> str:
    return (
        f"resolver=held reason={','.join(reasons)} base={blobs['base']} "
        f"local={blobs['local']} remote={blobs['remote']} result={result}"
    )


def _message(tip: str, page_lines: list[tuple[str, str]], held: list[HeldItem]) -> str:
    lines = [f"athenaeum sync: integrate {tip[:12]}", "", "Athenaeum-Sync: 1"]
    lines += [f"Athenaeum-Page: {uid} {rest}" for uid, rest in page_lines]
    lines += [
        f"Athenaeum-Held: kind={h.kind} reason={','.join(h.reasons)} "
        f"base={h.base} local={h.local} remote={h.remote}"
        for h in held
        if not h.uid
    ]
    return "\n".join(lines) + "\n"


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
        held.append(HeldItem("page", reasons, uid, **blobs))
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
