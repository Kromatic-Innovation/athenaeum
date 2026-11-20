# SPDX-License-Identifier: Apache-2.0
"""The filing checks after a merge is published (athenaeum#2928, slice S17 of the sync design).

Section 4.3 of ``docs/design/federation-multi-writer-sync.md``. Once a push that
carried a sync merge has succeeded, the install that pushed runs the
deterministic filing checks of the north star section 2.12 on what the push
published, and opens a decision in the store's own queue for each failure.

* **Only what changed.** A unit is checked when its value at the pushed commit
  differs from its value at the remote tip as it stood just before the push
  (:func:`athenaeum.page_merge.page_units` names the units). A push whose range
  holds no merge commit, or whose merge published no changed unit, runs no check.
* **Never a hold.** This runs after the push, reads only, and never raises into the
  sync: a crash is returned as a failed result and the caller reports it. No page
  is edited, held or reverted; the fix is an ordinary follow-up commit.
* **No index.** Pages are read through the store seam
  (:func:`athenaeum.store.read_file_at_revision`,
  :func:`athenaeum.store.list_files_at_revision`) at the pushed commit, inside the one
  repository that was pushed. The contact records and the entity index that the
  attribution seam of :mod:`athenaeum.pii_attribution` builds are not consulted; the
  detectors are the same ones (:func:`athenaeum.pii.find_inline_emails`,
  :func:`athenaeum.pii.find_inline_phones`, the name forms of C2).
* **Three checks**, each the section 2.12 check of the same name reduced to page
  text: ``held-elsewhere`` (D2: a contact value already on another page of the
  store), ``local-part-names-other`` (C2: an address whose local part names a
  different person) and ``person-value-on-organisation`` (D3: a person-shaped
  address on an organisation's page). The fourth, a value absent from its source,
  holds by construction of the merge and is not run.

The other pages are read only when a changed unit carries a contact value, and then
once per run, so the cost follows the merge and a push with nothing to check reads
no page beyond the changed ones.

Layering: L4. Imports :mod:`athenaeum.sync_integrate` (L4), :mod:`athenaeum.store` (L0),
:mod:`athenaeum.store_decisions` (L2), :mod:`athenaeum.page_merge` (L2),
:mod:`athenaeum.pii` and :mod:`athenaeum.pii_attribution` (L3). Runs no git command itself.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athenaeum import pii
from athenaeum.models import parse_frontmatter
from athenaeum.page_merge import page_units
from athenaeum.pii_attribution import ROLE_LOCAL_PARTS, _person_name_forms
from athenaeum.store import (
    changed_paths,
    list_files_at_revision,
    merge_commits_between,
    read_file_at_revision,
)
from athenaeum.store_decisions import (
    PageQuote,
    StoreDecisionRefused,
    file_decision,
    load_queue,
)
from athenaeum.sync_integrate import PageRoot, PushOutcome, SyncReport, _classify

log = logging.getLogger(__name__)

__all__ = [
    "CHECK_HELD_ELSEWHERE",
    "CHECK_NAMES_OTHER",
    "CHECK_PERSON_ON_ORGANISATION",
    "DECISION_KIND_FILING_CHECK",
    "FilingFinding",
    "FilingResult",
    "file_findings",
    "run_after_push",
    "run_filing_checks",
]

CHECK_HELD_ELSEWHERE = "held-elsewhere"
CHECK_NAMES_OTHER = "local-part-names-other"
CHECK_PERSON_ON_ORGANISATION = "person-value-on-organisation"

#: ``kind`` of the item a failed check opens in the store's decision queue.
DECISION_KIND_FILING_CHECK = "filing-check"

#: Page types that stand for an organisation, not a person.
ORGANISATION_TYPES = frozenset({"company", "organization", "organisation", "org"})

#: A local part shaped like a person: two words of letters joined by ``.``, ``_`` or ``-``.
_PERSON_SHAPE = re.compile(r"^[a-z]{2,}[._-][a-z]{2,}$")
_MIN_PHONE_DIGITS = 7


@dataclass(frozen=True)
class FilingFinding:
    """One failed check on one unit of one page. Carries no contact value."""

    check: str
    path: str  # path of the page from the repository root
    uid: str
    title: str
    unit: str  # a unit name of :func:`athenaeum.page_merge.page_units`
    value_kind: str  # "email" or "phone"
    others: tuple[tuple[str, str, str], ...] = ()  # (uid, title, path) of the other pages named


@dataclass
class FilingResult:
    """What the checks did: how many units they looked at and what failed."""

    merge_published: bool = False
    pages_changed: int = 0
    units_checked: int = 0
    findings: list[FilingFinding] = field(default_factory=list)
    filed: int = 0
    skipped_existing: int = 0
    refused: int = 0


@dataclass(frozen=True)
class _PageRecord:
    path: str
    uid: str
    title: str
    type: str
    names: frozenset[str]  # forms of the page's own name and aliases
    values: frozenset[str]  # every contact value on the page, normalised


def _normalise(kind: str, value: str) -> str:
    if kind == "phone":
        digits = "".join(ch for ch in value if ch.isdigit())
        return digits if len(digits) >= _MIN_PHONE_DIGITS else ""
    return pii.normalize_identifier(value)


def contact_values(text: str) -> list[tuple[str, str]]:
    """``(kind, normalised value)`` for every address and phone number in *text*.

    Service addresses (a clone URL, a calendar group id) are not contact data and
    are left out, as the attribution seam leaves them out.
    """
    found: list[tuple[str, str]] = []
    for token in pii.find_inline_emails(text):
        if pii.is_service_address(token):
            continue
        norm = _normalise("email", token)
        if norm:
            found.append(("email", norm))
    for token in pii.find_inline_phones(text):
        norm = _normalise("phone", token)
        if norm:
            found.append(("phone", norm))
    return found


def _split(value: str) -> tuple[str, str]:
    local, _, domain = value.rpartition("@")
    return local, domain


def _collapsed(local: str) -> str:
    return local.replace(".", "").replace("_", "").replace("-", "")


def _name_forms(meta: Mapping[str, Any]) -> frozenset[str]:
    forms: set[str] = set()
    names = [meta.get("name")]
    aliases = meta.get("aliases")
    if isinstance(aliases, list):
        names += aliases
    elif isinstance(aliases, str):
        names.append(aliases)
    for name in names:
        if isinstance(name, str) and name.strip():
            forms |= _person_name_forms(name)
    return frozenset(forms)


class _Store:
    """The pages of one repository at one commit, read through the store seam.

    Built lazily: nothing is read until a check needs the other pages.
    """

    def __init__(self, repo: Path, rev: str, roots: tuple[PageRoot, ...]) -> None:
        self._repo, self._rev, self._roots = repo, rev, roots
        self._records: list[_PageRecord] | None = None

    def page_paths(self) -> list[str]:
        paths: list[str] = []
        for root in self._roots:
            for path in list_files_at_revision(self._repo, self._rev, root.rel.strip("/")):
                if _classify(path, self._roots)[0] == "page":
                    paths.append(path)
        return sorted(set(paths))

    def records(self) -> list[_PageRecord]:
        if self._records is None:
            out: list[_PageRecord] = []
            for path in self.page_paths():
                text = _decode(read_file_at_revision(self._repo, self._rev, path))
                if text is None:
                    continue
                out.append(_record(path, text))
            self._records = out
        return self._records


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _record(path: str, text: str) -> _PageRecord:
    meta, _ = parse_frontmatter(text)
    meta = meta if isinstance(meta, dict) else {}
    return _PageRecord(
        path=path,
        uid=str(meta.get("uid") or ""),
        title=str(meta.get("name") or ""),
        type=str(meta.get("type") or "").strip().lower(),
        names=_name_forms(meta),
        values=frozenset(v for _k, v in contact_values(text)),
    )


def run_filing_checks(
    repo: Path,
    remote_before: str,
    pushed: str,
    roots: tuple[PageRoot, ...],
) -> FilingResult:
    """Run the filing checks on the units *pushed* published over *remote_before*.

    Read-only. Raises ``subprocess.CalledProcessError`` for an unknown revision; the
    caller (:func:`run_after_push`) turns that into a failed step.
    """
    result = FilingResult()
    if remote_before == pushed or not merge_commits_between(repo, remote_before, pushed):
        return result
    result.merge_published = True
    store = _Store(repo, pushed, roots)
    for path in changed_paths(repo, remote_before, pushed):
        if _classify(path, roots)[0] != "page":
            continue
        after = _decode(read_file_at_revision(repo, pushed, path))
        if after is None:
            continue
        before = _decode(read_file_at_revision(repo, remote_before, path))
        after_units, before_units = page_units(after), page_units(before)
        changed = [u for u, v in after_units.items() if before_units.get(u) != v]
        if not changed:
            continue
        result.pages_changed += 1
        page = _record(path, after)
        for unit in changed:
            result.units_checked += 1
            result.findings.extend(_check_unit(page, unit, after_units[unit], store))
    return result


def _check_unit(page: _PageRecord, unit: str, text: str, store: _Store) -> Iterable[FilingFinding]:
    values = sorted(set(contact_values(text)))
    if not values:
        return
    others = [r for r in store.records() if r.path != page.path and r.uid != page.uid]
    persons = [r for r in others if r.type == "person"]
    raised: dict[str, dict[str, list[_PageRecord]]] = {}
    for kind, value in values:
        holders = [r for r in others if value in r.values]
        if holders:
            raised.setdefault(CHECK_HELD_ELSEWHERE, {}).setdefault(kind, []).extend(holders)
        if kind != "email":
            continue
        local, _domain = _split(value)
        collapsed = _collapsed(local)
        named = [r for r in persons if local in r.names or collapsed in r.names]
        if named and not (local in page.names or collapsed in page.names):
            raised.setdefault(CHECK_NAMES_OTHER, {}).setdefault(kind, []).extend(named)
        if page.type in ORGANISATION_TYPES and local not in ROLE_LOCAL_PARTS:
            if named or _PERSON_SHAPE.match(local):
                raised.setdefault(CHECK_PERSON_ON_ORGANISATION, {}).setdefault(kind, [])
    for check in (CHECK_HELD_ELSEWHERE, CHECK_NAMES_OTHER, CHECK_PERSON_ON_ORGANISATION):
        for kind, named_pages in sorted(raised.get(check, {}).items()):
            unique = {r.path: r for r in named_pages}
            yield FilingFinding(
                check=check,
                path=page.path,
                uid=page.uid,
                title=page.title,
                unit=unit,
                value_kind=kind,
                others=tuple((r.uid, r.title, r.path) for r in unique.values() if r.uid),
            )


# -- the queue ----------------------------------------------------------------

_QUESTIONS = {
    CHECK_HELD_ELSEWHERE: (
        "A {kind} on this page, in unit {unit}, is also on another page of this store. "
        "Decide which page it belongs to and correct the other in a follow-up commit."
    ),
    CHECK_NAMES_OTHER: (
        "An address on this page, in unit {unit}, has a local part that names a different "
        "person of this store. Decide whether it belongs to that person and correct the page."
    ),
    CHECK_PERSON_ON_ORGANISATION: (
        "An address on this organisation's page, in unit {unit}, looks like a person's. "
        "Decide whether it belongs on this page or on that person's."
    ),
}


def _already_open(queue: Iterable[Mapping[str, Any]], finding: FilingFinding) -> bool:
    for item in queue:
        detail = item.get("detail") or {}
        if (
            item.get("kind") == DECISION_KIND_FILING_CHECK
            and item.get("disposition", "pending") == "pending"
            and detail.get("check") == finding.check
            and detail.get("unit") == finding.unit
            and detail.get("page_uid") == finding.uid
            and detail.get("value_kind") == finding.value_kind
        ):
            return True
    return False


def file_findings(
    knowledge_root: Path,
    store_id: str,
    findings: Iterable[FilingFinding],
    result: FilingResult,
    *,
    mounts: Mapping[str, Path] | None = None,
    asserter: Mapping[str, object] | None = None,
) -> None:
    """Open one item in *store_id*'s queue per finding; an identical open one is not repeated.

    The item names the page (uid, title, path) and the unit, and the other pages the
    check named; it never carries the contact value. Counts land on *result*.
    """
    queue = load_queue(knowledge_root, store_id)
    for finding in findings:
        if not finding.uid:
            continue  # an item names a page by uid; a page with none was held earlier
        if _already_open(queue, finding):
            result.skipped_existing += 1
            continue
        pages = [PageQuote(store_id, finding.uid, finding.title, "", finding.path)]
        pages += [PageQuote(store_id, uid, title, "", path) for uid, title, path in finding.others]
        try:
            record = file_decision(
                knowledge_root,
                store_id=store_id,
                question=_QUESTIONS[finding.check].format(
                    kind=finding.value_kind, unit=finding.unit
                ),
                pages=pages,
                kind=DECISION_KIND_FILING_CHECK,
                mounts=mounts,
                asserter=asserter,
                detail={
                    "check": finding.check,
                    "unit": finding.unit,
                    "page_uid": finding.uid,
                    "page_path": finding.path,
                    "value_kind": finding.value_kind,
                },
            )
        except StoreDecisionRefused as exc:
            result.refused += 1
            log.info("filing checks: no decision filed: %s", exc)
            continue
        queue.append(record)
        result.filed += 1


def run_after_push(
    repo: Path,
    outcome: PushOutcome,
    roots: tuple[PageRoot, ...],
    *,
    knowledge_root: Path,
    store_id: str,
    report: SyncReport | None = None,
    mounts: Mapping[str, Path] | None = None,
    asserter: Mapping[str, object] | None = None,
) -> FilingResult | None:
    """The post-push step. Never raises, never changes *outcome*, holds nothing.

    Runs only after a successful push whose remote tip before it is known. Records one
    ``filing-checks`` step on *report* when given: ``ok`` (with the number filed in
    the reason), or ``failed`` with ``check-crashed`` when anything went wrong, in
    which case the push stays as reported.
    """
    if not outcome.ok or not outcome.remote_before or not outcome.pushed:
        return None
    try:
        result = run_filing_checks(repo, outcome.remote_before, outcome.pushed, roots)
        if result.findings:
            file_findings(
                knowledge_root,
                store_id,
                result.findings,
                result,
                mounts=mounts,
                asserter=asserter,
            )
    except Exception as exc:  # noqa: BLE001 - the checks must never fail the sync
        log.warning("filing checks after push failed in %s: %s", repo, type(exc).__name__)
        if report is not None:
            report.add("filing-checks", "failed", "check-crashed")
        return None
    if report is not None:
        if not result.merge_published:
            report.add("filing-checks", "skipped", "no-merge-published")
        elif not result.units_checked:
            report.add("filing-checks", "skipped", "no-changed-unit")
        else:
            note = f"{len(result.findings)} failed"
            if result.refused:
                note += f", {result.refused} not filed (no compile right)"
            report.add("filing-checks", "ok", note)
    return result
