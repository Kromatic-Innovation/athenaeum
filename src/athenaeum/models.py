# SPDX-License-Identifier: Apache-2.0
"""Data models, YAML frontmatter parsing, and entity index for Athenaeum."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import ItemsView, Iterator

import yaml

log = logging.getLogger("athenaeum")

# --- UID generation ---


def generate_uid() -> str:
    """Generate an 8-character hex UID from uuid4."""
    return uuid.uuid4().hex[:8]


# --- Origin-traced source provenance (issue #260, slice A of #259) ---

# The four legal ``source_type`` values for an origin-traced citation. The
# librarian must cite the ULTIMATE source of a fact — the user, an external
# URL, a permanent document, or (when nothing can be established) an honest
# ``inferred``. It must NEVER cite the raw ``auto-memory/...`` filename as the
# source. See ``policies/auto-memory-citation.md``.
SOURCE_TYPES: frozenset[str] = frozenset(
    {"user-stated", "external", "document", "inferred"}
)

# Default when origin cannot be established. ``inferred`` is the honest
# fallback — an unverifiable agent leap is labeled as such, not promoted to
# ``user-stated``.
DEFAULT_SOURCE_TYPE = "inferred"


def coerce_source_type(value: object) -> str:
    """Return a valid ``source_type``, defaulting unknown input to ``inferred``.

    Backward-compatible: legacy sources written before #260 carry no
    ``source_type`` (``None``) and resolve to ``inferred``. A typo'd or
    out-of-vocabulary value is also coerced rather than raising — the
    citation policy is enforced at write time, and a bad value must not
    crash the nightly compile.
    """
    if isinstance(value, str) and value in SOURCE_TYPES:
        return value
    # A non-empty, out-of-vocabulary value is a real downgrade (typo or stale
    # schema) worth a breadcrumb; ``None`` / empty is the ordinary legacy path
    # and stays quiet.
    if value not in (None, ""):
        log.debug(
            "coerce_source_type: downgrading invalid source_type %r to %s",
            value,
            DEFAULT_SOURCE_TYPE,
        )
    return DEFAULT_SOURCE_TYPE


def is_filename_like_ref(ref: object) -> bool:
    """True when a ``source_ref`` looks like a raw ``auto-memory`` filename.

    The load-bearing #260 invariant: a citation must point at the ULTIMATE
    source (session+turn / URL / document), never at the transient raw
    ``auto-memory/<scope>/<prefix>_<slug>.md`` view that retires on move
    (#259). A ref is filename-shaped when it references the auto-memory tree
    or ends in ``.md``.
    """
    if not isinstance(ref, str) or not ref:
        return False
    lowered = ref.lower()
    return "auto-memory" in lowered or lowered.endswith(".md")


def safe_source_ref(candidate: object, fallback: str) -> str:
    """Return ``candidate`` unless it is filename-shaped, else ``fallback``.

    Enforces the #260 invariant on the EXPLICIT path: a producer that stamps
    a raw filename into ``source_ref`` is rejected and replaced with a safe
    session-anchored fallback. Empty candidate also falls back.
    """
    if isinstance(candidate, str) and candidate and not is_filename_like_ref(candidate):
        return candidate
    if is_filename_like_ref(candidate):
        log.debug(
            "safe_source_ref: rejecting filename-shaped source_ref %r; using %r",
            candidate,
            fallback,
        )
    return fallback


def slugify(name: str) -> str:
    """Convert a name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:60]  # cap length


# --- Frontmatter parsing ---

_FM_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split YAML frontmatter from body. Returns ``(metadata, body)``.

    The metadata dict has string keys and arbitrary YAML-scalar/list/dict
    values (hence ``object``). Callers that need narrower types should
    validate the fields they depend on — the schema is intentionally
    open so non-core frontmatter keys round-trip cleanly.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    # Coerce identity fields at the YAML boundary. PyYAML loads bare
    # all-decimal hex uids (e.g. ``19052``) and unquoted numeric names
    # as ``int`` — downstream code (schema validation, index lookup,
    # filename rendering) expects ``str``. Fixing it here keeps the
    # on-disk dict consistent with the model and removes the need for
    # int-coercion shims further down.
    if isinstance(meta, dict):
        for _k in ("uid", "type", "name"):
            _v = meta.get(_k)
            if isinstance(_v, int) and not isinstance(_v, bool):
                meta[_k] = str(_v)
    body = text[m.end() :]
    return meta, body


def parse_refines(meta: dict[str, object] | None) -> list[str]:
    """Coerce a frontmatter ``refines:`` value into a clean list of slugs.

    Accepts:
    - ``None`` / missing key → ``[]``.
    - ``list[str]`` of memory ``name:`` slugs (the documented shape).

    Raises:
        ValueError: when ``refines`` is present but not a list, or any
            entry is not a non-empty string. The frontmatter is a
            durable contract — a typo (``refines: name-x`` rendered as a
            scalar) should be loud, not silent.
    """
    if not meta:
        return []
    raw = meta.get("refines")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"refines must be a list of memory name slugs, got {type(raw).__name__}"
        )
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"refines entries must be non-empty strings, got {entry!r}"
            )
        out.append(entry.strip())
    return out


