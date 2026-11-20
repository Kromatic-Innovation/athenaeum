# SPDX-License-Identifier: Apache-2.0
"""The deterministic filing checks of the north star, reduced to page text.

Section 2.12 of ``docs/north-star.md``. Two places run these checks on a page that
has already been published, and never before: the sync after a push that carried a
merge (:mod:`athenaeum.sync_filing_checks`, athenaeum#2928) and the target store
after a raised copy is created or replaced (:mod:`athenaeum.raise_filing_checks`,
athenaeum#2927). This module is what they share: the checks themselves and the item
a failure opens in the store's own decision queue. It reads no file and runs no git
command; the caller hands it page text and the other pages of the store.

* **Three checks**, each the section 2.12 check of the same name: ``held-elsewhere``
  (a contact value already on another page of the store), ``local-part-names-other``
  (an address whose local part names a different person) and
  ``person-value-on-organisation`` (a person-shaped address on an organisation's
  page). The fourth, a value absent from the source it cites, holds by construction
  in both callers and is not run.
* **No index.** The detectors are the ones the attribution seam uses
  (:func:`athenaeum.pii.find_inline_emails`, :func:`athenaeum.pii.find_inline_phones`,
  the name forms of C2); the contact records and the entity index it builds are not
  consulted.
* **Never a hold.** A failure is an item in the queue naming the page; it never
  carries the contact value, and nothing here edits, holds or reverts a page.

Layering: L3. Imports :mod:`athenaeum.pii` and :mod:`athenaeum.pii_attribution` (L3),
:mod:`athenaeum.store_decisions` (L2) and :mod:`athenaeum.models` (L1).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from athenaeum import pii
from athenaeum.models import parse_frontmatter
from athenaeum.pii_attribution import ROLE_LOCAL_PARTS, _person_name_forms
from athenaeum.store_decisions import (
    PageQuote,
    StoreDecisionRefused,
    file_decision,
    load_queue,
)

log = logging.getLogger(__name__)

__all__ = [
    "CHECK_HELD_ELSEWHERE",
    "CHECK_NAMES_OTHER",
    "CHECK_PERSON_ON_ORGANISATION",
    "DECISION_KIND_FILING_CHECK",
    "ORGANISATION_TYPES",
    "FilingFinding",
    "PageRecord",
    "check_unit",
    "contact_values",
    "file_findings",
    "page_record",
]

CHECK_HELD_ELSEWHERE = "held-elsewhere"
CHECK_NAMES_OTHER = "local-part-names-other"
CHECK_PERSON_ON_ORGANISATION = "person-value-on-organisation"
CHECKS = (CHECK_HELD_ELSEWHERE, CHECK_NAMES_OTHER, CHECK_PERSON_ON_ORGANISATION)

#: ``kind`` of the item a failed check opens in the store's decision queue.
DECISION_KIND_FILING_CHECK = "filing-check"

#: Page types that stand for an organisation, not a person.
ORGANISATION_TYPES = frozenset({"company", "organization", "organisation", "org"})

#: A local part shaped like a person: two words of letters joined by ``.``, ``_`` or ``-``.
_PERSON_SHAPE = re.compile(r"^[a-z]{2,}[._-][a-z]{2,}$")
_MIN_PHONE_DIGITS = 7
#: Words that make a compound local part a role inbox (``customer.service``, ``help-desk``),
#: on top of the single-word role lists of the attribution seam and :mod:`athenaeum.pii`.
_ROLE_WORDS = frozenset(
    {"customer", "service", "services", "desk", "enquiries", "inquiries", "orders", "general"}
)
_ROLE_TOKENS = ROLE_LOCAL_PARTS | pii.ROLE_LOCALPARTS | _ROLE_WORDS


@dataclass(frozen=True)
class FilingFinding:
    """One failed check on one unit of one page. Carries no contact value."""

    check: str
    path: str  # path of the page, as the caller names it
    uid: str
    title: str
    unit: str  # a unit name of :func:`athenaeum.page_merge.page_units`
    value_kind: str  # "email" or "phone"
    others: tuple[tuple[str, str, str], ...] = ()  # (uid, title, path) of the other pages named


@dataclass(frozen=True)
class PageRecord:
    """What the checks need of one page: who it is and which contact values it holds."""

    path: str
    uid: str
    title: str
    type: str
    names: frozenset[str]  # forms of the page's own name and aliases
    values: frozenset[str]  # every contact value on the page, normalised


class FiledCounts(Protocol):
    """Where :func:`file_findings` counts what it did."""

    filed: int
    skipped_existing: int
    refused: int


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


def _is_role(local: str) -> bool:
    """A role inbox: the local part, or any word of it, names a role."""
    return local in _ROLE_TOKENS or any(t in _ROLE_TOKENS for t in re.split(r"[._-]", local))


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


def page_record(path: str, text: str) -> PageRecord:
    """The :class:`PageRecord` of the page *text* found at *path*. Pure."""
    meta, _ = parse_frontmatter(text)
    meta = meta if isinstance(meta, dict) else {}
    return PageRecord(
        path=path,
        uid=str(meta.get("uid") or ""),
        title=str(meta.get("name") or ""),
        type=str(meta.get("type") or "").strip().lower(),
        names=_name_forms(meta),
        values=frozenset(v for _k, v in contact_values(text)),
    )


def check_unit(
    page: PageRecord,
    unit: str,
    text: str,
    other_pages: Callable[[], list[PageRecord]],
) -> Iterable[FilingFinding]:
    """The findings on unit *unit* (its value *text*) of *page*.

    *other_pages* returns every page of the store; it is called only when the unit
    carries a contact value, so a caller can read the store lazily.
    """
    values = sorted(set(contact_values(text)))
    if not values:
        return
    others = [r for r in other_pages() if r.path != page.path and r.uid != page.uid]
    persons = [r for r in others if r.type == "person"]
    raised: dict[str, dict[str, list[PageRecord]]] = {}
    for kind, value in values:
        holders = [r for r in others if value in r.values]
        if holders:
            raised.setdefault(CHECK_HELD_ELSEWHERE, {}).setdefault(kind, []).extend(holders)
        if kind != "email":
            continue
        local = value.rpartition("@")[0]
        collapsed = _collapsed(local)
        named = [r for r in persons if local in r.names or collapsed in r.names]
        if named and not (local in page.names or collapsed in page.names):
            raised.setdefault(CHECK_NAMES_OTHER, {}).setdefault(kind, []).extend(named)
        if page.type in ORGANISATION_TYPES and not _is_role(local):
            if named or _PERSON_SHAPE.match(local):
                raised.setdefault(CHECK_PERSON_ON_ORGANISATION, {}).setdefault(kind, [])
    for check in CHECKS:
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
    counts: FiledCounts,
    *,
    mounts: Mapping[str, Path] | None = None,
    asserter: Mapping[str, object] | None = None,
    preamble: str = "",
    detail: Mapping[str, Any] | None = None,
) -> list[dict]:
    """Open one item in *store_id*'s queue per finding; an identical open one is not repeated.

    The item names the page (uid, title, path) and the unit, and the other pages the
    check named; it never carries the contact value. *preamble* is put before the
    question and *detail* is added to the item's own detail. Counts land on
    *counts*; the items filed are returned.
    """
    queue = load_queue(knowledge_root, store_id)
    filed: list[dict] = []
    for finding in findings:
        if not finding.uid:
            continue  # an item names a page by uid; a page with none was held earlier
        if _already_open(queue, finding):
            counts.skipped_existing += 1
            continue
        pages = [PageQuote(store_id, finding.uid, finding.title, "", finding.path)]
        pages += [PageQuote(store_id, uid, title, "", path) for uid, title, path in finding.others]
        question = _QUESTIONS[finding.check].format(kind=finding.value_kind, unit=finding.unit)
        try:
            record = file_decision(
                knowledge_root,
                store_id=store_id,
                question=f"{preamble}{question}",
                pages=pages,
                kind=DECISION_KIND_FILING_CHECK,
                mounts=mounts,
                asserter=asserter,
                detail={
                    **(detail or {}),
                    "check": finding.check,
                    "unit": finding.unit,
                    "page_uid": finding.uid,
                    "page_path": finding.path,
                    "value_kind": finding.value_kind,
                },
            )
        except StoreDecisionRefused as exc:
            counts.refused += 1
            log.info("filing checks: no decision filed: %s", exc)
            continue
        queue.append(record)
        filed.append(record)
        counts.filed += 1
    return filed
