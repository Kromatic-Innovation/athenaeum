# SPDX-License-Identifier: Apache-2.0
"""One retirement path for any information, in whichever store holds it (issue athenaeum#3151).

``athenaeum retire`` (:mod:`athenaeum._cmd_retire`) used to reach wiki pages
only (``retire-pages``, issue athenaeum#1625). The private stores had
primitives that no command reached (issue athenaeum#2454): an operator who
wanted a stale contact record gone removed it by hand, bypassing every guard
the page path has. This module resolves a uid to the store that holds it,
checks it against the caller's rights in that store, and retires it with the
same guarantees as a page.

**Three stores.**

- *Wiki pages*: ``wiki/*.md`` (the same shallow scan as
  :func:`athenaeum.retire_pages.resolve_uids`). Retired through
  :func:`athenaeum.retire_pages.apply_retirement` unchanged: a provenance
  snapshot commit, then ``git rm`` + index rebuild + pending-merge withdrawal.
  The page's contact records are settled afterwards (the athenaeum#2217
  cascade: retired, or folded onto a successor).
- *Contact records*: every record on the excluded contacts surface
  (:func:`athenaeum.contact_record_fold.resolve_contacts_root`) that carries
  the uid. A uid owns all of its records, as it does for the athenaeum#2217
  settle. Retired with the same two-commit mechanics
  (:func:`athenaeum.decay_sweep.archive_via_two_commit_git_rm`), so recovery
  is git-only, then the persisted contact-record index (athenaeum#2182) drops
  each record (:func:`athenaeum.contact_index_cache.invalidate_cached_record`).
- *Off-corpus records*: a ``*.md`` file under the off-corpus root
  (:mod:`athenaeum.off_corpus`) whose frontmatter carries the uid. That store
  lives outside git on purpose, so that a delete is a true erasure. It is
  therefore erased through :func:`athenaeum.off_corpus.erase_off_corpus_record`
  (content and both index shards in one call), with no snapshot: copying it
  into git history would defeat the reason it is off-corpus. The dry run and
  the apply output both say it cannot be recovered.

**Resolution.** A bare uid resolves to whichever store holds it. A uid that
names a wiki page is that page, even when its contact records carry the same
uid: they belong to the page and follow it (the cascade). A uid found in two
stores otherwise (a page and an off-corpus record, or a contact record and an
off-corpus record), or on two pages, or on two off-corpus records, is
ambiguous. A prefix pins the store: ``page:<uid>``, ``contact:<uid>``,
``off-corpus:<uid>``. ``contact:<uid>`` is the only way to name the contact
records of a uid whose page is live.

**Access.** Each match is checked against the configured owner's rights on the
surface that holds it (:func:`athenaeum.store_rights.resolve_surface_rights`
over the surface's ``_store/access.yaml``). Without ``read`` the match is
dropped before resolution, so the uid reads exactly like an unknown one and
nothing confirms the record exists. With ``read`` but not ``compile`` (the
right a write needs everywhere else in the engine), or on a surface declared
``read_only``, the target is refused by name. A declaration that is present
but invalid resolves as reader only, so it refuses. A surface with no
declaration behaves as it always has. One refusal refuses the whole run.

**No values.** Nothing this module returns, logs or writes to a commit
message carries an address: uids, counts, and values masked with
:func:`athenaeum.pii.mask_contact_address`. Record filenames are never
printed either, since an old record may be named after an address.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.retire_pages`,
:mod:`athenaeum.decay_sweep` and :mod:`athenaeum.contact_record_repair` (L4),
:mod:`athenaeum.off_corpus`, :mod:`athenaeum.contact_index_cache`,
:mod:`athenaeum.contact_record_fold` and :mod:`athenaeum.pii` (L3),
:mod:`athenaeum.storage` and :mod:`athenaeum.store_rights` (L2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum import pii
from athenaeum.contact_index_cache import invalidate_cached_record
from athenaeum.contact_record_fold import resolve_contacts_root
from athenaeum.contact_record_repair import plan_contact_record_repair
from athenaeum.decay_sweep import _git, archive_via_two_commit_git_rm
from athenaeum.models import parse_frontmatter
from athenaeum.off_corpus import (
    OffCorpusConfigError,
    erase_off_corpus_record,
    off_corpus_adapter,
    off_corpus_root,
    off_corpus_store,
)
from athenaeum.retire_pages import (
    RetireCandidate,
    RetireReport,
    UidResolutionError,
    apply_retirement,
    build_retire_report,
)
from athenaeum.storage import StorageAdapter, available_adapters
from athenaeum.store import StoreKey

log = logging.getLogger(__name__)

STORE_PAGE = "page"
STORE_CONTACT = "contact"
STORE_OFF_CORPUS = "off-corpus"
STORES = (STORE_PAGE, STORE_CONTACT, STORE_OFF_CORPUS)

#: Ledger, under the off-corpus root, that records each off-corpus retirement
#: and its reason. Holds the uid, the relative path and the reason, never
#: content. Underscore-prefixed, so :func:`_off_corpus_uid_map` never reads it
#: as a record.
OFF_CORPUS_RETIREMENT_LEDGER = "_retirements.jsonl"

ACCESS_OK = "ok"
ACCESS_HIDDEN = "hidden"
ACCESS_REFUSED = "refused"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class RetireTarget:
    """One uid resolved to one store.

    *paths*: the page (one), the contact records (one or more), or the
    off-corpus record (one). *cascade_records*: for a page, how many contact
    records the athenaeum#2217 cascade settles with it. *cascade_hidden*: the
    page's contact records sit on a surface the caller cannot read, so the
    cascade is skipped rather than writing to a store the caller cannot see.
    """

    uid: str
    store: str
    paths: list[Path]
    cascade_records: int = 0
    cascade_hidden: bool = False

    def label(self) -> str:
        return f"{self.uid} ({self.store})"


@dataclass
class TargetRefusal:
    """A target the caller can see but may not retire."""

    uid: str
    store: str
    reason: str

    def __str__(self) -> str:
        return f"{self.uid} ({self.store}): refused - {self.reason}"


@dataclass
class PagelessRecordSummary:
    """One page-less uid, for the ``--pageless`` preview. Never a raw value."""

    uid: str
    records: int
    masked_values: list[str] = field(default_factory=list)
    #: Parallel to *masked_values*: the value is also listed on a record whose
    #: uid has a live page.
    on_live_record: list[bool] = field(default_factory=list)
    origin_commit: str | None = None


@dataclass
class RetirePlan:
    """Everything a run would do, computed read-only."""

    targets: list[RetireTarget] = field(default_factory=list)
    unresolved: list[UidResolutionError] = field(default_factory=list)
    refused: list[TargetRefusal] = field(default_factory=list)
    pageless: list[PagelessRecordSummary] = field(default_factory=list)
    #: ``(uid, rule)`` for each page a relevance-policy ``always`` rule
    #: protects that ``--force-retire`` overrode (athenaeum#3152). Recorded in
    #: the archive commit and the relevance-policy audit ledger on apply.
    forced_protected: list[tuple[str, Any]] = field(default_factory=list)
    #: Problems that stop the run before any write but name no one target
    #: (an off-corpus misconfiguration, a missing contacts surface for
    #: ``--pageless``).
    errors: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.unresolved or self.refused or self.errors)

    def of_store(self, store: str) -> list[RetireTarget]:
        return [t for t in self.targets if t.store == store]


@dataclass
class RetireRunReport:
    """Outcome of :func:`apply_plan`."""

    pages: RetireReport | None = None
    contact_records_retired: int = 0
    off_corpus_erased: int = 0
    committed: bool = False
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _surface_of(
    path: Path, config: dict[str, Any] | None, knowledge_root: Path
) -> tuple[StorageAdapter, Path] | None:
    """The configured surface whose root most closely contains *path*."""
    resolved = Path(path).resolve()
    best: tuple[StorageAdapter, Path] | None = None
    for adapter in available_adapters(config).values():
        try:
            root = adapter.resolve_root(knowledge_root).resolve()
        except Exception:  # noqa: BLE001 - an unresolvable surface holds nothing
            continue
        if resolved == root or root in resolved.parents:
            if best is None or len(root.parts) > len(best[1].parts):
                best = (adapter, root)
    return best


def _surface_rights(root: Path, config: dict[str, Any] | None) -> frozenset[str] | None:
    """The owner's rights on the store at *root*, or ``None`` with no declaration.

    Read fresh for each run rather than from the run-start table, so a
    declaration edited a moment ago governs this run. Fails closed: any error
    resolves as reader only.
    """
    from athenaeum.config import resolve_owner_asserter
    from athenaeum.store_rights import READER_ONLY, resolve_surface_rights

    try:
        return resolve_surface_rights(root, resolve_owner_asserter(config))
    except Exception as exc:  # noqa: BLE001 - failure narrows to read, never widens
        log.warning("retire: rights for a surface could not be resolved (%s)", type(exc).__name__)
        return READER_ONLY


def target_access(
    path: Path, knowledge_root: Path, config: dict[str, Any] | None
) -> tuple[str, str | None]:
    """``(verdict, reason)`` for retiring *path*.

    *verdict* is :data:`ACCESS_OK`, :data:`ACCESS_HIDDEN` (the caller cannot
    read the store, so the target must read as not found) or
    :data:`ACCESS_REFUSED` (readable but not writable; *reason* names why).
    """
    surface = _surface_of(path, config, knowledge_root)
    if surface is None:
        return ACCESS_OK, None
    adapter, root = surface
    rights = _surface_rights(root, config)
    if rights is not None and "read" not in rights:
        return ACCESS_HIDDEN, None
    if adapter.read_only:
        return ACCESS_REFUSED, (
            f"surface {adapter.name!r} is declared read_only (a shared mount this "
            "install never writes)"
        )
    if rights is not None and "compile" not in rights:
        held = ",".join(sorted(rights)) or "none"
        return ACCESS_REFUSED, (
            f"the store declaration on surface {adapter.name!r} does not grant this "
            f"install compile (rights held: {held})"
        )
    return ACCESS_OK, None


# ---------------------------------------------------------------------------
# Store scans
# ---------------------------------------------------------------------------


def _frontmatter_uid(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, _body = parse_frontmatter(text)
    uid = meta.get("uid") if isinstance(meta, dict) else None
    return uid if isinstance(uid, str) and uid else None


def wiki_uid_map(wiki_root: Path) -> dict[str, list[Path]]:
    """``{uid: [page, ...]}`` over ``wiki/*.md`` (underscore files skipped)."""
    out: dict[str, list[Path]] = {}
    if not wiki_root.is_dir():
        return out
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        uid = _frontmatter_uid(path)
        if uid:
            out.setdefault(uid, []).append(path)
    return out


def _off_corpus_uid_map(root: Path | None) -> dict[str, list[Path]]:
    """``{uid: [record, ...]}`` over the off-corpus root's ``*.md`` files.

    Any path with an underscore-prefixed component (the ledger shard, the
    store declaration) is operational, not a record.
    """
    out: dict[str, list[Path]] = {}
    if root is None or not root.is_dir():
        return out
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part.startswith("_") for part in rel.parts):
            continue
        uid = _frontmatter_uid(path)
        if uid:
            out.setdefault(uid, []).append(path)
    return out


class _Stores:
    """The three stores, scanned once per plan, with hidden matches removed."""

    def __init__(self, knowledge_root: Path, config: dict[str, Any] | None) -> None:
        self.knowledge_root = knowledge_root
        self.config = config
        self.errors: list[str] = []
        self._access: dict[Path, tuple[str, str | None]] = {}

        self.wiki = self._visible(wiki_uid_map(knowledge_root / "wiki"))

        self.contacts_root = resolve_contacts_root(knowledge_root, config)
        self.contact_index: pii.ExcludedRecordIndex | None = None
        self.contacts_hidden = False
        if self.contacts_root is not None:
            self.contact_index = pii.ExcludedRecordIndex(self.contacts_root)
            verdict, _ = self.access(self.contacts_root)
            self.contacts_hidden = verdict == ACCESS_HIDDEN

        self.off_corpus_root: Path | None = None
        try:
            self.off_corpus_root = off_corpus_root(config, knowledge_root)
        except OffCorpusConfigError as exc:
            self.errors.append(f"off-corpus store misconfigured: {exc}")
        self.off_corpus = self._visible(_off_corpus_uid_map(self.off_corpus_root))

    def access(self, path: Path) -> tuple[str, str | None]:
        key = Path(path).resolve()
        if key not in self._access:
            self._access[key] = target_access(key, self.knowledge_root, self.config)
        return self._access[key]

    def _visible(self, uid_map: dict[str, list[Path]]) -> dict[str, list[Path]]:
        out: dict[str, list[Path]] = {}
        for uid, paths in uid_map.items():
            kept = [p for p in paths if self.access(p)[0] != ACCESS_HIDDEN]
            if kept:
                out[uid] = kept
        return out

    def contact_records(self, uid: str) -> list[Path]:
        if self.contact_index is None or self.contacts_hidden:
            return []
        return [p for p in self.contact_index.all_by_uid(uid) if p.exists()]

    def contact_records_ignoring_access(self, uid: str) -> list[Path]:
        if self.contact_index is None:
            return []
        return [p for p in self.contact_index.all_by_uid(uid) if p.exists()]


# ---------------------------------------------------------------------------
# Planning (read-only)
# ---------------------------------------------------------------------------


def split_token(token: str) -> tuple[str | None, str]:
    """``"contact:ab12"`` -> ``("contact", "ab12")``; a bare uid -> ``(None, uid)``."""
    head, sep, tail = token.partition(":")
    if sep and head in STORES and tail:
        return head, tail
    return None, token


def _resolve_one(
    token: str, stores: _Stores
) -> RetireTarget | UidResolutionError:
    store, uid = split_token(token.strip())
    pages = stores.wiki.get(uid, []) if store in (None, STORE_PAGE) else []
    contacts = stores.contact_records(uid) if store in (None, STORE_CONTACT) else []
    off = stores.off_corpus.get(uid, []) if store in (None, STORE_OFF_CORPUS) else []

    if len(pages) > 1:
        return UidResolutionError(token, "ambiguous", matches=pages)
    if len(off) > 1:
        return UidResolutionError(token, "ambiguous", matches=off)
    if pages and off:
        return UidResolutionError(token, "ambiguous", matches=pages + off)
    if pages:
        cascade_hidden = stores.contacts_hidden and bool(
            stores.contact_records_ignoring_access(uid)
        )
        return RetireTarget(
            uid=uid,
            store=STORE_PAGE,
            paths=pages,
            cascade_records=len(stores.contact_records(uid)),
            cascade_hidden=cascade_hidden,
        )
    if contacts and off:
        # Contact record filenames may be address-derived, so the error lists
        # only the off-corpus match by name (see UidResolutionError.__str__).
        return UidResolutionError(token, "ambiguous", matches=off)
    if contacts:
        return RetireTarget(uid=uid, store=STORE_CONTACT, paths=contacts)
    if off:
        return RetireTarget(uid=uid, store=STORE_OFF_CORPUS, paths=off)
    return UidResolutionError(token, "unknown")


def _refusals(
    target: RetireTarget, stores: _Stores, *, force: bool
) -> list[TargetRefusal]:
    out: list[TargetRefusal] = []
    for path in target.paths:
        verdict, reason = stores.access(path)
        if verdict == ACCESS_REFUSED and reason:
            out.append(TargetRefusal(target.uid, target.store, reason))
            break
    if target.store == STORE_PAGE and target.cascade_records and stores.contacts_root:
        verdict, reason = stores.access(stores.contacts_root)
        if verdict == ACCESS_REFUSED and reason:
            out.append(
                TargetRefusal(
                    target.uid,
                    target.store,
                    f"its contact records cannot be retired with it: {reason}",
                )
            )
    if target.store == STORE_CONTACT:
        live = stores.wiki.get(target.uid, [])
        if live and not force:
            out.append(
                TargetRefusal(
                    target.uid,
                    target.store,
                    f"these are the only contact records of live page {live[0].name}; "
                    "retire the page instead, or pass --force-retire",
                )
            )
        refusal = _not_git_tracked(target.paths, stores.knowledge_root)
        if refusal:
            out.append(TargetRefusal(target.uid, target.store, refusal))
    return out


def _not_git_tracked(paths: list[Path], knowledge_root: Path) -> str | None:
    """Why *paths* cannot get a git provenance snapshot, else ``None``."""
    kr = knowledge_root.resolve()
    if not (kr / ".git").exists():
        return "the knowledge root is not a git repository, and recovery is git-only"
    for path in paths:
        try:
            rel = str(Path(path).resolve().relative_to(kr))
        except ValueError:
            return (
                "the contacts surface lies outside the knowledge root's git "
                "repository, so no provenance snapshot can be taken"
            )
        if _git(kr, "check-ignore", "-q", "--", rel).returncode == 0:
            return (
                "the contacts surface is ignored by git, so no provenance "
                "snapshot can be taken"
            )
    return None


def plan_retirement(
    knowledge_root: Path,
    tokens: list[str],
    config: dict[str, Any] | None,
    *,
    force: bool = False,
) -> RetirePlan:
    """Resolve *tokens* across every store and check each against the caller's rights.

    Read-only. Every token is checked even after an earlier one fails, so a
    caller can report every problem in one pass. Duplicate tokens collapse.
    """
    stores = _Stores(knowledge_root, config)
    plan = RetirePlan(errors=list(stores.errors))
    seen: set[tuple[str, str]] = set()
    for token in tokens:
        result = _resolve_one(token, stores)
        if isinstance(result, UidResolutionError):
            if all(e.uid != result.uid for e in plan.unresolved):
                plan.unresolved.append(result)
            continue
        key = (result.store, result.uid)
        if key in seen:
            continue
        seen.add(key)
        plan.targets.append(result)
    # A page target already settles its uid's contact records (the cascade),
    # so a ``contact:<uid>`` for the same uid in the same run is redundant, and
    # would try to remove records the page step has just moved.
    page_uids = {t.uid for t in plan.targets if t.store == STORE_PAGE}
    plan.targets = [
        t for t in plan.targets if not (t.store == STORE_CONTACT and t.uid in page_uids)
    ]
    for target in plan.targets:
        plan.refused.extend(_refusals(target, stores, force=force))
    _check_policy_protection(knowledge_root, plan, config, force=force)
    return plan


def _check_policy_protection(
    knowledge_root: Path, plan: RetirePlan, config: dict[str, Any] | None, *, force: bool
) -> None:
    """Refuse (or, under *force*, record) each page an ``always`` rule protects.

    The one shared decision is :func:`athenaeum.relevance_policy.protecting_rule`,
    reached through :func:`athenaeum.retire_pages.find_policy_protected`
    (athenaeum#3152). An invalid policy file fails closed: the run is blocked.
    """
    from athenaeum.relevance_policy import RelevancePolicyError
    from athenaeum.retire_pages import find_policy_protected

    pages = [
        RetireCandidate(uid=t.uid, path=t.paths[0]) for t in plan.targets if t.store == STORE_PAGE
    ]
    if not pages:
        return
    try:
        protected = find_policy_protected(knowledge_root, pages, config)
    except RelevancePolicyError as exc:
        plan.errors.append(f"relevance policy is invalid, so protection cannot be checked: {exc}")
        return
    for cand, rule in protected:
        if force:
            plan.forced_protected.append((cand.uid, rule))
        else:
            plan.refused.append(
                TargetRefusal(
                    cand.uid,
                    STORE_PAGE,
                    f"protected by relevance-policy rule {rule.number} (line {rule.line}): "
                    f"{rule.describe()}; pass --force-retire to retire it anyway",
                )
            )


def _record_values(meta: dict[str, Any]) -> list[str]:
    """Every address a record lists, including a slug-keyed record's ``identifier:``."""
    found = pii.identifiers_on_record(meta)
    if found:
        return found
    own = meta.get("identifier")
    if isinstance(own, str) and own.strip():
        return [own.strip()]
    return []


def _origin_commit(path: Path, knowledge_root: Path) -> str | None:
    kr = knowledge_root.resolve()
    try:
        rel = str(Path(path).resolve().relative_to(kr))
    except ValueError:
        return None
    if not (kr / ".git").exists():
        return None
    out = _git(kr, "log", "--diff-filter=A", "--format=%h", "--", rel)
    if out.returncode != 0:
        return None
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    return lines[-1].strip() if lines else None


def plan_pageless_retirement(
    knowledge_root: Path, config: dict[str, Any] | None
) -> RetirePlan:
    """Select every page-less contact record ``contact-surface-lint`` reports.

    The selection is :func:`athenaeum.contact_record_repair.plan_contact_record_repair`'s
    ``pageless_records`` (a uid with no live page and no merge record), the
    same population ``contact-surface-lint`` counts. The same access rules
    apply as for a named uid: records the caller cannot read are left out, a
    store the caller cannot write refuses the run.
    """
    stores = _Stores(knowledge_root, config)
    plan = RetirePlan(errors=list(stores.errors))
    if stores.contacts_root is None or stores.contact_index is None:
        plan.errors.append(
            "no contacts surface: the pii class is not mapped to an excluded surface"
        )
        return plan
    if stores.contacts_hidden:
        return plan
    repair = plan_contact_record_repair(knowledge_root, stores.contacts_root)
    index = stores.contact_index
    for uid, paths in repair.pageless_records:
        live_paths = [p for p in paths if p.exists()]
        if not live_paths:
            continue
        target = RetireTarget(uid=uid, store=STORE_CONTACT, paths=live_paths)
        plan.targets.append(target)
        plan.refused.extend(_refusals(target, stores, force=False))
        summary = PagelessRecordSummary(uid=uid, records=len(live_paths))
        for path in live_paths:
            for value in _record_values(pii.read_bounce_record(path)):
                summary.masked_values.append(pii.mask_contact_address(value))
                summary.on_live_record.append(
                    _on_live_record(value, uid, index, stores)
                )
            if summary.origin_commit is None:
                summary.origin_commit = _origin_commit(path, knowledge_root)
        plan.pageless.append(summary)
    return plan


def _on_live_record(
    value: str, uid: str, index: pii.ExcludedRecordIndex, stores: _Stores
) -> bool:
    for other in index.all_by_identifier(value):
        other_uid = str(pii.read_bounce_record(other).get("uid") or "").strip()
        if other_uid and other_uid != uid and other_uid in stores.wiki:
            return True
    return False


def preview_pages(knowledge_root: Path, plan: RetirePlan) -> RetireReport:
    """The page half of a dry run: pending merges to withdraw, index lines to drop."""
    return build_retire_report(knowledge_root, _page_candidates(plan))


def _page_candidates(plan: RetirePlan) -> list[RetireCandidate]:
    return [
        RetireCandidate(uid=t.uid, path=t.paths[0], settle_contacts=not t.cascade_hidden)
        for t in plan.of_store(STORE_PAGE)
    ]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_plan(
    knowledge_root: Path,
    plan: RetirePlan,
    *,
    reason: str,
    cache_dir: Path,
    config: dict[str, Any] | None = None,
    force: bool = False,
) -> RetireRunReport:
    """Retire every target in *plan*: pages, then contact records, then off-corpus.

    Refuses outright (no write) when the plan is blocked. Stops at the first
    store that reports an error, so a failed page retirement never carries on
    into an irreversible off-corpus erasure.
    """
    report = RetireRunReport()
    if plan.blocked:
        report.errors.append("plan is blocked - nothing retired")
        return report

    pages = _page_candidates(plan)
    if pages:
        rules = dict(plan.forced_protected)
        forced = [(c, rules[c.uid]) for c in pages if c.uid in rules]
        page_report = apply_retirement(
            knowledge_root, pages, reason=reason, config=config, forced_protected=forced or None
        )
        report.pages = page_report
        report.errors.extend(page_report.errors)
        report.committed = report.committed or page_report.committed
        if report.errors:
            return report

    contacts = plan.of_store(STORE_CONTACT)
    if contacts:
        _apply_contacts(knowledge_root, contacts, reason, cache_dir, config, force, report)
        if report.errors:
            return report

    off = plan.of_store(STORE_OFF_CORPUS)
    if off:
        _apply_off_corpus(knowledge_root, off, reason, cache_dir, config, report)
    return report


def _apply_contacts(
    knowledge_root: Path,
    targets: list[RetireTarget],
    reason: str,
    cache_dir: Path,
    config: dict[str, Any] | None,
    force: bool,
    report: RetireRunReport,
) -> None:
    kr = knowledge_root.resolve()
    contacts_root = resolve_contacts_root(knowledge_root, config)
    paths = [p for t in targets for p in t.paths]
    rel_paths = [str(Path(p).resolve().relative_to(kr)) for p in paths]
    uids = ", ".join(t.uid for t in targets)
    forced = (
        "\n\nForced: --force-retire retired the only contact records of a live page."
        if force
        else ""
    )
    result = archive_via_two_commit_git_rm(
        kr,
        rel_paths,
        snapshot_message=(
            f"chore(retire): provenance snapshot before retiring "
            f"{len(paths)} contact record(s) (athenaeum#3151)"
        ),
        archive_message=(
            f"chore(retire): retire {len(paths)} contact record(s) of uid(s) {uids}: "
            f"{reason} (athenaeum#3151)" + forced
        ),
    )
    report.errors.extend(result.errors)
    if not result.committed:
        return
    report.committed = True
    report.contact_records_retired += len(paths)
    if contacts_root is not None:
        for path in paths:
            try:
                invalidate_cached_record(contacts_root, cache_dir, path)
            except OSError as exc:
                report.errors.append(
                    f"contact-record index update failed ({type(exc).__name__}); "
                    "the next load rebuilds it"
                )
    log.info("retire: retired %d contact record(s); committed", len(paths))


def _apply_off_corpus(
    knowledge_root: Path,
    targets: list[RetireTarget],
    reason: str,
    cache_dir: Path,
    config: dict[str, Any] | None,
    report: RetireRunReport,
) -> None:
    root = off_corpus_root(config, knowledge_root)
    adapter = off_corpus_adapter(config)
    store = off_corpus_store(config, knowledge_root)
    if root is None or adapter is None or store is None:
        report.errors.append("off-corpus store is not enabled - nothing erased")
        return
    for target in targets:
        rel = str(target.paths[0].resolve().relative_to(root.resolve()))
        try:
            erase_off_corpus_record(config, knowledge_root, cache_dir, rel)
        except (OSError, OffCorpusConfigError) as exc:
            report.errors.append(f"{target.uid} (off-corpus): erase failed ({type(exc).__name__})")
            return
        report.off_corpus_erased += 1
        row = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "op": "retire",
            "uid": target.uid,
            "record": rel,
            "reason": reason,
        }
        try:
            store.append(
                StoreKey(surface=adapter.name, key=OFF_CORPUS_RETIREMENT_LEDGER),
                (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"),
            )
        except OSError as exc:
            report.errors.append(
                f"{target.uid} (off-corpus): erased, but the reason was not recorded "
                f"({type(exc).__name__})"
            )
    log.info("retire: erased %d off-corpus record(s)", report.off_corpus_erased)