def parse_supersedes(meta: dict[str, object] | None) -> list[dict[str, str]]:
    """Coerce a frontmatter ``supersedes:`` value into a list of records.

    Accepts:
    - ``None`` / missing key → ``[]``.
    - ``list[dict]`` of ``{name, as_of, reason}`` records. ``name`` is
      required and must be a non-empty string. ``as_of`` and ``reason``
      are optional; missing values are stored as empty strings so
      downstream consumers can rely on the keys existing.

    Raises:
        ValueError: when ``supersedes`` is not a list, an entry is not a
            mapping, or an entry lacks a non-empty ``name`` key.
    """
    if not meta:
        return []
    raw = meta.get("supersedes")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"supersedes must be a list of records, got {type(raw).__name__}"
        )
    out: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(
                f"supersedes entries must be mappings, got {type(entry).__name__}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("supersedes entries require a non-empty 'name' key")
        as_of = entry.get("as_of", "")
        reason = entry.get("reason", "")
        out.append(
            {
                "name": name.strip(),
                "as_of": str(as_of) if as_of is not None else "",
                "reason": str(reason) if reason is not None else "",
            }
        )
    return out


def parse_superseded_by(meta: dict[str, object] | None) -> str:
    """Return the frontmatter ``superseded_by`` pointer (winner name slug), or "".

    Set by the resolver's keep_a/keep_b enactment on the LOSING member to
    mark it as valid-then-replaced history. Non-empty => the member is
    inactive (excluded from recall + C3 compile) but preserved on disk.
    Tolerant: a non-string value coerces to its str form; missing => "".
    """
    if not meta:
        return ""
    raw = meta.get("superseded_by")
    if raw is None:
        return ""
    return str(raw).strip()


def parse_deprecated(meta: dict[str, object] | None) -> bool:
    """Return the truthy ``deprecated`` frontmatter flag (deprecate_both, #191).

    Accepts a real bool, or a string variant (``true``/``1``/``yes``,
    case-insensitive); any other truthy value coerces via ``bool``.
    Missing / falsey => ``False``.
    """
    if not meta:
        return False
    dep = meta.get("deprecated")
    if isinstance(dep, bool):
        return dep
    if isinstance(dep, str):
        return dep.strip().lower() in ("true", "1", "yes")
    return bool(dep)


# --- Claim-level temporal validity (issue #308, slice 1) ---
#
# ``valid_from:`` / ``valid_until:`` are optional ISO-8601 date frontmatter
# fields declaring the real-world window over which a claim is true. They sit
# BESIDE ``source:`` provenance (which answers *where/when ingested*, not *over
# what window valid*) — the bi-temporal split from Zep/Graphiti. Slice 1 makes
# the READER honor a ``valid_until`` set by a human or the resolver; slice 2
# (shipped) has the resolver auto-stamp the interval on a temporal supersession
# (``resolutions.enact_resolution`` — see ``docs/provenance-shape.md`` §8.4).
# There is no ``--as-of`` view yet (slice 3, for which the predicate already
# takes an ``as_of`` parameter). See ``docs/provenance-shape.md`` §8.


def _coerce_iso_date(value: object) -> date | None:
    """Coerce a frontmatter value to a :class:`datetime.date`, or ``None``.

    Fail-OPEN (issue #308): a missing, empty, or UNPARSEABLE value returns
    ``None`` (treated as an open bound / no constraint), mirroring
    :func:`coerce_source_type`'s "must not crash the nightly compile" contract.
    Silently dropping a page on a bad date is worse than keeping it visible for
    a knowledge base, so a malformed date is logged and treated as absent.

    Accepts a real :class:`datetime.date` (YAML auto-parses a bare
    ``YYYY-MM-DD`` scalar into one) or an ISO-8601 ``YYYY-MM-DD`` string
    (e.g. a quoted date). Anything else => ``None`` + a debug breadcrumb.
    """
    if value is None or value == "":
        return None
    # ``datetime`` subclasses ``date`` — reduce to a bare date so a later
    # ``as_of`` (a ``date``) comparison never hits the date-vs-datetime
    # TypeError. Slice 1 is date-resolution; any time component is dropped.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            log.debug(
                "temporal-validity: unparseable ISO date %r; treating as open "
                "(fail-open, claim stays active)",
                value,
            )
            return None
    log.debug(
        "temporal-validity: non-date value %r for a validity bound; treating "
        "as open (fail-open, claim stays active)",
        value,
    )
    return None


def parse_valid_from(meta: dict[str, object] | None) -> date | None:
    """Return the frontmatter ``valid_from`` as a date, or ``None`` (open lower bound).

    Fail-open: missing / unparseable => ``None`` (valid since always). Not part
    of the slice-1 inactive predicate (which keys on ``valid_until`` only), but
    parsed for round-trip and future not-yet-valid handling.
    """
    if not meta:
        return None
    return _coerce_iso_date(meta.get("valid_from"))


def parse_valid_until(meta: dict[str, object] | None) -> date | None:
    """Return the frontmatter ``valid_until`` as a date, or ``None`` (open upper bound).

    Fail-open: missing / unparseable => ``None`` (open interval, still valid).
    ``valid_until`` is the LAST date the claim was valid (inclusive).
    """
    if not meta:
        return None
    return _coerce_iso_date(meta.get("valid_until"))


def validity_bound_str(meta: dict[str, object] | None, key: str) -> str:
    """Return a ``valid_from`` / ``valid_until`` bound NORMALIZED to ``YYYY-MM-DD``.

    Used at :class:`AutoMemoryFile` construction to store the bound so the
    dataclass predicate (:meth:`AutoMemoryFile.is_inactive`, which re-parses this
    string) reaches the SAME verdict the dict predicate
    (:func:`is_inactive_memory`) reaches parsing the raw ``meta`` value directly.

    Critically, the bound is run through the SAME :func:`_coerce_iso_date` the
    dict path uses and re-emitted as an ISO date string, rather than a naive
    ``str(raw)``. ``str(raw)`` diverged from the dict path on two reachable YAML
    types: a ``datetime`` (``2026-06-30 12:00:00`` → ``str`` is not
    ``fromisoformat``-parseable → fail-open, but the dict path ``.date()``
    honors it) and an ``int`` (``20260630`` → ``str`` parses as a bogus date,
    but the dict path returns ``None``). Normalizing here makes both predicates
    parse identical text and agree on ``date``/``datetime``/``int``/``str``/
    malformed inputs. A genuinely unparseable value normalizes to ``""``
    (fail-open — the claim stays active), matching the dict path's ``None``.
    """
    if not meta:
        return ""
    coerced = _coerce_iso_date(meta.get(key))
    return coerced.isoformat() if coerced is not None else ""


