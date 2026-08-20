# SPDX-License-Identifier: Apache-2.0
"""Dimension registry + kernel dimensions (issue athenaeum#714) — L1/L2.

Root of the memory-model v6 dimension chain (child of epic athenaeum#709; blocks
athenaeum#715, athenaeum#716, athenaeum#719). A **dimension** is a declared, typed axis on
which a claim takes a coordinate. Two claims' coordinates on one axis compare
to exactly one of five relations (:class:`Relation`): ``equal | contains |
overlaps | disjoint | unknown``. This module owns the registry (parsing +
validation of the ``dimensions:`` config block), the four kind-comparators
(``interval | hierarchy | enum | identity``), the six built-in kernel
dimensions, the write-side origin/claimed-scope split, intake temporal
validation, and the ``backfill -> enforced`` lifecycle flip (wired to
athenaeum#712's targeted stale-marking). It does **not** build the five-verdict
comparator that will eventually consume this algebra — that is explicitly a
separate, future child of athenaeum#709 (see that issue's Out-of-scope section).
This PR's consumer is coordinate stamping at write time
(:class:`athenaeum.models.WikiEntity.__post_init__`) plus the
``athenaeum dimensions show|compare`` CLI surface (:mod:`athenaeum._cmd_dimensions`).

Relation direction (a documented design decision, not literally in the issue
text): every comparator here is intentionally **undirected** — ``CONTAINS``
means "one side's region strictly contains the other's," not "``a`` contains
``b`` specifically." The issue's own examples ("prefix subsumption gives
contains") never distinguish direction, and the five-value vocabulary has no
"contained-by" counterpart. A future caller that needs direction (e.g. which
side is the more specific claim) should compare the two raw coordinate values
directly rather than lean on the relation value for that.

**Ambiguity resolved here (dispatch aperture, reversible, recorded per the
orchestrator's ambiguity policy):** the ``scope`` and ``subject`` kernel
dimensions have no existing frontmatter reader the way valid-time/
observed-time/memory-class do. Three NEW frontmatter keys are introduced:
``claimed_scope`` (the ``scope`` dimension's coordinate), ``origin_scope``
(provenance — see the write-discipline section below), and ``subject`` (the
``subject`` dimension's coordinate). ``recorded_at`` is a fourth new key (the
``recorded-time`` dimension). None of the four collide with any existing
frontmatter key.

The obvious alternative — reusing the *existing* ``scope:`` frontmatter key
(``schemas.WikiBase.scope``, issue athenaeum#434, a free-text "where this axiom
applies" scalar with zero enforcement today) — was considered and REJECTED:
``scope:`` is already read as a *different, incompatible shape* (a nested
``{org, locale}`` dict) by two live consumers, :mod:`athenaeum.scoped_claims`
(issue athenaeum#329) and :func:`athenaeum.contradictions` (its scope-block
advisory-context line). Stacking a THIRD, string-shaped reader onto the same
key would not "match existing frontmatter conventions" so much as compound an
existing collision this issue did not create and is not chartered to fix. This
task's own scope boundary also does not authorize reading the live
``~/knowledge`` store to check whether any page already sets ``scope:`` in
either shape, so the collision's real blast radius could not be measured
before deciding. ``claimed_scope`` is zero-collision, additive, and trivially
reversible: retargeting the ``scope`` dimension's reader to a different key is
a one-line change in :func:`coordinate_value` / :func:`parsed_coordinate`
below, with no data migration (coordinates are additive metadata — see the
Lifecycle section's "retiring a dimension" note).

Write-side discipline — origin scope vs. claimed scope (the round-4 blocker
this issue names as its highest-risk item): **origin scope is PROVENANCE**
(``WikiEntity.origin_scope`` — where/what context wrote the claim; a writer
gets this "for free," the same way ``source``/``source_type`` are free).
**Claimed scope is an ASSERTED coordinate** (``WikiEntity.claimed_scope`` —
where the claim APPLIES; must be explicit, never derived). The two are
different dataclass fields and no code path in this module or
:mod:`athenaeum.models` ever copies one into the other — see
``tests/test_dimensions.py::test_origin_scope_never_populates_claimed_scope``
for the regression test the issue's AC demands.

Layering: L1/L2, mirroring :mod:`athenaeum.scoped_claims`'s posture (the
direct conceptual precursor to the ``hierarchy`` comparator here — see that
module's docstring). Imports :mod:`athenaeum.models` (L1, for
``parse_valid_from``/``parse_valid_until``/``parse_observed_at``) and
:mod:`athenaeum.memory_class` (L0, for ``MEMORY_CLASSES``) at module level.

Deliberately does NOT import :mod:`athenaeum.verdicts` anywhere (not even
function-locally — ``tests/test_import_graph_acyclic.py`` counts deferred
imports too): :mod:`athenaeum.config` already imports THIS module
(``resolve_dimensions``), and :mod:`athenaeum.verdicts` imports
:mod:`athenaeum.pii`, which imports :mod:`athenaeum.config` — a
``dimensions -> verdicts`` edge would close ``config -> dimensions ->
verdicts -> pii -> config``, a real cycle. :func:`maybe_flip_to_enforced`
instead takes an injected ``on_flip`` callback the CALLER wires to
athenaeum#712's targeted stale-marking, keeping the dependency arrow one-way.
"""

