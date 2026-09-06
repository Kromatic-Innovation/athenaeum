# SPDX-License-Identifier: Apache-2.0
"""Migrate a live entity page's PII to the athenaeum#427 excluded surface (issue athenaeum#479).

athenaeum#427/#429 shipped the storage-adapter layer (:mod:`athenaeum.storage`) with a
built-in ``excluded`` surface (all corpus-policy flags false) that a
``storage.mapping`` entry routes an entity class to. athenaeum#426 shipped the analogous
single-page migration CLI for a *different* shape (``authority convert``, a
pointer-stub rewrite). This module is the missing operator tool athenaeum#437's
migration step needs: read a live entity page, extract its archival contact
data and write it to a page under the excluded surface, rewriting the original
page down to durable identifiers only (name, LinkedIn, record id,
Google-Contact id — everything *except* the archival contact data).

Contact-data detection is DETECTOR-DRIVEN across the whole page (issue athenaeum#502).
athenaeum#479 read only the ``emails:`` / ``phones:`` frontmatter keys; the live sweep
then found the residual PII lives mostly *elsewhere* — ``aliases:`` (dominant),
``former_emails:`` / ``alt_emails:`` / ``source:`` provenance strings, and body
prose. So the migrator now scans EVERY frontmatter value (and the body) with
the email/phone detectors rather than an allow-list of keys — a newly-invented
contact key cannot reopen the hole. The scan RECURSES into nested lists and
dicts to arbitrary depth (issue athenaeum#507): the athenaeum#502 sweep walked only the top level
of each value, so an address buried in a *list of dicts* — ``sources[].claim``
provenance blocks (the compiler copies claim text verbatim into frontmatter) or
``apollo_employment_history[].title`` enrichment payloads — survived. The
recursive walk targets the exact leaf and leaves every sibling structure
byte-identical: a ``sources[]`` block keeps its session/scope/date intact while
only the address in ``claim`` is redacted. Service identifiers that are
email-*shaped* but not contact data — ``git@github.com`` (an SSH clone-URL
pseudo-user) and ``…@group.calendar.google.com`` (a calendar group id) — are
EXCLUDED from migration by the explicit :func:`~athenaeum.pii.is_service_address`
predicate, so migrating them can't damage a repo/calendar reference. The
:data:`~athenaeum.pii.DURABLE_IDENTIFIER_FIELDS` (name, ``linkedin_url``,
``handles_verified``, record IDs, …) are PRESERVED verbatim at every level, and
pages whose only PII is in ``name:`` / ``preferred_name:`` are EXCLUDED from
this automatic path (renaming breaks slugs/edges — its own slice); see
:data:`~athenaeum.pii.DURABLE_IDENTIFIER_FIELDS`.

This module is a pure transform: it reads a page and returns the two would-be
file texts (:class:`PiiMigrationPlan`); it never writes. The thin CLI
(:mod:`athenaeum._cmd_storage`) is what applies a plan, dry-run by default —
mirroring the read / transform / write split ``authority.py`` /
``_cmd_authority.py`` use for ``authority convert``.

Detection is obtained through :func:`athenaeum.sensitivity.classify` (issue
athenaeum#992) — the ``email``/``phone`` shipped recognisers, which iterate the same
compiled patterns :mod:`athenaeum.pii` defines, rather than importing
:func:`athenaeum.pii.find_inline_emails`/:func:`~athenaeum.pii.find_inline_phones`
by name. :data:`~athenaeum.pii.DURABLE_IDENTIFIER_FIELDS` is still consumed
directly, since that is identity/field policy, not detection.

A detected token is never migrated when it carries an entry in
``wiki/_pii-allowlist.yml`` — the same adjudicated allowlist ``lint-pii``
already consults (issue athenaeum#1275, unblocking the athenaeum#936 artifact from
being overridden by the migrator it exists to police). Matching is EXACT
value equality only, via the shared :func:`athenaeum.pii.load_pii_allowlist`
reader — no case-folding, no substring/fuzzy matching — so this module and
``lint-pii`` can never disagree about which token is adjudicated. Callers
resolve the allowlist file (a missing file means nothing has been
adjudicated yet; a file that exists but fails to parse is refused rather
than silently treated as empty) and pass a ``{value: reason}`` mapping in;
this module stays a pure transform and never reads the allowlist file
itself.

Layering: L4 domain/pipeline module. May import L3 services (``models``,
``pii``, ``storage``) freely. Factoring rule: this module is a PURE
transform — it reads a page and returns the two would-be file texts; it never
writes to disk. Applying a plan (dry-run vs. ``--apply``) is the L5 CLI's job
(:mod:`athenaeum._cmd_storage`), mirroring the read/transform/write split
``authority.py``/``_cmd_authority.py`` already use.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.pii import (
    DURABLE_IDENTIFIER_FIELDS,
    PII_ENTITY_CLASS,
    PII_FLAG,
    PiiAllowlistEntry,
    is_pii_class_excluded,
    is_service_address,
    name_field_holds_pii,
)
from athenaeum.sensitivity import classify
from athenaeum.storage import surface_root_for_class


def _classified_values(
    text: str, recognizer_name: str, config: dict[str, Any] | None
) -> list[str]:
    """Values a single named recogniser matched in *text*, in order, deduped.

    The migrated replacement for a direct ``athenaeum.pii.find_inline_emails``/
    ``find_inline_phones`` call (issue athenaeum#992): routes through
    :func:`athenaeum.sensitivity.classify` instead of importing a detector
    function by name, so a deployment's own ``sensitivity:`` config (not just
    the two shipped recognisers) is honoured here. *recognizer_name* is not
    special-cased — ``"email"``/``"phone"`` reach this function through the
    exact same filter a custom recogniser name would, which is what lets a
    test-defined recogniser prove it travels the identical path (see
    ``TestSensitivityRegistryEndToEnd`` in ``tests/test_storage_migrate_pii.py``).

    Dedup is order-preserving, matching the contract
    :func:`~athenaeum.pii.find_inline_emails`/:func:`~athenaeum.pii.find_inline_phones`
    already had — :func:`athenaeum.sensitivity.classify` itself reports one
    match per occurrence (no dedup, per its built-ins' span contract), so this
    wrapper is what keeps this module's caller-visible behaviour unchanged.
    """
    seen: list[str] = []
    for classified in classify(text=text or "", config=config):
        if classified.match.recognizer != recognizer_name:
            continue
        value = classified.match.value
        if value not in seen:
            seen.append(value)
    return seen

# The migrated contact record is routed through the same ``PII_ENTITY_CLASS``
# (``"pii"``) the rest of the module uses, so an operator's existing
# ``storage.mapping: {pii: excluded}`` wiring governs where the record lands.

#: Marker left in the original page's body in place of an inline email/phone
#: token. Redaction (rather than deletion of surrounding prose) is the safe,
#: reversible default: it removes the raw contact datum from the corpus-visible
#: page — so ``recall`` no longer surfaces it (athenaeum#437's spot-check) — while
#: keeping the sentence structure intact and the change trivially reviewable in
#: the dry-run diff. The archived value is preserved verbatim on the excluded
#: contact record, so nothing is lost.
INLINE_REDACTION_MARKER = "[contact redacted → excluded surface]"


@dataclass(frozen=True)
class ExcludedRecordConflict:
    """One identity field where a re-migration disagrees with the record on disk.

    Raised (as a finding, not an exception) by :func:`_merge_excluded_record`
    when the excluded record already at ``excluded_page_path`` carries a
    DIFFERENT value for a scalar identity field (``uid``/``name``/
    ``contact_of``) than this run would write. This is deliberately narrow:
    ``emails``/``phones`` are plural by nature, so a re-migration that finds a
    NEW value there is additive (a union), never a conflict — only a
    disagreeing SCALAR is (issue athenaeum#1108, AC1).
    """

    field: str
    existing_value: Any
    new_value: Any


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
    #: The archival contact-record text for the excluded surface — a MERGE
    #: with whatever is already on disk at ``excluded_page_path`` when that
    #: file exists (issue athenaeum#1108), not a wholesale replacement. ``None``
    #: when :attr:`changed` is False.
    excluded_page_text: str | None
    #: True when the page's ``name:`` / ``preferred_name:`` is itself an email
    #: (or phone). Such pages are the athenaeum#502 name-is-an-email population — EXCLUDED
    #: from this automatic path (renaming breaks slugs/edges) and handled in a
    #: separate slice. The migrator never rewrites the name field; this flag
    #: lets the bulk driver COUNT the excluded population so it is visible, not
    #: silently dropped. Independent of :attr:`changed` — a page can both carry
    #: a migratable alias AND be named after an email.
    name_field_pii: bool = False
    #: True when a record already existed at ``excluded_page_path`` before
    #: this plan was computed — distinguishes "new record" from "merged into
    #: existing" in CLI output (issue athenaeum#1108, AC3).
    excluded_record_existed: bool = False
    #: Non-empty when the record already on disk disagrees with this run on a
    #: scalar identity field. The CLI refuses to ``--apply`` a conflicted plan
    #: (issue athenaeum#1108, AC1: surfaced, never silently resolved).
    excluded_record_conflicts: tuple[ExcludedRecordConflict, ...] = ()
    #: Detected email/phone tokens that were NOT migrated because they carry
    #: an adjudicated entry in the allowlist (issue athenaeum#1275) — exact-value
    #: matches only, deduped by value across the whole page (frontmatter +
    #: body), in first-seen order. A value here is left byte-identical
    #: wherever it appeared: never redacted inline, never dropped from
    #: frontmatter, never added to the excluded contact record. Reported so a
    #: skip is never silent (the same principle as athenaeum#1273's exclusion
    #: reporting) — the CLI prints these in both dry-run and ``--apply``.
    skipped_allowlisted: tuple[PiiAllowlistEntry, ...] = ()

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


def _dedupe_allowlist_entries(
    entries: list[PiiAllowlistEntry],
) -> tuple[PiiAllowlistEntry, ...]:
    """Dedupe skipped-allowlist entries by value, first-seen order, page-wide."""
    seen: set[str] = set()
    out: list[PiiAllowlistEntry] = []
    for e in entries:
        if e.value in seen:
            continue
        seen.add(e.value)
        out.append(e)
    return tuple(out)


def _split_allowlisted(
    tokens: list[str], allowlist: Mapping[str, str]
) -> tuple[list[str], list[PiiAllowlistEntry]]:
    """Split detected *tokens* into (migratable, skipped) against *allowlist*.

    *allowlist* is a ``{value: reason}`` mapping built from the adjudicated
    allowlist (issue athenaeum#1275) — matching is EXACT value equality only, no
    case-folding, no substring/fuzzy matching, mirroring
    :func:`athenaeum.pii.adjudicate_corpus_pii`'s matching semantics exactly
    so this module and ``lint-pii`` can never disagree about which token
    counts as adjudicated. A skipped token is returned with its reason
    (never migrated, never redacted, left byte-identical) so the caller can
    report it — a silent skip in a PII tool is its own hazard.
    """
    if not allowlist:
        return tokens, []
    migratable: list[str] = []
    skipped: list[PiiAllowlistEntry] = []
    for t in tokens:
        reason = allowlist.get(t)
        if reason is not None:
            skipped.append(PiiAllowlistEntry(value=t, reason=reason))
        else:
            migratable.append(t)
    return migratable, skipped


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


def _migratable_emails(
    text: str,
    config: dict[str, Any] | None,
    allowlist: Mapping[str, str] | None = None,
) -> tuple[list[str], list[PiiAllowlistEntry]]:
    """Email-shaped tokens in *text* that are genuine contact data, split against *allowlist*.

    Filters out service identifiers (``git@github.com``, Google Calendar group
    addresses, …) via :func:`~athenaeum.pii.is_service_address` (issue athenaeum#507): a
    naïve sweep that migrated those would damage the page (a broken clone URL /
    calendar ref) while archiving no real PII. Order and dedup follow
    :func:`_classified_values`, which mirrors :func:`~athenaeum.pii.find_inline_emails`'s
    contract exactly. Remaining candidates are then split via
    :func:`_split_allowlisted` (issue athenaeum#1275): an adjudicated value is
    returned separately, never as migratable.
    """
    candidates = [
        e for e in _classified_values(text, "email", config) if not is_service_address(e)
    ]
    return _split_allowlisted(candidates, allowlist or {})


def _migrate_str_value(
    value: str,
    config: dict[str, Any] | None,
    allowlist: Mapping[str, str] | None = None,
) -> tuple[str | None, list[str], list[str], list[PiiAllowlistEntry]]:
    """Extract contact tokens from one frontmatter string value.

    Returns ``(new_value, emails, phones, skipped)``:

    * ``emails`` / ``phones`` — the contact tokens MIGRATED from *value*
      (service identifiers excluded — see :func:`_migratable_emails` — and
      allowlisted values excluded — see :func:`_split_allowlisted`, issue
      athenaeum#1275).
    * ``skipped`` — tokens that were PII-shaped but carry an allowlist entry;
      left in *new_value* byte-identical, never redacted or dropped.
    * ``new_value`` — the value with the migrated tokens handled:
      - no migratable PII → *value* unchanged (a bare ``git@github.com`` service
        address, or a value whose only token is allowlisted, is left
        byte-identical, not redacted).
      - the value is ENTIRELY contact data (a bare ``foo@bar.com`` alias, or a
        scalar that is just the address) → ``None``, signalling the caller to
        DROP this list entry / frontmatter key (nothing archival is lost — the
        token is preserved on the excluded record).
      - PII embedded in surrounding text (a ``source:`` provenance string like
        ``"imported from foo@bar.com via Streak"``, or a ``sources[].claim``
        like ``"Reached Priya at priya@example.com"``) → the token redacted
        in place with :data:`INLINE_REDACTION_MARKER`, keeping the non-PII
        context so the field stays meaningful.
    """
    emails, skipped_emails = _migratable_emails(value, config, allowlist)
    raw_phones = _classified_values(value, "phone", config)
    phones, skipped_phones = _split_allowlisted(raw_phones, allowlist or {})
    skipped = skipped_emails + skipped_phones
    if not (emails or phones):
        return value, [], [], skipped
    redacted = _redact_inline_tokens(value, emails + phones)
    # If nothing but the marker(s)/whitespace survives, the value WAS pure
    # contact data — drop it rather than leave a content-free marker behind.
    residual = redacted.replace(INLINE_REDACTION_MARKER, "").strip()
    if not residual:
        return None, emails, phones, skipped
    return redacted, emails, phones, skipped


def _migrate_value(
    value: Any,
    config: dict[str, Any] | None,
    allowlist: Mapping[str, str] | None = None,
) -> tuple[Any, list[str], list[str], list[PiiAllowlistEntry]]:
    """Recursively migrate one frontmatter value of arbitrary nesting depth.

    The athenaeum#502 sweep scanned only the TOP level of each frontmatter value: a
    string was detector-scanned and a list had its string entries scanned, but a
    value that was a *list of dicts* or a *nested dict* was copied through
    untouched — so contact data at ``sources[].claim`` or
    ``apollo_employment_history[].title`` was invisible to the migrator (issue
    athenaeum#507). This walks strings, lists AND dicts to arbitrary depth so every leaf
    is reached, while every sibling leaf is preserved byte-identical (the
    rewrite targets the exact leaf — e.g. ``sources[].claim`` — rather than
    replacing a whole structure).

    Returns ``(new_value, emails, phones, skipped)``. ``new_value is None``
    signals the caller to DROP this leaf — a list entry or dict key whose
    value was ENTIRELY contact data (a bare ``foo@bar.com``) — exactly the
    scalar contract in :func:`_migrate_str_value`; an emptied container is
    likewise dropped, mirroring the top-level "drop a key whose every entry
    was contact data" rule. Non-string, non-container scalars (int/bool/date)
    are returned unchanged. Nested dicts honour
    :data:`~athenaeum.pii.DURABLE_IDENTIFIER_FIELDS` too, so a durable
    identifier nested inside a structure is preserved verbatim. *allowlist*
    (issue athenaeum#1275) is threaded to every leaf unchanged — an adjudicated
    value anywhere in the structure is collected in ``skipped`` and left out
    of both ``emails``/``phones`` and the drop/redact decision.
    """
    if isinstance(value, str):
        return _migrate_str_value(value, config, allowlist)

    emails: list[str] = []
    phones: list[str] = []
    skipped: list[PiiAllowlistEntry] = []

    if isinstance(value, list):
        new_list: list[Any] = []
        for item in value:
            new_item, em, ph, sk = _migrate_value(item, config, allowlist)
            emails += em
            phones += ph
            skipped += sk
            if new_item is not None:
                new_list.append(new_item)
        return (new_list if new_list else None), emails, phones, skipped

    if isinstance(value, dict):
        new_dict: dict[Any, Any] = {}
        for key, item in value.items():
            if key in DURABLE_IDENTIFIER_FIELDS:
                new_dict[key] = item
                continue
            new_item, em, ph, sk = _migrate_value(item, config, allowlist)
            emails += em
            phones += ph
            skipped += sk
            if new_item is not None:
                new_dict[key] = new_item
        return (new_dict if new_dict else None), emails, phones, skipped

    return value, [], [], []  # non-string scalar (int/bool/date/None): keep verbatim


def _migrate_frontmatter(
    meta: dict[str, Any],
    config: dict[str, Any] | None,
    allowlist: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], list[str], list[str], list[PiiAllowlistEntry]]:
    """Rewrite frontmatter, extracting contact data from every non-durable field.

    Detector-driven (issue athenaeum#502): scans EVERY frontmatter value — not just
    ``emails:`` / ``phones:`` — so contact data in ``aliases:``,
    ``former_emails:``, ``source:`` etc. is migrated, while a newly-invented
    contact key cannot reopen the hole. Recurses into nested lists AND dicts to
    arbitrary depth (issue athenaeum#507), so an address buried at ``sources[].claim`` or
    ``apollo_employment_history[].title`` is reached — targeting the exact leaf
    and leaving every sibling structure byte-identical.
    :data:`~athenaeum.pii.DURABLE_IDENTIFIER_FIELDS` (identity + the
    name-is-an-email carve-out) are preserved verbatim at every level. List
    values keep their non-PII entries (a real alias survives even when a sibling
    entry was an email); scalar values keep their non-PII context; service
    identifiers (``git@github.com``, calendar group addresses) are left in place.

    Returns ``(new_meta, emails, phones, skipped)`` — the rewritten
    frontmatter dict (key order preserved), the deduped-later contact tokens
    pulled out of it, and any allowlisted values found but left in place
    (issue athenaeum#1275).
    """
    new_meta: dict[str, Any] = {}
    emails: list[str] = []
    phones: list[str] = []
    skipped: list[PiiAllowlistEntry] = []
    for key, value in meta.items():
        if key in DURABLE_IDENTIFIER_FIELDS:
            new_meta[key] = value
            continue
        new_value, em, ph, sk = _migrate_value(value, config, allowlist)
        emails += em
        phones += ph
        skipped += sk
        if new_value is not None:
            new_meta[key] = new_value
    return new_meta, emails, phones, skipped


def _build_excluded_record(
    meta: dict[str, Any],
    emails: list[str],
    phones: list[str],
) -> dict[str, Any]:
    """Build the archival contact-record frontmatter dict (pure, no I/O).

    Carries the durable identity linkage back to the origin entity (``uid`` /
    ``name``) plus the archival ``emails``/``phones``, and sets ``pii: true``
    (belt-and-suspenders: excluded even by the flag path, not only by
    placement). Split out from :func:`_render_excluded_record` (issue
    athenaeum#1108) so :func:`plan_pii_migration` can merge this dict against
    whatever record already exists on disk before rendering.
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
    return record


def _excluded_record_body(meta: dict[str, Any]) -> str:
    """Boilerplate body text for an excluded contact record. No data lives here."""
    origin = meta.get("name") or meta.get("uid") or "the entity page"
    return (
        f"Archival contact data migrated off entity page {origin!r} to the "
        "excluded surface (issues athenaeum#427/#437). This record is outside the "
        "corpus: not embedded, recalled, or merge-eligible. The origin page "
        "retains durable identifiers only.\n"
    )


def _render_excluded_record(
    meta: dict[str, Any],
    emails: list[str],
    phones: list[str],
) -> str:
    """Render a brand-new archival contact record for the excluded surface.

    Kept deliberately minimal — the excluded surface is outside the corpus, so
    this record is never embedded or recalled. Used only when NO record
    already exists at the target path; when one does, :func:`plan_pii_migration`
    merges into it via :func:`_merge_excluded_record` instead of calling this.
    """
    record = _build_excluded_record(meta, emails, phones)
    return render_frontmatter(record) + "\n" + _excluded_record_body(meta)


#: Scalar identity fields on an excluded contact record. A DIFFERING value
#: here between the record on disk and a fresh migration run is a genuine
#: disagreement about the entity's own identity (a corrected ``uid``, a
#: changed display ``name``) and must be surfaced, never silently resolved
#: by picking a winner (issue athenaeum#1108, AC1).
_EXCLUDED_SCALAR_IDENTITY_FIELDS = ("uid", "name", "contact_of")

#: List-valued fields on an excluded contact record. Plural by nature — an
#: entity can genuinely have more than one email/phone — so a NEW value found
#: here on re-migration is additive (a union with what is already on disk),
#: never a conflict.
_EXCLUDED_LIST_FIELDS = ("emails", "phones")


def _merge_excluded_record(
    existing_meta: dict[str, Any],
    new_record: dict[str, Any],
) -> tuple[dict[str, Any], list[ExcludedRecordConflict]]:
    """Merge a freshly-planned excluded record into the one already on disk.

    Pre-existing keys survive; ``emails``/``phones`` are unioned (order-
    preserving, deduped) rather than replaced — a second email discovered on
    re-migration is a NEW fact, not a disagreement. A differing SCALAR
    identity field (:data:`_EXCLUDED_SCALAR_IDENTITY_FIELDS`) is a genuine
    conflict: the existing on-disk value is kept (never silently overwritten)
    and reported via the returned conflict list — the caller (``_cmd_storage``)
    refuses to ``--apply`` a plan with any conflicts, so this function's
    keep-existing default never actually reaches disk unresolved.
    """
    merged: dict[str, Any] = dict(existing_meta)
    conflicts: list[ExcludedRecordConflict] = []

    for field in _EXCLUDED_SCALAR_IDENTITY_FIELDS:
        if field not in new_record:
            continue
        new_value = new_record[field]
        if field not in existing_meta:
            merged[field] = new_value
            continue
        existing_value = existing_meta[field]
        if existing_value != new_value:
            conflicts.append(ExcludedRecordConflict(field, existing_value, new_value))
        # else: identical — nothing to do, `merged` already carries it.

    for field in _EXCLUDED_LIST_FIELDS:
        new_values = new_record.get(field) or []
        existing_values = existing_meta.get(field) or []
        if not isinstance(existing_values, list):
            existing_values = [existing_values]
        combined = _dedupe_preserving_order([*existing_values, *new_values])
        if combined:
            merged[field] = combined

    merged[PII_FLAG] = True
    return merged, conflicts


def plan_pii_migration(
    page_path: Path,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    allowlist: Mapping[str, str] | None = None,
) -> PiiMigrationPlan:
    """Compute the migration for one entity page — pure, writes nothing.

    Extracts archival contact data from *page_path* (frontmatter
    ``emails``/``phones`` + inline email/phone tokens in the body), and returns
    the would-be excluded-surface record plus the rewritten origin page (those
    fields dropped, inline tokens redacted). When the page carries no contact
    data the plan's :attr:`~PiiMigrationPlan.changed` is False and both texts
    are ``None`` (a no-op the CLI reports rather than writing an empty record).

    *allowlist* (issue athenaeum#1275) is an optional ``{value: reason}`` mapping —
    a detected token matching a key EXACTLY is never migrated (not redacted,
    not dropped, not added to the excluded record) and is instead reported in
    :attr:`~PiiMigrationPlan.skipped_allowlisted`. This function does not
    resolve or read the allowlist file itself (that is the CLI's job, via
    :func:`athenaeum.pii.load_pii_allowlist` — the same reader ``lint-pii``
    uses); omitting *allowlist* preserves this function's pre-athenaeum#1275
    behaviour exactly.
    """
    text = page_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        meta = {}

    # Detector-driven frontmatter scan (athenaeum#502): pull contact tokens from EVERY
    # non-durable field, preserving durable identifiers and the name-is-an-email
    # carve-out. Then the body inline tokens.
    new_meta, fm_emails, fm_phones, fm_skipped = _migrate_frontmatter(meta, config, allowlist)
    # Body: same service-identifier exclusion as the frontmatter path (athenaeum#507) —
    # a `git@github.com` in prose is left byte-identical, not redacted.
    inline_emails, inline_skipped_emails = _migratable_emails(body, config, allowlist)
    raw_inline_phones = _classified_values(body, "phone", config)
    inline_phones, inline_skipped_phones = _split_allowlisted(raw_inline_phones, allowlist or {})

    emails = _dedupe_preserving_order(fm_emails + inline_emails)
    phones = _dedupe_preserving_order(fm_phones + inline_phones)
    skipped_allowlisted = _dedupe_allowlist_entries(
        fm_skipped + inline_skipped_emails + inline_skipped_phones
    )
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
            skipped_allowlisted=skipped_allowlisted,
        )

    # Rewrite origin: frontmatter with contact data stripped/redacted (durable
    # identifiers untouched, real aliases preserved), then the body inline
    # tokens redacted.
    new_body = _redact_inline_tokens(body, inline_emails + inline_phones)
    rewritten_page_text = render_frontmatter(new_meta) + "\n" + new_body

    # athenaeum#1108: when this page was migrated before, a record already sits at
    # excluded_page_path. MERGE into it rather than overwriting — the earlier
    # bug wrote only this run's values, silently dropping everything a prior
    # migration had archived. Pre-existing keys survive; emails/phones union;
    # a differing scalar identity field is surfaced as a conflict, never
    # silently picked (see _merge_excluded_record).
    new_record = _build_excluded_record(meta, emails, phones)
    # Only attempt a merge when 'pii' is actually mapped to a distinct
    # excluded surface. When it is NOT (is_pii_class_excluded is False),
    # surface_root_for_class falls back to the default wiki surface — so
    # excluded_page_path can equal page_path itself, and treating the ORIGIN
    # page's own frontmatter as if it were a prior excluded record would
    # manufacture a bogus conflict on every dry-run preview. --apply is
    # already refused in that configuration by the CLI's own safety gate
    # (_cmd_storage.py); dry-run simply previews a would-be-new record, same
    # as before this fix.
    excluded_record_existed = is_pii_class_excluded(config) and excluded_page_path.is_file()
    excluded_record_conflicts: tuple[ExcludedRecordConflict, ...] = ()
    if excluded_record_existed:
        existing_text = excluded_page_path.read_text(encoding="utf-8")
        existing_meta, _existing_body = parse_frontmatter(existing_text)
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        merged_record, conflicts = _merge_excluded_record(existing_meta, new_record)
        excluded_record_conflicts = tuple(conflicts)
        excluded_page_text = render_frontmatter(merged_record) + "\n" + _excluded_record_body(meta)
    else:
        excluded_page_text = render_frontmatter(new_record) + "\n" + _excluded_record_body(meta)

    return PiiMigrationPlan(
        page_path=page_path,
        excluded_page_path=excluded_page_path,
        emails=emails,
        phones=phones,
        rewritten_page_text=rewritten_page_text,
        excluded_page_text=excluded_page_text,
        name_field_pii=name_field_pii,
        excluded_record_existed=excluded_record_existed,
        excluded_record_conflicts=excluded_record_conflicts,
        skipped_allowlisted=skipped_allowlisted,
    )


# ---------------------------------------------------------------------------
# Name-is-an-email rename migration (issue athenaeum#505 — the athenaeum#502 carve-out's slice)
# ---------------------------------------------------------------------------
#
# athenaeum#502 preserves ``name:``/``preferred_name:`` verbatim even when it is an
# email address (:data:`~athenaeum.pii.DURABLE_IDENTIFIER_FIELDS`) — renaming
# a page changes its slug and breaks inbound ``[[wikilink]]``/``aliases:``
# resolution, so that population was EXCLUDED from the automatic path and left
# for this dedicated slice. APPROACH 1 (operator decision, athenaeum#505): derive a
# display name from the local-part with a confidence gate
# (:func:`~athenaeum.pii.derive_display_name_from_email`), rename the page to
# that name (new slug/filename), move the address to the excluded contact
# record (the same surface :func:`plan_pii_migration` writes to), and rewrite
# every inbound edge that pointed at the OLD slug so nothing dangles.
#
# "Inbound edge" in this corpus is two things, both already-shipped machinery
# in :mod:`athenaeum.pending_merges` (the ``fold-into-existing`` merge-write
# path rewrites both when a page is folded into another under a new
# canonical slug — the exact same slug-rename + edge-preservation shape this
# migration needs, just triggered by a different reason):
#
# 1. ``[[old-slug]]`` wikilinks anywhere under ``wiki/`` — rewritten via
#    :func:`athenaeum.pending_merges._rewrite_inbound_wikilinks`.
# 2. Alias resolution — the OLD slug (and the raw email local-part, so a
#    literal ``[[jane.doe]]`` or the historical ``[[jdoe]]``-shaped link some
#    other page might carry) is recorded in the renamed page's own
#    ``aliases:`` frontmatter via
#    :func:`athenaeum.pending_merges._add_aliases_to_frontmatter`, so
#    :func:`athenaeum.pending_merges.resolve_alias_slug` continues to resolve
#    it after the rename.
#
# Reusing this machinery (rather than re-implementing slug-rewrite) keeps
# there being exactly one definition of "how a wiki-tree rename propagates".


@dataclass(frozen=True)
class NameEmailRenamePlan:
    """The would-be result of renaming one name-is-an-email entity page.

    ``confident`` is False for the DEFERRED case (issue athenaeum#505's REQUIRED
    FALLBACK): an ambiguous local-part (role address, ``+tag``, initial-blob,
    opaque/numeric) is never guessed at — the page is left exactly as-is and
    this plan carries no rewrite, only the reason it was deferred.
    """

    page_path: Path
    email: str
    #: True when a confident display name was derived (approach 1 applies).
    #: False => DEFER; every field below is meaningless/empty on a deferred
    #: plan except :attr:`deferred_reason`.
    confident: bool
    #: The derived display name (e.g. ``"Jane Doe"``). ``""`` when deferred.
    display_name: str = ""
    #: New slug (``models.slugify(display_name)``). ``""`` when deferred.
    new_slug: str = ""
    #: New on-disk filename for the renamed page. ``""`` when deferred.
    new_filename: str = ""
    #: New path the page would be renamed to. ``None`` when deferred.
    new_page_path: Path | None = None
    #: Where the archival contact record would be written. ``None`` when
    #: deferred (nothing is migrated for a deferred page).
    excluded_page_path: Path | None = None
    #: Rewritten page text (new ``name:``, email dropped, old slug/local-part
    #: added to ``aliases:``). ``None`` when deferred.
    rewritten_page_text: str | None = None
    #: The archival contact-record text for the excluded surface. ``None``
    #: when deferred.
    excluded_page_text: str | None = None
    #: Human-readable reason the page was deferred (e.g. "role address",
    #: "numeric/opaque local-part"). ``""`` when confident.
    deferred_reason: str = ""


def _name_email_deferred_reason(email: str) -> str:
    """Best-effort human-readable reason :func:`derive_display_name_from_email`
    declined *email* — purely for operator-facing reporting, not re-derivation.
    """
    from athenaeum.pii import ROLE_LOCALPARTS

    if "@" not in email:
        return "malformed address"
    local = email.split("@", 1)[0].strip()
    if not local:
        return "malformed address"
    if "+" in local:
        return "+tag address"
    if local.lower() in ROLE_LOCALPARTS:
        return "role/service address"
    if any(ch.isdigit() for ch in local):
        return "numeric or opaque local-part"
    return "ambiguous local-part (initial-blob or too short to name confidently)"


def plan_name_email_rename(
    page_path: Path,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    *,
    display_name_override: str | None = None,
) -> NameEmailRenamePlan:
    """Compute the rename migration for one name-is-an-email page — pure.

    Reads *page_path*, and when its ``name:``/``preferred_name:`` is a
    confidently-nameable email (per
    :func:`~athenaeum.pii.derive_display_name_from_email`), returns a plan to:

    - rename the page (``name:`` becomes the derived display name; the old
      slug and the raw local-part are recorded in ``aliases:`` so old
      wikilinks/alias-resolution keep working post-rename);
    - move the address to an excluded contact record (mirrors
      :func:`_render_excluded_record`'s shape);
    - leave the origin page's OTHER frontmatter and body untouched.

    When the name is NOT confidently nameable (role address, ``+tag``,
    initial-blob, numeric/opaque local-part), returns a plan with
    ``confident=False`` and writes nothing — the REQUIRED FALLBACK (issue
    athenaeum#505): never guess a name.

    Only the FIRST of ``name:``/``preferred_name:`` that is email-shaped is
    used as the rename source, matching :data:`~athenaeum.pii.NAME_FIELDS`
    order — a page is renamed once, not twice.
    """
    from athenaeum.models import slugify
    from athenaeum.pending_merges import _add_aliases_to_frontmatter
    from athenaeum.pii import NAME_FIELDS, derive_display_name_from_email

    text = page_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        meta = {}

    email: str | None = None
    name_field: str | None = None
    for field in NAME_FIELDS:
        raw = meta.get(field)
        if raw is None:
            continue
        candidate = str(raw)
        hits = _classified_values(candidate, "email", config)
        if hits:
            email = hits[0]
            name_field = field
            break

    if email is None or name_field is None:
        # Not a name-is-an-email page at all — nothing to plan.
        return NameEmailRenamePlan(page_path=page_path, email="", confident=False)

    # athenaeum#745: an OPERATOR-SUPPLIED name is the missing half of athenaeum#505's
    # fallback. athenaeum#505 correctly refuses to guess a display name from an
    # ambiguous local-part, but it offered no way to *provide* one — so the
    # deferred population had no route through the tool at all and could only
    # be hand-edited, which skips the excluded record, the slug rename and the
    # inbound-link rewrite. An override is a human asserting the name, not the
    # tool inferring it, so it bypasses the confidence gate by design.
    display_name = (display_name_override or "").strip() or derive_display_name_from_email(email)
    if display_name is None:
        return NameEmailRenamePlan(
            page_path=page_path,
            email=email,
            confident=False,
            deferred_reason=_name_email_deferred_reason(email),
        )

    new_slug = slugify(display_name)
    new_filename = f"{new_slug}.md"
    new_page_path = page_path.with_name(new_filename)

    old_slug = slugify(page_path.stem)
    local_part = email.split("@", 1)[0]

    new_meta = dict(meta)
    new_meta[name_field] = display_name
    # Record the old slug + raw local-part as aliases so a `[[old-slug]]`
    # wikilink or an `aliases:`-resolution lookup for the historical
    # local-part keeps resolving post-rename (mirrors the fold-into-existing
    # merge write path's alias bookkeeping via the same helper).
    new_meta = _add_aliases_to_frontmatter(new_meta, [old_slug, local_part])

    rewritten_page_text = render_frontmatter(new_meta) + "\n" + body

    excluded_root = surface_root_for_class(PII_ENTITY_CLASS, config, knowledge_root)
    excluded_page_path = excluded_root / new_filename
    excluded_page_text = _render_excluded_record(
        {"uid": meta.get("uid"), "name": display_name}, [email], []
    )

    return NameEmailRenamePlan(
        page_path=page_path,
        email=email,
        confident=True,
        display_name=display_name,
        new_slug=new_slug,
        new_filename=new_filename,
        new_page_path=new_page_path,
        excluded_page_path=excluded_page_path,
        rewritten_page_text=rewritten_page_text,
        excluded_page_text=excluded_page_text,
    )


@dataclass
class NameEmailRenameReport:
    """Result of applying (or dry-running) the bulk name-is-an-email rename.

    ``residual`` is the REQUIRED count of pages deliberately deferred (issue
    athenaeum#505's fallback) — reported so the population never silently vanishes,
    mirroring how :func:`plan_pii_migration`'s ``name_field_pii`` flag lets the
    bulk PII driver surface this same population today.
    """

    scanned: int = 0
    renamed: int = 0
    residual: int = 0
    links_rewritten: int = 0
    renames: list[tuple[str, str]] = None  # type: ignore[assignment]
    deferred: list[tuple[Path, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.renames is None:
            self.renames = []
        if self.deferred is None:
            self.deferred = []


def apply_name_email_rename(
    plan: NameEmailRenamePlan,
    wiki_root: Path,
) -> int:
    """Write one CONFIDENT rename plan: excluded record, rename, rewrite edges.

    Order mirrors :func:`_apply_plan`'s crash-safety discipline (archival copy
    lands before anything origin-side changes):

    1. Write the excluded contact record.
    2. Write the rewritten page text to the NEW path (new slug/filename).
    3. Remove the OLD path (the rename is a write-then-delete, not an
       ``os.replace``, so a crash between steps 2 and 3 leaves BOTH the new
       page and the stale old one on disk rather than losing content — the
       stale old copy is simply a duplicate a re-run's rewrite would rewrite
       again, or an operator can delete by hand; nothing is silently lost).
    4. Rewrite every inbound ``[[old-slug]]`` wikilink under *wiki_root* to
       the new slug (:func:`athenaeum.pending_merges._rewrite_inbound_wikilinks`
       — reused, not reimplemented).

    Returns the number of OTHER files whose inbound wikilinks were rewritten
    (mirrors ``links_rewritten`` in the fold-into-existing merge write path).
    Raises if *plan* is not confident — callers must check
    :attr:`NameEmailRenamePlan.confident` first (the CLI/bulk driver never
    calls this on a deferred plan).
    """
    from athenaeum.atomic_io import atomic_write_text
    from athenaeum.models import slugify
    from athenaeum.pending_merges import _rewrite_inbound_wikilinks

    if not plan.confident or plan.new_page_path is None:
        raise ValueError(f"cannot apply a deferred rename plan: {plan.page_path}")

    assert plan.excluded_page_path is not None
    plan.excluded_page_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(plan.excluded_page_path, plan.excluded_page_text or "")

    atomic_write_text(plan.new_page_path, plan.rewritten_page_text or "")
    if plan.new_page_path != plan.page_path and plan.page_path.is_file():
        plan.page_path.unlink()

    old_slug = slugify(plan.page_path.stem)
    return _rewrite_inbound_wikilinks(
        wiki_root, [old_slug], plan.new_slug, skip=plan.new_page_path
    )


def bulk_rename_name_email_pages(
    wiki_root: Path,
    config: dict[str, Any] | None,
    knowledge_root: Path,
    *,
    apply: bool = False,
    pages: Iterable[Path] | None = None,
    display_name_override: str | None = None,
) -> NameEmailRenameReport:
    """Drive :func:`plan_name_email_rename` over every entity page (issue athenaeum#505).

    Idempotent/resumable the same way :func:`plan_pii_migration`'s bulk driver
    is: a renamed page's ``name:`` is no longer email-shaped, so
    :func:`plan_name_email_rename` returns an unplanned (non-name-is-email)
    result for it on a re-run — already-renamed pages are simply skipped, not
    double-processed. A deferred page is likewise re-evaluated (and re-deferred)
    on every run rather than remembered in a side ledger — the frontmatter
    itself is the checkpoint.

    ``pages`` scopes the driver to an explicit target set (issue athenaeum#745).
    It defaults to every entity page, which is the corpus-wide behaviour this
    function has always had. Passing a narrower set lets a caller rename ONE
    page, or a ``--glob`` selection, without also running the body-text
    migration over the whole corpus — which previously made the rename slice
    reachable only through ``--all``, and therefore only at the price of
    accepting every body migration ``--all`` would perform.
    """
    report = NameEmailRenameReport()
    for page_path in iter_entity_pages(wiki_root) if pages is None else pages:
        report.scanned += 1
        try:
            plan = plan_name_email_rename(
                page_path,
                config,
                knowledge_root,
                display_name_override=display_name_override,
            )
        except (OSError, UnicodeDecodeError):
            continue
        if not plan.email:
            continue  # not a name-is-an-email page at all
        if not plan.confident:
            report.residual += 1
            report.deferred.append((page_path, plan.deferred_reason))
            continue
        report.renamed += 1
        report.renames.append((page_path.stem, plan.new_slug))
        if apply:
            report.links_rewritten += apply_name_email_rename(plan, wiki_root)
    return report


# ---------------------------------------------------------------------------
# Bulk target-set resolution (issue athenaeum#495)
# ---------------------------------------------------------------------------
#
# athenaeum#479 shipped the single-page path (``--page``); the live corpus needs the
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