def valid_until_expired(
    meta: dict[str, object] | None, as_of: date | None = None
) -> bool:
    """True when ``valid_until`` is strictly in the past relative to ``as_of``.

    The single shared upper-bound predicate wired into BOTH
    :func:`is_inactive_memory` (dict path, recall) and
    :meth:`AutoMemoryFile.is_inactive` (dataclass path, C3 compile) so they stay
    in lockstep. ``as_of`` defaults to :func:`date.today` — pass an explicit
    date (slice 3's ``--as-of``) to rewind the view. Open upper bound (absent /
    malformed ``valid_until``) => ``False`` (still valid). Inclusive last-valid
    date: inactive iff ``as_of > valid_until``.
    """
    until = parse_valid_until(meta)
    if until is None:
        return False
    return (as_of or date.today()) > until


def is_inactive_memory(
    meta: dict[str, object] | None, as_of: date | None = None
) -> bool:
    """True when a memory file is marked inactive and must not surface as a live claim.

    Inactive == frontmatter declares ANY of: a non-empty ``superseded_by``
    (keep_a/keep_b loser, issue #191), a truthy ``deprecated`` flag
    (deprecate_both, issue #191), OR a ``valid_until`` in the past relative to
    ``as_of`` (claim-level temporal validity, issue #308). Inactive members are
    preserved on disk for audit but are skipped by recall (search index) and by
    the C3 merge compile so their claims drop out of the live wiki.

    ``as_of`` defaults to today; the past-``valid_until`` disjunct filters
    expired claims by default. An absent or malformed ``valid_until`` is an open
    interval (fail-open — the claim stays active).
    """
    if not meta:
        return False
    if parse_superseded_by(meta):
        return True
    if parse_deprecated(meta):
        return True
    return valid_until_expired(meta, as_of)


def validity_windows_disjoint(
    meta_a: dict[str, object] | None, meta_b: dict[str, object] | None
) -> bool:
    """True when two claims' validity windows cannot overlap in time (issue #324).

    Two claims are DISJOINT — sequential states of the world that cannot
    contradict (A true through March, B true from April) — iff one side has a
    CLOSED upper bound (``valid_until``) ending strictly before the other side's
    lower bound (``valid_from``) begins::

        a_until is not None and b_from is not None and a_until < b_from
        # OR the symmetric
        b_until is not None and a_from is not None and b_until < a_from

    ``valid_until`` is the INCLUSIVE last-valid date, so the comparison is strict
    ``<``: A ending 2026-03-31 and B starting 2026-04-01 → ``03-31 < 04-01`` →
    disjoint; A ending 2026-04-01 and B starting 2026-04-01 → they share that
    day → NOT disjoint.

    Each bound is parsed with the fail-open :func:`parse_valid_from` /
    :func:`parse_valid_until`: a missing OR malformed value coerces to ``None``
    (an open bound). Open bounds overlap by default, so a claim with no window —
    or a malformed one — is never disjoint from anything (detection proceeds).
    This is the fail-open posture the contradiction detector needs; no separate
    malformed handling is added here because ``parse_*`` already does it.
    """
    a_from = parse_valid_from(meta_a)
    a_until = parse_valid_until(meta_a)
    b_from = parse_valid_from(meta_b)
    b_until = parse_valid_until(meta_b)
    if a_until is not None and b_from is not None and a_until < b_from:
        return True
    if b_until is not None and a_from is not None and b_until < a_from:
        return True
    return False


# --- Audience / access scoping (issue #312) ---
#
# Read-scoping for secondary agents/routines. The audience model is
# RBAC-compatible: ``audience:`` is a free-form list of opaque role/group
# identifiers the operator aligns with an external directory (AD group, app
# role, routine name). The pre-existing schema-validated ``access:`` field
# (open/internal/confidential/personal) is reused as the COARSE visibility
# default and composes with ``audience:``.
#
# Every helper here is FAIL-CLOSED: malformed / unparseable input yields the
# most restrictive interpretation (audience-∅, i.e. owner-only), never
# "public". The owner (no serve-time audience pin, ``caller_audience=None``)
# bypasses every check and sees everything, so single-user behavior is
# unbroken.

# The ``access:`` level that maps to "world-readable by every audience".
_ACCESS_PUBLIC = "open"

# Internal sentinel token that marks a page public in the serialized index
# audience string. It is DELIBERATELY distinct from the ``open`` access word so
# "public" is decided ONLY by ``access == open`` at serialization time, never
# by an ``audience:`` role literally named ``open``. ``parse_audience`` also
# refuses this token (and the access-level words) as role ids, so no role can
# ever produce this marker — closing the collision at the source. Exported so
# the backends test the same marker instead of hardcoding a literal.
AUDIENCE_PUBLIC_TOKEN = "__access_open__"

# Words that are access levels / the internal public sentinel, NOT audience
# roles. Dropped from any ``audience:`` list so a mislabeled entry can never be
# read as a role grant (and can never forge the public marker).
_RESERVED_AUDIENCE_ROLES: frozenset[str] = frozenset(
    {"open", "internal", "confidential", "personal", AUDIENCE_PUBLIC_TOKEN}
)


