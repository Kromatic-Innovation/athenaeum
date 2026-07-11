# SPDX-License-Identifier: Apache-2.0
"""Scoped-claim poset model + three-way overlap verdict (issue #329).

#308 gave claims a TIME dimension (``valid_from`` / ``valid_until``). Time is
one dimension of a general pattern: two claims can BOTH be true when separated
by ORGANIZATIONAL scope (team A's rule vs team B's), SPECIFICITY (an org-wide
default vs a team's local exception), or LOCALE. Without a way to represent
that, scope-separated claims surface as contradictions and get winner-picked.

Formal core (small and testable — the buildable subset of #329's design pass):

- Each dimension is a **poset** with TOP = *unscoped* (absent coordinate).
    * ``org`` / ``locale`` are **trees**: a descendant node ⊑ its ancestor
      (``kromatic/platform`` ⊑ ``kromatic`` ⊑ unscoped;
      ``en-US`` ⊑ ``en`` ⊑ unscoped). "Lower" = MORE specific = covers a
      SMALLER region.
    * ``time`` is **intervals under inclusion**: ``[Apr, Jun]`` ⊑ ``[Jan, Dec]``.
- A claim's **context** is one coordinate per dimension.
- The **meet** (region intersection) is componentwise: interval intersection
  for time; "lower-if-comparable, else empty" for the trees.

The org/locale tree is a small **versioned config** (``scope.org`` /
``scope.locale`` node lists in ``athenaeum.yaml``). Claim authors may NOT mint
scope values that are not in the tree — the hard lesson from Cyc's microtheory
proliferation. An out-of-tree value **fails open to detection**: it is treated
as *unscoped* (adds no constraint) so a typo can never silently hide a claim,
matching the fail-open posture of #308's temporal validity.

Three-way verdict (:func:`scope_comparison`) replaces the binary
conflict/no-conflict split:

- **DISJOINT** — the meet is empty in some dimension → the two claims can never
  both apply, so they cannot contradict. (Generalizes #324's disjoint-validity
  time pre-filter to org/locale.)
- **OVERRIDE** — one context is strictly below the other in the *tree*
  dimensions (org/locale): the specific claim is an exception carving out its
  region; the general claim governs the remainder. Both stay active
  (defeasible-logic specificity — penguins-don't-fly vs birds-fly). Without
  this verdict every org-rule / team-exception pair is a false positive.
- **OVERLAP** — incomparable-but-overlapping (or same-context) → a genuine
  contradiction to resolve or escalate.

DELIBERATELY DEFERRED to the ADR (design-only, not built here):

- **Time-interval nesting does NOT trigger OVERRIDE.** #324 shipped a semantic
  where overlapping-but-nested validity windows still reach the resolver (only
  DISJOINT time short-circuits). Auto-promoting a sub-interval to a silent
  override would change that shipped behavior, so tree-specificity alone drives
  OVERRIDE here; time only contributes to DISJOINT. Whether a bounded-time
  exception should override an always-valid claim is left to the #329 ADR.
- **Recall caller-context filter** (``serve --scope org:...``) and the broader
  team/multi-tenant scope-IDENTITY system are out of scope (#314).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from athenaeum.models import parse_valid_from, parse_valid_until

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tree dimension (org / locale)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TreeDimension:
    """A poset dimension whose values are nodes of a small path-prefix tree.

    ``separator`` joins a node's segments (``"/"`` for ``org``, ``"-"`` for
    ``locale``). A node's ancestors are derived by dropping trailing segments
    (``kromatic/platform`` → ``kromatic`` → *unscoped*; ``en-US`` → ``en`` →
    *unscoped*). ``nodes`` is the closed set of values a claim may declare —
    a value not in the set is not a trusted scope and normalizes to ``None``
    (unscoped / TOP).

    ``None`` always denotes the TOP (unscoped) element: its region is the whole
    dimension.
    """

    name: str
    separator: str
    nodes: frozenset[str]

    def normalize(self, value: Any) -> str | None:
        """Coerce a raw coordinate value to a known node, else ``None`` (TOP).

        Fail-open: an empty, non-string, or OUT-OF-TREE value returns ``None``
        (unscoped) with a debug breadcrumb — a minted/typo'd value adds no
        constraint rather than silently carving a phantom scope. Values are
        compared case-folded and whitespace-trimmed.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            log.debug(
                "scope[%s]: non-string coordinate %r; treating as unscoped",
                self.name,
                value,
            )
            return None
        norm = value.strip().lower()
        if not norm:
            return None
        if norm not in self.nodes:
            log.debug(
                "scope[%s]: value %r is not a declared tree node; treating as "
                "unscoped (authors may not mint scope values, #329)",
                self.name,
                norm,
            )
            return None
        return norm

    def _ancestors_or_self(self, value: str) -> list[str]:
        """Return ``value`` plus every prefix-ancestor, nearest first."""
        out = [value]
        parts = value.split(self.separator)
        for i in range(len(parts) - 1, 0, -1):
            out.append(self.separator.join(parts[:i]))
        return out

    def leq(self, a: str | None, b: str | None) -> bool:
        """True when ``a``'s region ⊆ ``b``'s region (``a`` is at-or-below ``b``).

        TOP (``None``) is the top element: ``x ⊑ None`` for every ``x``, and
        ``None ⊑ b`` only when ``b`` is also TOP. Otherwise ``a ⊑ b`` iff ``b``
        equals ``a`` or is a prefix-ancestor of ``a``.
        """
        if b is None:
            return True
        if a is None:
            return False
        return b in self._ancestors_or_self(a)

    def meet_empty(self, a: str | None, b: str | None) -> bool:
        """True when the regions of ``a`` and ``b`` do not intersect.

        Two subtree regions are either nested (one node an ancestor-or-equal of
        the other → intersection is the lower subtree, non-empty) or disjoint
        (neither comparable → empty). A TOP operand covers everything, so it
        never yields an empty meet.
        """
        if a is None or b is None:
            return False
        return not (self.leq(a, b) or self.leq(b, a))


