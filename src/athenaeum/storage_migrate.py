# SPDX-License-Identifier: Apache-2.0
"""Migrate a live entity page's PII to the #427 excluded surface (issue #479).

#427/#429 shipped the storage-adapter layer (:mod:`athenaeum.storage`) with a
built-in ``excluded`` surface (all corpus-policy flags false) that a
``storage.mapping`` entry routes an entity class to. #426 shipped the analogous
single-page migration CLI for a *different* shape (``authority convert``, a
pointer-stub rewrite). This module is the missing operator tool #437's
migration step needs: read a live entity page, extract its archival contact
data and write it to a page under the excluded surface, rewriting the original
page down to durable identifiers only (name, LinkedIn, record id,
Google-Contact id — everything *except* the archival contact data).

Contact-data detection is DETECTOR-DRIVEN across the whole page (issue #502).
#479 read only the ``emails:`` / ``phones:`` frontmatter keys; the live sweep
then found the residual PII lives mostly *elsewhere* — ``aliases:`` (dominant),
``former_emails:`` / ``alt_emails:`` / ``source:`` provenance strings, and body
prose. So the migrator now scans EVERY frontmatter value (and the body) with
the email/phone detectors rather than an allow-list of keys — a newly-invented
contact key cannot reopen the hole. The :data:`~athenaeum.pii.DURABLE_IDENTIFIER_FIELDS`
(name, ``linkedin_url``, ``handles_verified``, record IDs, …) are PRESERVED
verbatim, and pages whose only PII is in ``name:`` / ``preferred_name:`` are
EXCLUDED from this automatic path (renaming breaks slugs/edges — its own
slice); see :data:`~athenaeum.pii.DURABLE_IDENTIFIER_FIELDS`.

This module is a pure transform: it reads a page and returns the two would-be
file texts (:class:`PiiMigrationPlan`); it never writes. The thin CLI
(:mod:`athenaeum._cmd_storage`) is what applies a plan, dry-run by default —
mirroring the read / transform / write split ``authority.py`` /
``_cmd_authority.py`` use for ``authority convert``.

Detection reuses :mod:`athenaeum.pii` verbatim (``find_inline_emails`` /
``find_inline_phones`` / ``DURABLE_IDENTIFIER_FIELDS``) — the #455 outbound-lint
scanner's single source of truth for the patterns — rather than defining a
second detector.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.pii import (
    DURABLE_IDENTIFIER_FIELDS,
    PII_ENTITY_CLASS,
    PII_FLAG,
    find_inline_emails,
    find_inline_phones,
    name_field_holds_pii,
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
    #: True when the page's ``name:`` / ``preferred_name:`` is itself an email
    #: (or phone). Such pages are the #502 name-is-an-email population — EXCLUDED
    #: from this automatic path (renaming breaks slugs/edges) and handled in a
    #: separate slice. The migrator never rewrites the name field; this flag
    #: lets the bulk driver COUNT the excluded population so it is visible, not
    #: silently dropped. Independent of :attr:`changed` — a page can both carry
    #: a migratable alias AND be named after an email.
    name_field_pii: bool = False

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


def _migrate_str_value(value: str) -> tuple[str | None, list[str], list[str]]:
    """Extract contact tokens from one frontmatter string value.

    Returns ``(new_value, emails, phones)``:

    * ``emails`` / ``phones`` — the contact tokens detected in *value*.
    * ``new_value`` — the value with those tokens handled:
      - no PII → *value* unchanged.
      - the value is ENTIRELY contact data (a bare ``foo@bar.com`` alias, or a
        scalar that is just the address) → ``None``, signalling the caller to
        DROP this list entry / frontmatter key (nothing archival is lost — the
        token is preserved on the excluded record).
      - PII embedded in surrounding text (a ``source:`` provenance string like
        ``"imported from foo@bar.com via Streak"``) → the token redacted
        in place with :data:`INLINE_REDACTION_MARKER`, keeping the non-PII
        context so the field stays meaningful.
    """
    emails = find_inline_emails(value)
    phones = find_inline_phones(value)
    if not (emails or phones):
        return value, [], []
    redacted = _redact_inline_tokens(value, emails + phones)
    # If nothing but the marker(s)/whitespace survives, the value WAS pure
    # contact data — drop it rather than leave a content-free marker behind.
    residual = redacted.replace(INLINE_REDACTION_MARKER, "").strip()
    if not residual:
        return None, emails, phones
    return redacted, emails, phones


def _migrate_frontmatter(
    meta: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Rewrite frontmatter, extracting contact data from every non-durable field.

    Detector-driven (issue #502): scans EVERY frontmatter value — not just
    ``emails:`` / ``phones:`` — so contact data in ``aliases:``,
    ``former_emails:``, ``source:`` etc. is migrated, while a newly-invented
    contact key cannot reopen the hole. :data:`~athenaeum.pii.DURABLE_IDENTIFIER_FIELDS`
    (identity + the name-is-an-email carve-out) are preserved verbatim. List
    values keep their non-PII entries (a real alias survives even when a sibling
    entry was an email); scalar values keep their non-PII context.

    Returns ``(new_meta, emails, phones)`` — the rewritten frontmatter dict
    (key order preserved) and the deduped-later contact tokens pulled out of it.
    """
    new_meta: dict[str, Any] = {}
    emails: list[str] = []
    phones: list[str] = []
    for key, value in meta.items():
        if key in DURABLE_IDENTIFIER_FIELDS:
            new_meta[key] = value
            continue
        if isinstance(value, list):
            new_list: list[Any] = []
            for item in value:
                if isinstance(item, str):
                    new_item, em, ph = _migrate_str_value(item)
                    emails += em
                    phones += ph
                    if new_item is not None:
                        new_list.append(new_item)
                else:
                    new_list.append(item)
            if new_list:  # drop a key whose every entry was contact data
                new_meta[key] = new_list
        elif isinstance(value, str):
            new_value, em, ph = _migrate_str_value(value)
            emails += em
            phones += ph
            if new_value is not None:
                new_meta[key] = new_value
        else:
            new_meta[key] = value  # non-string scalar (int/bool/date/dict): keep
    return new_meta, emails, phones


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

    # Detector-driven frontmatter scan (#502): pull contact tokens from EVERY
    # non-durable field, preserving durable identifiers and the name-is-an-email
    # carve-out. Then the body inline tokens.
    new_meta, fm_emails, fm_phones = _migrate_frontmatter(meta)
    inline_emails = find_inline_emails(body)
    inline_phones = find_inline_phones(body)

    emails = _dedupe_preserving_order(fm_emails + inline_emails)
    phones = _dedupe_preserving_order(fm_phones + inline_phones)
    name_field_pii = name_field_holds_pii(meta)

    excluded_root = surface_root_for_class(PII_ENTITY_CLASS, config, knowledge_root)
    excluded_page_path = excluded_root / page_path.name

    if not (emails or phones):
        # No migratable contact data. A page whose only PII is in its name is
        # NOT migrated here (renaming is a separate slice) — but the flag lets
        # the bulk driver surface the excluded population rather than lose it.
        return PiiMigrationPlan(
            page_path=page_path,
            excluded_page_path=excluded_page_path,
            emails=[],
            phones=[],
            rewritten_page_text=None,
            excluded_page_text=None,
            name_field_pii=name_field_pii,
        )

    # Rewrite origin: frontmatter with contact data stripped/redacted (durable
    # identifiers untouched, real aliases preserved), then the body inline
    # tokens redacted.
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
        name_field_pii=name_field_pii,
    )