def parse_access(meta: dict[str, object] | None) -> str:
    """Return the normalized ``access:`` level, or ``""`` when absent/malformed.

    Case-folded and whitespace-trimmed. A non-string value (a typo'd list or
    mapping) returns ``""`` — which, being neither ``open`` nor a granting
    ``audience:``, fails closed to owner-only for a restricted caller.
    """
    if not meta:
        return ""
    raw = meta.get("access")
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def parse_audience(meta: dict[str, object] | None) -> list[str]:
    """Coerce a frontmatter ``audience:`` value into a clean list of role ids.

    The single normalization point for the read-scoping control (issue #312),
    sibling to :func:`parse_refines`/:func:`parse_supersedes`. Accepts:

    - ``None`` / missing key → ``[]`` (no explicit grant).
    - ``list[str]`` of non-empty role/group identifiers → case-folded,
      whitespace-trimmed list.

    Unlike :func:`parse_refines`, this **degrades to withhold rather than
    raise** on malformed input: a scalar ``audience:`` value, a list holding a
    non-string / empty entry, or any other bad shape returns ``[]`` (audience-∅
    → withheld from a restricted caller). This is the fail-closed posture the
    security boundary requires — one bad page must not crash a scheduled recall,
    and a malformed tag must never be read as "public". A debug breadcrumb is
    logged so the operator can find the offending page.

    Reserved words — the access-level names (``open`` / ``internal`` /
    ``confidential`` / ``personal``) and the internal public sentinel — are NOT
    valid role ids and are dropped: ``audience: [open]`` grants no role (public
    is decided only by ``access: open``), so it cannot be mistaken for a
    world-readable grant or forge the index's public marker.
    """
    if not meta:
        return []
    raw = meta.get("audience")
    if raw is None:
        return []
    if not isinstance(raw, list):
        log.debug(
            "audience must be a list of role ids, got %s; withholding",
            type(raw).__name__,
        )
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            log.debug(
                "audience entries must be non-empty strings, got %r; withholding", entry
            )
            return []
        role = entry.strip().lower()
        if role in _RESERVED_AUDIENCE_ROLES:
            log.debug(
                "audience role %r is a reserved access-level word, not a role; "
                "dropping",
                role,
            )
            continue
        out.append(role)
    return out


def effective_audience(meta: dict[str, object] | None) -> tuple[set[str], bool]:
    """Return ``(granted_roles, is_public)`` for a page's frontmatter.

    - ``is_public`` is True iff ``access: open`` (world-readable).
    - ``granted_roles`` is the set of explicit ``audience:`` role ids. The
      coarse ``access:`` levels ``internal``/``confidential``/``personal``
      contribute NO roles (owner-only) unless an explicit ``audience:`` grant
      is present — the composition rule from the design.

    A page with neither ``access: open`` nor an ``audience:`` grant has
    ``(set(), False)`` — audience-∅, withheld from every restricted caller.
    """
    public = parse_access(meta) == _ACCESS_PUBLIC
    roles = set(parse_audience(meta))
    return roles, public


def is_page_authorized(
    meta: dict[str, object] | None,
    caller_audience: set[str] | None,
) -> bool:
    """True iff a caller pinned to ``caller_audience`` may read this page.

    ``caller_audience=None`` is the owner / default caller: authorized for
    EVERYTHING (untagged included). A non-None set is a restricted caller:
    authorized iff the page is public (``access: open``) OR the caller holds at
    least one role in the page's granted set. Fail-closed: an untagged or
    malformed page has an empty granted set and is withheld from a restricted
    caller.
    """
    if caller_audience is None:
        return True
    roles, public = effective_audience(meta)
    if public:
        return True
    return bool(caller_audience & roles)


def audience_index_string(meta: dict[str, object] | None) -> str:
    """Serialize a page's effective audience for storage in the search index.

    Returns a delimiter-anchored string so a substring/``LIKE`` test can never
    cross a role boundary (``|ops|`` never matches ``|opsadmin|``):

    - public page → ``"|__access_open__|"`` (may also include granted roles).
      The public marker is the internal sentinel, never the ``open`` word, so a
      role can't forge it.
    - roles ``{a, b}`` → ``"|a|b|"`` (sorted for deterministic rebuilds).
    - audience-∅ → ``"|"`` (empty sentinel).

    Stored UNINDEXED in FTS5 (out of the BM25 term space) and as chromadb
    metadata so Layer B can filter INSIDE each backend query.
    """
    roles, public = effective_audience(meta)
    parts: list[str] = []
    if public:
        parts.append(AUDIENCE_PUBLIC_TOKEN)
    parts.extend(sorted(roles))
    if not parts:
        return "|"
    return "|" + "|".join(parts) + "|"


def audience_string_authorized(
    audience_str: str,
    caller_audience: set[str] | None,
) -> bool:
    """Authorize a caller against a stored :func:`audience_index_string`.

    The string-based counterpart to :func:`is_page_authorized`, used by the
    vector backend's Python post-filter (chromadb metadata is scalar-only, so
    the audience is stored as this delimited string and filtered here).
    ``caller_audience=None`` (owner) is always authorized.
    """
    if caller_audience is None:
        return True
    if f"|{AUDIENCE_PUBLIC_TOKEN}|" in audience_str:
        return True
    return any(f"|{role}|" in audience_str for role in caller_audience)


