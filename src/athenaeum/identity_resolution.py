# SPDX-License-Identifier: Apache-2.0
"""Handle-shaped identity resolution for ``recall`` (issue athenaeum#907).

Gives ``recall`` an exact reverse lookup for a handle-shaped query ("who is
this address?", "is this address still current?") instead of routing it
through similarity search. The response is a structured, JSON-parseable set
of FACTS — the person ``uid``, display name, entity class, and the relevant
per-value fact fields (usage/provenance classification, bounce history,
validity dates).

**Explicit boundary (operator ruling, 2026-08-14, restated from the issue):**
this module returns FACTS, never a permission or action predicate. It must
never surface eligibility, "may contact", or anything resembling
``outreach_eligible`` — that decision belongs to the caller's own policy.
Access control is a separate, deferred question (athenaeum#864); this module
does not implement it, and the audience/``recallable`` checks it performs are
the SAME read-gates ``recall`` already applies to every hit, not a new
authorization layer.

**Detection is deliberately conservative** (:func:`resolve_handle_query`
returns ``None`` — "not handle-shaped" — for anything that does not
unambiguously look like a handle). A query is handle-shaped iff:

(a) it contains EXACTLY ONE email-shaped token (:data:`_EMAIL_SHAPE_RE`,
    a minimal local shape check used only to detect the token; the matched
    substring is normalized through :func:`athenaeum.pii.normalize_identifier`
    — the repo's existing identifier comparison — before any lookup), or
(b) the whole trimmed query, with a small closed list of leading/trailing
    interrogative framing removed (:data:`INTERROGATIVE_FRAMING`), EXACTLY
    matches an existing ``registry.json`` handle value. This is why (b) can
    never resolve to a "no-match" disposition: the match is what makes it
    handle-shaped in the first place. A query that merely *looks* like a bare
    token but matches nothing in the registry is not handle-shaped and falls
    through to similarity search unchanged.

Neither branch ever fires for an ordinary keyword/semantic query — the hard
requirement (per the issue) is that a non-handle query produces byte-identical
output to what ``recall`` returned before this module existed.

**Resolution walk** mirrors :func:`athenaeum.corrections._resolve_email_handle`
(NOT imported — that function is scoped to correction-target resolution and
returns a bare :class:`~pathlib.Path`, not a fact record):

- an email/address handle resolves via
  :meth:`athenaeum.pii.ExcludedRecordIndex.all_by_identifier` — the
  ambiguity-PRESERVING accessor. Never ``by_identifier`` (first-match-wins),
  which would silently guess which of several people an address belongs to.
- a registry handle resolves via :func:`athenaeum.corrections.load_registry`
  + :func:`athenaeum.corrections._handle_matches` (both imported — they are
  the general registry primitives, not correction-specific).

Either walk lands on zero, one, or several candidate uids, and the closed
disposition vocabulary (:data:`RESOLUTION_REASONS`) mirrors
``EmailHandleResolution``'s: ``no-match``, ``record-without-uid``,
``ambiguous``, ``orphan-uid`` — plus the audience/``recallable`` fail-closed
drop, which also reports ``no-match`` (never a distinct reason, since from the
caller's perspective an unauthorized or non-recallable page must look
identical to one that never existed).

**``with_pii`` gating** reuses the athenaeum#885 layer ordering exactly: the
audience check, then the athenaeum#532 ``recallable`` check, both BEFORE any
excluded-surface lookup — either drop triggers ZERO excluded scans.
Only once a page survives both does this module resolve its excluded record
and assemble facts via the athenaeum#883 seam
(:func:`athenaeum.pii.assemble_excluded_read`) — never ``read_entity``,
never a fresh :class:`~athenaeum.models.EntityIndex` beyond
the one already needed to locate the page. The response SHAPE for that
assembly follows the :class:`~athenaeum.pii.EntityRead` /
:class:`~athenaeum.pii.RedactionMarker` precedent exactly: ``with_pii=True``
populates ``contact_values`` (real values plus their facts) and leaves
``redactions`` empty; ``with_pii=False`` populates ``redactions`` (naming each
withheld field and how many values exist — "withheld ≠ absent", the rule
athenaeum#885 established) and leaves ``contact_values`` empty.

Both ``recall`` entry points (:func:`athenaeum.mcp_server.recall_search` and
:func:`athenaeum._cmd_query.cmd_recall`) call :func:`resolve_handle_query`
once each — this module holds the one resolution implementation; neither
caller re-derives it.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only, mirroring the lazy-import convention every module in
    # this read path already follows (`mcp_server.py`, `_cmd_query.py`) — the
    # real import stays local to each function that needs it.
    from athenaeum.models import EntityIndex
    from athenaeum.person_registry import PersonRegistry, PersonRegistryEntry
    from athenaeum.pii import (
        DoNotEmailState,
        ExcludedRecordIndex,
        IdentifierValidity,
        RedactionMarker,
    )

#: Minimal LOCAL email-shape check used only to DETECT a handle-shaped query
#: (D2(a)). Never used for comparison/lookup — the matched substring is
#: normalized through :func:`athenaeum.pii.normalize_identifier` (the repo's
#: existing identifier comparison) before any resolution happens.
_EMAIL_SHAPE_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: Closed list of leading/trailing interrogative framing stripped before
#: comparing a query against a registry handle value (D2(b)). A NAMED
#: constant rather than scattered literals, so the vocabulary has one place
#: to review or extend. Longest-first within each list: `"still current"`
#: must be tried before `"current"` (a suffix of it) or the shorter match
#: would fire first and leave `"still"` dangling.
INTERROGATIVE_FRAMING: dict[str, tuple[str, ...]] = {
    "prefixes": ("whose is", "who owns", "who is", "is"),
    "suffixes": ("still current", "current", "valid", "?"),
}

#: The closed disposition-reason vocabulary for an UNRESOLVED handle (D4),
#: mirroring `EmailHandleResolution`'s reasons minus the `email-handle-`
#: prefix (this module resolves more than one handle shape, so the prefix
#: would be misleading for a registry handle). `resolved` itself is not a
#: member — it is the SUCCESS disposition, carried on `HandleResolution` as
#: `resolved=True, reason=None`, never as a fourth string alongside these.
RESOLUTION_REASONS: tuple[str, ...] = (
    "no-match",
    "record-without-uid",
    "ambiguous",
    "orphan-uid",
)


def _strip_interrogative_framing(query: str) -> str:
    """Strip at most one leading and one trailing framing phrase, longest-first.

    Never raises and never strips more than the closed list in
    :data:`INTERROGATIVE_FRAMING` — an ordinary query with no framing at all
    returns unchanged (modulo surrounding whitespace).
    """
    text = query.strip()
    lowered = text.lower()
    for prefix in INTERROGATIVE_FRAMING["prefixes"]:
        candidate = f"{prefix} "
        if lowered.startswith(candidate):
            text = text[len(candidate) :].strip()
            break
    lowered = text.lower()
    if lowered.endswith("?"):
        text = text[:-1].strip()
        lowered = text.lower()
    for suffix in INTERROGATIVE_FRAMING["suffixes"]:
        if suffix == "?":
            continue  # handled unconditionally above
        candidate = f" {suffix}"
        if lowered.endswith(candidate):
            text = text[: -len(candidate)].strip()
            break
    return text.strip()


def _detect_email_handle(query: str) -> str | None:
    """The lone email-shaped token in *query*, or ``None`` (D2(a)).

    Deliberately conservative: ``None`` unless EXACTLY one token in the whole
    query looks like an email address — a query naming two addresses is not
    a reverse lookup for either of them.
    """
    matches = _EMAIL_SHAPE_RE.findall(query)
    if len(matches) != 1:
        return None
    return matches[0]


def carries_email_shape(text: str) -> bool:
    """True iff *text* contains at least one email-shaped token (athenaeum#1126).

    A thin public wrapper over the same conservative, LOCAL shape check
    (:data:`_EMAIL_SHAPE_RE`) :func:`_detect_email_handle` already uses — the
    email-shape regex stays owned by this one module rather than being
    duplicated elsewhere (e.g. :mod:`athenaeum.tiers`, which uses this
    function as its tier-2 fast-path gate). Like the module-private detector,
    this is DETECTION only, never comparison/lookup: the substring that makes
    this ``True`` is normalized through :func:`athenaeum.pii.normalize_identifier`
    downstream, before any resolution happens — see the module docstring.
    """
    return _EMAIL_SHAPE_RE.search(text) is not None


def sole_email_token(text: str) -> str | None:
    """The lone email-shaped token in *text*, or ``None`` (athenaeum#1126).

    Public alias for :func:`_detect_email_handle`, which already implements
    exactly this: conservative by design, returning ``None`` when *text*
    carries zero OR two-or-more email-shaped tokens (a name naming two
    addresses is not a reverse lookup for either of them). DETECTION only —
    see :func:`carries_email_shape` and the module docstring for why the
    matched substring is never used for comparison/lookup directly.
    """
    return _detect_email_handle(text)


def _registry_handle_matches(registry_entities: dict[str, Any], value: str) -> list[str]:
    """Every uid whose registry handles (any :data:`SOURCE_HANDLE_KEYS` field) carry *value*.

    Scans every source-handle key rather than one, since a handle-shaped
    query (D2(b)) is not told in advance which key it might be — a domain, a
    Slack channel, a LinkedIn URL. Uses
    :func:`athenaeum.corrections._handle_matches`, the SAME exact-match
    primitive :func:`athenaeum.corrections.resolve_target` uses, so this
    resolves identically to what a correction targeting the same handle
    would resolve to. Order is stable (key order in
    :data:`athenaeum.registry.SOURCE_HANDLE_KEYS`, then registry insertion
    order) and de-duplicated.
    """
    from athenaeum import corrections
    from athenaeum.registry import SOURCE_HANDLE_KEYS

    matches: list[str] = []
    for key in SOURCE_HANDLE_KEYS:
        for uid in corrections._handle_matches(registry_entities, key, value):
            if uid not in matches:
                matches.append(uid)
    return matches


@dataclass(frozen=True)
class _Walk:
    """Internal outcome of the identity-resolution walk (D3), pre-audience-check.

    Never returned to a caller of this module — :func:`resolve_handle_query`
    always converts this into a :class:`HandleResolution`.
    """

    kind: str  # "resolved" | "unresolvable"
    uid: str | None = None
    reason: str | None = None
    candidate_uids: tuple[str, ...] = ()


def _walk_email_handle(
    knowledge_root: Path,
    value: str,
    *,
    config: dict[str, Any] | None,
    excluded_index: ExcludedRecordIndex | None = None,
) -> _Walk:
    """``email -> contact record(s) -> uid(s)`` (D3), mirroring `_resolve_email_handle`.

    Deduped by uid, not by record: several records carrying the SAME uid are
    one person described twice, not an ambiguous address.

    *excluded_index* (athenaeum#1124): an already-built
    :class:`~athenaeum.pii.ExcludedRecordIndex` over the ``pii`` contacts
    surface (the same root :func:`athenaeum.pii.contacts_surface_root`
    resolves), for a caller resolving many handles in one process. ``None``
    (the default) builds one here exactly as before — byte-identical to the
    pre-athenaeum#1124 behaviour.
    """
    from athenaeum import pii

    if excluded_index is not None:
        index = excluded_index
    else:
        contacts_root = pii.contacts_surface_root(knowledge_root, config)
        index = pii.ExcludedRecordIndex(contacts_root)
    records = index.all_by_identifier(value)
    if not records:
        return _Walk(kind="unresolvable", reason="no-match")

    uids: list[str] = []
    for record in records:
        uid = pii.uid_on_record(record)
        if uid is not None and uid not in uids:
            uids.append(uid)

    if not uids:
        return _Walk(kind="unresolvable", reason="record-without-uid")
    if len(uids) > 1:
        return _Walk(kind="unresolvable", reason="ambiguous", candidate_uids=tuple(sorted(uids)))
    return _Walk(kind="resolved", uid=uids[0])


def _walk_registry_handle(uids: list[str]) -> _Walk:
    """Finish resolving a non-empty registry-handle match set (D3).

    *uids* is never empty here — an empty match set means the query was never
    handle-shaped in the first place (see :func:`resolve_handle_query`), so a
    "no-match" disposition never originates from this function.
    """
    if len(uids) > 1:
        return _Walk(kind="unresolvable", reason="ambiguous", candidate_uids=tuple(sorted(uids)))
    return _Walk(kind="resolved", uid=uids[0])


def _validity_bounds_for_value(
    record_meta: dict[str, Any] | None, value: str
) -> tuple[str | None, str | None]:
    """The ``(valid_from, valid_until)`` bounds for one value on *record_meta*.

    Reads the same two shapes :func:`athenaeum.pii.is_bounced_identifier`
    reads — a per-identifier entry in ``identifier_validity`` first, else the
    record's own top-level bounds when the record's ``identifier:`` IS the
    value asked about. Rendered through
    :func:`athenaeum.models.validity_bound_str` (the same renderer recall's
    ``**Valid:**`` line uses) so a bound compares identically wherever it is
    read. ``None`` (never ``""``) for an unset bound — a clear, parseable
    "no bound recorded" rather than an empty string a consumer must special-case.
    """
    from athenaeum import pii
    from athenaeum.models import validity_bound_str

    wanted = pii.normalize_identifier(value)
    for entry in pii.identifier_validity_entries(record_meta):
        if pii.normalize_identifier(str(entry.get("identifier", ""))) == wanted:
            return (
                validity_bound_str(entry, "valid_from") or None,
                validity_bound_str(entry, "valid_until") or None,
            )
    if (
        isinstance(record_meta, dict)
        and pii.normalize_identifier(str(record_meta.get("identifier", ""))) == wanted
    ):
        return (
            validity_bound_str(record_meta, "valid_from") or None,
            validity_bound_str(record_meta, "valid_until") or None,
        )
    return (None, None)


@dataclass(frozen=True)
class ContactValueFact:
    """One excluded value's facts — usage/provenance, bounce, validity (AC3).

    Built field-by-field, never via
    :meth:`athenaeum.pii.ContactClassification.to_dict` — that method includes
    ``outreach_eligible`` (AC5's hard "no action predicate" constraint forbids
    it appearing anywhere in this module's output).

    ``do_not_email`` and ``validity`` (issue athenaeum#961) close the field
    gap between this path and ``recall``'s similarity-search excluded-facts
    block: both are the raw, unreduced :meth:`athenaeum.pii.DoNotEmailState.to_dict`
    / :meth:`athenaeum.pii.IdentifierValidity.to_dict` shapes — the same
    objects :func:`athenaeum.mcp_server._excluded_block_for_hit` embeds via
    ``do_not_email.to_dict()`` / ``field_validity[position].to_dict()`` — so
    the two ``recall`` paths carry the identical vocabulary rather than a
    hand-picked subset that could drift from it. ``do_not_email`` is a
    per-RECORD fact (the mark is not scoped to one value), so every
    ``ContactValueFact`` for the same record carries the same
    ``do_not_email``. The existing ``valid_from``/``valid_until`` flat bounds
    are UNCHANGED — ``validity`` is additive, not a replacement.
    """

    identifier: str
    usage_class: str
    source: str | dict[str, Any] | None
    observed_at: str | None
    bounced: bool
    valid_from: str | None
    valid_until: str | None
    do_not_email: DoNotEmailState
    validity: IdentifierValidity

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape. No ``outreach_eligible`` key — see the class docstring."""
        return {
            "identifier": self.identifier,
            "usage_class": self.usage_class,
            "source": self.source,
            "observed_at": self.observed_at,
            "bounced": self.bounced,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "do_not_email": self.do_not_email.to_dict(),
            "validity": self.validity.to_dict(),
        }


def _assemble_contact_values(
    knowledge_root: Path,
    page_path: Path,
    page_fm: dict[str, Any],
    uid: str,
    page_type: str,
    *,
    config: dict[str, Any] | None,
    with_pii: bool,
    usage_classes: Collection[str] | None,
    excluded_index: ExcludedRecordIndex | None = None,
) -> tuple[tuple[ContactValueFact, ...], tuple[RedactionMarker, ...]]:
    """``(contact_values, redactions)`` for a page that survived both Layer-C drops.

    Reuses the athenaeum#883/#885 seam exactly (D6): the page class maps to a
    SURFACE class (:func:`athenaeum.pii.surface_class_for_page_class`), gated
    by :func:`athenaeum.storage.is_excluded` so a class whose surface is the
    ordinary wiki adapter is never scanned as though it were the excluded
    store, then :func:`athenaeum.pii.assemble_excluded_read` — never
    ``read_entity``/``read_entities``, never a second
    :class:`~athenaeum.models.EntityIndex`.

    The EntityRead/RedactionMarker precedent (D5) is a strict either/or:
    ``with_pii=True`` returns real values (plus their bounce/validity facts)
    in ``contact_values`` and an empty ``redactions``; ``with_pii=False``
    returns an empty ``contact_values`` and the field-level redaction markers
    — never both, and never neither when the record holds values.

    *usage_classes* (athenaeum#907 follow-up) restricts which classes'
    values ``assemble_excluded_read`` returns — it filters WITHIN the join,
    the same way it already does for the similarity-search path
    (:func:`athenaeum.mcp_server._excluded_block_for_hit`). It cannot widen
    anything and never perturbs the ``with_pii=False`` redaction-marker path,
    since that branch returns before *usage_classes* would apply.

    *excluded_index* (athenaeum#1124): an already-built
    :class:`~athenaeum.pii.ExcludedRecordIndex`, reused ONLY when its
    ``contacts_root`` matches *surface_root* below — a page class whose
    mapped surface differs from the caller's prepared surface (e.g. a
    non-``person`` class routed to its own adapter) still gets a freshly
    built index for its own surface rather than being silently joined
    against the wrong one. ``None`` (the default) builds one here exactly
    as before.
    """
    from athenaeum import pii
    from athenaeum.storage import is_excluded

    surface_class = pii.surface_class_for_page_class(page_type, config)
    if not surface_class or not is_excluded(surface_class, config):
        return (), ()

    surface_root = pii.excluded_surface_root(surface_class, knowledge_root, config)
    if excluded_index is not None and excluded_index.contacts_root == surface_root:
        index = excluded_index
    else:
        index = pii.ExcludedRecordIndex(surface_root)
    record_path = index.by_uid(uid)
    record_meta = pii.read_bounce_record(record_path) if record_path is not None else None

    fields, redactions, classifications = pii.assemble_excluded_read(
        page_path,
        page_fm,
        record_meta,
        surface_class=surface_class,
        config=config,
        include_excluded=with_pii,
        usage_classes=usage_classes,
    )

    if not with_pii:
        return (), redactions

    # Issue athenaeum#961: both facts below come from the shared `pii` state
    # helpers — never re-derived from `record_meta`/`page_fm` here — so this
    # module never grows a second, drift-prone reading of either surface.
    # `do_not_email` is a per-record mark (issue athenaeum#960 converged the
    # wiki-page and excluded-record surfaces into this one call), computed
    # once per record rather than once per value.
    do_not_email = pii.do_not_email_state(record_meta, page_fm)

    values: list[ContactValueFact] = []
    for field_name, field_values in fields.items():
        classified = classifications.get(field_name, [])
        for value, classification in zip(field_values, classified, strict=True):
            valid_from, valid_until = _validity_bounds_for_value(record_meta, value)
            values.append(
                ContactValueFact(
                    identifier=value,
                    usage_class=classification.usage_class,
                    source=classification.source,
                    observed_at=classification.observed_at,
                    bounced=pii.is_bounced_identifier(record_meta, value),
                    valid_from=valid_from,
                    valid_until=valid_until,
                    do_not_email=do_not_email,
                    validity=pii.validity_for_value(record_meta, value),
                )
            )
    return tuple(values), ()


@dataclass(frozen=True)
class HandleResolution:
    """The full answer to a handle-shaped query (D5) — facts, never a predicate.

    Rendered by the caller as
    ``json.dumps(result.to_dict(), indent=2, sort_keys=True)`` and nothing
    else — no prose wrapper, no leading header line (AC6: parseable without
    natural-language interpretation).
    """

    resolved: bool
    reason: str | None
    handle: str
    with_pii: bool
    uid: str | None = None
    display_name: str | None = None
    entity_class: str | None = None
    candidate_uids: tuple[str, ...] = ()
    contact_values: tuple[ContactValueFact, ...] = ()
    redactions: tuple[RedactionMarker, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape. Every key here is a fact field — see the module docstring."""
        return {
            "resolved": self.resolved,
            "reason": self.reason,
            "handle": self.handle,
            "uid": self.uid,
            "display_name": self.display_name,
            "entity_class": self.entity_class,
            "candidate_uids": list(self.candidate_uids),
            "contact_values": [value.to_dict() for value in self.contact_values],
            "redactions": [marker.to_dict() for marker in self.redactions],
            "with_pii": self.with_pii,
        }


def _unresolved(handle: str, walk: _Walk, *, with_pii: bool) -> HandleResolution:
    return HandleResolution(
        resolved=False,
        reason=walk.reason,
        handle=handle,
        with_pii=with_pii,
        candidate_uids=walk.candidate_uids,
    )


def _orphan_uid(handle: str, *, with_pii: bool) -> HandleResolution:
    """The uid resolved, but its wiki page is missing or unreadable (D4)."""
    return _unresolved(handle, _Walk(kind="unresolvable", reason="orphan-uid"), with_pii=with_pii)


def _finish(
    knowledge_root: Path,
    wiki_root: Path,
    handle: str,
    walk: _Walk,
    *,
    caller_audience: set[str] | None,
    config: dict[str, Any] | None,
    with_pii: bool,
    usage_classes: Collection[str] | None,
    excluded_index: ExcludedRecordIndex | None = None,
    entity_index: EntityIndex | None = None,
) -> HandleResolution:
    """Turn a resolved/unresolved walk outcome into the full response (D5/D6).

    For a resolved walk: locates the page, applies the audience filter (D6
    step 2) then the athenaeum#532 ``recallable`` filter (D6 step 3) — either
    drop reports ``reason="no-match"``, fail-closed, and performs NO
    excluded-surface lookup at all. Only a page that survives both reaches
    :func:`_assemble_contact_values` (D6 step 4).

    **Gating is structurally unaffected by *entity_index*/*excluded_index***
    (athenaeum#1124): both prepared indexes are used ONLY to locate a page
    and, later, an excluded record — never to decide whether the page is
    returned. Steps 2 and 3 below read `page_fm` (freshly parsed from disk
    either way) and run unconditionally, strictly BEFORE
    :func:`_assemble_contact_values` is ever reached — a prepared index
    changes what a lookup costs, never what it is permitted to see.
    """
    from athenaeum.models import EntityIndex as _EntityIndex
    from athenaeum.models import is_page_authorized, parse_frontmatter
    from athenaeum.storage import is_recallable, storage_policy_configured

    if walk.kind == "unresolvable":
        return _unresolved(handle, walk, with_pii=with_pii)

    uid = walk.uid
    assert uid is not None  # a "resolved" walk always carries a uid

    index = entity_index if entity_index is not None else _EntityIndex(wiki_root)
    page_path = index.get_by_uid(uid)
    if page_path is None or not page_path.exists() or not index.has_entity_format(page_path):
        return _orphan_uid(handle, with_pii=with_pii)

    try:
        text = page_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _orphan_uid(handle, with_pii=with_pii)
    page_fm, _ = parse_frontmatter(text)

    # D6 step 2 — audience filter, fail-closed, BEFORE any excluded lookup.
    if caller_audience is not None and not is_page_authorized(page_fm, caller_audience):
        return _unresolved(handle, _Walk(kind="unresolvable", reason="no-match"), with_pii=with_pii)

    # D6 step 3 — the athenaeum#532 `recallable` drop, likewise before any excluded lookup.
    page_type = str(page_fm.get("type") or "")
    if storage_policy_configured(config) and not is_recallable(page_type, config):
        return _unresolved(handle, _Walk(kind="unresolvable", reason="no-match"), with_pii=with_pii)

    # D6 step 4 — only now does an excluded-surface lookup happen.
    contact_values, redactions = _assemble_contact_values(
        knowledge_root,
        page_path,
        page_fm,
        uid,
        page_type,
        config=config,
        with_pii=with_pii,
        usage_classes=usage_classes,
        excluded_index=excluded_index,
    )

    display_name_raw = page_fm.get("name")
    display_name = str(display_name_raw) if display_name_raw else None

    return HandleResolution(
        resolved=True,
        reason=None,
        handle=handle,
        with_pii=with_pii,
        uid=uid,
        display_name=display_name,
        entity_class=page_type or None,
        contact_values=contact_values,
        redactions=redactions,
    )


def resolve_handle_query(
    knowledge_root: Path,
    wiki_root: Path,
    query: str,
    *,
    caller_audience: set[str] | None = None,
    config: dict[str, Any] | None = None,
    with_pii: bool = False,
    usage_classes: Collection[str] | None = None,
    excluded_index: ExcludedRecordIndex | None = None,
    entity_index: EntityIndex | None = None,
) -> HandleResolution | None:
    """The one entry point both ``recall`` implementations call (D7).

    Returns ``None`` when *query* is not handle-shaped (D2) — the caller's
    hard contract is that this is a complete no-op in that case: no lookup of
    any kind has happened, and the caller falls through to its existing
    similarity-search path completely unchanged (byte-identical output).

    Otherwise returns a :class:`HandleResolution` — resolved or not, per D3's
    walk and D6's audience/``recallable``/``with_pii`` gating, always in that
    order.

    Args:
        knowledge_root: Root of the knowledge base (parent of ``wiki/``).
        wiki_root: The compiled wiki directory (``knowledge_root / "wiki"``).
        query: The raw recall query string, exactly as the caller received it.
        caller_audience: Read-scope pin (issues athenaeum#312/#538). ``None`` is
            the owner (no check) — identical semantics to every other read
            path in this repo.
        config: Resolved ``athenaeum.yaml``, threaded to every ``pii``/
            ``storage`` call that needs it.
        with_pii: Whether to include real excluded values (with their facts)
            in the response, or their field-level redaction markers instead.
            Same flag, same semantics, as ``recall(with_pii=...)``.
        usage_classes: Restrict resolved excluded values to these usage
            classes (issue athenaeum#866/#907), threaded to the join exactly
            as the similarity-search path already accepts it (see
            :func:`athenaeum.mcp_server.recall_search`'s ``usage_classes``
            doc). ``None`` (default) returns every value. Only meaningful with
            *with_pii* — it filters WITHIN the excluded-value join and never
            widens it, and never perturbs the ``with_pii=False``
            redaction-marker path.
        excluded_index: An already-built
            :class:`~athenaeum.pii.ExcludedRecordIndex` over the ``pii``
            contacts surface, for a caller resolving many handles in one
            process (issue athenaeum#1124) — a 22,078-page/12,967-record corpus
            otherwise rebuilds both O(corpus) indexes on EVERY call. ``None``
            (the default, and every pre-athenaeum#1124 caller) builds one
            per call exactly as before; behaviour is byte-identical either
            way. **Staleness (athenaeum#850):** valid only for a read-only
            window — :func:`athenaeum.pii.mark_bounced` mints records and
            merges identifiers onto existing ones, so a caller interleaving
            bounce writes with resolution reads in the same process must
            rebuild or invalidate this index at that boundary rather than
            keep resolving through a now-stale one.
        entity_index: An already-built
            :class:`~athenaeum.models.EntityIndex` over the compiled wiki,
            for the same reason. ``None`` (the default) builds one per call.
            Not subject to the athenaeum#850 staleness concern itself, but a
            long-lived process that also compiles new wiki pages between
            resolutions should rebuild it at that boundary for the same
            reason.

    Gating is unaffected either way: :func:`_finish`'s audience and
    ``recallable`` checks run against frontmatter parsed fresh from disk on
    every call, strictly before any excluded-surface lookup — a prepared
    index changes only what a lookup costs, never what it is allowed to see.
    """
    from athenaeum import corrections

    email_handle = _detect_email_handle(query)
    if email_handle is not None:
        from athenaeum import pii

        normalized = pii.normalize_identifier(email_handle)
        walk = _walk_email_handle(
            knowledge_root, normalized, config=config, excluded_index=excluded_index
        )
        return _finish(
            knowledge_root,
            wiki_root,
            normalized,
            walk,
            caller_audience=caller_audience,
            config=config,
            with_pii=with_pii,
            usage_classes=usage_classes,
            excluded_index=excluded_index,
            entity_index=entity_index,
        )

    stripped = _strip_interrogative_framing(query)
    if not stripped:
        return None

    registry_entities = corrections.load_registry(knowledge_root)
    uids = _registry_handle_matches(registry_entities, stripped)
    if not uids:
        # Not handle-shaped: nothing in the registry matches this text, so it
        # is treated as an ordinary query rather than an unresolvable handle.
        return None

    walk = _walk_registry_handle(uids)
    return _finish(
        knowledge_root,
        wiki_root,
        stripped,
        walk,
        caller_audience=caller_audience,
        config=config,
        with_pii=with_pii,
        usage_classes=usage_classes,
        excluded_index=excluded_index,
        entity_index=entity_index,
    )


def resolve_person_mention(
    mention: str,
    *,
    wiki_root: Path,
    entity_index: "EntityIndex",
    knowledge_root: Path | None = None,
    config: dict[str, Any] | None = None,
    registry: "PersonRegistry | None" = None,
) -> "PersonRegistryEntry | None":
    """Resolve a raw-text person MENTION through the consult-only person
    registry, but only when the ordinary entity index has no entry for it
    (issue athenaeum#1183 AC2).

    This is the intake-time counterpart to :func:`resolve_handle_query`
    above: that function resolves a `recall` QUERY that looks like a handle
    (an email address, a ``registry.json`` value) to person FACTS. This
    function resolves a plain-text NAME/ALIAS mention encountered during
    intake against the consult-only person registry directly. It never
    inspects handle-shape at all — an ordinary name is exactly what it
    expects.

    **Why check *entity_index* at all, if athenaeum#1183 only withholds `person`
    from :meth:`~athenaeum.models.EntityIndex.items` (the raw-text MATCHING
    surface :func:`athenaeum.tiers.tier1_programmatic_match` walks), not
    from :meth:`~athenaeum.models.EntityIndex.lookup`?** Because
    :meth:`~athenaeum.models.EntityIndex.lookup` — a single, ADDRESSED
    name/alias lookup — deliberately KEEPS finding a `type: person` page
    exactly as before this issue, for every consumer that already
    name-addresses one (:func:`athenaeum.corrections.resolve_target`, the
    tier-0 handle-upsert fallback, tier-3's create-name collision check).
    So on an UNMIGRATED corpus (person pages still physically under the same
    *wiki_root* `entity_index` scans — the default, see
    :func:`athenaeum.config.resolve_person_registry_root`), *entity_index*
    already finds an existing person page and this function correctly
    returns ``None`` (nothing new to attribute through — the ordinary path
    already works). The person-registry consult below only pays off once
    athenaeum#1247 physically relocates person pages OUT of *wiki_root* — at
    that point *entity_index* (scoped to *wiki_root*) genuinely has no entry
    for a relocated person, while :class:`~athenaeum.person_registry.PersonRegistry`
    (pointed at the new root) still does. This is exactly the "only when
    that person has no entity-index entry" case the issue describes, and
    exactly the seam athenaeum#1247 needs — checked here so the caller never
    has to arrange it itself, and so a synthetic fixture can exercise the
    post-relocation shape today without waiting for athenaeum#1247 to land.

    *registry* lets a caller thread an already-built
    :class:`~athenaeum.person_registry.PersonRegistry` (the O(corpus) scan
    cost paid once per run, mirroring *entity_index* itself). ``None`` (the
    default) builds one from :func:`athenaeum.config.resolve_person_registry_root`
    — *knowledge_root* is required in that case (``wiki_root.parent`` when
    the caller has no other knowledge root at hand, matching the convention
    :func:`athenaeum.librarian.process_one` already uses for
    ``route_sensitive_values``).

    Returns the matched :class:`~athenaeum.person_registry.PersonRegistryEntry`,
    or ``None`` when neither index has anything for *mention*.
    """
    if entity_index.lookup(mention) is not None:
        return None

    if registry is None:
        from athenaeum.config import resolve_person_registry_root
        from athenaeum.person_registry import PersonRegistry as _PersonRegistry

        root = resolve_person_registry_root(
            knowledge_root if knowledge_root is not None else wiki_root.parent,
            config,
        )
        registry = _PersonRegistry(root)

    return registry.lookup(mention)