# ---------------------------------------------------------------------------
# Bulk target-set resolution (issue #495)
# ---------------------------------------------------------------------------
#
# #479 shipped the single-page path (``--page``); the live corpus needs the
# same transform over ~11.5k entity pages. Bulk mode is a thin driver over
# :func:`plan_pii_migration` — the transform stays per-page and pure, so the
# whole-run properties the issue asks for fall out of the single-page
# guarantees rather than from a second, batch-shaped code path:
#
# * **Idempotent / resumable.** A migrated origin page carries no archival
#   contact data, so its next plan's :attr:`~PiiMigrationPlan.changed` is
#   ``False`` — a re-run (whether after a clean finish or a crash halfway)
#   simply skips every already-migrated page and applies the remainder. There
#   is no run ledger to keep in sync and nothing to double-write: the corpus
#   itself is the checkpoint. (The excluded record is written under a
#   deterministic ``page_path.name``, so even a crash *between* the two writes
#   of one page re-converges — the re-run rewrites the same record and scrubs
#   the still-dirty origin.)
#
# Bulk mode migrates ENTITY pages (top-level ``wiki/*.md``, ``_``-prefixed
# queue/index/archive files excluded — those need per-file-kind operator
# decisions, not the entity-page transform; see the corpus-wide lint in
# :mod:`athenaeum.pii` for how they are surfaced). An operator who wants to
# redact a specific archive in place can still name it explicitly via a glob.


def iter_entity_pages(wiki_root: Path) -> Iterator[Path]:
    """Yield top-level ``wiki/*.md`` entity pages, skipping ``_``-prefixed files.

    The same flat, shallow entity-page scan the rest of the codebase uses
    (mirrors :func:`athenaeum.repair._iter_wiki_files` /
    :func:`athenaeum.search._iter_wiki_entries`): ``_``-prefixed queue, index
    and archive files are NOT entity pages and are deliberately excluded here —
    they are handled by the corpus-wide PII lint (:func:`athenaeum.pii.scan_corpus_pii`),
    which decides per file kind rather than running the entity-page transform.
    Missing wiki root yields nothing (never raises) so bulk mode is safe against
    an unconfigured knowledge base.
    """
    if not wiki_root.is_dir():
        return
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        yield path


def iter_glob_pages(wiki_root: Path, pattern: str) -> Iterator[Path]:
    """Yield files under *wiki_root* matching *pattern* (an operator-named set).

    Supports recursive ``**`` globs and is not restricted to ``*.md`` — an
    operator targeting a specific archive to redact in place (e.g.
    ``--glob '_*_archive.md'``) names exactly the file(s) they mean. Directories
    matched by the pattern are skipped; only regular files are yielded.
    """
    if not wiki_root.is_dir():
        return
    for path in sorted(wiki_root.glob(pattern)):
        if path.is_file():
            yield path