def render_frontmatter(meta: dict[str, object]) -> str:
    """Render a dict as a YAML frontmatter block.

    Contract: key order preserved (``sort_keys=False``) for tier0
    byte-for-byte round-trip. Do not change without updating
    ``test_render_frontmatter_preserves_key_order``.
    """
    dumped = yaml.dump(
        meta, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
    return f"---\n{dumped}---\n"


# --- Data classes ---


@dataclass
class RawFile:
    """A raw intake file from raw/{source}/{timestamp}-{uuid8}.md."""

    path: Path
    source: str
    timestamp: str
    uuid8: str
    _content: str | None = field(default=None, repr=False)

    @property
    def content(self) -> str:
        if self._content is None:
            self._content = self.path.read_text(encoding="utf-8")
        return self._content

    @property
    def ref(self) -> str:
        """Short reference for footnotes."""
        return f"{self.source}/{self.path.name}"


@dataclass
class AutoMemoryFile:
    """A raw intake file from ``raw/auto-memory/<scope>/<prefix>_<slug>.md``.

    Parallel sibling to :class:`RawFile` — auto-memory uses a different
    naming convention (``feedback_*.md``, ``project_*.md``, ``reference_*.md``,
    ``user_*.md``, ``Recall_*.md``) and a different frontmatter schema
    (``type`` / ``originSessionId`` / ``originTurn`` / ``sources`` instead
    of the entity schema's ``uid`` / ``name``).

    ``origin_scope`` is the scope directory name verbatim — the full
    path-hash identifier (e.g. ``-Users-alice-Code-projectx``) or
    the literal ``_unscoped``. Preserving this on the record is C2/C3's
    routing key; the compile step downstream will carry it through to the
    wiki entry metadata.
    """

    path: Path
    origin_scope: str
    memory_type: str  # feedback|project|reference|user|recall
    name: str = ""
    description: str = ""
    origin_session_id: str | None = None
    origin_turn: int | None = None
    sources: list[str] = field(default_factory=list)
    # Lane 1 / #167: declared relationships to other memories. Both
    # default to empty list. ``refines`` lists ``name:`` slugs of
    # memories this one narrows (general + exception — BOTH stay
    # active). ``supersedes`` lists ``{name, as_of, reason}`` records
    # declaring this memory replaces another (the superseded memory
    # stays for audit but is no longer active guidance). Matching is
    # by ``name:`` slug, not path.
    refines: list[str] = field(default_factory=list)
    supersedes: list[dict[str, str]] = field(default_factory=list)
    # Issue #191: non-destructive inactive markers written by the resolver's
    # keep_a/keep_b (superseded_by = winner name) and deprecate_both
    # (deprecated = True) enactment. An inactive member is preserved on disk
    # for audit but excluded from recall + the C3 compile so it does not
    # resurface as a live claim.
    superseded_by: str = ""
    deprecated: bool = False
    # Issue #260 (slice A of #259): origin-traced provenance. ``source_type``
    # is one of :data:`SOURCE_TYPES` (default ``inferred`` so memories written
    # before the citation policy still parse). ``source_ref`` is the ULTIMATE
    # reference — session-id+turn, URL, or document path — NEVER this file's
    # own ``raw/auto-memory/...`` name. Empty when unestablished.
    source_type: str = DEFAULT_SOURCE_TYPE
    source_ref: str = ""
    # Issue #308 (slice 1): claim-level temporal validity. Both are the RAW
    # frontmatter string form (``YYYY-MM-DD`` or "" when absent) so the
    # dataclass predicate re-parses to the SAME date as the dict predicate
    # sees — keeping :meth:`is_inactive` in lockstep with
    # :func:`is_inactive_memory`. ``valid_until`` is the last date the claim was
    # valid (inclusive); absent => open interval (still valid).
    valid_from: str = ""
    valid_until: str = ""
    _content: str | None = field(default=None, repr=False)

    @property
    def content(self) -> str:
        if self._content is None:
            self._content = self.path.read_text(encoding="utf-8")
        return self._content

    @property
    def ref(self) -> str:
        """Short reference for footnotes — scope/filename."""
        return f"{self.origin_scope}/{self.path.name}"

    def is_inactive(self, as_of: date | None = None) -> bool:
        """True when this member is inactive (#191 marker OR expired #308 validity).

        Mirrors :func:`is_inactive_memory` on the dataclass path (C3 compile):
        inactive iff a ``superseded_by`` pointer or ``deprecated`` flag is set,
        OR ``valid_until`` is in the past relative to ``as_of`` (default today).
        Delegates the temporal check to the shared :func:`valid_until_expired`
        helper — fed the raw ``valid_until`` string — so the two predicates
        cannot drift. An absent/malformed ``valid_until`` is an open interval
        (fail-open, stays active).
        """
        if self.superseded_by or self.deprecated:
            return True
        return valid_until_expired({"valid_until": self.valid_until}, as_of)

    def supersedes_names(self) -> list[str]:
        """Return just the ``name`` keys from :attr:`supersedes` records."""
        out: list[str] = []
        for rec in self.supersedes:
            if isinstance(rec, dict):
                n = rec.get("name")
                if isinstance(n, str) and n:
                    out.append(n)
        return out


@dataclass
class WikiEntity:
    """An entity page in wiki/ using the full entity template format."""

    uid: str
    type: str
    name: str
    aliases: list[str] = field(default_factory=list)
    access: str = "internal"
    tags: list[str] = field(default_factory=list)
    related: list[dict[str, str]] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    body: str = ""
    # Per-claim provenance (issue #90 / #95). Optional so old wikis
    # without provenance still round-trip cleanly. ``source`` is the
    # wiki-level default; ``field_sources`` overrides per field.
    source: str | dict | None = None
    # ``field_sources`` per-field value is ``str``/``dict`` (legacy)
    # OR ``list[dict]`` of ``{"value", "source"}`` records (per-value
    # attribution for list fields, issue #102).
    field_sources: dict[str, str | dict | list] | None = None
    # Issue #260: origin-traced provenance threaded onto the entity. Both
    # optional so legacy entities round-trip unchanged. ``source_type`` is one
    # of :data:`SOURCE_TYPES`; ``source_ref`` is the ultimate reference and is
    # never the raw ``auto-memory/...`` filename. Rendered into frontmatter
    # only when set.
    source_type: str | None = None
    source_ref: str | None = None

    @property
    def filename(self) -> str:
        return f"{self.uid}-{slugify(self.name)}.md"

    def render(self) -> str:
        """Render to full markdown with YAML frontmatter."""
        meta: dict = {
            "uid": self.uid,
            "type": self.type,
            "name": self.name,
        }
        if self.aliases:
            meta["aliases"] = self.aliases
        meta["access"] = self.access
        if self.tags:
            meta["tags"] = self.tags
        if self.related:
            meta["related"] = self.related
        if self.created:
            meta["created"] = self.created
        if self.updated:
            meta["updated"] = self.updated
        if self.source is not None:
            meta["source"] = self.source
        if self.field_sources:
            meta["field_sources"] = self.field_sources
        if self.source_type is not None:
            meta["source_type"] = self.source_type
        if self.source_ref is not None:
            meta["source_ref"] = self.source_ref
        return render_frontmatter(meta) + "\n" + self.body


@dataclass
class ClassifiedEntity:
    """Output of Tier 2 classification."""

    name: str
    entity_type: str
    tags: list[str]
    access: str
    is_new: bool
    existing_uid: str | None = None
    observations: str = ""


@dataclass
class EntityAction:
    """A create or update action for Tier 3."""

    kind: Literal["create", "update"]
    name: str
    entity_type: str
    tags: list[str]
    access: str
    existing_uid: str | None
    observations: str


@dataclass
class EscalationItem:
    """An item to escalate to _pending_questions.md."""

    raw_ref: str
    entity_name: str
    conflict_type: str  # "principled" | "ambiguous" | "classification_failed"
    description: str
    # Optional resolver proposal threaded through from
    # :func:`athenaeum.resolutions.propose_resolution`. When present and
    # confidence >= the configured threshold, :func:`tier4_escalate`
    # auto-applies the resolution to the rendered block. Typed as
    # ``Any`` to avoid a circular import (resolutions.py imports
    # AutoMemoryFile from this module). The runtime type is
    # ``athenaeum.resolutions.ResolutionProposal | None``.
    proposal: Any = None
    # Absolute paths of the flagged member files in resolver ``a``/``b``
    # order (``members[0]`` is side ``a``, ``members[1]`` is side ``b``).
    # Populated by :func:`athenaeum.merge._emit_escalation` so the
    # enactment lane (#166 follow-up) can DELETE the target member when a
    # high-confidence ``forget_*`` / ``correct_*`` verdict auto-applies.
    # Empty for non-source-attributed escalations (the enactment lane then
    # no-ops). Stored as strings to keep the dataclass trivially copyable.
    members: list[str] = field(default_factory=list)


# Per-model rate table (issue #247). Maps a model-id PREFIX to its
# (input, output) price in USD per million tokens. Matched by LONGEST
# prefix so dated ids (``claude-haiku-4-5-20251001``) resolve to the
# right family. Source: Anthropic public pricing
# (https://www.anthropic.com/pricing), as of 2026-06-17.
#
# PERIODIC REVIEW: these are hard-coded Anthropic public list prices captured
# on the date above. They do NOT auto-update — Anthropic price changes (new
# model families, rate cuts, tier changes) require a manual edit HERE. This
# constant is the single update site for model pricing; nothing else in the
# codebase hard-codes per-MTok rates. Re-check against the pricing page when a
# new Claude generation ships or when cost estimates drift from billing.
_MODEL_RATES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
}

