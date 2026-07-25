# SPDX-License-Identifier: Apache-2.0
"""Migrate a live entity page's PII to the #427 excluded surface (issue #479).

#427/#429 shipped the storage-adapter layer (:mod:`athenaeum.storage`) with a
built-in ``excluded`` surface (all corpus-policy flags false) that a
``storage.mapping`` entry routes an entity class to. #426 shipped the analogous
single-page migration CLI for a *different* shape (``authority convert``, a
pointer-stub rewrite). This module is the missing operator tool #437's
migration step needs: read a live entity page, extract its archival contact
data (``emails``/``phones`` frontmatter + inline email/phone-shaped tokens in
the body), write that contact data to a page under the excluded surface, and
rewrite the original page down to durable identifiers only (name, LinkedIn,
record id, Google-Contact id — everything *except* the archival contact fields).

This module is a pure transform: it reads a page and returns the two would-be
file texts (:class:`PiiMigrationPlan`); it never writes. The thin CLI
(:mod:`athenaeum._cmd_storage`) is what applies a plan, dry-run by default —
mirroring the read / transform / write split ``authority.py`` /
``_cmd_authority.py`` use for ``authority convert``.

Detection reuses :mod:`athenaeum.pii` verbatim (``find_inline_emails`` /
``find_inline_phones`` / ``CONTACT_FRONTMATTER_FIELDS``) — the #455 outbound-lint
scanner's single source of truth for the patterns — rather than defining a
second detector.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.pii import (
    CONTACT_FRONTMATTER_FIELDS,
    PII_ENTITY_CLASS,
    PII_FLAG,
    _frontmatter_contact_values,
    find_inline_emails,
    find_inline_phones,
)
from athenaeum.storage import surface_root_for_class

# The migrated contact record is routed through the same ``PII_ENTITY_CLASS``
# (``"pii"``) the rest of the module uses, so an operator's existing
# ``storage.mapping: {pii: excluded}`` wiring governs where the record lands.

#: Marker left in the original page's body in place of an inline email/phone
#: token. Redaction (rather than deletion of surrounding prose) is the safe,
#: reversible default: it removes the raw contact datum from the corpus-visible
#: page — so ``recall`` no longer surfaces it (#437's spot-check) — while
#: keeping the sentence structure intact and the change trivially reviewable in
#: the dry-run diff. The archived value is preserved verbatim on the excluded
#: contact record, so nothing is lost.
INLINE_REDACTION_MARKER = "[contact redacted → excluded surface]"


@dataclass(frozen=True)
class PiiMigrationPlan:
    """The would-be result of migrating one page's PII off the corpus surface."""

    page_path: Path
    #: Where the archival contact record would be written (under the excluded
    #: surface root, resolved via the ``pii`` entity class).
    excluded_page_path: Path
    emails: list[str]
    phones: list[str]
    #: Rewritten original-page text (archival contact fields dropped, inline
    #: tokens redacted). ``None`` when :attr:`changed` is False.
    rewritten_page_text: str | None
    #: The archival contact-record text for the excluded surface. ``None`` when
    #: :attr:`changed` is False.
    excluded_page_text: str | None

    @property
    def changed(self) -> bool:
        """True when the page carried any archival contact data to migrate."""
        return bool(self.emails or self.phones)


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: list[str] = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


def _redact_inline_tokens(body: str, tokens: list[str]) -> str:
    """Replace each raw inline contact token in *body* with the redaction marker.

    Longest-token-first so a phone that is a substring of another match can't
    partially rewrite it; idempotent because the marker contains no
    email/phone-shaped token of its own.
    """
    new_body = body
    for token in sorted(tokens, key=len, reverse=True):
        new_body = new_body.replace(token, INLINE_REDACTION_MARKER)
    return new_body


def _render_excluded_record(
    meta: dict[str, Any],
    emails: list[str],
    phones: list[str],
) -> str:
    """Render the archival contact record for the excluded surface.

    Carries the durable identity linkage back to the origin entity (``uid`` /
    ``name``) plus the archival ``emails``/``phones``, and sets ``pii: true``
    (belt-and-suspenders: excluded even by the flag path, not only by placement).
    Kept deliberately minimal — the excluded surface is outside the corpus, so
    this record is never embedded, recalled, or merged.
    """
    record: dict[str, Any] = {}
    uid = meta.get("uid")
    if uid is not None and str(uid).strip():
        record["uid"] = uid
    name = meta.get("name")
    if name is not None and str(name).strip():
        record["name"] = f"{name} — contact record"
        record["contact_of"] = name
    record[PII_FLAG] = True
    if emails:
        record["emails"] = emails
    if phones:
        record["phones"] = phones

    origin = meta.get("name") or meta.get("uid") or "the entity page"
    body = (
        f"Archival contact data migrated off entity page {origin!r} to the "
        "excluded surface (issues #427/#437). This record is outside the "
        "corpus: not embedded, recalled, or merge-eligible. The origin page "
        "retains durable identifiers only.\n"
    )
    return render_frontmatter(record) + "\n" + body


def plan_pii_migration(
    page_path: Path,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> PiiMigrationPlan:
    """Compute the migration for one entity page — pure, writes nothing.

    Extracts archival contact data from *page_path* (frontmatter
    ``emails``/``phones`` + inline email/phone tokens in the body), and returns
    the would-be excluded-surface record plus the rewritten origin page (those
    fields dropped, inline tokens redacted). When the page carries no contact
    data the plan's :attr:`~PiiMigrationPlan.changed` is False and both texts
    are ``None`` (a no-op the CLI reports rather than writing an empty record).
    """
    text = page_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        meta = {}

    fm_contacts = _frontmatter_contact_values(meta)
    inline_emails = find_inline_emails(body)
    inline_phones = find_inline_phones(body)

    emails = _dedupe_preserving_order(fm_contacts.get("emails", []) + inline_emails)
    phones = _dedupe_preserving_order(fm_contacts.get("phones", []) + inline_phones)

    excluded_root = surface_root_for_class(PII_ENTITY_CLASS, config, knowledge_root)
    excluded_page_path = excluded_root / page_path.name

    if not (emails or phones):
        return PiiMigrationPlan(
            page_path=page_path,
            excluded_page_path=excluded_page_path,
            emails=[],
            phones=[],
            rewritten_page_text=None,
            excluded_page_text=None,
        )

    # Rewrite origin: drop the archival contact frontmatter fields (durable
    # identifiers — name/linkedin/uid/google_contact/type/etc. — are untouched),
    # then redact the inline tokens found in the body.
    new_meta = {
        k: v for k, v in meta.items() if k not in CONTACT_FRONTMATTER_FIELDS
    }
    new_body = _redact_inline_tokens(body, inline_emails + inline_phones)
    rewritten_page_text = render_frontmatter(new_meta) + "\n" + new_body

    excluded_page_text = _render_excluded_record(meta, emails, phones)

    return PiiMigrationPlan(
        page_path=page_path,
        excluded_page_path=excluded_page_path,
        emails=emails,
        phones=phones,
        rewritten_page_text=rewritten_page_text,
        excluded_page_text=excluded_page_text,
    )
