# SPDX-License-Identifier: Apache-2.0
"""The pure intake decision for one fact (athenaeum#2797, slice S2 of athenaeum#2625).

``docs/design/federation-intake-routing.md`` section 4.1: one function,
:func:`decide`, gives the decision for a single fact: capture or not, which
store, which type, and a reason code. The three capture gates
(:func:`athenaeum.relevance_policy.gate_raw_content`,
:func:`athenaeum.relevance_classify.gate_classified_entities` and
:func:`athenaeum.relevance_classify.gate_update_actions`) call it in both
pipeline transports, so the capture logic exists once.

**In this slice only the capture answer is used.** The store and type answers
are computed and discarded in production: no gate reads them, and production
callers pass an empty ``stores`` mapping, so the store is always ``personal``.
Capture is the answer the gates computed before this module existed.

Pure: no file access, no clock, no model, no logging. A decision carries no
fact text, only the keys it matched on and a rule.

Layering: L2, a leaf. It imports nothing from :mod:`athenaeum`; the policy and
its rules are described by the structural :class:`PolicyLike` / :class:`RuleLike`
protocols, so :mod:`athenaeum.relevance_policy` (whose
``gate_raw_content`` calls :func:`decide`) can import this module without a
cycle. :class:`athenaeum.relevance_policy.RelevancePolicy` satisfies
:class:`PolicyLike`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

#: The classify prompt's (and the reasoning tier's) confidence for a category
#: that may be acted on without escalation.
CONFIDENCE_HIGH = "high"

#: The reserved store word for the install owner's own store.
PERSONAL = "personal"

#: The three capture answers. ``unsure`` is a ``never`` rule the classifier is
#: not confident about: the caller escalates to the reasoning tier (a model
#: call this pure function cannot make) and calls :func:`decide` again.
CAPTURE_KEEP = "keep"
CAPTURE_DROP = "drop"
CAPTURE_UNSURE = "unsure"

#: Reason codes (section 6), one closed set.
REASON_RULE = "rule"
REASON_MAPPING = "mapping"
REASON_EXPLICIT = "explicit"
REASON_DEFAULT = "default"
REASON_UNSURE = "unsure"
REASON_NO_GRANT = "no-grant"
REASON_STORE_REFUSES = "store-refuses"
REASON_PATH = "path"
REASONS: tuple[str, ...] = (
    REASON_RULE,
    REASON_MAPPING,
    REASON_EXPLICIT,
    REASON_DEFAULT,
    REASON_UNSURE,
    REASON_NO_GRANT,
    REASON_STORE_REFUSES,
    REASON_PATH,
)

_WRITE_RIGHTS = frozenset({"compile", "propose"})


class RuleLike(Protocol):
    """What :func:`decide` reads of a policy rule."""

    @property
    def number(self) -> int: ...

    @property
    def line(self) -> int: ...

    @property
    def verdict(self) -> str: ...

    @property
    def type(self) -> str | None: ...

    @property
    def tag(self) -> str | None: ...

    @property
    def store(self) -> str | None: ...

    def matches(
        self, type_: str | None, tags: "list[str] | tuple[str, ...]", subject: str | None
    ) -> bool: ...

    def describe(self) -> str: ...


class PolicyLike(Protocol):
    """What :func:`decide` reads of a parsed relevance policy."""

    @property
    def rules(self) -> "Sequence[RuleLike]": ...

    def match(
        self,
        type_: str | None = None,
        tags: "list[str] | tuple[str, ...]" = (),
        subject: str | None = None,
    ) -> "RuleLike | None": ...

    def match_route(
        self,
        type_: str | None = None,
        tags: "list[str] | tuple[str, ...]" = (),
        subject: str | None = None,
    ) -> "RuleLike | None": ...


@dataclass(frozen=True)
class Description:
    """What is known about one fact at a decision point. Holds no fact text."""

    type: str | None = None
    tags: tuple[str, ...] = ()
    subject: str | None = None
    #: The classifier's category and its confidence (``"high"`` or not).
    category: str | None = None
    confidence: str | None = None
    #: A store the owner named. Always ``None`` until the ``remember`` argument
    #: (a later slice) exists.
    store: str | None = None
    #: The explicit-remember marker: a deliberate "remember this".
    explicit: bool = False
    #: True when the keys came from classify, False when declared metadata.
    classified: bool = False
    #: True on an entry path that never leaves the personal store.
    personal_only: bool = False


@dataclass(frozen=True)
class StoreInfo:
    """What this install knows about one store, keyed by ``store_id``.

    ``rights`` is a subset of ``read``, ``propose``, ``compile`` and ``answer``
    (empty when none), never a role name. ``accepts`` is the store's own intake
    check, called as ``accepts(type, tags)``; ``None`` accepts everything.
    """

    rights: frozenset[str] = frozenset()
    accepts: Callable[[str | None, tuple[str, ...]], bool] | None = None


@dataclass(frozen=True)
class IntakeDecision:
    """The decision for one fact. Carries no content."""

    capture: str
    #: ``personal`` or a ``store_id``; ``None`` when the fact is not kept.
    store: str | None
    #: The type the fact is filed as (the candidate type in this slice).
    type: str | None
    reason: str
    #: The rule that decided: the ``never`` rule on a drop or unsure answer, the
    #: ``route`` rule when one chose the store, else ``None``.
    rule: RuleLike | None = None
    #: The ``never`` rule an explicit-remember marker overrode, when it did.
    overridden: RuleLike | None = field(default=None, compare=False)


def _confident(confidence: str | None) -> bool:
    return confidence == CONFIDENCE_HIGH


def _reachable_never(
    policy: PolicyLike, type_: str | None, tags: list[str], subject: str | None
) -> RuleLike | None:
    """The first ``never`` rule keyed on a tag that could still apply to this fact.

    "Could apply" means every OTHER key the rule names (type, subject)
    matches, so only the category is in doubt. ``None`` means no ``never`` rule
    is in play and an unsure category cannot matter.
    """
    for rule in policy.rules:
        if (
            rule.verdict == "never"
            and rule.tag is not None
            and rule.matches(type_, tags + [rule.tag], subject)
        ):
            return rule
    return None


def _capture(policy: PolicyLike, d: Description) -> tuple[str, RuleLike | None]:
    """``(capture, rule)``: the last matching always/never rule, as the gates did."""
    tags = list(d.tags)
    base = policy.match(d.type, tags, d.subject)
    full = policy.match(d.type, tags + [d.category], d.subject) if d.category else base
    if full is not None and full.verdict == "never":
        # A rule that decided without the category needs no confidence in it.
        if base is not None and base.number == full.number:
            return CAPTURE_DROP, full
        return (CAPTURE_DROP, full) if _confident(d.confidence) else (CAPTURE_UNSURE, full)
    if d.classified and not _confident(d.confidence):
        rule = _reachable_never(policy, d.type, tags, d.subject)
        if rule is not None:
            return CAPTURE_UNSURE, rule
    return CAPTURE_KEEP, None


def _store(
    policy: PolicyLike,
    d: Description,
    stores: Mapping[str, StoreInfo],
    mapping: Mapping[str, str] | None,
) -> tuple[str, str, RuleLike | None]:
    """``(store, reason, rule)`` for a fact that is kept (section 4.1 step 2, section 6)."""
    tags = list(d.tags)
    rule: RuleLike | None = None
    used_classified = False
    if d.store is not None:
        candidate, reason = d.store, REASON_EXPLICIT
    else:
        rule = policy.match_route(d.type, tags, d.subject)
        if rule is not None:
            used_classified = d.classified and rule.type is not None
        elif d.category:
            rule = policy.match_route(d.type, tags + [d.category], d.subject)
            used_classified = rule is not None
        if rule is not None and rule.store is not None:
            candidate, reason = rule.store, REASON_RULE
        elif mapping and d.type is not None and d.type in mapping:
            rule, candidate, reason = None, mapping[d.type], REASON_MAPPING
            used_classified = d.classified
        else:
            return PERSONAL, REASON_DEFAULT, None
    if candidate == PERSONAL:
        return PERSONAL, reason, rule
    if used_classified and not _confident(d.confidence):
        return PERSONAL, REASON_UNSURE, None
    info = stores.get(candidate)
    if info is None or not (info.rights & _WRITE_RIGHTS):
        return PERSONAL, REASON_NO_GRANT, None
    if "compile" in info.rights and info.accepts is not None:
        try:
            accepted = bool(info.accepts(d.type, d.tags))
        except Exception:  # noqa: BLE001 - a store whose intake check cannot be evaluated refuses
            accepted = False
        if not accepted:
            return PERSONAL, REASON_STORE_REFUSES, None
    if d.personal_only:
        return PERSONAL, REASON_PATH, None
    return candidate, reason, rule


def decide(
    description: Description,
    policy: PolicyLike,
    stores: Mapping[str, StoreInfo] | None = None,
    mapping: Mapping[str, str] | None = None,
) -> IntakeDecision:
    """The intake decision for one fact. Pure.

    1. **Capture.** The last matching ``always`` / ``never`` rule, exactly as the
       gates always computed it (``route`` rows never take part). A ``never``
       match with no explicit-remember marker is ``drop``; one the classifier is
       not confident about is ``unsure``. The marker keeps the fact and reports
       the overridden rule in :attr:`IntakeDecision.overridden`. A dropped or
       unsure fact reports no store.
    2. **Store.** The owner-named store, else the last matching ``route`` row,
       else the ``mapping`` entry (type to ``store_id``, resolved by the caller)
       for the candidate type, else ``personal``; then the checks of section 6
       in order, the first failure sending it to ``personal``.
    3. **Type.** The candidate type. The store's type vocabulary arrives with
       the store's intake rules, in a later slice.

    *stores* maps ``store_id`` to :class:`StoreInfo`; ``None`` and ``{}`` mean
    this install holds nothing, so the store is always ``personal``.
    """
    capture, rule = _capture(policy, description)
    overridden: RuleLike | None = None
    if capture != CAPTURE_KEEP:
        if not description.explicit:
            return IntakeDecision(
                capture,
                None,
                None,
                REASON_RULE if capture == CAPTURE_DROP else REASON_UNSURE,
                rule,
            )
        overridden = rule
    store, reason, route_rule = _store(policy, description, stores or {}, mapping)
    return IntakeDecision(
        CAPTURE_KEEP, store, description.type, reason, route_rule, overridden
    )