# Blended fallback rate (USD per million tokens) for tokens accumulated
# WITHOUT a model tag, or tagged with an id that matches no prefix above
# (e.g. routed via a proxy). Matches the historical pre-#247 estimate.
_BLENDED_INPUT_USD_PER_MTOK = 1.50
_BLENDED_OUTPUT_USD_PER_MTOK = 7.50


def _rates_for_model(model: str | None) -> tuple[float, float]:
    """Return ``(input, output)`` USD/MTok for *model* (longest-prefix match).

    Untagged (``None``) or unknown ids fall back to the blended rate.
    """
    if model:
        best: tuple[float, float] | None = None
        best_len = -1
        for prefix, rates in _MODEL_RATES_USD_PER_MTOK.items():
            if model.startswith(prefix) and len(prefix) > best_len:
                best, best_len = rates, len(prefix)
        if best is not None:
            return best
    return (_BLENDED_INPUT_USD_PER_MTOK, _BLENDED_OUTPUT_USD_PER_MTOK)


@dataclass
class TokenUsage:
    """Accumulated API token usage for a pipeline run."""

    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0
    # Prompt-caching counters (issue #230). ``input_tokens`` from the API
    # excludes cached tokens, so these accumulate separately: creation is
    # billed at ~1.25x the input rate, reads at ~0.1x.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # Batch API counters (issue #236). Batch traffic is folded into the
    # main counters above (so totals and the run-summary log include it)
    # AND tracked separately here so ``estimated_cost_usd`` can apply the
    # Batch API's 50% discount to exactly the batch-attributed share.
    batch_input_tokens: int = 0
    batch_output_tokens: int = 0
    batch_cache_creation_input_tokens: int = 0
    batch_cache_read_input_tokens: int = 0
    # Per-model attribution (issue #247). Keyed by the model-id string the
    # call site passed to ``messages.create``; each value tracks the same
    # six counters as the scalar fields above but for THAT model's share.
    # The scalar fields stay authoritative for totals/run-summary; this
    # dict is the additive subset that carries a model tag, letting
    # ``estimated_cost_usd`` price tagged tokens per model and fall back to
    # the blended rate for the untagged remainder. Excluded from ``repr``
    # to keep run-summary logging concise.
    per_model: dict[str, dict[str, int]] = field(default_factory=dict, repr=False)
    # Subscription-covered flag (issue #330). When the run is served by the
    # ``claude-cli`` provider, the operator's Claude Code SUBSCRIPTION pays for
    # the tokens — there is no per-token API bill. Token COUNTS still
    # accumulate (and appear in the run summary) exactly as for the API
    # backend, but ``estimated_cost_usd`` reports $0 rather than pricing the
    # tokens at list rates. Set once at run start by the caller that resolved
    # the provider; defaults False so the API backend is unchanged.
    subscription_covered: bool = False

    def _tag_model(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
        *,
        is_batch: bool,
    ) -> None:
        """Accumulate this call's counts into the per-model subset (#247)."""
        bucket = self.per_model.setdefault(
            model,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "batch_input_tokens": 0,
                "batch_output_tokens": 0,
                "batch_cache_creation_input_tokens": 0,
                "batch_cache_read_input_tokens": 0,
            },
        )
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_creation_input_tokens"] += cache_creation_input_tokens
        bucket["cache_read_input_tokens"] += cache_read_input_tokens
        if is_batch:
            bucket["batch_input_tokens"] += input_tokens
            bucket["batch_output_tokens"] += output_tokens
            bucket["batch_cache_creation_input_tokens"] += cache_creation_input_tokens
            bucket["batch_cache_read_input_tokens"] += cache_read_input_tokens

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        model: str | None = None,
    ) -> None:
        """Record tokens from one API call.

        *model* (issue #247) is the serving model-id; when given, the
        counts are additionally attributed to that model for per-model
        cost estimation. Untagged calls fall back to the blended rate.
        """
        self.add_tokens(
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
            model=model,
        )
        self.api_calls += 1

    def add_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        model: str | None = None,
    ) -> None:
        """Accumulate token counters WITHOUT counting an API call (#239).

        For callees whose orchestrating call site counts ``api_calls``
        separately (attempt counting — e.g. the merge-phase detector/
        resolver loop and the #188 reresolve pass): the call site bumps
        ``api_calls`` before the request; the callee lands the response's
        token + cache counts here once they are known.

        *model* (issue #247) optionally tags the serving model-id for
        per-model cost attribution.
        """
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_creation_input_tokens += cache_creation_input_tokens
        self.cache_read_input_tokens += cache_read_input_tokens
        if model:
            self._tag_model(
                model,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                is_batch=False,
            )

    def add_batch_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        model: str | None = None,
    ) -> None:
        """Accumulate token counters from a Batch API result (#236).

        Folds the counts into the main counters (so ``total_tokens`` and
        the run-summary line include batch traffic) and additionally into
        the batch-attributed counters so ``estimated_cost_usd`` applies
        the Batch API's 50% discount. Does NOT bump ``api_calls`` — batch
        call sites count one attempt per request at batch-assembly time
        (budget enforcement point, mirroring :meth:`add_tokens`'s
        attempt-counting contract from #239).

        *model* (issue #247) optionally tags the serving model-id; the
        batch share is attributed per model so the 50% discount composes
        with that model's rates.
        """
        # Accumulate into the scalar + per-model counters once (untagged
        # remainder stays blended); add_tokens with model=None here so the
        # batch share is tagged via _tag_model below with is_batch=True.
        self.add_tokens(
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
        )
        self.batch_input_tokens += input_tokens
        self.batch_output_tokens += output_tokens
        self.batch_cache_creation_input_tokens += cache_creation_input_tokens
        self.batch_cache_read_input_tokens += cache_read_input_tokens
        if model:
            self._tag_model(
                model,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                is_batch=True,
            )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @staticmethod
    def _cost_for(
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
        batch_input_tokens: int,
        batch_output_tokens: int,
        batch_cache_creation_input_tokens: int,
        batch_cache_read_input_tokens: int,
        rates_usd_per_mtok: tuple[float, float],
    ) -> float:
        """Price one model's share at *rates*, composing cache + batch (#247).

        ``input_tokens`` from the API excludes cached tokens, so the cache
        counters are folded in at the documented multipliers (#239): cache
        writes bill at 1.25x the input rate, cache reads at ~0.1x. Batch
        API traffic (#236) bills at 50% of the synchronous rate, so half of
        the batch-attributed share is subtracted.
        """
        input_rate = rates_usd_per_mtok[0] / 1_000_000
        output_rate = rates_usd_per_mtok[1] / 1_000_000
        cost = (
            input_tokens * input_rate
            + output_tokens * output_rate
            + cache_creation_input_tokens * input_rate * 1.25
            + cache_read_input_tokens * input_rate * 0.10
        )
        batch_cost = (
            batch_input_tokens * input_rate
            + batch_output_tokens * output_rate
            + batch_cache_creation_input_tokens * input_rate * 1.25
            + batch_cache_read_input_tokens * input_rate * 0.10
        )
        return cost - 0.5 * batch_cost

    @property
    def estimated_cost_usd(self) -> float:
        """Estimate cost with per-model attribution (issue #247).

        Tokens tagged with a known model (via the ``model=`` kwarg on the
        accumulation methods) price at that model's rates from
        :data:`_MODEL_RATES_USD_PER_MTOK`, matched by longest id prefix.
        Tokens accumulated WITHOUT a model tag — or tagged with an id that
        matches no known prefix (e.g. routed through a proxy) — fall back
        to the blended rate ($1.50/M input, $7.50/M output). The cache
        multipliers (#239) and the Batch API 50% discount (#236) compose
        unchanged per model.

        Caveat: untagged/unknown-model traffic is still only approximated
        at the blended rate; it cannot be attributed to a specific model.

        Subscription-covered runs (issue #330 ``claude-cli`` backend) short-
        circuit to $0: the operator's Claude Code subscription pays for the
        tokens, so pricing them at API list rates would be wrong. The token
        COUNTS remain in the accumulators and the run summary.
        """
        if self.subscription_covered:
            return 0.0
        total = 0.0
        # Per-model tagged share at each model's own rates.
        tagged = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "batch_input_tokens": 0,
            "batch_output_tokens": 0,
            "batch_cache_creation_input_tokens": 0,
            "batch_cache_read_input_tokens": 0,
        }
        for model, bucket in self.per_model.items():
            for key in tagged:
                tagged[key] += bucket.get(key, 0)
            total += self._cost_for(
                bucket.get("input_tokens", 0),
                bucket.get("output_tokens", 0),
                bucket.get("cache_creation_input_tokens", 0),
                bucket.get("cache_read_input_tokens", 0),
                bucket.get("batch_input_tokens", 0),
                bucket.get("batch_output_tokens", 0),
                bucket.get("batch_cache_creation_input_tokens", 0),
                bucket.get("batch_cache_read_input_tokens", 0),
                _rates_for_model(model),
            )
        # Untagged remainder (scalar totals minus the tagged subset) priced
        # at the blended rate. Clamped at 0 so a hypothetical double-count
        # can never make the remainder negative.
        blended_rates = (_BLENDED_INPUT_USD_PER_MTOK, _BLENDED_OUTPUT_USD_PER_MTOK)
        total += self._cost_for(
            max(self.input_tokens - tagged["input_tokens"], 0),
            max(self.output_tokens - tagged["output_tokens"], 0),
            max(
                self.cache_creation_input_tokens
                - tagged["cache_creation_input_tokens"],
                0,
            ),
            max(self.cache_read_input_tokens - tagged["cache_read_input_tokens"], 0),
            max(self.batch_input_tokens - tagged["batch_input_tokens"], 0),
            max(self.batch_output_tokens - tagged["batch_output_tokens"], 0),
            max(
                self.batch_cache_creation_input_tokens
                - tagged["batch_cache_creation_input_tokens"],
                0,
            ),
            max(
                self.batch_cache_read_input_tokens
                - tagged["batch_cache_read_input_tokens"],
                0,
            ),
            blended_rates,
        )
        return total