from __future__ import annotations

import logging
import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from athenaeum.memory_class import MEMORY_CLASSES
from athenaeum.models import parse_observed_at, parse_valid_from, parse_valid_until

log = logging.getLogger(__name__)


class DimensionRegistryError(ValueError):
    """Raised when a ``dimensions:`` config block or a dimension entry is invalid.

    Mirrors :class:`athenaeum.screening.ScreeningConfigError`'s shape (a
    leaf-module-defined ``ValueError`` subclass, imported function-locally by
    :func:`athenaeum.config.resolve_dimensions`) so config.py's fail-loud-knob
    convention has one exception type per subsystem, not a shared bare
    ``ValueError``.
    """


class ObservedAfterRecordedError(DimensionRegistryError):
    """Raised when ``observed_at`` is later than ``recorded_at`` — you cannot
    have observed the future (issue athenaeum#714 intake AC)."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Relation:
    """The five comparator outcomes (issue athenaeum#714). Plain string constants
    (not :class:`enum.Enum`) so a relation round-trips through JSON/YAML
    (verdict-ledger ``coords``/CLI output) without an extra ``.value`` at
    every call site — matching how ``verdicts.VERDICT_VALUES`` is a tuple of
    plain strings rather than an enum, for the same reason.
    """

    EQUAL = "equal"
    CONTAINS = "contains"
    OVERLAPS = "overlaps"
    DISJOINT = "disjoint"
    UNKNOWN = "unknown"

    ALL = (EQUAL, CONTAINS, OVERLAPS, DISJOINT, UNKNOWN)


class DimensionKind:
    """The four comparator kinds (issue athenaeum#714)."""

    INTERVAL = "interval"
    HIERARCHY = "hierarchy"
    ENUM = "enum"
    IDENTITY = "identity"

    ALL = (INTERVAL, HIERARCHY, ENUM, IDENTITY)


class NullMeans:
    """Per-dimension null semantics (issue athenaeum#714)."""

    UNIVERSAL = "universal"
    UNKNOWN = "unknown"

    ALL = (UNIVERSAL, UNKNOWN)


class LifecycleState:
    """Dimension lifecycle states (issue athenaeum#714)."""

    BACKFILL = "backfill"
    ENFORCED = "enforced"

    ALL = (BACKFILL, ENFORCED)


#: A claim may write this literal value to EXPLICITLY assert universal,
#: independent of the dimension's configured ``null_means`` (issue athenaeum#714 AC).
UNIVERSAL_MARKER = "*"

_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_VALID_STATIC_ORIGINS = ("builtin", "operator")

#: Default "deep back-date" flag threshold (issue athenaeum#714 intake AC): an
#: ``observed_at`` more than this many days before ``recorded_at`` is flagged
#: for review, not rejected. Two years, matching this repo's other
#: staleness-adjacent defaults being multi-year rather than multi-month.
DEEP_BACKDATE_THRESHOLD_DAYS = 730


# ---------------------------------------------------------------------------
# Dimension + registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dimension:
    """One declared, typed axis (issue athenaeum#714).

    ``applies_to`` is a selector dict (``{frontmatter_key: allowed_value(s)}``)
    bounding which claims carry this axis — see :func:`dimension_applies`.
    ``coverage_threshold`` is consulted only while ``state == backfill`` (see
    :func:`maybe_flip_to_enforced`); it has no effect once ``enforced``.
    """

    name: str
    kind: str
    null_means: str = NullMeans.UNKNOWN
    values: tuple[str, ...] | None = None
    separates: bool = True
    applies_to: dict[str, Any] = field(default_factory=dict)
    state: str = LifecycleState.ENFORCED
    origin: str = "operator"
    since: date | None = None
    coverage_threshold: float = 1.0

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise DimensionRegistryError(
                f"dimension name {self.name!r} must be kebab-case "
                "(lowercase letters/digits, hyphen-separated)"
            )
        if self.kind not in DimensionKind.ALL:
            raise DimensionRegistryError(
                f"dimension {self.name!r}: kind={self.kind!r} invalid; "
                f"expected one of {DimensionKind.ALL}"
            )
        if self.null_means not in NullMeans.ALL:
            raise DimensionRegistryError(
                f"dimension {self.name!r}: null_means={self.null_means!r} "
                f"invalid; expected one of {NullMeans.ALL}"
            )
        if self.kind == DimensionKind.ENUM and not self.values:
            raise DimensionRegistryError(
                f"dimension {self.name!r}: kind=enum requires a non-empty "
                "'values' closed vocabulary"
            )
        if self.state not in LifecycleState.ALL:
            raise DimensionRegistryError(
                f"dimension {self.name!r}: state={self.state!r} invalid; "
                f"expected one of {LifecycleState.ALL}"
            )
        if not isinstance(self.applies_to, dict):
            raise DimensionRegistryError(
                f"dimension {self.name!r}: applies_to must be a mapping"
            )
        if self.origin not in _VALID_STATIC_ORIGINS and not self.origin.startswith(
            "proposed:"
        ):
            raise DimensionRegistryError(
                f"dimension {self.name!r}: origin={self.origin!r} invalid; "
                f"expected 'builtin', 'operator', or 'proposed:<id>'"
            )


@dataclass(frozen=True)
class DimensionRegistry:
    """A validated, immutable set of dimensions (kernel + operator-declared)."""

    dimensions: tuple[Dimension, ...]

    def __post_init__(self) -> None:
        names = [d.name for d in self.dimensions]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise DimensionRegistryError(f"duplicate dimension name(s): {dupes}")

    def get(self, name: str) -> Dimension | None:
        for d in self.dimensions:
            if d.name == name:
                return d
        return None

    def __iter__(self):
        return iter(self.dimensions)

    def __len__(self) -> int:
        return len(self.dimensions)


def _coerce_since(name: str, raw: Any) -> date | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            pass
    raise DimensionRegistryError(
        f"dimension {name!r}: since={raw!r} must be an ISO-8601 date (YYYY-MM-DD)"
    )


def parse_dimension_entry(raw: Any) -> Dimension:
    """Parse ONE ``dimensions:`` list entry into a validated :class:`Dimension`.

    Raises :class:`DimensionRegistryError` with a clear, entry-specific
    message on any malformed field (issue athenaeum#714 AC).
    """
    if not isinstance(raw, dict):
        raise DimensionRegistryError(
            f"dimension entry must be a mapping, got {type(raw).__name__}"
        )
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DimensionRegistryError(
            f"dimension entry missing required non-empty 'name': {raw!r}"
        )
    name = name.strip()

    kind = raw.get("kind")
    if not isinstance(kind, str):
        raise DimensionRegistryError(
            f"dimension {name!r}: 'kind' is required and must be a string"
        )

    null_means = raw.get("null_means", NullMeans.UNKNOWN)
    if not isinstance(null_means, str):
        raise DimensionRegistryError(
            f"dimension {name!r}: 'null_means' must be a string"
        )

    values_raw = raw.get("values")
    values: tuple[str, ...] | None = None
    if values_raw:
        if not isinstance(values_raw, list) or not all(
            isinstance(v, str) for v in values_raw
        ):
            raise DimensionRegistryError(
                f"dimension {name!r}: 'values' must be a list of strings"
            )
        values = tuple(values_raw)

    separates = raw.get("separates", True)
    if not isinstance(separates, bool):
        raise DimensionRegistryError(f"dimension {name!r}: 'separates' must be a bool")

    applies_to = raw.get("applies_to") or {}
    if not isinstance(applies_to, dict):
        raise DimensionRegistryError(f"dimension {name!r}: 'applies_to' must be a mapping")

    state = raw.get("state", LifecycleState.ENFORCED)
    if not isinstance(state, str):
        raise DimensionRegistryError(f"dimension {name!r}: 'state' must be a string")

    origin = raw.get("origin", "operator")
    if not isinstance(origin, str):
        raise DimensionRegistryError(f"dimension {name!r}: 'origin' must be a string")

    coverage_threshold = raw.get("coverage_threshold", 1.0)
    try:
        coverage_threshold = float(coverage_threshold)
    except (TypeError, ValueError):
        raise DimensionRegistryError(
            f"dimension {name!r}: 'coverage_threshold' must be a number"
        ) from None

    since = _coerce_since(name, raw.get("since"))

    return Dimension(
        name=name,
        kind=kind,
        null_means=null_means,
        values=values,
        separates=separates,
        applies_to=applies_to,
        state=state,
        origin=origin,
        since=since,
        coverage_threshold=coverage_threshold,
    )


def build_registry(dimensions_config: Any) -> DimensionRegistry:
    """Build the full registry: kernel dimensions + validated operator entries.

    Kernel dimensions are always present and are NOT deletable/overridable —
    an operator entry that reuses a kernel dimension's name is a config error
    (issue athenaeum#714 AC: "Kernel dimensions (built in, not deletable)").
    ``dimensions_config`` is the raw ``dimensions:`` YAML value (a list, or
    ``None``/absent for a kernel-only registry).
    """
    entries: list[Dimension] = list(KERNEL_DIMENSIONS)
    kernel_names = {d.name for d in KERNEL_DIMENSIONS}
    if dimensions_config:
        if not isinstance(dimensions_config, list):
            raise DimensionRegistryError(
                "dimensions: must be a list of dimension entries, got "
                f"{type(dimensions_config).__name__}"
            )
        for raw in dimensions_config:
            dim = parse_dimension_entry(raw)
            if dim.name in kernel_names:
                raise DimensionRegistryError(
                    f"dimension {dim.name!r} collides with a kernel dimension "
                    "name; kernel dimensions are builtin and cannot be "
                    "redeclared or deleted"
                )
            entries.append(dim)
    return DimensionRegistry(dimensions=tuple(entries))


# ---------------------------------------------------------------------------
# Kernel dimensions (issue athenaeum#714 — built in, not deletable)
# ---------------------------------------------------------------------------

RECORDED_TIME = Dimension(
    name="recorded-time",
    kind=DimensionKind.INTERVAL,
    null_means=NullMeans.UNKNOWN,
    separates=False,  # precedence-only, never a separator
    origin="builtin",
)
OBSERVED_TIME = Dimension(
    name="observed-time",
    kind=DimensionKind.INTERVAL,
    null_means=NullMeans.UNKNOWN,
    separates=False,  # sequencer
    origin="builtin",
)
VALID_TIME = Dimension(
    name="valid-time",
    kind=DimensionKind.INTERVAL,
    null_means=NullMeans.UNIVERSAL,
    separates=True,
    origin="builtin",
)
SCOPE = Dimension(
    name="scope",
    kind=DimensionKind.HIERARCHY,
    null_means=NullMeans.UNIVERSAL,
    separates=True,
    origin="builtin",
)
SUBJECT = Dimension(
    name="subject",
    kind=DimensionKind.IDENTITY,
    null_means=NullMeans.UNKNOWN,
    separates=True,
    origin="builtin",
)
#: Issue athenaeum#972 disposition (2026-08-20, PR review comment on athenaeum#714):
#: ships at ``backfill`` — post-mechanical + classifier-pass coverage is real
#: but thin for ``decision``/``procedure`` — never ``enforced`` at ship. A
#: future coverage-threshold flip (see :func:`maybe_flip_to_enforced`) is the
#: intended path to ``enforced``, not a code change here.
MEMORY_CLASS = Dimension(
    name="memory-class",
    kind=DimensionKind.ENUM,
    null_means=NullMeans.UNKNOWN,
    values=tuple(sorted(MEMORY_CLASSES)),
    separates=True,
    state=LifecycleState.BACKFILL,
    origin="builtin",
)

KERNEL_DIMENSIONS: tuple[Dimension, ...] = (
    RECORDED_TIME,
    OBSERVED_TIME,
    VALID_TIME,
    SCOPE,
    SUBJECT,
    MEMORY_CLASS,
)

#: Athenaeum ships ONLY the kernel dimensions (issue athenaeum#714 AC:
#: "engagement/repo/maturity/environment... are deployment-declared examples
#: in the docs, not shipped defaults"). A fresh install with no ``dimensions:``
#: config key gets exactly this registry.
DEFAULT_REGISTRY = DimensionRegistry(dimensions=KERNEL_DIMENSIONS)


# ---------------------------------------------------------------------------
# Null / universal-marker handling shared across kind comparators
# ---------------------------------------------------------------------------


def _null_relation(a_is_none: bool, b_is_none: bool, null_means: str) -> str | None:
    """Return the relation dictated by null handling, or ``None`` to mean
    "both sides are present — proceed with the kind-specific comparison."

    Issue athenaeum#714 AC: two claims both null on a dimension are NOT separable
    by it (a dimension separates only pairs where at least one side carries a
    coordinate) -> UNKNOWN regardless of ``null_means``. A single null side
    resolves per ``null_means``: ``universal`` -> the null side is understood
    to contain any value (CONTAINS); ``unknown`` -> UNKNOWN.
    """
    if a_is_none and b_is_none:
        return Relation.UNKNOWN
    if a_is_none or b_is_none:
        return Relation.CONTAINS if null_means == NullMeans.UNIVERSAL else Relation.UNKNOWN
    return None


def _universal_marker_relation(a: str, b: str) -> str | None:
    """Explicit ``*`` handling (issue athenaeum#714 AC), independent of ``null_means``."""
    a_u = a == UNIVERSAL_MARKER
    b_u = b == UNIVERSAL_MARKER
    if a_u and b_u:
        return Relation.EQUAL
    if a_u or b_u:
        return Relation.CONTAINS
    return None


# ---------------------------------------------------------------------------
# Comparators — one per kind (issue athenaeum#714 AC: each unit-tested across its
# relation space; see tests/test_dimensions.py)
# ---------------------------------------------------------------------------


def compare_interval(
    a: tuple[date | None, date | None] | None,
    b: tuple[date | None, date | None] | None,
    *,
    null_means: str = NullMeans.UNKNOWN,
) -> str:
    """Half-open ``[from, until)`` interval comparator (issue athenaeum#714 AC).

    ``a``/``b`` are ``(from, until)`` tuples with an EXCLUSIVE ``until`` (or
    ``None`` for an open bound on that side); pass ``None`` for the whole
    tuple to mean "this dimension is null for this side" (handled per
    *null_means*), or ``(None, None)`` for an explicit, fully-open interval.
    An **instant** (a single date, e.g. observed-time/recorded-time) is
    represented as ``(d, d + 1 day)`` — "instants by containment" per the
    issue's comparators-by-kind summary; no special-casing is needed since an
    instant is just a one-day-wide interval under this algebra.

    Half-open is what makes ABUTTING windows compare DISJOINT rather than a
    zero-width OVERLAPS: an interval's own ``until`` is the first EXCLUDED
    instant, so ``a_until <= b_from`` (not strict ``<``) is disjoint. This is
    a DIFFERENT algebra than the rest of this repo's ``valid_until`` (which is
    documented INCLUSIVE — see ``models.py``'s validity-window comments); this
    function operates purely on the exclusive-bound tuples callers pass in.
    Kernel-dimension coordinate builders (:func:`parsed_coordinate`) are
    responsible for converting the inclusive on-disk ``valid_until`` to an
    exclusive bound before calling this — see there for exactly where that
    conversion happens; the on-disk field and every OTHER reader of it are
    unchanged by this issue.
    """
    null_rel = _null_relation(a is None, b is None, null_means)
    if null_rel is not None:
        return null_rel
    assert a is not None and b is not None
    a_from, a_until = a
    b_from, b_until = b

    if a_until is not None and b_from is not None and a_until <= b_from:
        return Relation.DISJOINT
    if b_until is not None and a_from is not None and b_until <= a_from:
        return Relation.DISJOINT

    if a_from == b_from and a_until == b_until:
        return Relation.EQUAL

    a_contains_b = (a_from is None or (b_from is not None and b_from >= a_from)) and (
        a_until is None or (b_until is not None and b_until <= a_until)
    )
    b_contains_a = (b_from is None or (a_from is not None and a_from >= b_from)) and (
        b_until is None or (a_until is not None and a_until <= b_until)
    )
    if a_contains_b or b_contains_a:
        return Relation.CONTAINS
    return Relation.OVERLAPS


def compare_hierarchy(
    a: str | None,
    b: str | None,
    *,
    null_means: str = NullMeans.UNKNOWN,
    separator: str = "/",
) -> str:
    """Prefix-tree comparator (issue athenaeum#714 AC).

    Prefix subsumption (``a`` is an ancestor-or-descendant of ``b``, or they
    are equal) gives CONTAINS/EQUAL; siblings (neither a prefix of the other)
    give DISJOINT. Case-folded, whitespace-trimmed, matching
    :class:`athenaeum.scoped_claims.TreeDimension.normalize`'s convention.
    """
    null_rel = _null_relation(a is None, b is None, null_means)
    if null_rel is not None:
        return null_rel
    assert a is not None and b is not None
    uni_rel = _universal_marker_relation(a, b)
    if uni_rel is not None:
        return uni_rel

    a_n, b_n = a.strip().lower(), b.strip().lower()
    if a_n == b_n:
        return Relation.EQUAL
    a_parts = a_n.split(separator)
    b_parts = b_n.split(separator)
    a_ancestor_of_b = b_parts[: len(a_parts)] == a_parts
    b_ancestor_of_a = a_parts[: len(b_parts)] == b_parts
    if a_ancestor_of_b or b_ancestor_of_a:
        return Relation.CONTAINS
    return Relation.DISJOINT


def compare_enum(
    a: str | None,
    b: str | None,
    *,
    null_means: str = NullMeans.UNKNOWN,
) -> str:
    """Closed-vocabulary comparator (issue athenaeum#714 AC): same -> EQUAL,
    different -> DISJOINT."""
    null_rel = _null_relation(a is None, b is None, null_means)
    if null_rel is not None:
        return null_rel
    assert a is not None and b is not None
    uni_rel = _universal_marker_relation(a, b)
    if uni_rel is not None:
        return uni_rel
    return Relation.EQUAL if a == b else Relation.DISJOINT


def compare_identity(
    a: str | None,
    b: str | None,
    *,
    null_means: str = NullMeans.UNKNOWN,
    ratified: bool = False,
) -> str:
    """Identity comparator (issue athenaeum#714 AC).

    Same value -> EQUAL. Different values -> DISJOINT **only when
    ``ratified=True``** (distinct uids each backed by human confirmation,
    independent provenance chains, or a prior ledgered human verdict) —
    otherwise UNKNOWN. There is deliberately NO numeric/confidence parameter
    here: "subjects never separate on a model-reported scalar — no confidence
    threshold, ever" is enforced by this function simply not accepting one;
    the caller must supply an explicit boolean backed by the AC's named
    evidence classes, never a score compared against a cutoff.
    """
    null_rel = _null_relation(a is None, b is None, null_means)
    if null_rel is not None:
        return null_rel
    assert a is not None and b is not None
    uni_rel = _universal_marker_relation(a, b)
    if uni_rel is not None:
        return uni_rel
    if a == b:
        return Relation.EQUAL
    return Relation.DISJOINT if ratified else Relation.UNKNOWN


def compare(dimension: Dimension, a: Any, b: Any, **kwargs: Any) -> str:
    """Dispatch to the comparator matching ``dimension.kind``."""
    if dimension.kind == DimensionKind.INTERVAL:
        return compare_interval(a, b, null_means=dimension.null_means)
    if dimension.kind == DimensionKind.HIERARCHY:
        return compare_hierarchy(a, b, null_means=dimension.null_means)
    if dimension.kind == DimensionKind.ENUM:
        return compare_enum(a, b, null_means=dimension.null_means)
    if dimension.kind == DimensionKind.IDENTITY:
        return compare_identity(
            a, b, null_means=dimension.null_means, ratified=bool(kwargs.get("ratified", False))
        )
    raise DimensionRegistryError(f"dimension {dimension.name!r}: unknown kind {dimension.kind!r}")


def can_separate(dimension: Dimension, relation: str) -> bool:
    """True when *relation*, on *dimension*, may contribute to a DISTINCT
    verdict (issue athenaeum#714 AC).

    Separators (``separates=True``) partition territory: a DISJOINT relation
    on a separator means the two claims occupy different territory and
    cannot conflict. Sequencers (``separates=False``, e.g. recorded-time/
    observed-time) order beliefs about ONE territory — even a DISJOINT
    relation there never separates; it only feeds supersession ordering
    (out of scope here — the future comparator's job). Without this gate,
    every standing-state update in the corpus would exit DISTINCT and stale
    facts would accumulate as live.
    """
    return dimension.separates and relation == Relation.DISJOINT


# ---------------------------------------------------------------------------
# applies_to — blast-radius bounding (issue athenaeum#714 AC)
# ---------------------------------------------------------------------------


def dimension_applies(dimension: Dimension, meta: Mapping[str, Any] | None) -> bool:
    """True when *meta* is within *dimension*'s ``applies_to`` selector.

    An empty ``applies_to`` (the default) applies to every claim. Each
    selector entry is ``{frontmatter_key: allowed_value_or_list}``; ALL
    entries must match for the dimension to apply (conjunctive selector).
    A claim outside the selector is treated as NOT carrying the axis at all
    (see :func:`compare_dimension`) — "ratifying a CRM-only axis must never
    touch dev-rig pages."
    """
    if not dimension.applies_to:
        return True
    if not isinstance(meta, Mapping):
        return False
    for key, allowed in dimension.applies_to.items():
        if not isinstance(allowed, (list, tuple, set, frozenset)):
            allowed = (allowed,)
        if meta.get(key) not in allowed:
            return False
    return True


# ---------------------------------------------------------------------------
# Coordinate reading — bind kernel dimensions to their frontmatter keys
# ---------------------------------------------------------------------------


def _coerce_date_or_none(value: object) -> date | None:
    """Fail-open ISO date/datetime coercion, self-contained to this module.

    Deliberately NOT importing ``athenaeum.models._coerce_iso_date`` (private
    to that module by leading-underscore convention) — this small duplicate
    keeps :mod:`athenaeum.dimensions` independently testable, and matches
    this repo's fail-open posture (malformed => ``None``, never raises).
    Accepts a bare ``YYYY-MM-DD`` date or a full ISO datetime string (for
    ``recorded_at``, which is stamped with second precision).
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        v = value.strip()
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            try:
                return datetime.fromisoformat(v).date()
            except ValueError:
                log.debug("dimensions: unparseable date-ish value %r; treating as absent", v)
                return None
    return None


def coordinate_value(dimension: Dimension, meta: Mapping[str, Any] | None) -> Any:
    """Read *dimension*'s RAW (unparsed) coordinate value from frontmatter."""
    if not isinstance(meta, Mapping):
        return None
    if dimension.name == "recorded-time":
        return meta.get("recorded_at")
    if dimension.name == "observed-time":
        return meta.get("observed_at")
    if dimension.name == "valid-time":
        vf, vu = meta.get("valid_from"), meta.get("valid_until")
        return {"valid_from": vf, "valid_until": vu} if (vf or vu) else None
    if dimension.name == "scope":
        return meta.get("claimed_scope")
    if dimension.name == "subject":
        return meta.get("subject")
    if dimension.name == "memory-class":
        return meta.get("memory_class")
    # Deployment-declared (non-kernel) dimensions read their own bare name.
    return meta.get(dimension.name)


def parsed_coordinate(dimension: Dimension, meta: Mapping[str, Any] | None) -> Any:
    """Read + parse *dimension*'s coordinate into the shape its comparator expects.

    Interval kinds return an EXCLUSIVE-``until`` ``(from, until)`` tuple (or
    ``None``); hierarchy/enum/identity return a ``str`` (or ``None``).
    """
    if dimension.name == "valid-time":
        vf = parse_valid_from(meta)
        vu = parse_valid_until(meta)
        if vf is None and vu is None:
            return None
        # The on-disk ``valid_until`` is INCLUSIVE (repo-wide convention,
        # models.py). This issue's interval algebra is half-open — convert
        # here, at the coordinate boundary, so the on-disk field and every
        # OTHER existing reader (validity_windows_disjoint, ScopeTree, ...)
        # stay byte-identical and semantically unchanged.
        until_exclusive = vu + timedelta(days=1) if vu is not None else None
        return (vf, until_exclusive)
    if dimension.name == "observed-time":
        # parse_observed_at is typed dict[str, object] | None (narrower than
        # this function's Mapping[str, Any] | None) — coerce at the call
        # boundary rather than widen the shared models.py signature.
        d = parse_observed_at(dict(meta) if meta is not None else None)
        return (d, d + timedelta(days=1)) if d is not None else None
    if dimension.name == "recorded-time":
        d = _coerce_date_or_none(meta.get("recorded_at") if isinstance(meta, Mapping) else None)
        return (d, d + timedelta(days=1)) if d is not None else None
    if dimension.kind in (DimensionKind.HIERARCHY, DimensionKind.ENUM, DimensionKind.IDENTITY):
        raw = coordinate_value(dimension, meta)
        return str(raw) if raw is not None else None
    return None


def compare_dimension(
    dimension: Dimension,
    meta_a: Mapping[str, Any] | None,
    meta_b: Mapping[str, Any] | None,
    **kwargs: Any,
) -> str:
    """Compare two claims on ONE dimension, honoring ``applies_to`` and lifecycle state.

    - Outside ``applies_to`` for EITHER side -> UNKNOWN (not consulted at all).
    - ``state == backfill`` -> UNKNOWN unless BOTH sides carry a coordinate
      (issue athenaeum#714 AC: "the comparator consults the dimension only for
      pairs where BOTH sides carry coordinates").
    - Otherwise -> the kind comparator, honoring ``null_means``.
    """
    if not (dimension_applies(dimension, meta_a) and dimension_applies(dimension, meta_b)):
        return Relation.UNKNOWN
    a = parsed_coordinate(dimension, meta_a)
    b = parsed_coordinate(dimension, meta_b)
    if dimension.state == LifecycleState.BACKFILL and (a is None or b is None):
        return Relation.UNKNOWN
    return compare(dimension, a, b, **kwargs)


# ---------------------------------------------------------------------------
# Write-side discipline: recorded-time stamping (issue athenaeum#714 AC)
# ---------------------------------------------------------------------------


def stamp_recorded_time(now: datetime | None = None) -> str:
    """Return an ISO-8601 timestamp for the recorded-time kernel dimension.

    System transaction time: the CALLER decides *when* to invoke this
    (:meth:`athenaeum.models.WikiEntity.__post_init__`, on first construction
    only — see that method's docstring for why "first only," not "every
    construction") but never *what* value it returns; a writer-supplied
    ``recorded_at`` is never consulted here. Second-precision UTC so claims
    recorded in the same intake batch still order under the corpus's
    documented single-writer assumption (see :mod:`athenaeum.verdicts`'s
    module docstring, "recorded-time single-writer assumption").
    """
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Intake temporal validation (issue athenaeum#714 AC)
# ---------------------------------------------------------------------------


def validate_intake_temporal(
    *,
    observed_at: object,
    recorded_at: object,
    deep_backdate_days: int = DEEP_BACKDATE_THRESHOLD_DAYS,
) -> None:
    """Enforce the athenaeum#714 intake temporal-validation AC.

    Accepts RAW values (``str | date | datetime | None``, the on-disk
    frontmatter shape) and coerces fail-open via :func:`_coerce_date_or_none`
    — callers (e.g. ``schemas.WikiBase``'s model validator) do not need to
    pre-parse. Hard reject: ``observed_at`` later than ``recorded_at`` —
    raises :class:`ObservedAfterRecordedError` ("you cannot have observed
    the future"). Soft flag: ``observed_at`` more than *deep_backdate_days*
    before ``recorded_at`` — emits a :class:`UserWarning` for review; the
    value is kept (fail-open, matching every other temporal parser in this
    repo — see ``models._coerce_iso_date``'s docstring for the same posture).
    A missing/unparseable ``observed_at`` is a no-op (a claim missing a
    coordinate is not rejected — issue athenaeum#714 AC); a missing/unparseable
    ``recorded_at`` falls back to :func:`datetime.date.today` (the anchor
    ``recorded_at`` would be stamped to anyway at construction time — see
    :meth:`athenaeum.models.WikiEntity.__post_init__`).
    """
    observed = _coerce_date_or_none(observed_at)
    recorded = _coerce_date_or_none(recorded_at) or date.today()
    if observed is None:
        return
    if observed > recorded:
        raise ObservedAfterRecordedError(
            f"observed_at={observed.isoformat()} is later than "
            f"recorded_at={recorded.isoformat()} — cannot have observed "
            "the future"
        )
    age_days = (recorded - observed).days
    if age_days > deep_backdate_days:
        warnings.warn(
            f"observed_at={observed.isoformat()} is {age_days} days before "
            f"recorded_at={recorded.isoformat()} — flagged as a deep "
            "back-date for review (issue athenaeum#714)",
            UserWarning,
            stacklevel=2,
        )


# ---------------------------------------------------------------------------
# Lifecycle: backfill -> enforced (issue athenaeum#714 AC), wired to athenaeum#712's
# targeted stale-marking
# ---------------------------------------------------------------------------


def coverage_ratio(populated: int, total_eligible: int) -> float:
    """Fraction of the ``applies_to``-eligible population that already
    carries a coordinate for a dimension. ``total_eligible <= 0`` -> ``1.0``
    (vacuously fully covered — an empty eligible set has nothing left to
    backfill)."""
    if total_eligible <= 0:
        return 1.0
    return populated / total_eligible


def maybe_flip_to_enforced(
    dimension: Dimension,
    *,
    coverage: float,
    on_flip: Callable[[Dimension], int] | None = None,
) -> tuple[Dimension, int]:
    """Flip *dimension* ``backfill`` -> ``enforced`` once *coverage* crosses
    ``dimension.coverage_threshold`` (issue athenaeum#714 AC).

    No-op (returns *dimension* unchanged, ``0`` marked) when the dimension is
    already ``enforced`` or coverage has not yet crossed the threshold. On a
    genuine flip, calls *on_flip* (given the still-``backfill`` dimension)
    and returns its result as the stale-marked count, alongside the
    dimension with ``state=enforced``.

    *on_flip* is INJECTED rather than this module calling
    :mod:`athenaeum.verdicts` directly — :mod:`athenaeum.config`'s
    ``resolve_dimensions`` already imports THIS module, and
    :mod:`athenaeum.verdicts` imports :mod:`athenaeum.pii`, which imports
    :mod:`athenaeum.config`; a direct ``dimensions -> verdicts`` edge here
    would therefore close ``config -> dimensions -> verdicts -> pii ->
    config``, a real import cycle
    (``tests/test_import_graph_acyclic.py`` counts function-local imports
    too, so deferring the import does not avoid it — the cycle is a
    dependency-graph fact, not a timing one). A caller wires athenaeum#712's
    targeted stale-marking as the closure::

        def on_flip(dim: Dimension) -> int:
            reasons = select_stale_for_dimension_change(
                entries, dim.name, changed_ids=changed_ids
            )
            return mark_pairs_stale(wiki_root, reasons, lock=lock) if reasons else 0

        flipped, marked = maybe_flip_to_enforced(dimension, coverage=cov, on_flip=on_flip)

    ``lock`` (an already-acquired :class:`athenaeum.runlock.RunLock`) is the
    caller's responsibility to hold before invoking *on_flip*, per every
    other mutating ``verdicts.py`` call's single-appender contract.
    """
    if dimension.state != LifecycleState.BACKFILL or coverage < dimension.coverage_threshold:
        return dimension, 0
    marked = on_flip(dimension) if on_flip is not None else 0
    return replace(dimension, state=LifecycleState.ENFORCED), marked


def retire_dimension_coordinate(meta: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a copy of *meta* with coordinate *key* RE-NULLED, never deleted.

    Issue athenaeum#714 AC: "Retiring a dimension re-nulls its coordinates.
    Coordinates are additive metadata — never moves, never deletes." The key
    stays present (documented, inert) rather than being removed outright, so
    a future re-adoption of the same dimension name does not silently
    resurrect stale data as if freshly asserted.
    """
    out = dict(meta)
    if key in out:
        out[key] = None
    return out


# ---------------------------------------------------------------------------
# Corpus namespacing (issue athenaeum#714 AC)
# ---------------------------------------------------------------------------


def cross_corpus_compare(
    dim_local: Dimension | None,
    dim_remote: Dimension | None,
    a: Any,
    b: Any,
    *,
    mapping: dict[str, str] | None = None,
    **kwargs: Any,
) -> str:
    """Compare a coordinate across two corpora's registries (issue athenaeum#714 AC).

    ``org:maturity`` and ``personal:maturity`` are DIFFERENT axes unless a
    ratified *mapping* (``{local_dimension_name: remote_dimension_name}``)
    declares a translation. Missing, unmapped, or kind-mismatched dimensions
    degrade to UNKNOWN (underdetermined) — NEVER a false DISJOINT from
    colliding enum vocabularies that happen to share a name or value set.
    """
    if dim_local is None or dim_remote is None:
        return Relation.UNKNOWN
    mapped_name = (mapping or {}).get(dim_local.name)
    if dim_local.name != dim_remote.name and mapped_name != dim_remote.name:
        return Relation.UNKNOWN
    if dim_local.kind != dim_remote.kind:
        return Relation.UNKNOWN
    return compare(dim_local, a, b, **kwargs)


__all__ = [
    "DEEP_BACKDATE_THRESHOLD_DAYS",
    "DEFAULT_REGISTRY",
    "KERNEL_DIMENSIONS",
    "MEMORY_CLASS",
    "OBSERVED_TIME",
    "RECORDED_TIME",
    "SCOPE",
    "SUBJECT",
    "UNIVERSAL_MARKER",
    "VALID_TIME",
    "Dimension",
    "DimensionKind",
    "DimensionRegistry",
    "DimensionRegistryError",
    "LifecycleState",
    "NullMeans",
    "ObservedAfterRecordedError",
    "Relation",
    "build_registry",
    "can_separate",
    "compare",
    "compare_dimension",
    "compare_enum",
    "compare_hierarchy",
    "compare_identity",
    "compare_interval",
    "coordinate_value",
    "coverage_ratio",
    "cross_corpus_compare",
    "dimension_applies",
    "maybe_flip_to_enforced",
    "parse_dimension_entry",
    "parsed_coordinate",
    "retire_dimension_coordinate",
    "stamp_recorded_time",
    "validate_intake_temporal",
]
