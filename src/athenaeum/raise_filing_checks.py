# SPDX-License-Identifier: Apache-2.0
"""The filing checks after a raised copy is published (athenaeum#2927, raise slice S21).

``docs/design/federation-raise-retract.md`` §4.4, "What happens around an acceptance
with no person looking", rule 1. Once a raised copy has been created in the target
store (:func:`athenaeum.raise_acceptance.accept_raise`) or replaced by a supersede
(:func:`athenaeum.raise_supersede.apply_supersede`), the accepting install runs the
deterministic filing checks of ``docs/north-star.md`` §2.12 on the copy, against the
target's own pages, and opens a ``filing-check`` item in the target's decision queue
naming the copy for each failure.

* **After, never before.** The checks run once the copy is committed and its
  acceptance is recorded. They are never a reason to reject a raise or to ask about
  it: a raise accepted without a person looking stays accepted that way, and the
  outcome of the acceptance is never changed by what the checks find.
* **Never a hold.** Nothing here edits, holds or reverts the copy. It stays as
  published until the item is settled; the fix is an ordinary follow-up commit.
  :func:`run_after_publish` never raises: a crash is recorded on the result.
* **What is checked.** A created copy is checked whole, including one an earlier
  acceptance created before it crashed. A replaced copy is checked on the units
  whose value differs from the copy before the replacement
  (:func:`athenaeum.page_merge.page_units` names the units), so a finding already
  settled on an unchanged unit is not opened again.
* **Through the store seam.** The copy and the other pages are read at the commit
  that published the copy (:func:`athenaeum.store.read_file_at_revision`,
  :func:`athenaeum.store.list_files_at_revision`); the version before a replacement
  is the copy at that commit's parent. A target that is not a git repository has no
  commit, and its files are read as they stand. No index is consulted.
* **Three checks**, those of :mod:`athenaeum.filing_checks`. The fourth, a value
  absent from the source it cites, holds by construction: the copy cites
  ``raise:<raise_id>``, its body is the payload's body accepted whole, and the
  ``content_hash`` was checked.

Layering: L3. Imports :mod:`athenaeum.filing_checks` (L3), :mod:`athenaeum.page_merge`
(L2) and :mod:`athenaeum.store` (L0). Runs no git command itself.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from athenaeum.filing_checks import (
    FilingFinding,
    PageRecord,
    check_unit,
    file_findings,
    page_record,
)
from athenaeum.page_merge import page_units
from athenaeum.store import list_files_at_revision, read_file_at_revision

log = logging.getLogger(__name__)

__all__ = [
    "COPY_CREATED",
    "COPY_REPLACED",
    "RaiseFilingResult",
    "run_after_publish",
    "run_filing_checks",
]

#: What happened to the copy, as the queue item records it.
COPY_CREATED = "created"
COPY_REPLACED = "replaced"


@dataclass
class RaiseFilingResult:
    """What the checks on one copy did."""

    copy: str = COPY_CREATED  # COPY_CREATED or COPY_REPLACED
    units_checked: int = 0
    findings: list[FilingFinding] = field(default_factory=list)
    filed: int = 0
    skipped_existing: int = 0
    refused: int = 0
    #: The checks failed to run; the copy and its acceptance stand as they were.
    crashed: bool = False


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_page(path: str) -> bool:
    """A page of the store: a ``.md`` file outside ``_store``, ``_``-files and dot-dirs."""
    parts = PurePosixPath(path).parts
    if not parts or not parts[-1].endswith(".md"):
        return False
    return not any(p.startswith(("_", ".")) for p in parts)


class _Pages:
    """The other pages of the target, read once and only when a check needs them."""

    def __init__(self, root: Path, rev: str | None) -> None:
        self._root, self._rev = root, rev
        self._records: list[PageRecord] | None = None

    def _paths(self) -> list[str]:
        if self._rev is not None:
            return [p for p in list_files_at_revision(self._root, self._rev) if _is_page(p)]
        found = (p.relative_to(self._root).as_posix() for p in self._root.rglob("*.md"))
        return sorted(p for p in found if _is_page(p))

    def read(self, path: str) -> str | None:
        if self._rev is not None:
            return _decode(read_file_at_revision(self._root, self._rev, path))
        try:
            return (self._root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def records(self) -> list[PageRecord]:
        if self._records is None:
            out: list[PageRecord] = []
            for path in self._paths():
                text = self.read(path)
                if text is not None:
                    out.append(page_record(path, text))
            self._records = out
        return self._records


def _before(root: Path, commit: str | None, path: str) -> str | None:
    """The copy at the parent of *commit*, or ``None`` (created there, or no parent)."""
    if commit is None:
        return None
    try:
        return _decode(read_file_at_revision(root, f"{commit}^", path))
    except subprocess.CalledProcessError:
        return None  # a root commit has no parent: the copy was created by it


def run_filing_checks(
    target_root: Path, copy_path: Path, commit: str | None, *, created: bool = False
) -> RaiseFilingResult:
    """Run the filing checks on the copy at *copy_path*, as *commit* published it.

    *created* says the copy was created (by an acceptance), so it is checked whole
    whatever commit is named; otherwise the version before is the copy at the
    parent of *commit*. A copy that *commit* does not hold (written, never
    committed) is read, with the other pages, as the files stand.

    Read-only. Raises on an unreadable store; :func:`run_after_publish` turns that
    into a crashed result.
    """
    root = Path(target_root)
    rel = Path(copy_path).resolve().relative_to(root.resolve()).as_posix()
    pages = _Pages(root, commit)
    after = pages.read(rel)
    if after is None and commit is not None:
        commit, pages = None, _Pages(root, None)
        after = pages.read(rel)
    before = None if created else _before(root, commit, rel)
    result = RaiseFilingResult(copy=COPY_CREATED if before is None else COPY_REPLACED)
    if after is None:
        return result
    after_units, before_units = page_units(after), page_units(before)
    record = page_record(rel, after)
    for unit, value in after_units.items():
        if before_units.get(unit) == value:
            continue
        result.units_checked += 1
        result.findings.extend(check_unit(record, unit, value, pages.records))
    return result


_PREAMBLE = (
    "This page is a raised copy (raise {raise_id}), {copy} here and published as it "
    "arrived. It stays as published until this is settled. "
)


def run_after_publish(
    knowledge_root: Path,
    *,
    target_store_id: str,
    target_root: Path,
    copy_path: Path,
    commit: str | None,
    raise_id: str,
    created: bool = False,
    mounts: Mapping[str, Path] | None = None,
    asserter: Mapping[str, object] | None = None,
) -> RaiseFilingResult:
    """The step after a copy is published. Never raises, holds nothing, changes no page.

    Opens one ``filing-check`` item in *target_store_id*'s queue per failure, naming
    the copy (an identical open item is not repeated). A crash anywhere is logged
    and returned as ``crashed``; the copy and its acceptance stand.
    """
    try:
        result = run_filing_checks(target_root, copy_path, commit, created=created)
        if result.findings:
            file_findings(
                knowledge_root,
                target_store_id,
                result.findings,
                result,
                mounts=mounts,
                asserter=asserter,
                preamble=_PREAMBLE.format(raise_id=raise_id, copy=result.copy),
                detail={"raise_id": raise_id, "copy": result.copy},
            )
    except Exception as exc:  # noqa: BLE001 - the checks must never undo an acceptance
        log.warning("filing checks after a raise failed for %s: %s", raise_id, type(exc).__name__)
        return RaiseFilingResult(crashed=True)
    return result