def cache_usage_counts(response: object) -> tuple[int, int, int, int]:
    """Extract token counts from an Anthropic API response (issue #230).

    Returns ``(input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens)``. Missing or non-int fields coerce to 0 so
    callers can log/accumulate without guarding against older SDK shapes
    or test doubles that omit the cache fields.
    """
    usage = getattr(response, "usage", None)

    def _count(name: str) -> int:
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return (
        _count("input_tokens"),
        _count("output_tokens"),
        _count("cache_creation_input_tokens"),
        _count("cache_read_input_tokens"),
    )


@dataclass
class ProcessingResult:
    """Result of processing one raw file."""

    raw_file: RawFile
    created: list[WikiEntity] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    escalated: list[EscalationItem] = field(default_factory=list)


# --- Schema loading ---


def load_schema_list(schema_path: Path, filename: str) -> list[str]:
    """Load a list of valid values from a schema markdown table.

    Parses standard markdown tables, extracting the first cell from each
    data row. Header and separator rows are skipped.
    """
    fpath = schema_path / filename
    if not fpath.exists():
        return []
    text = fpath.read_text(encoding="utf-8")
    lines = text.splitlines()
    values: list[str] = []
    # Collect separator row indices so we can skip headers
    separator_indices: set[int] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and all(c in "-| " for c in stripped):
            separator_indices.add(i)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # Skip separator rows
        if i in separator_indices:
            continue
        # Skip header rows (the row immediately before a separator)
        if (i + 1) in separator_indices:
            continue
        cells = [c.strip() for c in stripped.split("|")]
        for cell in cells:
            if cell:
                values.append(cell)
                break
    return values


