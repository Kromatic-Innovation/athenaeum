# SPDX-License-Identifier: Apache-2.0
"""Sensitivity-class recogniser protocol + registry (S1a of athenaeum#910's design note).

**Scope — this is slice S1a only.** Implements the *recogniser* half of
``docs/sensitivity-class-vocabulary.md``: :class:`SensitivityMatch`, the
:class:`SensitivityRecognizer` protocol, :func:`register_recognizer` /
:func:`available_recognizers`, :class:`SensitivityConfigError`, and the two
shipped built-in recognisers (``email``, ``phone``) registered through the
same public call an external deployment's own recogniser would use. It does
**not** implement the class-vocabulary half (the ``sensitivity.classes``
config resolver, ``SensitivityClass``/``available_classes``, read-policy
inheritance, or a ``classify()`` entry point) — that is slice S1b, a separate
issue. No production module imports this one yet.

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
:mod:`athenaeum.screening`. Imports only :mod:`athenaeum.pii` (a sibling L3
module, for its compiled detection patterns and phone false-positive
helpers) at module scope — no L1/L2 imports are needed for this slice's
scope (the config resolver that will need :mod:`athenaeum.config` is S1b's
job, not this module's, yet). Importing this module has no side effect
beyond registering the two built-in recognisers in-process — no file is
read or written, no network call is made.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from athenaeum.pii import (
    _EMAIL_RE,
    _PHONE_RE,
    _has_enough_digits,
    _has_labeled_identifier_prefix,
    _is_excluded_phone_shape,
)


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