# ---------------------------------------------------------------------------
# Time dimension (intervals under inclusion)
# ---------------------------------------------------------------------------


def interval_leq(
    a: tuple[date | None, date | None],
    b: tuple[date | None, date | None],
) -> bool:
    """True when interval ``a`` ⊆ interval ``b`` (``a`` no wider than ``b``).

    Bounds are ``(valid_from, valid_until)`` with ``None`` = open (±infinity).
    ``a ⊆ b`` iff ``b``'s lower bound is open OR ``a`` starts no earlier, AND
    ``b``'s upper bound is open OR ``a`` ends no later.
    """
    a_from, a_until = a
    b_from, b_until = b
    lower_ok = b_from is None or (a_from is not None and a_from >= b_from)
    upper_ok = b_until is None or (a_until is not None and a_until <= b_until)
    return lower_ok and upper_ok


def interval_meet_empty(
    a: tuple[date | None, date | None],
    b: tuple[date | None, date | None],
) -> bool:
    """True when intervals ``a`` and ``b`` cannot overlap in time.

    Mirrors :func:`athenaeum.models.validity_windows_disjoint` (issue #324):
    ``valid_until`` is the INCLUSIVE last-valid date, so the comparison is
    strict ``<`` — an interval ending on X and another starting on X share day
    X and are NOT disjoint. Open bounds overlap by default.
    """
    a_from, a_until = a
    b_from, b_until = b
    if a_until is not None and b_from is not None and a_until < b_from:
        return True
    if b_until is not None and a_from is not None and b_until < a_from:
        return True
    return False


# ---------------------------------------------------------------------------
# Scope tree (config-loaded) + coordinate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeCoordinate:
    """One claim's context: a coordinate per scope dimension.

    ``org`` / ``locale`` are normalized tree nodes (``None`` = unscoped/TOP).
    ``valid_from`` / ``valid_until`` are the #308 temporal bounds (``None`` =
    open). Built by :meth:`ScopeTree.coordinate`.
    """

    org: str | None = None
    locale: str | None = None
    valid_from: date | None = None
    valid_until: date | None = None

    @property
    def interval(self) -> tuple[date | None, date | None]:
        return (self.valid_from, self.valid_until)


class ScopeVerdict(Enum):
    """Three-way overlap verdict (issue #329)."""

    DISJOINT = "disjoint"
    OVERRIDE = "override"
    OVERLAP = "overlap"


@dataclass(frozen=True)
class ScopeComparison:
    """Result of :func:`scope_comparison`.

    ``specific`` names the strictly-more-specific side (``"a"`` / ``"b"``) on an
    OVERRIDE verdict, and is ``None`` for DISJOINT / OVERLAP.
    """

    verdict: ScopeVerdict
    specific: str | None = None


