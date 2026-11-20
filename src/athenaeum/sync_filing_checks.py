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

The checks and the queue item are :mod:`athenaeum.filing_checks`, shared with the
filing checks after a raised copy is accepted (:mod:`athenaeum.raise_filing_checks`).

Layering: L4. Imports :mod:`athenaeum.sync_integrate` (L4), :mod:`athenaeum.filing_checks`
(L3), :mod:`athenaeum.page_merge` (L2) and :mod:`athenaeum.store` (L0). Runs no git
command itself.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from athenaeum.filing_checks import (
    CHECK_HELD_ELSEWHERE,
    CHECK_NAMES_OTHER,
    CHECK_PERSON_ON_ORGANISATION,
    DECISION_KIND_FILING_CHECK,
    ORGANISATION_TYPES,
    FilingFinding,
    PageRecord,
    check_unit,
    contact_values,
    page_record,
)
from athenaeum.filing_checks import file_findings as _file_findings
from athenaeum.page_merge import page_units
from athenaeum.store import (
    changed_paths,
    list_files_at_revision,
    merge_commits_between,
    read_file_at_revision,
)
from athenaeum.sync_integrate import PageRoot, PushOutcome, SyncReport, _classify

log = logging.getLogger(__name__)

__all__ = [
    "CHECK_HELD_ELSEWHERE",
    "CHECK_NAMES_OTHER",
    "CHECK_PERSON_ON_ORGANISATION",
    "DECISION_KIND_FILING_CHECK",
    "ORGANISATION_TYPES",
    "FilingFinding",
    "FilingResult",
    "contact_values",
    "file_findings",
    "run_after_push",
    "run_filing_checks",
]


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


class _Store:
    """The pages of one repository at one commit, read through the store seam.

    Built lazily: nothing is read until a check needs the other pages.
    """

    def __init__(self, repo: Path, rev: str, roots: tuple[PageRoot, ...]) -> None:
        self._repo, self._rev, self._roots = repo, rev, roots
        self._records: list[PageRecord] | None = None

    def page_paths(self) -> list[str]:
        paths: list[str] = []
        for root in self._roots:
            for path in list_files_at_revision(self._repo, self._rev, root.rel.strip("/")):
                if _classify(path, self._roots)[0] == "page":
                    paths.append(path)
        return sorted(set(paths))

    def records(self) -> list[PageRecord]:
        if self._records is None:
            out: list[PageRecord] = []
            for path in self.page_paths():
                text = _decode(read_file_at_revision(self._repo, self._rev, path))
                if text is None:
                    continue
                out.append(page_record(path, text))
            self._records = out
        return self._records


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


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
        page = page_record(path, after)
        for unit in changed:
            result.units_checked += 1
            result.findings.extend(_check_unit(page, unit, after_units[unit], store))
    return result


def _check_unit(page: PageRecord, unit: str, text: str, store: _Store) -> Iterable[FilingFinding]:
    return check_unit(page, unit, text, store.records)


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
    check named; it never carries the contact value. Counts land on *result*. The
    queue item is :func:`athenaeum.filing_checks.file_findings`'s.
    """
    _file_findings(knowledge_root, store_id, findings, result, mounts=mounts, asserter=asserter)


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
