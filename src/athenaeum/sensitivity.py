# SPDX-License-Identifier: Apache-2.0
"""Sensitivity-class recogniser protocol + registry (athenaeum#910's design note).

**Scope.** Implements both halves of ``docs/sensitivity-class-vocabulary.md``:

- **S1a (athenaeum#989, shipped first)** — the *recogniser* half:
  :class:`SensitivityMatch`, the :class:`SensitivityRecognizer` protocol,
  :func:`register_recognizer` / :func:`available_recognizers`,
  :class:`SensitivityConfigError`, and the two shipped built-in recognisers
  (``email``, ``phone``) registered through the same public call an external
  deployment's own recogniser would use.
- **S1b (athenaeum#990, this slice)** — the *class-vocabulary* half:
  :class:`SensitivityClass` / :class:`ReadPolicy`, :func:`available_classes`,
  ``inherits``-chain read-policy resolution (§4), the partition invariant and
  unknown-recognizer validation (§3.2 Decision D6), the shipped
  :data:`_BUILTIN_CLASSES` ``pii`` class (§5), and :func:`classify`, the
  registry entry point.

No production module imports this one yet — no caller is migrated onto
:func:`classify` in this slice (that is slice S3, per §9 of the design note).

**The span decision (design note §3.2's open question, resolved here).** The
note simultaneously specifies ``SensitivityMatch.span: tuple[int, int] | None``
and says the built-ins "wrap ``find_inline_emails``/``find_inline_phones``" —
but those functions return ``list[str]``, deduped and order-preserving, with
no offsets, so a wrapper over them can never populate ``span`` and collapses
repeated occurrences of one value into a single match. This module picks
**option (a)**: the built-ins iterate :data:`athenaeum.pii._EMAIL_RE` /
:data:`athenaeum.pii._PHONE_RE` via ``.finditer`` directly (applying
:func:`athenaeum.pii._is_excluded_phone_shape` and
:func:`athenaeum.pii._has_labeled_identifier_prefix` so the existing phone
false-positive suppression — athenaeum#500 / athenaeum#720 / athenaeum#732 — is preserved
byte-for-byte), yielding a real ``(start, end)`` offset into the scanned text
for every match and one match per occurrence rather than a deduped set. This
is the choice that keeps a future span-consuming caller migratable onto the
registry; :mod:`athenaeum.pii` itself is untouched — ``find_inline_emails``/
``find_inline_phones`` and their existing callers keep working byte-identically
(this module only reads their compiled patterns and private helpers, exactly
as :mod:`athenaeum.outbound_pii` already does for the same regexes).

**Layering:** L3 service, peer to :mod:`athenaeum.pii` and
:mod:`athenaeum.screening`. Imports :mod:`athenaeum.pii` (a sibling L3
module, for its compiled detection patterns and phone false-positive
helpers) and :mod:`athenaeum.screening` (a sibling L3 module, for
``_ACCESS_RANK`` — the same ``access:`` vocabulary a class's ``read_policy``
reuses rather than inventing, per design note §4) at module scope; both are
safe peer imports (neither imports back). This slice additionally imports
:mod:`athenaeum.config` (L2) for :func:`~athenaeum.config.resolve_sensitivity_classes`
— an L3-importing-L2 "reach down" is the normal direction (unlike
``config.py``'s own module docstring, which must NOT import L3 at module
scope; this module has no such restriction). Importing this module has no
side effect beyond registering the two built-in recognisers in-process — no
file is read or written, no network call is made; :func:`available_classes`
and :func:`classify` do no I/O either — ``config`` is passed in by the
caller, never loaded from disk by this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from athenaeum.config import resolve_sensitivity_classes
from athenaeum.pii import (
    _EMAIL_RE,
    _PHONE_RE,
    _has_enough_digits,
    _has_labeled_identifier_prefix,
    _is_excluded_phone_shape,
)
from athenaeum.screening import _ACCESS_RANK


class SensitivityConfigError(ValueError):
    """Raised when the sensitivity-recogniser registry is misused.

    Mirrors :class:`athenaeum.storage.StorageConfigError` /
    :class:`athenaeum.screening.ScreeningConfigError`: loud by design. A
    recogniser name collision (shadowing a built-in, or a duplicate custom
    name registered without ``replace=True``) must never silently drop one
    of the two registrations — that would leave a class's ``recognizers:``
    list bound to the wrong detector with no visible signal.
    """


@dataclass(frozen=True)
class SensitivityMatch:
    """One recogniser hit: which recogniser, what value, and where.

    ``field`` is set when the match came off structured frontmatter data
    (a field name) rather than body text; ``span`` is the ``(start, end)``
    half-open character offset into the scanned ``text`` when the match came
    from body text. Neither built-in recogniser in this module currently
    populates ``field`` — both scan ``text`` only (see each class's
    docstring) — but the field exists on the contract now so a frontmatter-
    aware recogniser (a future deployment's own, or a later built-in) needs
    no shape change to use it.
    """

    recognizer: str
    value: str
    field: str | None = None
    span: tuple[int, int] | None = None


@runtime_checkable
class SensitivityRecognizer(Protocol):
    """The code extension point a recogniser implements.

    ``name`` is the stable id a class's ``recognizers:`` list (§3.1 of the
    design note) names to bind a class to this recogniser's matches.
    :meth:`detect` must be pure, offline and deterministic — no I/O, no
    network, no LLM call — the same posture :mod:`athenaeum.screening`
    (``screening.py``'s "transparent keyword + regex... auditable and
    diff-reviewable") and :mod:`athenaeum.outbound_pii` ("a pure, offline,
    deterministic text lint") already commit to for detection code in this
    codebase.
    """

    name: str

    def detect(
        self, *, text: str, frontmatter: Mapping[str, Any] | None
    ) -> list[SensitivityMatch]:
        """Return every match this recogniser finds in *text*/*frontmatter*."""
        ...


#: Recogniser names this module ships. Protected from shadowing by
#: :func:`register_recognizer` once registered — mirrors
#: :data:`athenaeum.storage._BUILTIN_ADAPTERS`'s shadow protection, applied
#: to names instead of a separate dict, because (per the design note's
#: §3.2 "no ``if built_in`` branch anywhere in this contract") the built-ins
#: register through the exact same public :func:`register_recognizer` call a
#: deployment's own recogniser would use, rather than bypassing it.
_BUILTIN_RECOGNIZER_NAMES: frozenset[str] = frozenset({"email", "phone"})

#: In-process registry (the code extension point). Populated by
#: :func:`register_recognizer` — both for the two built-ins (at the bottom of
#: this module) and for any custom recogniser a deployment registers from its
#: own import.
_REGISTERED_RECOGNIZERS: dict[str, SensitivityRecognizer] = {}


def register_recognizer(recognizer: SensitivityRecognizer, *, replace: bool = False) -> None:
    """Register a recogniser in-process (the code extension point).

    Mirrors :func:`athenaeum.storage.register_adapter`'s documented shape
    exactly: a **built-in** recogniser name (``email``, ``phone``) can never
    be shadowed — once either has registered once (at this module's import
    time, before any external caller could reach this function at all, since
    reaching it requires importing this module first), a second registration
    under that name always raises, regardless of *replace*. Re-registering a
    non-built-in name raises unless *replace* is set, so two consumers
    silently colliding on a name is a loud error, not a last-write-wins
    surprise.
    """
    name = recognizer.name
    if name in _BUILTIN_RECOGNIZER_NAMES and name in _REGISTERED_RECOGNIZERS:
        raise SensitivityConfigError(
            f"cannot register recognizer {name!r}: it shadows a built-in recognizer"
        )
    if name in _REGISTERED_RECOGNIZERS and not replace:
        raise SensitivityConfigError(
            f"recognizer {name!r} is already registered "
            "(pass replace=True to override)"
        )
    _REGISTERED_RECOGNIZERS[name] = recognizer


def available_recognizers(config: dict[str, Any] | None) -> dict[str, SensitivityRecognizer]:
    """Return every recogniser available to this config, keyed by name.

    Built-ins union code-:func:`register_recognizer` entries. Unlike a future
    ``available_classes`` (S1b), *config* is accepted for signature symmetry
    with :func:`athenaeum.storage.available_adapters` but is **not**
    consulted here: a ``sensitivity.classes.<name>.recognizers`` config entry
    may only *name* a recogniser that already exists in this registry, it can
    never cause one to spring into existence — detection is code only (design
    note §3.2 / Decision D2's converse). Binding a class's declared recogniser
    names against this dict (and raising when a name is unknown) is S1b's
    resolver's job, not this function's.
    """
    return dict(_REGISTERED_RECOGNIZERS)


class _EmailRecognizer:
    """Built-in ``email`` recogniser.

    Iterates :data:`athenaeum.pii._EMAIL_RE` directly rather than calling
    :func:`athenaeum.pii.find_inline_emails` (see this module's docstring for
    why — the span decision) — same compiled pattern, so "what an
    email looks like" still has exactly one definition. Scans ``text`` only;
    ``frontmatter`` is accepted for protocol conformance but not consulted
    (this recogniser reports no ``field``-carrying matches).
    """

    name = "email"

    def detect(
        self, *, text: str, frontmatter: Mapping[str, Any] | None
    ) -> list[SensitivityMatch]:
        source = text or ""
        return [
            SensitivityMatch(
                recognizer=self.name, value=m.group(0), span=(m.start(), m.end())
            )
            for m in _EMAIL_RE.finditer(source)
        ]


class _PhoneRecognizer:
    """Built-in ``phone`` recogniser.

    Iterates :data:`athenaeum.pii._PHONE_RE` directly (see this module's
    docstring for the span decision) and applies the same two exclusion
    checks :func:`athenaeum.pii.find_inline_phones` applies — a token below
    the digit floor is skipped, a provably-non-phone shape
    (:func:`athenaeum.pii._is_excluded_phone_shape` — ISO dates, year
    ranges, bare id fragments, bare ISBN-13s; athenaeum#500 / athenaeum#683 / athenaeum#720)
    is skipped, and a run the surrounding prose already types as a labeled
    record id (:func:`athenaeum.pii._has_labeled_identifier_prefix`; athenaeum#732)
    is skipped — so migrating a caller onto this registry cannot regress
    either issue's fix. Unlike :func:`~athenaeum.pii.find_inline_phones` this
    does not dedupe: each occurrence of a repeated value is its own match,
    carrying its own span. Scans ``text`` only; ``frontmatter`` is accepted
    for protocol conformance but not consulted.
    """

    name = "phone"

    def detect(
        self, *, text: str, frontmatter: Mapping[str, Any] | None
    ) -> list[SensitivityMatch]:
        source = text or ""
        matches: list[SensitivityMatch] = []
        for m in _PHONE_RE.finditer(source):
            token = m.group(1)
            if not _has_enough_digits(token):
                continue
            if _is_excluded_phone_shape(token):
                continue
            if _has_labeled_identifier_prefix(source[: m.start(1)]):
                continue
            matches.append(
                SensitivityMatch(
                    recognizer=self.name, value=token, span=(m.start(1), m.end(1))
                )
            )
        return matches


# ---------------------------------------------------------------------------
# Built-in registration — the identical public call a deployment's own
# recogniser would make (design note §3.2). Runs at import time; this is the
# module's only side effect.
# ---------------------------------------------------------------------------

register_recognizer(_EmailRecognizer())
register_recognizer(_PhoneRecognizer())


# ---------------------------------------------------------------------------
# Class vocabulary (S1b, athenaeum#990) — `SensitivityClass`, `ReadPolicy`,
# `available_classes`, and the `classify()` registry entry point. Everything
# below is pure computation over *config* passed in by the caller; nothing
# here does I/O or has an import-time side effect (the built-in recogniser
# registration above remains this module's only one).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadPolicy:
    """A sensitivity class's fully resolved read policy.

    ``access`` is one of the four :data:`athenaeum.screening._ACCESS_RANK`
    levels (``open`` / ``internal`` / ``confidential`` / ``personal``) — the
    SAME vocabulary athenaeum#312 already ships, not a new one (design note
    §4). ``audience`` is the same opaque role-list mechanism
    ``docs/security-posture.md`` §2.1 already documents for
    ``athenaeum serve --audience``; unlike ``access`` it is **not** validated
    against a known vocabulary — any role name an operator writes is
    accepted as-is, per design note §4.
    """

    access: str
    audience: tuple[str, ...] = ()


@dataclass(frozen=True)
class SensitivityClass:
    """One resolved sensitivity class: its name, bound recognisers, and read policy.

    ``recognizers`` is this class's OWN ``recognizers:`` list, validated
    against the S1a registry (an unknown name raises
    :class:`SensitivityConfigError` at :func:`available_classes` build time)
    — it is **not** inherited; only ``read_policy`` inherits (design note
    §4). An empty tuple means the class is never auto-detected and is
    reachable only by explicit operator/agent tagging (design note §3.2's
    "empty ``recognizers: []`` is honoured literally" rule) — this is also
    what an unset ``recognizers:`` key resolves to, since there is no
    fallback source for it to inherit from.
    """

    name: str
    recognizers: tuple[str, ...]
    read_policy: ReadPolicy


#: The shipped default sensitivity class (design note §5) — this module's own
#: single source of truth for it, mirroring how
#: :data:`athenaeum.pii.DEFAULT_EXCLUDED_READ_MAPPING` (not
#: ``config._DEFAULTS``) is the shipped-default home for
#: ``excluded_read_mapping`` (design note §2.4). Merged in by
#: :func:`available_classes` at the LOWEST precedence tier — a
#: ``sensitivity.classes.pii`` config entry overrides this block WHOLESALE
#: (recognisers and read_policy alike, not merged field-by-field — design
#: note §3.1's "config wins" rule for classes has no partial-merge case).
_BUILTIN_CLASSES: dict[str, dict[str, Any]] = {
    "pii": {
        "recognizers": ["email", "phone"],
        "read_policy": {"access": "personal"},
    },
}


def _resolve_read_policy(
    name: str,
    raw_by_name: Mapping[str, dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    stack: list[str],
) -> dict[str, Any]:
    """Resolve one class's raw ``read_policy`` dict, parent-first (design note §4).

    Field-default-fill only: an unset field on the child takes the parent's
    already-resolved value (the parent's own inheritance, if any, resolved
    first); an explicitly set child field always wins, in either direction —
    tighter or looser than the parent (no monotonic-restriction floor, design
    note §7 Decision D4). *stack* is the in-progress ancestor chain for THIS
    top-level call, mutated in place across recursive calls so a class
    reappearing in its own ancestry — one hop (self-``inherits``) or several
    — is detected and raises :class:`SensitivityConfigError` naming every
    class in the cycle. A ``inherits`` value naming a class absent from
    *raw_by_name* raises the same error naming the missing parent. *cache* is
    shared across the whole :func:`available_classes` call so a class
    referenced from more than one child's chain is resolved once.
    """
    if name in cache:
        return cache[name]
    if name in stack:
        cycle = stack[stack.index(name) :] + [name]
        raise SensitivityConfigError(
            "sensitivity.classes inheritance cycle: " + " -> ".join(cycle)
        )
    stack.append(name)
    block = raw_by_name[name]
    own_raw = block.get("read_policy")
    own_policy = dict(own_raw) if isinstance(own_raw, dict) else {}
    parent_name = block.get("inherits")
    if parent_name is not None:
        if not isinstance(parent_name, str) or parent_name not in raw_by_name:
            raise SensitivityConfigError(
                f"sensitivity.classes.{name} inherits unknown class {parent_name!r}"
            )
        parent_policy = _resolve_read_policy(parent_name, raw_by_name, cache, stack)
        resolved = {**parent_policy, **own_policy}
    else:
        resolved = own_policy
    stack.pop()
    cache[name] = resolved
    return resolved


def available_classes(config: dict[str, Any] | None) -> dict[str, SensitivityClass]:
    """Resolve every sensitivity class available to this config, keyed by name.

    Precedence, lowest to highest: :data:`_BUILTIN_CLASSES`, then
    :func:`athenaeum.config.resolve_sensitivity_classes`'s operator entries —
    mirroring :func:`athenaeum.storage.available_adapters`'s precedence with
    the "code" tier omitted, because a class is pure declared policy with
    nothing to register in code (design note §7 Decision D2). UNLIKE
    adapters/recognisers, a config entry reusing a built-in class name
    **overrides** it wholesale rather than raising — there is no name-shadow
    protection for classes (design note §3.1): an operator may redefine
    ``pii``'s read policy or recognisers outright, and doing so replaces the
    whole block (a redefinition that omits ``recognizers:`` gets an EMPTY
    recogniser list, not the built-in's, since there is no field-level merge).

    Raises :class:`SensitivityConfigError` at build time (never a silent
    fallback) for any of:

    - a class naming a recognizer absent from
      ``available_recognizers(config)`` (design note §3.2);
    - a recognizer name bound to more than one class's ``recognizers:`` list
      — the partition invariant (design note §7 Decision D6);
    - an ``inherits`` cycle or a dangling ``inherits`` parent (design note
      §4);
    - a resolved ``read_policy.access`` outside the four
      :data:`athenaeum.screening._ACCESS_RANK` levels — including when NO
      class in the ``inherits`` chain ever sets ``access`` at all, since an
      unset access level is exactly as unusable as an invalid one and this
      module's fail-loud posture (matching
      :class:`athenaeum.storage.StorageConfigError` /
      :class:`athenaeum.screening.ScreeningConfigError`) treats the two
      identically rather than silently defaulting.
    """
    raw_by_name: dict[str, dict[str, Any]] = dict(_BUILTIN_CLASSES)
    raw_by_name.update(resolve_sensitivity_classes(config))

    recognizers = available_recognizers(config)
    read_policy_cache: dict[str, dict[str, Any]] = {}
    recognizer_owner: dict[str, str] = {}
    classes: dict[str, SensitivityClass] = {}

    for name, block in raw_by_name.items():
        raw_recognizers = block.get("recognizers")
        if not isinstance(raw_recognizers, list):
            raw_recognizers = []
        bound: list[str] = []
        for rec_name in raw_recognizers:
            if not isinstance(rec_name, str) or not rec_name.strip():
                continue
            rec_name = rec_name.strip()
            if rec_name not in recognizers:
                raise SensitivityConfigError(
                    f"sensitivity.classes.{name} names unknown recognizer "
                    f"{rec_name!r}; known recognizers: {sorted(recognizers)}"
                )
            owner = recognizer_owner.get(rec_name)
            if owner is not None and owner != name:
                raise SensitivityConfigError(
                    f"recognizer {rec_name!r} is bound to both class {owner!r} "
                    f"and class {name!r}; a recognizer may feed at most one "
                    "class (classification is a partition, design note §7 "
                    "Decision D6)"
                )
            recognizer_owner[rec_name] = name
            bound.append(rec_name)

        policy = _resolve_read_policy(name, raw_by_name, read_policy_cache, [])
        access = policy.get("access")
        if not isinstance(access, str) or access.strip() not in _ACCESS_RANK:
            raise SensitivityConfigError(
                f"sensitivity.classes.{name} read_policy.access {access!r} "
                f"must be one of {sorted(_ACCESS_RANK)} (set directly, or "
                "inherited from a parent class)"
            )
        raw_audience = policy.get("audience")
        audience = (
            tuple(a for a in raw_audience if isinstance(a, str) and a.strip())
            if isinstance(raw_audience, list)
            else ()
        )

        classes[name] = SensitivityClass(
            name=name,
            recognizers=tuple(bound),
            read_policy=ReadPolicy(access=access.strip(), audience=audience),
        )
    return classes


@dataclass(frozen=True)
class ClassifiedMatch:
    """One recogniser hit paired with the sensitivity class it belongs to.

    :func:`classify`'s return unit. ``match`` is the raw
    :class:`SensitivityMatch` (which recognizer, what value, where);
    ``sensitivity_class`` is the name of the class whose ``recognizers:``
    list named that recognizer.
    """

    match: SensitivityMatch
    sensitivity_class: str


def classify(
    *,
    text: str,
    frontmatter: Mapping[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> list[ClassifiedMatch]:
    """Detect and classify sensitivity matches in *text*/*frontmatter*.

    The registry entry point named in design note §3.2 and §9's S3 but never
    given a signature there — athenaeum#990 closes that gap. Runs every
    recogniser :func:`available_recognizers` returns against *text* and
    *frontmatter*, then routes each resulting :class:`SensitivityMatch` to
    the sensitivity class whose ``recognizers:`` list names that recognizer,
    per :func:`available_classes(config) <available_classes>`. A match from a recognizer that
    no configured class currently names contributes nothing — it is detected
    but has nowhere to route (naming a recognizer that isn't registered
    ANYWHERE already raised earlier, at :func:`available_classes` build
    time; a recognizer that is registered but simply unbound from every
    class is a legal, silent no-route case, not an error).

    **The design note §7 Decision D6 escape-hatch consequence, made
    observable here per athenaeum#990's Motivation.** Classification is a
    partition — one recognizer feeds at most one class — but nothing stops a
    deployment from registering two thin recognisers that wrap the same
    detection function under two different names, each bound to a different
    class. Both then fire on the same input value, and this function returns
    TWO :class:`ClassifiedMatch` entries for that one value, one per class.
    This function does not deduplicate or arbitrate between them — which of
    the two "wins" (if either should) is a routing-policy question left to
    whichever consumer first has to answer it.

    No production caller is migrated onto this function in this slice
    (athenaeum#990) — that is slice S3 (design note §9).
    """
    classes = available_classes(config)
    recognizer_to_class: dict[str, str] = {}
    for sensitivity_class in classes.values():
        for rec_name in sensitivity_class.recognizers:
            recognizer_to_class[rec_name] = sensitivity_class.name

    results: list[ClassifiedMatch] = []
    for rec_name, recognizer in available_recognizers(config).items():
        dest_class = recognizer_to_class.get(rec_name)
        if dest_class is None:
            continue
        for match in recognizer.detect(text=text, frontmatter=frontmatter):
            results.append(ClassifiedMatch(match=match, sensitivity_class=dest_class))
    return results