class ScopeTree:
    """The versioned org/locale tree loaded from ``athenaeum.yaml``.

    Built via :meth:`from_config`. A fresh install (no ``scope:`` config key)
    yields empty dimensions, so every declared org/locale value normalizes to
    unscoped and scope frontmatter is inert — the single-user default is
    unchanged. Time is always available (it needs no config).
    """

    def __init__(self, org: TreeDimension, locale: TreeDimension) -> None:
        self.org = org
        self.locale = locale

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "ScopeTree":
        """Build a :class:`ScopeTree` from ``config['scope']['org'|'locale']``.

        Each is a list of node strings. Missing / malformed → empty dimension
        (no ``_DEFAULTS`` seed, per issue #231). Node values are case-folded
        and whitespace-trimmed; non-string / empty entries are dropped.
        """
        org_nodes: list[str] = []
        locale_nodes: list[str] = []
        if isinstance(config, dict):
            scope_cfg = config.get("scope")
            if isinstance(scope_cfg, dict):
                org_nodes = cls._clean_nodes(scope_cfg.get("org"))
                locale_nodes = cls._clean_nodes(scope_cfg.get("locale"))
        return cls(
            org=TreeDimension("org", "/", frozenset(org_nodes)),
            locale=TreeDimension("locale", "-", frozenset(locale_nodes)),
        )

    @staticmethod
    def _clean_nodes(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for entry in raw:
            if isinstance(entry, str) and entry.strip():
                out.append(entry.strip().lower())
        return out

    def coordinate(self, meta: dict[str, Any] | None) -> ScopeCoordinate:
        """Parse a member's frontmatter into a normalized :class:`ScopeCoordinate`.

        Reads the nested ``scope: {org:, locale:}`` block (org/locale) and the
        top-level ``valid_from`` / ``valid_until`` (#308, temporal). Unknown
        org/locale values normalize to ``None`` (unscoped); malformed dates
        fail open to ``None`` via the shared #308 parsers.
        """
        org_raw: Any = None
        locale_raw: Any = None
        if isinstance(meta, dict):
            scope_block = meta.get("scope")
            if isinstance(scope_block, dict):
                org_raw = scope_block.get("org")
                locale_raw = scope_block.get("locale")
        return ScopeCoordinate(
            org=self.org.normalize(org_raw),
            locale=self.locale.normalize(locale_raw),
            valid_from=parse_valid_from(meta),
            valid_until=parse_valid_until(meta),
        )


# ---------------------------------------------------------------------------
# Meet / leq / three-way verdict
# ---------------------------------------------------------------------------


def scope_meet_empty(a: ScopeCoordinate, b: ScopeCoordinate, tree: ScopeTree) -> bool:
    """True when the componentwise meet is empty in ANY dimension.

    Empty meet ⇒ the two contexts never co-apply ⇒ no contradiction possible.
    """
    return (
        tree.org.meet_empty(a.org, b.org)
        or tree.locale.meet_empty(a.locale, b.locale)
        or interval_meet_empty(a.interval, b.interval)
    )


def scope_leq(a: ScopeCoordinate, b: ScopeCoordinate, tree: ScopeTree) -> bool:
    """True when ``a``'s context ⊆ ``b``'s context in EVERY dimension.

    The full product-poset order (org AND locale AND time). Provided for
    completeness/tests; the OVERRIDE verdict uses :func:`tree_leq` (org+locale
    only) so time-interval nesting does not silently drive an override — see the
    module docstring's deferred-design note.
    """
    return (
        tree.org.leq(a.org, b.org)
        and tree.locale.leq(a.locale, b.locale)
        and interval_leq(a.interval, b.interval)
    )


def tree_leq(a: ScopeCoordinate, b: ScopeCoordinate, tree: ScopeTree) -> bool:
    """True when ``a`` ⊆ ``b`` in the TREE dimensions (org AND locale) only."""
    return tree.org.leq(a.org, b.org) and tree.locale.leq(a.locale, b.locale)


def scope_comparison(
    a: ScopeCoordinate, b: ScopeCoordinate, tree: ScopeTree
) -> ScopeComparison:
    """Return the three-way overlap verdict for two claim contexts.

    - Empty meet (any dimension) → :attr:`ScopeVerdict.DISJOINT`.
    - Else strictly-below in the org/locale trees → :attr:`ScopeVerdict.OVERRIDE`
      (``specific`` = the narrower side).
    - Else → :attr:`ScopeVerdict.OVERLAP` (same-context or incomparable-but-
      overlapping — a genuine contradiction).
    """
    if scope_meet_empty(a, b, tree):
        return ScopeComparison(ScopeVerdict.DISJOINT, None)
    a_le_b = tree_leq(a, b, tree)
    b_le_a = tree_leq(b, a, tree)
    if a_le_b and not b_le_a:
        return ScopeComparison(ScopeVerdict.OVERRIDE, "a")
    if b_le_a and not a_le_b:
        return ScopeComparison(ScopeVerdict.OVERRIDE, "b")
    return ScopeComparison(ScopeVerdict.OVERLAP, None)