# --- Entity Index ---


class EntityIndex:
    """In-memory index of all wiki entities for name/alias lookup."""

    def __init__(self, wiki_root: Path) -> None:
        self.wiki_root = wiki_root
        self._by_name: dict[str, tuple[str, Path]] = {}
        self._entities: dict[str, dict] = {}
        self._by_uid: dict[str, Path] = {}
        self._entity_format_paths: set[Path] = set()
        self._load()

    def _load(self) -> None:
        for fpath in sorted(self.wiki_root.glob("*.md")):
            if fpath.name.startswith("_"):
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _ = parse_frontmatter(text)
            if not meta:
                continue

            uid = meta.get("uid", "")
            name = meta.get("name", "")
            if not name:
                continue

            key = name.lower()
            self._by_name[key] = (uid or name, fpath)
            if uid:
                self._entities[uid] = meta
                self._by_uid[uid] = fpath
                self._entity_format_paths.add(fpath)

            for alias in meta.get("aliases", []):
                if alias:
                    self._by_name[alias.lower()] = (uid or name, fpath)

    def lookup(self, name: str) -> tuple[str, Path] | None:
        """Look up by name or alias (case-insensitive). Returns (uid_or_name, path) or None."""
        return self._by_name.get(name.lower())

    def get_by_uid(self, uid: str) -> Path | None:
        """Look up entity file path by UID. Returns None if not found."""
        return self._by_uid.get(uid)

    def has_entity_format(self, path: Path) -> bool:
        """Check if a wiki page uses the full entity template format (has uid field)."""
        return path in self._entity_format_paths

    def register(self, entity: WikiEntity) -> None:
        """Add a newly created entity to the index."""
        key = entity.name.lower()
        path = self.wiki_root / entity.filename
        self._by_name[key] = (entity.uid, path)
        self._entities[entity.uid] = {
            "uid": entity.uid,
            "type": entity.type,
            "name": entity.name,
        }
        self._by_uid[entity.uid] = path
        self._entity_format_paths.add(path)
        for alias in entity.aliases:
            if alias:
                self._by_name[alias.lower()] = (entity.uid, path)

    def __len__(self) -> int:
        """Number of unique name/alias keys indexed."""
        return len(self._by_name)

    def items(self) -> "ItemsView[str, tuple[str, Path]]":
        """Iterate over ``(name_or_alias_key, (uid_or_name, path))`` pairs.

        Replaces direct access to ``_by_name`` from callers that need to
        walk the index (e.g. tier-based scans). Returns a live view — do
        not mutate the index mid-iteration.
        """
        return self._by_name.items()

    def __iter__(self) -> "Iterator[str]":
        """Iterate over indexed name/alias keys."""
        return iter(self._by_name)
